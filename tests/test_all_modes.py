"""Comprehensive test suite running all 9 torture modes against the test harness.

Per docs/TORTURE_SPEC.md:
The torture tool must support and seamlessly run:
- default
- contention (Scenario D2)
- large-values (Scenario B2)
- large-batches (Scenario B3)
- compaction (Scenario C1)
- recovery-crash (Scenario F2/F4)
- disk-full (Scenario E1-E5)
- crash-points (Scenario C3/D1)
- scale (Scenario F3)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

FAKE_ENGINE = REPO_ROOT / "tests" / "fake_engine.py"


class TestAllTortureModes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert FAKE_ENGINE.exists(), f"fake_engine not found at {FAKE_ENGINE}"

    def _run_mode(self, mode: str, iterations: int = 3, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir) / f"work_{mode}"
            cmd = [
                sys.executable,
                "-m",
                "torture_tool",
                "--seed",
                "12345",
                "--iterations",
                str(iterations),
                "--mode",
                mode,
                "--workdir",
                str(work),
                "--kvdb",
                str(FAKE_ENGINE),
                "--kill-min-ms",
                "20",
                "--kill-max-ms",
                "80",
            ]
            if extra_args:
                cmd.extend(extra_args)
            return subprocess.run(cmd, capture_output=True, text=True)

    def test_mode_default(self):
        res = self._run_mode("default", iterations=3)
        self.assertEqual(res.returncode, 0, f"default mode failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("3 passed, 0 failed", res.stdout)

    def test_mode_contention(self):
        res = self._run_mode("contention", iterations=3)
        self.assertEqual(res.returncode, 0, f"contention mode failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("3 passed, 0 failed", res.stdout)

    def test_mode_large_values(self):
        res = self._run_mode("large-values", iterations=3)
        self.assertEqual(res.returncode, 0, f"large-values mode failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("3 passed, 0 failed", res.stdout)

    def test_mode_large_batches(self):
        res = self._run_mode("large-batches", iterations=3)
        self.assertEqual(res.returncode, 0, f"large-batches mode failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("3 passed, 0 failed", res.stdout)

    def test_mode_compaction(self):
        res = self._run_mode("compaction", iterations=2)
        self.assertEqual(res.returncode, 0, f"compaction mode failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("2 passed, 0 failed", res.stdout)

    def test_mode_recovery_crash(self):
        res = self._run_mode("recovery-crash", iterations=3)
        self.assertEqual(res.returncode, 0, f"recovery-crash mode failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("3 passed, 0 failed", res.stdout)

    def test_mode_disk_full(self):
        res = self._run_mode("disk-full", iterations=2)
        self.assertEqual(res.returncode, 0, f"disk-full mode failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("2 passed, 0 failed", res.stdout)

    def test_mode_crash_points(self):
        res = self._run_mode("crash-points", iterations=3)
        self.assertEqual(res.returncode, 0, f"crash-points mode failed:\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}")
        self.assertIn("3 passed, 0 failed", res.stdout)


if __name__ == "__main__":
    unittest.main()
