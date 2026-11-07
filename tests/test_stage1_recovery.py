"""Unit tests for kvdb.recovery (database init, replay, and torn tail truncation)."""

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tempfile
import unittest
from kvdb.errors import CorruptFileError
from kvdb.frame import (
    HEADER_SIZE,
    OP_TYPE_PUT,
    OP_TYPE_DEL,
    encode_frame,
    encode_header,
)
from kvdb.recovery import (
    apply_ops,
    cleanup_temp_files,
    init_db,
    pack_val,
    recover,
    unpack_val,
)


class TestRecoveryHelpers(unittest.TestCase):
    def test_pack_unpack_val(self):
        offset = 123456789
        vlen = 1048576  # 1 MB
        packed = pack_val(offset, vlen)
        o, l = unpack_val(packed)
        self.assertEqual(o, offset)
        self.assertEqual(l, vlen)


class TestInitAndCleanup(unittest.TestCase):
    def test_init_db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            init_db(db_path)
            self.assertTrue(os.path.exists(db_path))
            self.assertEqual(os.path.getsize(db_path), HEADER_SIZE)

            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            fd = os.open(db_path, flags)
            try:
                res = recover(fd, HEADER_SIZE)
                self.assertEqual(res.generation, 1)
                self.assertEqual(res.end_offset, HEADER_SIZE)
                self.assertEqual(len(res.index), 0)
            finally:
                os.close(fd)

    def test_cleanup_temp_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            c_path = db_path + ".compact"
            i_path = db_path + ".init"
            Path(c_path).write_bytes(b"temp compact")
            Path(i_path).write_bytes(b"temp init")

            cleanup_temp_files(db_path)
            self.assertFalse(os.path.exists(c_path))
            self.assertFalse(os.path.exists(i_path))


class TestRecoveryScenarios(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")
        init_db(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _append_bytes(self, data: bytes):
        with open(self.db_path, "ab") as f:
            f.write(data)
            f.flush()

    def test_corrupt_header_leaves_file_untouched(self):
        # Corrupt the header magic
        with open(self.db_path, "r+b") as f:
            f.seek(0)
            f.write(b"BADMAGIC")

        orig_size = os.path.getsize(self.db_path)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(self.db_path, flags)
        try:
            with self.assertRaises(CorruptFileError):
                recover(fd, orig_size)
            # File must be untouched
            self.assertEqual(os.path.getsize(self.db_path), orig_size)
        finally:
            os.close(fd)

    def test_clean_replay_and_value_lookup(self):
        f1 = encode_frame([(OP_TYPE_PUT, b"k1", b"val_1")])
        f2 = encode_frame([(OP_TYPE_PUT, b"k2", b"val_2")])
        self._append_bytes(f1 + f2)

        file_size = os.path.getsize(self.db_path)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(self.db_path, flags)
        try:
            res = recover(fd, file_size)
            self.assertEqual(res.end_offset, file_size)
            self.assertEqual(len(res.index), 2)
            self.assertIn(b"k1", res.index)
            self.assertIn(b"k2", res.index)

            # Check value extraction via index offset
            o1, l1 = unpack_val(res.index[b"k1"])
            os.lseek(fd, o1, os.SEEK_SET)
            self.assertEqual(os.read(fd, l1), b"val_1")

            o2, l2 = unpack_val(res.index[b"k2"])
            os.lseek(fd, o2, os.SEEK_SET)
            self.assertEqual(os.read(fd, l2), b"val_2")
        finally:
            os.close(fd)

    def test_batch_ordering_b5(self):
        # Scenario B5: put X then del X leaves X absent
        batch1 = encode_frame([
            (OP_TYPE_PUT, b"x", b"val_x"),
            (OP_TYPE_DEL, b"x", b""),
        ])
        # del Y then put Y leaves Y present
        batch2 = encode_frame([
            (OP_TYPE_DEL, b"y", b""),
            (OP_TYPE_PUT, b"y", b"val_y"),
        ])
        self._append_bytes(batch1 + batch2)

        file_size = os.path.getsize(self.db_path)
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(self.db_path, flags)
        try:
            res = recover(fd, file_size)
            self.assertNotIn(b"x", res.index)
            self.assertIn(b"y", res.index)

            oy, ly = unpack_val(res.index[b"y"])
            os.lseek(fd, oy, os.SEEK_SET)
            self.assertEqual(os.read(fd, ly), b"val_y")
        finally:
            os.close(fd)

    def test_torn_tail_truncation_short_header(self):
        # Append 1 valid frame, then 4 bytes of garbage (short frame header)
        valid_frame = encode_frame([(OP_TYPE_PUT, b"k", b"v")])
        clean_len = HEADER_SIZE + len(valid_frame)
        self._append_bytes(valid_frame + b"\x0e\x00\x00\x00")

        file_size = os.path.getsize(self.db_path)
        self.assertGreater(file_size, clean_len)

        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(self.db_path, flags)
        try:
            res = recover(fd, file_size)
            self.assertEqual(res.end_offset, clean_len)
            self.assertEqual(os.path.getsize(self.db_path), clean_len)
            self.assertEqual(len(res.index), 1)

            # Idempotence: recovering again does not truncate further
            res2 = recover(fd, clean_len)
            self.assertEqual(res2.end_offset, clean_len)
            self.assertEqual(os.path.getsize(self.db_path), clean_len)
        finally:
            os.close(fd)

    def test_torn_tail_truncation_bad_crc(self):
        valid1 = encode_frame([(OP_TYPE_PUT, b"k1", b"v1")])
        valid2 = bytearray(encode_frame([(OP_TYPE_PUT, b"k2", b"v2")]))
        clean_len = HEADER_SIZE + len(valid1)

        # Corrupt valid2 CRC
        valid2[4] ^= 0xFF
        self._append_bytes(valid1 + valid2)

        flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(self.db_path, flags)
        try:
            res = recover(fd, os.path.getsize(self.db_path))
            self.assertEqual(res.end_offset, clean_len)
            self.assertEqual(os.path.getsize(self.db_path), clean_len)
            self.assertIn(b"k1", res.index)
            self.assertNotIn(b"k2", res.index)
        finally:
            os.close(fd)

    def test_recovery_crash_points_c2(self):
        # Scenario C2: kill before truncate vs kill after truncate
        valid1 = encode_frame([(OP_TYPE_PUT, b"k1", b"v1")])
        clean_len = HEADER_SIZE + len(valid1)
        torn_tail = b"\x0e\x00\x00\x00"  # 4 bytes garbage
        self._append_bytes(valid1 + torn_tail)
        full_len = clean_len + len(torn_tail)

        # 1. Kill before truncate
        src_dir = str(SRC).replace("\\", "/")
        db_path_str = str(self.db_path).replace("\\", "/")
        script = (
            f"import sys, os\n"
            f"sys.path.insert(0, {src_dir!r})\n"
            f"from kvdb.recovery import recover\n"
            f"flags = os.O_RDWR | getattr(os, 'O_BINARY', 0)\n"
            f"fd = os.open({db_path_str!r}, flags)\n"
            f"recover(fd, os.path.getsize({db_path_str!r}))\n"
        )
        env = dict(os.environ)
        env["KVDB_CRASH_POINT"] = "recovery_before_truncate:1"
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", script], env=env)
        proc.wait(timeout=5)
        self.assertIn(proc.returncode, (137, -9))
        # File should still have torn tail
        self.assertEqual(os.path.getsize(self.db_path), full_len)

        # 2. Kill after truncate
        env["KVDB_CRASH_POINT"] = "recovery_after_truncate:1"
        proc = subprocess.Popen([sys.executable, "-c", script], env=env)
        proc.wait(timeout=5)
        self.assertIn(proc.returncode, (137, -9))
        # File should now be truncated to clean_len
        self.assertEqual(os.path.getsize(self.db_path), clean_len)


if __name__ == "__main__":
    unittest.main()
