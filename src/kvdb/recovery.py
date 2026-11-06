"""Recovery and database initialization for kvdb engine."""

from __future__ import annotations

import os
import struct
import sys
import time
from typing import NamedTuple

from kvdb.crashpoints import crash_point
from kvdb.errors import CorruptFileError
from kvdb.frame import (
    HEADER_SIZE,
    OP_TYPE_PUT,
    OP_TYPE_DEL,
    ParsedOp,
    encode_header,
    parse_frame,
    parse_header,
)

# Scan chunk size (16 MiB per FORMAT.md Section 8)
CHUNK_SIZE = 16 * 1024 * 1024


class RecoveryResult(NamedTuple):
    index: dict[bytes, int]
    end_offset: int
    generation: int
    recovery_ms: int


def pack_val(val_offset: int, val_len: int) -> int:
    """Pack value offset and length into a single 64-bit integer.
    
    val_len is at most 2^20 (1 MiB), fitting in 24 bits.
    val_offset fits in the upper 40 bits (up to 1 TiB).
    """
    return (val_offset << 24) | (val_len & 0xFFFFFF)


def unpack_val(packed: int) -> tuple[int, int]:
    """Unpack a packed int into (val_offset, val_len)."""
    return packed >> 24, packed & 0xFFFFFF


def apply_ops(index: dict[bytes, int], ops: list[ParsedOp], frame_start_offset: int) -> None:
    """Apply operations in order to the index (last op on a key wins).
    
    This is the SINGLE mutator function shared between recovery and live writes (FORMAT.md Section 5.2, ARCHITECTURE Section 2).
    """
    for op in ops:
        if op.op_type == OP_TYPE_PUT:
            val_offset = frame_start_offset + op.val_offset_in_frame
            index[op.key] = pack_val(val_offset, op.val_len)
        elif op.op_type == OP_TYPE_DEL:
            index.pop(op.key, None)


def cleanup_temp_files(path: str | os.PathLike) -> None:
    """Unconditionally delete leftover path.compact and path.init temporary files."""
    path_str = str(path)
    for ext in (".compact", ".init"):
        tmp_path = path_str + ext
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def init_db(path: str | os.PathLike) -> None:
    """Atomically create a new database file at path with generation 1."""
    path_str = str(path)
    init_path = path_str + ".init"
    # Ensure directory exists if path has one
    dirname = os.path.dirname(os.path.abspath(path_str))
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(init_path, flags, 0o644)
    try:
        header = encode_header(generation=1)
        # Write loop
        total = 0
        while total < len(header):
            written = os.write(fd, header[total:])
            if written == 0:
                raise OSError("Write returned 0 while writing init header")
            total += written
        os.fsync(fd)
    finally:
        os.close(fd)

    # Atomic swap
    os.replace(init_path, path)

    # Directory fsync (skipped on Windows)
    if sys.platform != "win32":
        try:
            dir_fd = os.open(dirname or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


def recover(fd: int, file_size: int) -> RecoveryResult:
    """Scan and recover the database from an open file descriptor.
    
    Applies valid frames to in-memory index and truncates any torn tail.
    """
    t0 = time.perf_counter()

    # Step 3: Validate header
    os.lseek(fd, 0, os.SEEK_SET)
    header_bytes = b""
    while len(header_bytes) < HEADER_SIZE:
        chunk = os.read(fd, HEADER_SIZE - len(header_bytes))
        if not chunk:
            break
        header_bytes += chunk

    if len(header_bytes) < HEADER_SIZE:
        raise CorruptFileError(f"File too short for header ({len(header_bytes)} bytes)")

    generation, _ = parse_header(header_bytes)

    # Step 4: Scan frames from offset 24
    index: dict[bytes, int] = {}
    buf = bytearray()
    buf_start_file_offset = HEADER_SIZE
    os.lseek(fd, HEADER_SIZE, os.SEEK_SET)

    stopping_offset = HEADER_SIZE
    scan_done = False

    while not scan_done:
        read_chunk = os.read(fd, CHUNK_SIZE)
        if read_chunk:
            buf.extend(read_chunk)

        eof_reached = len(read_chunk) == 0
        buf_pos = 0

        # Scan frames within the current buffer
        while True:
            frame_file_offset = buf_start_file_offset + buf_pos

            # Rule 1 check: need at least 8 bytes for length & crc
            if buf_pos + 8 > len(buf):
                if eof_reached:
                    # Torn tail (fewer than 8 bytes remaining at EOF)
                    stopping_offset = frame_file_offset
                    scan_done = True
                    break
                else:
                    # Need more bytes from disk
                    break

            frame_len = struct.unpack_from("<I", buf, buf_pos)[0]
            needed = 8 + frame_len

            if buf_pos + needed > len(buf):
                # Frame extends past current buffer
                if frame_file_offset + needed > file_size or eof_reached:
                    # Frame extends past actual file EOF -> torn tail
                    stopping_offset = frame_file_offset
                    scan_done = True
                    break
                else:
                    # Buffer needs more data from disk
                    break

            # Parse frame and check validity rules 1-8
            # Limit in buf coordinate space is file_size - buf_start_file_offset
            buf_file_limit = file_size - buf_start_file_offset
            parsed_res = parse_frame(buf, buf_pos, buf_file_limit, extract_values=False)

            if parsed_res is None:
                # Frame is invalid (CRC mismatch, bad kind, etc.) -> torn tail
                stopping_offset = frame_file_offset
                scan_done = True
                break

            parsed_frame, next_pos = parsed_res
            apply_ops(index, parsed_frame.ops, frame_file_offset)
            buf_pos = next_pos
            stopping_offset = buf_start_file_offset + buf_pos

        if scan_done:
            break

        if eof_reached and buf_pos == len(buf):
            # Cleanly parsed all frames to EOF
            break

        # Slide the buffer: keep unparsed remainder
        buf = buf[buf_pos:]
        buf_start_file_offset += buf_pos

    # Step 5: Truncate torn tail if stopping_offset < file_size
    if stopping_offset < file_size:
        crash_point("recovery_before_truncate")
        os.ftruncate(fd, stopping_offset)
        os.fsync(fd)
        crash_point("recovery_after_truncate")

    os.lseek(fd, stopping_offset, os.SEEK_SET)
    recovery_ms = max(0, int((time.perf_counter() - t0) * 1000))

    return RecoveryResult(
        index=index,
        end_offset=stopping_offset,
        generation=generation,
        recovery_ms=recovery_ms,
    )
