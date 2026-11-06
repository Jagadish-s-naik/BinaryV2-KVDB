"""Unit tests for kvdb.check (read-only file verification)."""

import hashlib
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tempfile
import unittest
from kvdb.check import check
from kvdb.db import DB
from kvdb.frame import (
    HEADER_SIZE,
    OP_TYPE_DEL,
    OP_TYPE_PUT,
    encode_frame,
    encode_header,
)
from kvdb.recovery import init_db


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_check_clean_empty_db(self):
        init_db(self.db_path)
        ok, msg = check(self.db_path)
        self.assertTrue(ok)
        self.assertEqual(msg, "OK")

    def test_check_clean_db_with_data(self):
        with DB.open(self.db_path) as db:
            db.put(b"k1", b"v1")
            db.put(b"k2", b"v2")
            db.batch([(OP_TYPE_PUT, b"k3", b"v3"), (OP_TYPE_DEL, b"k1", b"")])

        ok, msg = check(self.db_path)
        self.assertTrue(ok)
        self.assertEqual(msg, "OK")

    def test_check_nonexistent_file(self):
        ok, msg = check(os.path.join(self.tmpdir.name, "missing.db"))
        self.assertFalse(ok)
        self.assertTrue(msg.startswith("FAIL"))

    def test_check_file_shorter_than_header(self):
        Path(self.db_path).write_bytes(b"short_bytes")
        ok, msg = check(self.db_path)
        self.assertFalse(ok)
        self.assertIn("FAIL", msg)
        self.assertIn("header", msg)

    def test_check_corrupt_header_magic(self):
        hdr = bytearray(encode_header(1))
        hdr[0] = ord(b"X")
        Path(self.db_path).write_bytes(hdr)
        ok, msg = check(self.db_path)
        self.assertFalse(ok)
        self.assertIn("FAIL", msg)
        self.assertIn("Corrupt header", msg)

    def test_check_corrupt_header_crc(self):
        hdr = bytearray(encode_header(1))
        hdr[20] ^= 0xFF
        Path(self.db_path).write_bytes(hdr)
        ok, msg = check(self.db_path)
        self.assertFalse(ok)
        self.assertIn("FAIL", msg)
        self.assertIn("Corrupt header", msg)

    def test_check_trailing_garbage(self):
        with DB.open(self.db_path) as db:
            db.put(b"key", b"val")

        with open(self.db_path, "ab") as f:
            f.write(b"torn_tail_bytes")

        ok, msg = check(self.db_path)
        self.assertFalse(ok)
        self.assertIn("FAIL", msg)

    def test_check_corrupt_frame_crc(self):
        with DB.open(self.db_path) as db:
            db.put(b"key1", b"val1")
            db.put(b"key2", b"val2")

        data = bytearray(Path(self.db_path).read_bytes())
        # Flip bit in payload of second frame
        data[-1] ^= 0x01
        Path(self.db_path).write_bytes(data)

        ok, msg = check(self.db_path)
        self.assertFalse(ok)
        self.assertIn("FAIL", msg)
        self.assertIn("Invalid frame", msg)

    def test_check_strictly_read_only(self):
        # Create a database with torn tail
        with DB.open(self.db_path) as db:
            db.put(b"safe", b"data")
        with open(self.db_path, "ab") as f:
            f.write(b"\x0e\x00\x00\x00")

        bytes_before = Path(self.db_path).read_bytes()
        sha_before = hashlib.sha256(bytes_before).hexdigest()

        ok, msg = check(self.db_path)
        self.assertFalse(ok)

        bytes_after = Path(self.db_path).read_bytes()
        sha_after = hashlib.sha256(bytes_after).hexdigest()

        # Byte contents and hash must be 100% identical!
        self.assertEqual(sha_before, sha_after)
        self.assertEqual(bytes_before, bytes_after)


if __name__ == "__main__":
    unittest.main()
