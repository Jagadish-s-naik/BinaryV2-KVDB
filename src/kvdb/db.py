"""Core database engine implementing write-ordering, disk-full limits, and durability."""

from __future__ import annotations

import os
import threading
from typing import Self

from kvdb.crashpoints import crash_point
from kvdb.errors import CorruptFileError, DBPoisonedError, DiskFullError
from kvdb.frame import (
    OP_TYPE_DEL,
    OP_TYPE_PUT,
    encode_frame,
    parse_frame,
)
from kvdb.recovery import (
    apply_ops,
    cleanup_temp_files,
    init_db,
    recover,
    unpack_val,
)


class DB:
    """Embedded crash-safe key-value database engine."""

    def __init__(
        self,
        path: str,
        fd: int,
        max_bytes: int | None,
        index: dict[bytes, int],
        end_offset: int,
        generation: int,
        recovery_ms: int = 0,
    ) -> None:
        self._path = path
        self._fd = fd
        self._max_bytes = max_bytes
        self._index = index
        self._end_offset = end_offset
        self._generation = generation
        self._recovery_ms = recovery_ms

        self._lock = threading.Lock()
        self._poisoned = False
        self._closed = False

    @property
    def path(self) -> str:
        return self._path

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def end_offset(self) -> int:
        return self._end_offset

    @property
    def recovery_ms(self) -> int:
        return self._recovery_ms

    @property
    def is_poisoned(self) -> bool:
        return self._poisoned

    @property
    def is_closed(self) -> bool:
        return self._closed

    def __len__(self) -> int:
        with self._lock:
            return len(self._index)

    def __contains__(self, key: bytes) -> bool:
        with self._lock:
            return key in self._index

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    @classmethod
    def open(cls, path: str, max_bytes: int | None = None) -> DB:
        """Open or create a database at path, executing recovery and temp file cleanup."""
        cleanup_temp_files(path)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            init_db(path)

        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        try:
            file_size = os.path.getsize(path)
            res = recover(fd, file_size)
        except Exception:
            os.close(fd)
            raise

        return cls(
            path=path,
            fd=fd,
            max_bytes=max_bytes,
            index=res.index,
            end_offset=res.end_offset,
            generation=res.generation,
            recovery_ms=res.recovery_ms,
        )

    def _check_cap(self, frame_size: int, is_delete_only: bool) -> None:
        """Verify write fits within max_bytes cap and reserve rules (FORMAT.md Section 10)."""
        if self._max_bytes is None:
            return

        reserve = min(65536, self._max_bytes // 8)
        if not is_delete_only:
            if self._end_offset + frame_size > self._max_bytes - reserve:
                raise DiskFullError(
                    f"Write of {frame_size} bytes would exceed max_bytes ({self._max_bytes}) with reserve ({reserve})"
                )
        else:
            if self._end_offset + frame_size > self._max_bytes:
                raise DiskFullError(
                    f"Delete-only write of {frame_size} bytes would exceed max_bytes ({self._max_bytes})"
                )

    def _append_frame(self, ops: list[tuple[int, bytes, bytes]], is_delete_only: bool = False) -> None:
        """Execute durable write sequence adhering strictly to ARCHITECTURE.md Section 1.3."""
        if self._closed:
            raise ValueError("Database is closed")
        if self._poisoned:
            raise DBPoisonedError("Database is poisoned by an earlier fsync/truncate failure")

        # Step 1 & 2: Validate inputs and build complete frame in memory
        frame_bytes = encode_frame(ops)
        frame_len = len(frame_bytes)

        # Step 3: Disk-full check
        self._check_cap(frame_len, is_delete_only)

        crash_point("before_write")

        # Step 4: Seek to end_offset and write the whole frame (looping on short writes)
        try:
            os.lseek(self._fd, self._end_offset, os.SEEK_SET)
            written_total = 0
            mid_write_triggered = False
            half_len = frame_len // 2

            while written_total < frame_len:
                if not mid_write_triggered and half_len > 0:
                    chunk = frame_bytes[written_total:half_len]
                    w = os.write(self._fd, chunk)
                    written_total += w
                    if written_total >= half_len:
                        mid_write_triggered = True
                        crash_point("mid_write")
                else:
                    w = os.write(self._fd, frame_bytes[written_total:])
                    written_total += w
        except OSError:
            # Rollback via ftruncate
            try:
                os.ftruncate(self._fd, self._end_offset)
                os.fsync(self._fd)
            except OSError:
                self._poisoned = True
            raise

        crash_point("after_write_before_fsync")

        # Step 5: os.fsync(fd) - THE DURABILITY POINT
        try:
            os.fsync(self._fd)
        except OSError:
            self._poisoned = True
            raise DBPoisonedError("fsync failed; instance poisoned") from None

        crash_point("after_fsync_before_index")

        # Step 6: Update index and end_offset
        parsed_res = parse_frame(frame_bytes, 0, frame_len, extract_values=False)
        assert parsed_res is not None
        parsed_frame, _ = parsed_res
        self._apply_ops(self._index, parsed_frame.ops, self._end_offset)
        self._end_offset += frame_len

        crash_point("after_fsync_before_ack")

    @staticmethod
    def _apply_ops(index: dict[bytes, int], ops: list, base_offset: int) -> None:
        """Apply operations to index using canonical mutator (ARCHITECTURE Section 2)."""
        apply_ops(index, ops, base_offset)

    def compact(self) -> None:
        """Rewrite database with live data to reclaim space (Stage 3)."""
        raise NotImplementedError("Compaction will be implemented in Stage 3")

    def put(self, key: bytes, value: bytes) -> None:
        """Store a key-value pair durably."""
        with self._lock:
            self._append_frame([(OP_TYPE_PUT, key, value)], is_delete_only=False)

    def delete(self, key: bytes) -> None:
        """Delete a key durably (deleting a missing key is not an error)."""
        with self._lock:
            self._append_frame([(OP_TYPE_DEL, key, b"")], is_delete_only=True)

    def batch(self, ops: list[tuple[int, bytes, bytes]]) -> None:
        """Apply a batch of operations atomically as a single frame."""
        if not ops:
            return  # Empty batch returns success without writing
        with self._lock:
            is_del = all(op[0] == OP_TYPE_DEL for op in ops)
            self._append_frame(ops, is_delete_only=is_del)

    def get(self, key: bytes) -> bytes | None:
        """Retrieve the value for key under lock. Returns None if missing."""
        with self._lock:
            if self._closed:
                raise ValueError("Database is closed")
            packed = self._index.get(key)
            if packed is None:
                return None

            val_offset, val_len = unpack_val(packed)
            if val_len == 0:
                return b""

            os.lseek(self._fd, val_offset, os.SEEK_SET)
            val_bytes = bytearray()
            while len(val_bytes) < val_len:
                chunk = os.read(self._fd, val_len - len(val_bytes))
                if not chunk:
                    break
                val_bytes.extend(chunk)

            if len(val_bytes) != val_len:
                raise CorruptFileError(f"Unexpected EOF reading value for key {key!r}")

            return bytes(val_bytes)

    def keys(self) -> list[bytes]:
        """Return a sorted list of live keys."""
        with self._lock:
            if self._closed:
                raise ValueError("Database is closed")
            return sorted(self._index.keys())

    def items(self) -> list[tuple[bytes, bytes]]:
        """Return sorted (key, value) pairs for all live keys."""
        with self._lock:
            if self._closed:
                raise ValueError("Database is closed")
            result: list[tuple[bytes, bytes]] = []
            for k in sorted(self._index.keys()):
                packed = self._index[k]
                val_offset, val_len = unpack_val(packed)
                if val_len == 0:
                    result.append((k, b""))
                else:
                    os.lseek(self._fd, val_offset, os.SEEK_SET)
                    val = bytearray()
                    while len(val) < val_len:
                        chunk = os.read(self._fd, val_len - len(val))
                        if not chunk:
                            break
                        val.extend(chunk)
                    result.append((k, bytes(val)))
            return result

    def close(self) -> None:
        """Close the database file descriptor."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._fd is not None:
                try:
                    os.close(self._fd)
                finally:
                    self._fd = None
