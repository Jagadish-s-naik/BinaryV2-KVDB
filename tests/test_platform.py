#!/usr/bin/env python3
"""Cross-platform sanity tests for atomic rename and process kill."""

import os
import sys
import tempfile
import unittest
import subprocess
from pathlib import Path


class TestPlatformSanity(unittest.TestCase):
    def test_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig = Path(tmpdir) / "data.db"
            new_file = Path(tmpdir) / "data.db.compact"

            orig.write_bytes(b"initial data")
            new_file.write_bytes(b"compacted data")

            # Perform atomic replace
            os.replace(new_file, orig)

            self.assertTrue(orig.exists())
            self.assertFalse(new_file.exists())
            self.assertEqual(orig.read_bytes(), b"compacted data")

    def test_process_kill(self):
        # Test proc.kill() termination on long-running process
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertIsNone(proc.poll())
        proc.kill()
        proc.wait(timeout=2)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        self.assertIsNotNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
