"""Unit tests for kvdb.compaction (space reclamation, generation bumping, crash points, and E5 auto-compaction)."""

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tempfile
import unittest
from kvdb.db import DB
from kvdb.errors import DiskFullError


class TestCompactionBasic(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_space_reclamation_and_generation_bump(self):
        with DB.open(self.db_path) as db:
            self.assertEqual(db.generation, 1)

            # Insert 100 keys
            for i in range(100):
                db.put(f"key_{i}".encode("ascii"), b"initial_val_" + b"x" * 50)

            # Overwrite all 100 keys
            for i in range(100):
                db.put(f"key_{i}".encode("ascii"), b"updated_val_" + b"y" * 50)

            # Delete 50 keys
            for i in range(50):
                db.delete(f"key_{i}".encode("ascii"))

            size_before = os.path.getsize(self.db_path)

            # Compact
            db.compact()

            size_after = os.path.getsize(self.db_path)
            self.assertLess(size_after, size_before)
            self.assertEqual(db.generation, 2)
            self.assertEqual(len(db), 50)

            # Verify surviving keys
            for i in range(50):
                self.assertIsNone(db.get(f"key_{i}".encode("ascii")))
            for i in range(50, 100):
                self.assertEqual(
                    db.get(f"key_{i}".encode("ascii")),
                    b"updated_val_" + b"y" * 50,
                )

        # Reopen and check persistence of compacted file
        with DB.open(self.db_path) as db2:
            self.assertEqual(db2.generation, 2)
            self.assertEqual(len(db2), 50)
            self.assertEqual(
                db2.get(b"key_50"),
                b"updated_val_" + b"y" * 50,
            )


class TestCompactionDiskFullE5(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_auto_compact_and_retry_on_disk_full(self):
        # Scenario E5:
        # Cap file size. Fill with data, delete keys using reserve, then write new put.
        # The new put triggers auto-compaction and succeeds!
        max_bytes = 2000
        with DB.open(self.db_path, max_bytes=max_bytes) as db:
            # Fill with puts
            count = 0
            while True:
                try:
                    db.put(f"k{count}".encode("ascii"), b"v" * 30)
                    count += 1
                except DiskFullError:
                    break

            self.assertGreater(count, 10)

            # File is full for puts (end_offset > max_bytes - reserve).
            # Delete 5 keys using the reserve (5 * 23 = 115 bytes < 250 reserve).
            for i in range(5):
                db.delete(f"k{i}".encode("ascii"))

            # At this point, end_offset is in the reserve (e.g. > 1800).
            # Writing a put would exceed (max_bytes - reserve = 1750).
            # This triggers auto-compaction, which reclaims the 5 deleted keys!
            # After compaction, file shrinks and the put succeeds!
            gen_before = db.generation
            db.put(b"new_after_del", b"val_success")

            self.assertEqual(db.get(b"new_after_del"), b"val_success")
            self.assertEqual(db.generation, gen_before + 1)


class TestCompactionCrashPoints(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _run_script_with_cp(self, cp: str, op_script: str) -> int:
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

    def test_crash_c3_after_tmp(self):
        # C3: path is complete old file; path.compact is fsynced but not swapped.
        with DB.open(self.db_path) as db:
            db.put(b"k1", b"v1")
            db.put(b"k2", b"v2")

        rc = self._run_script_with_cp("compact_after_tmp:1", "db.compact()")
        self.assertIn(rc, (137, -9))

        # Reopen: path.compact is purged, old file intact, gen is 1
        with DB.open(self.db_path) as db:
            self.assertEqual(db.generation, 1)
            self.assertEqual(db.get(b"k1"), b"v1")
            self.assertEqual(db.get(b"k2"), b"v2")
            self.assertFalse(os.path.exists(self.db_path + ".compact"))

    def test_crash_c4_before_and_after_replace(self):
        # C4 before replace: old file intact
        with DB.open(self.db_path) as db:
            db.put(b"k1", b"v1")

        rc = self._run_script_with_cp("compact_before_replace:1", "db.compact()")
        self.assertIn(rc, (137, -9))
        with DB.open(self.db_path) as db:
            self.assertEqual(db.generation, 1)
            self.assertEqual(db.get(b"k1"), b"v1")

        # C4 after replace: new file in place! Generation is 2!
        rc = self._run_script_with_cp("compact_after_replace:1", "db.compact()")
        self.assertIn(rc, (137, -9))
        with DB.open(self.db_path) as db:
            self.assertEqual(db.generation, 2)
            self.assertEqual(db.get(b"k1"), b"v1")


if __name__ == "__main__":
    unittest.main()
