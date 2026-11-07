"""Compaction subsystem for kvdb: rewrites file with only live data."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from kvdb.crashpoints import crash_point
from kvdb.errors import DBPoisonedError, DiskFullError
from kvdb.frame import (
    OP_TYPE_PUT,
    encode_frame,
    encode_header,
    parse_frame,
)
from kvdb.recovery import apply_ops, unpack_val

if TYPE_CHECKING:
    from kvdb.db import DB


def compact(db: DB) -> None:
    """Execute database compaction following ARCHITECTURE.md Section 1.5.
    
    Must be called while holding db._lock.
    """
    if db.is_closed:
        raise ValueError("Database is closed")
    if db.is_poisoned:
        raise DBPoisonedError("Database is poisoned")

    compact_path = f"{db.path}.compact"
    try:
        os.remove(compact_path)
    except OSError:
        pass

    flags = os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    compact_fd = os.open(compact_path, flags, 0o644)
    old_fd = db._fd

    try:
        # Step 1: Create path.compact with header generation + 1
        new_gen = db.generation + 1
        header_bytes = encode_header(generation=new_gen)
        os.write(compact_fd, header_bytes)
        compact_offset = len(header_bytes)

        # Step 2: Write all live keys as frames of up to 1,000 puts each
        # Stream read values in physical disk offset order to eliminate random seek thrashing
        new_index: dict[bytes, int] = {}
        batch_ops: list[tuple[int, bytes, bytes]] = []
        items_by_offset = sorted(db._index.items(), key=lambda kv: unpack_val(kv[1])[0])

        def flush_batch() -> None:
            nonlocal compact_offset
            if not batch_ops:
                return

            frame_bytes = encode_frame(batch_ops)
            frame_len = len(frame_bytes)

            # Cap check against max_bytes individually
            if db._max_bytes is not None and compact_offset + frame_len > db._max_bytes:
                raise DiskFullError(
                    f"Compacted data exceeds max_bytes ({db._max_bytes})"
                )

            written = 0
            while written < frame_len:
                w = os.write(compact_fd, frame_bytes[written:])
                written += w

            parsed_res = parse_frame(frame_bytes, 0, frame_len, extract_values=False)
            assert parsed_res is not None
            parsed_frame, _ = parsed_res
            apply_ops(new_index, parsed_frame.ops, compact_offset)
            compact_offset += frame_len
            batch_ops.clear()

        for k, packed in items_by_offset:
            val_offset, val_len = unpack_val(packed)
            if val_len == 0:
                val = b""
            else:
                assert old_fd is not None
                os.lseek(old_fd, val_offset, os.SEEK_SET)
                val_buf = bytearray()
                while len(val_buf) < val_len:
                    chunk = os.read(old_fd, val_len - len(val_buf))
                    if not chunk:
                        break
                    val_buf.extend(chunk)
                val = bytes(val_buf)

            batch_ops.append((OP_TYPE_PUT, k, val))
            if len(batch_ops) == 1000:
                flush_batch()

        flush_batch()

        # Step 3: fsync path.compact
        os.fsync(compact_fd)
        crash_point("compact_after_tmp")

        # Step 4: Close old fd (required on Windows before atomic replace)
        assert old_fd is not None
        os.close(old_fd)
        db._fd = None
        crash_point("compact_before_replace")

        # Step 5: os.replace(path.compact, path) - THE ATOMIC SWAP
        os.close(compact_fd)
        compact_fd = None
        os.replace(compact_path, db.path)
        crash_point("compact_after_replace")

        # Step 6: fsync the directory (skipped on Windows)
        if sys.platform != "win32":
            dirname = os.path.dirname(os.path.abspath(db.path)) or "."
            try:
                dir_fd = os.open(dirname, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        crash_point("compact_after_dirsync")

        # Step 7: Reopen the fd on path; update generation, end_offset, and index
        open_flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        new_fd = os.open(db.path, open_flags)
        db._fd = new_fd
        db._generation = new_gen
        db._end_offset = compact_offset
        db._index = new_index

    except Exception:
        # Cleanup temporary file
        if compact_fd is not None:
            try:
                os.close(compact_fd)
            except OSError:
                pass
        try:
            os.remove(compact_path)
        except OSError:
            pass

        # If old_fd was closed and swap failed, reopen old file
        if db._fd is None and os.path.exists(db.path):
            try:
                db._fd = os.open(db.path, os.O_RDWR | getattr(os, "O_BINARY", 0))
            except OSError:
                db._poisoned = True
        raise
