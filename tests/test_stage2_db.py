"""Unit tests for kvdb.db (core storage engine)."""

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tempfile
import threading
import unittest
from kvdb.db import DB
from kvdb.errors import DBPoisonedError, DiskFullError
from kvdb.frame import OP_TYPE_DEL, OP_TYPE_PUT


class TestDBBasicCRUD(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_open_close_context_manager(self):
        with DB.open(self.db_path) as db:
            self.assertEqual(len(db), 0)
            db.put(b"hello", b"world")
            self.assertEqual(len(db), 1)
            self.assertTrue(b"hello" in db)
            self.assertEqual(db.get(b"hello"), b"world")
        self.assertTrue(db.is_closed)

    def test_crud_lifecycle(self):
        db = DB.open(self.db_path)
        try:
            # Get missing
            self.assertIsNone(db.get(b"missing"))

            # Put and Get
            db.put(b"k1", b"v1")
            self.assertEqual(db.get(b"k1"), b"v1")

            # Update
            db.put(b"k1", b"v2")
            self.assertEqual(db.get(b"k1"), b"v2")

            # Put empty value
            db.put(b"empty", b"")
            self.assertEqual(db.get(b"empty"), b"")

            # Delete
            db.delete(b"k1")
            self.assertIsNone(db.get(b"k1"))
            self.assertFalse(b"k1" in db)

            # Delete missing key (safe no-op)
            db.delete(b"k1")

            # Keys and Items
            db.put(b"b", b"2")
            db.put(b"a", b"1")
            self.assertEqual(db.keys(), [b"a", b"b", b"empty"])
            self.assertEqual(db.items(), [(b"a", b"1"), (b"b", b"2"), (b"empty", b"")])
        finally:
            db.close()

    def test_persistence_across_reopen(self):
        with DB.open(self.db_path) as db:
            db.put(b"persisted", b"data")

        with DB.open(self.db_path) as db2:
            self.assertEqual(db2.get(b"persisted"), b"data")


class TestDBBatch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_empty_batch(self):
        with DB.open(self.db_path) as db:
            offset_before = db.end_offset
            db.batch([])
            self.assertEqual(db.end_offset, offset_before)

    def test_batch_ordering_b5(self):
        with DB.open(self.db_path) as db:
            db.batch([
                (OP_TYPE_PUT, b"x", b"val_x"),
                (OP_TYPE_DEL, b"x", b""),
                (OP_TYPE_DEL, b"y", b""),
                (OP_TYPE_PUT, b"y", b"val_y"),
            ])
            self.assertIsNone(db.get(b"x"))
            self.assertEqual(db.get(b"y"), b"val_y")


class TestDBDiskFullAndReserve(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_disk_full_guard_and_reserve(self):
        # Cap at 500 bytes. Reserve = min(65536, 500 // 8) = 62 bytes.
        # Put cap threshold = 500 - 62 = 438 bytes.
        max_bytes = 500
        with DB.open(self.db_path, max_bytes=max_bytes) as db:
            # Each put is ~30 bytes
            count = 0
            while True:
                try:
                    db.put(f"k{count}".encode("ascii"), b"val")
                    count += 1
                except DiskFullError:
                    break

            # Must have written some keys before hitting the cap
            self.assertGreater(count, 0)

            # Reads MUST still work!
            self.assertEqual(db.get(b"k0"), b"val")

            # Writing another put must fail
            with self.assertRaises(DiskFullError):
                db.put(b"new_key", b"new_val")

            # But a DELETE is delete-only and CAN use the reserve!
            # It must succeed
            db.delete(b"k0")
            self.assertIsNone(db.get(b"k0"))


class TestDBPoisoning(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_poisoning_on_fsync_failure(self):
        with DB.open(self.db_path) as db:
            db.put(b"safe", b"val")

            # Monkeypatch os.fsync to simulate I/O failure
            real_fsync = os.fsync
            try:
                def failing_fsync(fd):
                    raise OSError("Simulated EIO on disk")

                os.fsync = failing_fsync

                with self.assertRaises(DBPoisonedError):
                    db.put(b"fail", b"val")

                self.assertTrue(db.is_poisoned)

                # Subsequent writes must be immediately rejected with DBPoisonedError
                with self.assertRaises(DBPoisonedError):
                    db.put(b"another", b"val")

                # Reads are still permitted
                self.assertEqual(db.get(b"safe"), b"val")
            finally:
                os.fsync = real_fsync


class TestDBConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_concurrent_writers_and_readers(self):
        with DB.open(self.db_path) as db:
            errors = []

            def worker(w_id: int):
                try:
                    for i in range(50):
                        key = f"key_{w_id}_{i}".encode("ascii")
                        val = f"val_{w_id}_{i}".encode("ascii")
                        db.put(key, val)
                        got = db.get(key)
                        if got != val:
                            errors.append(f"Mismatch: expected {val}, got {got}")
                except Exception as e:
                    errors.append(str(e))

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            self.assertEqual(len(db), 8 * 50)


class TestDBCrashPoints(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run_with_crash_point(self, cp: str, op_script: str) -> int:
        src_dir = str(SRC).replace("\\", "/")
        db_path_str = str(self.db_path).replace("\\", "/")
        script = (
            f"import sys, os\n"
            f"sys.path.insert(0, {src_dir!r})\n"
            f"from kvdb.db import DB\n"
            f"db = DB.open({db_path_str!r})\n"
            f"{op_script}\n"
            f"db.close()\n"
        )
        env = dict(os.environ)
        env["KVDB_CRASH_POINT"] = cp
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", script], env=env)
        proc.wait(timeout=5)
        return proc.returncode

    def test_crash_before_write_a1(self):
        # A1: Process killed before write touches disk
        rc = self._run_with_crash_point("before_write:1", "db.put(b'k', b'v')")
        self.assertEqual(rc, 137)
        with DB.open(self.db_path) as db:
            self.assertIsNone(db.get(b'k'))

    def test_crash_mid_write_a2(self):
        # A2: Process killed mid-write -> torn tail on disk
        rc = self._run_with_crash_point("mid_write:1", "db.put(b'k', b'v'*100)")
        self.assertEqual(rc, 137)
        # Reopening executes recovery and truncates torn bytes
        with DB.open(self.db_path) as db:
            self.assertIsNone(db.get(b'k'))

    def test_crash_after_fsync_before_index_a4(self):
        # A4: Durable on disk, killed before index updated
        rc = self._run_with_crash_point("after_fsync_before_index:1", "db.put(b'durable', b'yes')")
        self.assertEqual(rc, 137)
        # Reopening recovers the durable frame from disk!
        with DB.open(self.db_path) as db:
            self.assertEqual(db.get(b'durable'), b'yes')

    def test_crash_after_fsync_before_ack_a5(self):
        # A5: Durable on disk, killed before ACK sent
        rc = self._run_with_crash_point("after_fsync_before_ack:1", "db.put(b'durable2', b'yes2')")
        self.assertEqual(rc, 137)
        with DB.open(self.db_path) as db:
            self.assertEqual(db.get(b'durable2'), b'yes2')


if __name__ == "__main__":
    unittest.main()
