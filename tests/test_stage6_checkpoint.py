"""Unit tests for kvdb checkpoint frames (kind = 2)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
ROOT = Path(__file__).resolve().parent.parent
KVDB_BIN = str(ROOT / "kvdb")

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kvdb.db import DB
from kvdb.frame import (
    KIND_CHECKPOINT,
    KIND_OPS,
    encode_checkpoint_frame,
    parse_frame,
)


class TestCheckpointFrames(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "checkpoint_test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_encode_and_parse_checkpoint_frame(self):
        index = {
            b"key1": (1024, 25),
            b"key2": (2048, 100),
            b"another:long:key": (5000, 12),
        }
        frame_bytes = encode_checkpoint_frame(index)
        self.assertGreater(len(frame_bytes), 25)

        target_index = {}
        res = parse_frame(
            frame_bytes,
            pos=0,
            file_size=len(frame_bytes),
            extract_values=True,
            target_index=target_index,
        )
        self.assertIsNotNone(res)
        parsed_frame, next_pos = res
        self.assertEqual(next_pos, len(frame_bytes))
        self.assertEqual(parsed_frame.kind, KIND_CHECKPOINT)
        self.assertEqual(len(target_index), 3)
        for k, (off, length) in index.items():
            packed = target_index[k]
            from kvdb.frame import unpack_val
            self.assertEqual(unpack_val(packed), (off, length))

    def test_db_checkpoint_lifecycle(self):
        # 1. Put keys and checkpoint
        with DB.open(self.db_path) as db:
            for i in range(100):
                db.put(f"key_{i}".encode("utf-8"), f"val_{i}".encode("utf-8"))
            self.assertEqual(len(db), 100)
            db.checkpoint()

        # 2. Reopen and verify data recovered from checkpoint
        with DB.open(self.db_path) as db:
            self.assertEqual(len(db), 100)
            for i in range(100):
                self.assertEqual(
                    db.get(f"key_{i}".encode("utf-8")),
                    f"val_{i}".encode("utf-8"),
                )

            # 3. Add operational writes after checkpoint
            db.put(b"new_key", b"new_val")
            db.put(b"key_0", b"val_0_updated")
            db.delete(b"key_1")

        # 4. Reopen and verify snapshot + subsequent operations
        with DB.open(self.db_path) as db:
            self.assertEqual(len(db), 100)  # 100 + 1 (new) - 1 (del) = 100
            self.assertEqual(db.get(b"new_key"), b"new_val")
            self.assertEqual(db.get(b"key_0"), b"val_0_updated")
            self.assertIsNone(db.get(b"key_1"))
            self.assertEqual(db.get(b"key_2"), b"val_2")

    def test_checkpoint_empty_db(self):
        with DB.open(self.db_path) as db:
            self.assertEqual(len(db), 0)
            db.checkpoint()

        with DB.open(self.db_path) as db:
            self.assertEqual(len(db), 0)

    def test_checkpoint_torn_recovery(self):
        # Write valid data and checkpoint
        with DB.open(self.db_path) as db:
            for i in range(50):
                db.put(f"k{i}".encode(), f"v{i}".encode())
            db.checkpoint()

        file_size_before = os.path.getsize(self.db_path)

        # Simulate a crash during a second checkpoint: write partial frame bytes
        fake_index = {f"k{i}".encode(): (100, 10) for i in range(10)}
        partial_frame = encode_checkpoint_frame(fake_index)[:20]  # Torn header/body
        with open(self.db_path, "ab") as f:
            f.write(partial_frame)

        self.assertGreater(os.path.getsize(self.db_path), file_size_before)

        # Reopen DB - recovery should detect tear at EOF and truncate safely
        with DB.open(self.db_path) as db:
            self.assertEqual(len(db), 50)
            for i in range(50):
                self.assertEqual(db.get(f"k{i}".encode()), f"v{i}".encode())

        # File size should be rolled back to valid state
        self.assertEqual(os.path.getsize(self.db_path), file_size_before)

    def test_checkpoint_cli(self):
        # Open and insert via CLI
        cmd = [sys.executable, KVDB_BIN, "put", self.db_path, "cli_key", "cli_val"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0)

        # Run checkpoint CLI
        cmd = [sys.executable, KVDB_BIN, "checkpoint", self.db_path]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "OK")

        # Check via check command
        cmd = [sys.executable, KVDB_BIN, "check", self.db_path]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "OK")

        # Verify get
        cmd = [sys.executable, KVDB_BIN, "get", self.db_path, "cli_key"]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "cli_val")

    def test_checkpoint_serve_protocol(self):
        cmd = [sys.executable, KVDB_BIN, "serve", self.db_path]
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        stdin = proc.stdin
        stdout = proc.stdout

        try:
            # 1. Expect READY
            ready_line = stdout.readline()
            self.assertTrue(ready_line.startswith(b"READY keys=0"))

            # 2. PUT (hex: "foo" -> 666f6f, "bar" -> 626172)
            stdin.write(b"PUT op1 666f6f 626172\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"ACK op1\n")

            # 3. CHECKPOINT
            stdin.write(b"CHECKPOINT op2\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"ACK op2\n")

            # 4. GET
            stdin.write(b"GET op3 666f6f\n")
            stdin.flush()
            res = stdout.readline()
            self.assertEqual(res, b"VAL op3 626172\n")

            # 5. QUIT
            stdin.write(b"QUIT\n")
            stdin.flush()
            proc.wait(timeout=2)
            self.assertEqual(proc.returncode, 0)
        finally:
            if stdin:
                stdin.close()
            if stdout:
                stdout.close()
            if proc.stderr:
                proc.stderr.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
