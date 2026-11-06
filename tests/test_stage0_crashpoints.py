"""Unit tests for kvdb.crashpoints."""

import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import subprocess
import unittest
from kvdb.crashpoints import crash_point, reset_crash_points


class TestCrashPoints(unittest.TestCase):
    def setUp(self):
        reset_crash_points()

    def test_noop_when_unset(self):
        old_val = os.environ.pop("KVDB_CRASH_POINT", None)
        try:
            # Should not raise or exit
            crash_point("any_point")
            crash_point("any_point")
        finally:
            if old_val is not None:
                os.environ["KVDB_CRASH_POINT"] = old_val

    def test_subprocess_exit_on_nth_trigger(self):
        src_dir = str(SRC).replace("\\", "/")
        script = (
            f"import sys\n"
            f"sys.path.insert(0, {src_dir!r})\n"
            f"from kvdb.crashpoints import crash_point\n"
            f"crash_point('hit_me')\n"  # 1st hit
            f"sys.stdout.write('FIRST_OK\\n')\n"
            f"sys.stdout.flush()\n"
            f"crash_point('hit_me')\n"  # 2nd hit -> should abort immediately!
            f"sys.stdout.write('SHOULD_NOT_REACH\\n')\n"
            f"sys.stdout.flush()\n"
        )
        env = dict(os.environ)
        env["KVDB_CRASH_POINT"] = "hit_me:2"

        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        stdout, stderr = proc.communicate(timeout=5)
        # On Windows or Linux, exit code must be 137
        self.assertEqual(proc.returncode, 137)
        self.assertIn(b"FIRST_OK", stdout)
        self.assertNotIn(b"SHOULD_NOT_REACH", stdout)


if __name__ == "__main__":
    unittest.main()
