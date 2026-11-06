#!/usr/bin/env python3
"""Resilience and fuzzing suite for binary WAL recovery and raw auditor.

Tests recovery resilience against:
- Bit-rot in header and frame CRCs
- Corrupted frame length prefixes and payload bounds
- Sub-header and truncated file boundaries
- Unaligned tail truncations (1..32 byte cuts)
- Zero-byte preallocation padding at EOF
- Garbage byte injections between frames
- Invalid op types, key lengths, and delete values
"""

import os
import sys
import zlib
import struct
import tempfile
import unittest
from pathlib import Path

# Add project root to path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from torture_tool.oracle import validate_raw_binary_db

MAGIC = b"KVDBLOG\x00"
HEADER_FMT = "<8sIQI"
OP_HDR_FMT = "<BHI"


def make_valid_header(generation: int = 1) -> bytes:
    prefix = struct.pack("<8sIQ", MAGIC, 1, generation)
    crc = zlib.crc32(prefix) & 0xFFFFFFFF
    return prefix + struct.pack("<I", crc)


def make_frame(ops: list[tuple[int, bytes, bytes]]) -> bytes:
    payload_parts = [struct.pack("<BI", 1, len(ops))]
    for op_type, key, val in ops:
        payload_parts.append(struct.pack(OP_HDR_FMT, op_type, len(key), len(val)))
        payload_parts.append(key)
        payload_parts.append(val)
    payload = b"".join(payload_parts)
    frame_len = len(payload)
    len_bytes = struct.pack("<I", frame_len)
    crc = zlib.crc32(payload, zlib.crc32(len_bytes)) & 0xFFFFFFFF
    return len_bytes + struct.pack("<I", crc) + payload


class TestFuzzRecovery(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "fuzz_test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_db(self, data: bytes) -> None:
        with open(self.db_path, "wb") as f:
            f.write(data)

    def test_empty_and_sub_header_truncation(self):
        """Files shorter than 24 bytes must be rejected as invalid/corrupted header (except 0-byte initial)."""
        # Size 0 is empty initial file
        self._write_db(b"")
        valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
        self.assertTrue(valid)
        self.assertIn("Empty database", msg)

        # 1..23 bytes are truncated sub-header
        for size in [1, 4, 8, 16, 23]:
            self._write_db(b"A" * size)
            valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
            self.assertFalse(valid, f"Expected rejection for size {size}")
            self.assertIn("less than 24-byte header", msg)

    def test_header_magic_bit_rot(self):
        """Corrupted magic bytes must be rejected."""
        valid_hdr = make_valid_header()
        for idx in range(8):
            corrupt_hdr = bytearray(valid_hdr)
            corrupt_hdr[idx] ^= 0xFF
            self._write_db(bytes(corrupt_hdr))
            valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
            self.assertFalse(valid)
            self.assertIn("Invalid magic", msg)

    def test_header_version_and_generation_corruption(self):
        """Unsupported version or generation < 1 must be rejected."""
        # Version = 2
        prefix = struct.pack("<8sIQ", MAGIC, 2, 1)
        crc = zlib.crc32(prefix) & 0xFFFFFFFF
        self._write_db(prefix + struct.pack("<I", crc))
        valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
        self.assertFalse(valid)
        self.assertIn("Unsupported version", msg)

        # Generation = 0
        prefix = struct.pack("<8sIQ", MAGIC, 1, 0)
        crc = zlib.crc32(prefix) & 0xFFFFFFFF
        self._write_db(prefix + struct.pack("<I", crc))
        valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
        self.assertFalse(valid)
        self.assertIn("Invalid generation", msg)

    def test_header_crc_bit_flip(self):
        """Single bit flips in header data or header CRC must trigger CRC mismatch."""
        valid_hdr = make_valid_header()
        # Flip bits in bytes 8..19 (covered by CRC, keeping magic intact)
        for offset in [8, 9, 10, 11, 12, 15, 18, 19]:
            corrupted = bytearray(valid_hdr)
            corrupted[offset] ^= 0x01
            self._write_db(bytes(corrupted))
            valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
            self.assertFalse(valid)
            self.assertTrue("Header CRC mismatch" in msg or "Invalid generation" in msg or "Unsupported version" in msg)

        # Flip bit in CRC field itself (bytes 20..23)
        for offset in [20, 21, 22, 23]:
            corrupted = bytearray(valid_hdr)
            corrupted[offset] ^= 0x80
            self._write_db(bytes(corrupted))
            valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
            self.assertFalse(valid)
            self.assertIn("Header CRC mismatch", msg)

    def test_frame_crc_bit_rot(self):
        """Single bit-flips in frame payload or frame CRC must be caught."""
        hdr = make_valid_header()
        frame1 = make_frame([(1, b"key1", b"val1")])
        frame2 = make_frame([(1, b"key2", b"val2")])
        full_data = bytearray(hdr + frame1 + frame2)

        # Fuzz every single byte in frame1 (header + payload)
        frame1_start = 24
        frame1_end = 24 + len(frame1)

        for byte_idx in range(frame1_start, frame1_end):
            fuzzed = bytearray(full_data)
            fuzzed[byte_idx] ^= 0x04
            self._write_db(bytes(fuzzed))
            valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
            self.assertFalse(valid, f"Failed to catch bit flip at byte {byte_idx}")
            self.assertTrue(
                "CRC mismatch" in msg or "bounds" in msg or "overflow" in msg or "Torn" in msg or "invalid" in msg
            )

    def test_unaligned_tail_truncation_fuzzer(self):
        """Simulate crash mid-write by progressively truncating 1 to N-1 bytes from the last frame."""
        hdr = make_valid_header()
        frame1 = make_frame([(1, b"key1", b"val1")])
        frame2 = make_frame([(1, b"key2", b"val2_longer_payload_for_truncation")])
        valid_db = hdr + frame1 + frame2

        # Verify initial state is completely valid
        self._write_db(valid_db)
        valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
        self.assertTrue(valid, msg)

        # Progressively truncate from the tail of frame2 (1 to len(frame2) - 1 bytes)
        for cut_bytes in range(1, len(frame2)):
            truncated = valid_db[:-cut_bytes]
            self._write_db(truncated)
            valid, msg = validate_raw_binary_db(self.db_path, strict_magic=True)
            self.assertFalse(valid, f"Unaligned cut of {cut_bytes} bytes was not detected")
            self.assertTrue(
                "Torn" in msg or "CRC mismatch" in msg or "less than" in msg,
                f"Unexpected error msg for cut {cut_bytes}: {msg}",
            )

    def test_zero_byte_preallocation_padding(self):
        """Zero bytes appended at EOF (e.g. disk preallocation) must be detected as invalid tail."""
        hdr = make_valid_header()
        frame = make_frame([(1, b"key", b"val")])
        for pad_len in [1, 4, 8, 16, 512]:
            padded = hdr + frame + (b"\x00" * pad_len)
            self._write_db(padded)
            valid, msg = validate_raw_binary_db(self.db_path)
            self.assertFalse(valid, f"Zero-padding of length {pad_len} was not detected")
            self.assertTrue("Torn" in msg or "CRC mismatch" in msg or "Untruncated" in msg)

    def test_garbage_bytes_between_frames(self):
        """Injected garbage between frames must fail CRC or frame structure rules."""
        hdr = make_valid_header()
        frame1 = make_frame([(1, b"k1", b"v1")])
        frame2 = make_frame([(1, b"k2", b"v2")])
        corrupted = hdr + frame1 + b"CORRUPT_BYTES" + frame2
        self._write_db(corrupted)
        valid, msg = validate_raw_binary_db(self.db_path)
        self.assertFalse(valid)
        self.assertTrue("CRC mismatch" in msg or "Torn" in msg)

    def test_invalid_delete_val_len_and_key_bounds(self):
        """FORMAT rules: delete must have val_len=0; key_len must be [1, 1024]."""
        hdr = make_valid_header()

        # Delete with non-zero val_len (rule violation)
        bad_delete_payload = (
            struct.pack("<BI", 1, 1)  # kind=1, op_count=1
            + struct.pack(OP_HDR_FMT, 2, 4, 5)  # op_type=2 (delete), key_len=4, val_len=5 (ILLEGAL)
            + b"key1"
            + b"val12"
        )
        frame_len = len(bad_delete_payload)
        len_bytes = struct.pack("<I", frame_len)
        crc = zlib.crc32(bad_delete_payload, zlib.crc32(len_bytes)) & 0xFFFFFFFF
        bad_frame = len_bytes + struct.pack("<I", crc) + bad_delete_payload
        self._write_db(hdr + bad_frame)
        valid, msg = validate_raw_binary_db(self.db_path)
        self.assertFalse(valid)
        self.assertIn("delete op 0 has non-zero val_len", msg)

        # Zero key_len (rule violation)
        bad_key_payload = (
            struct.pack("<BI", 1, 1)
            + struct.pack(OP_HDR_FMT, 1, 0, 3)  # key_len=0 (ILLEGAL)
            + b"val"
        )
        frame_len = len(bad_key_payload)
        len_bytes = struct.pack("<I", frame_len)
        crc = zlib.crc32(bad_key_payload, zlib.crc32(len_bytes)) & 0xFFFFFFFF
        bad_frame = len_bytes + struct.pack("<I", crc) + bad_key_payload
        self._write_db(hdr + bad_frame)
        valid, msg = validate_raw_binary_db(self.db_path)
        self.assertFalse(valid)
        self.assertIn("key_len 0 out of bounds", msg)


if __name__ == "__main__":
    unittest.main()
