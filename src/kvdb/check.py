"""Read-only integrity verification tool for kvdb files (PROTOCOL.md Section 6)."""

from __future__ import annotations

import os
import struct

from kvdb.errors import CorruptFileError
from kvdb.frame import HEADER_SIZE, parse_frame, parse_header

CHUNK_SIZE = 16 * 1024 * 1024


def check(path: str) -> tuple[bool, str]:
    """Verify file integrity in strictly read-only mode without modifying the file.
    
    Returns (True, "OK") if valid, or (False, "FAIL <reason>") if corrupted or torn.
    """
    if not os.path.exists(path):
        return False, f"FAIL File does not exist: {path}"

    try:
        file_size = os.path.getsize(path)
    except OSError as e:
        return False, f"FAIL Cannot read file size: {e}"

    if file_size < HEADER_SIZE:
        return False, f"FAIL File shorter than 24-byte header ({file_size} bytes)"

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        return False, f"FAIL Cannot open file for reading: {e}"

    try:
        # Step 1: Read and validate header
        header_bytes = bytearray()
        while len(header_bytes) < HEADER_SIZE:
            chunk = os.read(fd, HEADER_SIZE - len(header_bytes))
            if not chunk:
                break
            header_bytes.extend(chunk)

        if len(header_bytes) < HEADER_SIZE:
            return False, f"FAIL Truncated header ({len(header_bytes)} bytes)"

        try:
            parse_header(bytes(header_bytes))
        except CorruptFileError as e:
            return False, f"FAIL Corrupt header: {e}"

        if file_size == HEADER_SIZE:
            return True, "OK"

        # Step 2: Scan all frames to EOF
        buf = bytearray()
        buf_start_file_offset = HEADER_SIZE
        os.lseek(fd, HEADER_SIZE, os.SEEK_SET)

        while True:
            read_chunk = os.read(fd, CHUNK_SIZE)
            if read_chunk:
                buf.extend(read_chunk)

            eof_reached = len(read_chunk) == 0
            buf_pos = 0

            while True:
                frame_file_offset = buf_start_file_offset + buf_pos

                if frame_file_offset == file_size:
                    if buf_pos == len(buf):
                        return True, "OK"
                    else:
                        return False, f"FAIL Trailing bytes at offset {frame_file_offset}"

                # Rule 1: need at least 8 bytes for length & crc
                if buf_pos + 8 > len(buf):
                    if eof_reached:
                        return False, f"FAIL Truncated frame header at offset {frame_file_offset}"
                    else:
                        break

                frame_len = struct.unpack_from("<I", buf, buf_pos)[0]
                needed = 8 + frame_len

                if buf_pos + needed > len(buf):
                    if frame_file_offset + needed > file_size or eof_reached:
                        return False, f"FAIL Truncated frame payload at offset {frame_file_offset}"
                    else:
                        break

                buf_file_limit = file_size - buf_start_file_offset
                parsed_res = parse_frame(buf, buf_pos, buf_file_limit, extract_values=False)
                if parsed_res is None:
                    return False, f"FAIL Invalid frame at offset {frame_file_offset}"

                _, next_pos = parsed_res
                buf_pos = next_pos

            if eof_reached and buf_pos == len(buf):
                break

            buf = buf[buf_pos:]
            buf_start_file_offset += buf_pos

        final_offset = buf_start_file_offset + buf_pos
        if final_offset == file_size:
            return True, "OK"
        else:
            return False, f"FAIL Trailing unparsed bytes at offset {final_offset} (file_size={file_size})"

    finally:
        os.close(fd)
