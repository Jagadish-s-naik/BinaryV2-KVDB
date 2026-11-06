"""Unit tests for kvdb.frame (FORMAT.md encoding and 8 validity rules)."""

import sys
from pathlib import Path
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import unittest
from kvdb.errors import CorruptFileError
from kvdb.frame import (
    MAGIC,
    HEADER_SIZE,
    encode_header,
    parse_header,
    encode_op,
    encode_frame,
    parse_frame,
    OP_TYPE_PUT,
    OP_TYPE_DEL,
)


class TestFrameWorkedExample(unittest.TestCase):
    """Test against the exact test vector in docs/FORMAT.md Section 3.5."""

    def test_golden_header(self):
        hdr = encode_header(generation=1)
        expected_hex = "4b5644424c4f470001000000010000000000000095a2c297"
        self.assertEqual(hdr.hex(), expected_hex)

        gen, ver = parse_header(hdr)
        self.assertEqual(gen, 1)
        self.assertEqual(ver, 1)

    def test_golden_frame(self):
        ops = [(OP_TYPE_PUT, b"a", b"1")]
        frame_bytes = encode_frame(ops)
        expected_hex = "0e00000093781a030101000000010100010000006131"
        self.assertEqual(frame_bytes.hex(), expected_hex)

        parsed, next_pos = parse_frame(frame_bytes, 0, len(frame_bytes))
        self.assertIsNotNone(parsed)
        self.assertEqual(next_pos, len(frame_bytes))
        self.assertEqual(parsed.frame_len, 14)
        self.assertEqual(parsed.crc32, 0x031A7893)
        self.assertEqual(parsed.kind, 1)
        self.assertEqual(parsed.op_count, 1)
        self.assertEqual(len(parsed.ops), 1)

        op = parsed.ops[0]
        self.assertEqual(op.op_type, OP_TYPE_PUT)
        self.assertEqual(op.key, b"a")
        self.assertEqual(op.val, b"1")
        self.assertEqual(op.val_len, 1)
        # val offset relative to start of frame:
        # frame_len(4) + crc(4) + kind(1) + op_count(4) + type(1) + klen(2) + vlen(4) + key(1) = 21
        self.assertEqual(op.val_offset_in_frame, 21)


class TestHeaderValidation(unittest.TestCase):
    def test_short_header(self):
        with self.assertRaises(CorruptFileError):
            parse_header(b"SHORT")

    def test_invalid_magic(self):
        hdr = bytearray(encode_header(1))
        hdr[0] = ord(b"X")
        with self.assertRaises(CorruptFileError):
            parse_header(hdr)

    def test_invalid_version(self):
        hdr = bytearray(encode_header(1))
        hdr[8] = 2  # version 2
        with self.assertRaises(CorruptFileError):
            parse_header(hdr)

    def test_corrupt_header_crc(self):
        hdr = bytearray(encode_header(1))
        hdr[20] ^= 0xFF  # corrupt CRC
        with self.assertRaises(CorruptFileError):
            parse_header(hdr)


class TestOpEncoding(unittest.TestCase):
    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            encode_op(OP_TYPE_PUT, b"", b"val")

    def test_key_over_1024_rejected(self):
        with self.assertRaises(ValueError):
            encode_op(OP_TYPE_PUT, b"k" * 1025, b"val")

    def test_val_over_1mb_rejected(self):
        with self.assertRaises(ValueError):
            encode_op(OP_TYPE_PUT, b"key", b"v" * (1_048_576 + 1))

    def test_delete_with_val_rejected(self):
        with self.assertRaises(ValueError):
            encode_op(OP_TYPE_DEL, b"key", b"some_val")

    def test_empty_val_put_allowed(self):
        encoded = encode_op(OP_TYPE_PUT, b"key", b"")
        self.assertEqual(len(encoded), 7 + 3)


class TestFrameValidityRules(unittest.TestCase):
    """Test every rule in FORMAT.md Section 4 individually."""

    def setUp(self):
        self.valid_frame = encode_frame([(OP_TYPE_PUT, b"foo", b"bar")])

    def test_rule1_less_than_8_bytes(self):
        # Buffer has fewer than 8 bytes remaining
        self.assertIsNone(parse_frame(b"1234567", 0, 7))

    def test_rule2_min_length(self):
        # All zeros: frame_len=0, crc=0. Must fail rule 2.
        zero_buf = b"\x00" * 32
        self.assertIsNone(parse_frame(zero_buf, 0, len(zero_buf)))

        # frame_len = 12 (< 13)
        short_len_buf = b"\x0c\x00\x00\x00" + b"\x00" * 20
        self.assertIsNone(parse_frame(short_len_buf, 0, len(short_len_buf)))

    def test_rule3_past_end_of_file(self):
        # File size claim is shorter than frame
        self.assertIsNone(parse_frame(self.valid_frame, 0, len(self.valid_frame) - 1))
        # Buffer slice is shorter than frame
        self.assertIsNone(parse_frame(self.valid_frame[: len(self.valid_frame) - 2], 0, 1000))

    def test_rule4_crc_mismatch(self):
        bad_crc_frame = bytearray(self.valid_frame)
        bad_crc_frame[-1] ^= 0x01  # flip payload bit
        self.assertIsNone(parse_frame(bad_crc_frame, 0, len(bad_crc_frame)))

    def test_rule5_invalid_kind(self):
        # Manually alter kind = 2
        bad_kind_frame = bytearray(self.valid_frame)
        bad_kind_frame[8] = 2  # kind is at offset 8 (after 4-byte len, 4-byte crc)
        # Recalculate CRC to isolate rule 5
        import struct, zlib
        flen = len(bad_kind_frame) - 8
        payload = bad_kind_frame[8:]
        new_crc = zlib.crc32(payload, zlib.crc32(struct.pack("<I", flen))) & 0xFFFFFFFF
        bad_kind_frame[4:8] = struct.pack("<I", new_crc)
        self.assertIsNone(parse_frame(bad_kind_frame, 0, len(bad_kind_frame)))

    def test_rule6_op_count_limits(self):
        with self.assertRaises(ValueError):
            encode_frame([])
        with self.assertRaises(ValueError):
            encode_frame([(OP_TYPE_PUT, b"k", b"v")] * 10_001)

    def test_rule7_payload_leftover_bytes(self):
        # Append junk byte inside payload with matching CRC
        import struct, zlib
        payload = self.valid_frame[8:] + b"\xFF"
        flen = len(payload)
        new_crc = zlib.crc32(payload, zlib.crc32(struct.pack("<I", flen))) & 0xFFFFFFFF
        bad_frame = struct.pack("<II", flen, new_crc) + payload
        self.assertIsNone(parse_frame(bad_frame, 0, len(bad_frame)))

    def test_rule8_delete_non_empty_in_stream(self):
        # Craft frame with delete having non-zero val_len
        import struct, zlib
        # kind (1), op_count (1), type (2 = delete), key_len (1), val_len (2), key 'k', val 'ab'
        payload = struct.pack("<BIBHI", 1, 1, OP_TYPE_DEL, 1, 2) + b"kab"
        flen = len(payload)
        crc = zlib.crc32(payload, zlib.crc32(struct.pack("<I", flen))) & 0xFFFFFFFF
        crafted = struct.pack("<II", flen, crc) + payload
        self.assertIsNone(parse_frame(crafted, 0, len(crafted)))


class TestBatchEncoding(unittest.TestCase):
    def test_multi_op_batch(self):
        ops = [
            (OP_TYPE_PUT, b"user:1", b"Alice"),
            (OP_TYPE_PUT, b"user:2", b"Bob"),
            (OP_TYPE_DEL, b"user:old", b""),
            (OP_TYPE_PUT, b"empty_key", b""),
        ]
        frame = encode_frame(ops)
        parsed, next_pos = parse_frame(frame, 0, len(frame))
        self.assertIsNotNone(parsed)
        self.assertEqual(next_pos, len(frame))
        self.assertEqual(parsed.op_count, 4)
        self.assertEqual(len(parsed.ops), 4)

        self.assertEqual(parsed.ops[0].key, b"user:1")
        self.assertEqual(parsed.ops[0].val, b"Alice")
        self.assertEqual(parsed.ops[2].op_type, OP_TYPE_DEL)
        self.assertEqual(parsed.ops[2].key, b"user:old")
        self.assertEqual(parsed.ops[2].val_len, 0)
        self.assertEqual(parsed.ops[3].val, b"")
        self.assertEqual(parsed.ops[3].val_len, 0)


if __name__ == "__main__":
    unittest.main()
