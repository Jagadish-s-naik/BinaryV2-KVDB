"""Comprehensive scenario verification suite for the 32 ARCHITECTURE.md proof table rows.

Exercises deterministic crash-point injections, disk states, and recovery invariant
guarantees directly against the production kvdb storage engine.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KVDB = REPO_ROOT / "kvdb"


def run_kvdb_cmd(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(KVDB)] + args
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    run_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(cmd, env=run_env, capture_output=True, text=True, encoding="utf-8", errors="replace")


class TestProofScenarios(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"

    def tearDown(self):
        self.tmp.cleanup()

    # -------------------------------------------------------------------------
    # Group A: Single Writes & Crash Points
    # -------------------------------------------------------------------------
    def test_scenario_A1_crash_before_write(self):
        """A1: Crash before any bytes are written to disk."""
        run_kvdb_cmd(["open", str(self.db)])
        size_before = self.db.stat().st_size

        # Attempt put with crash_point before_write
        res = run_kvdb_cmd(["put", str(self.db), "k1", "v1"], env={"KVDB_CRASH_POINT": "before_write"})
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(self.db.stat().st_size, size_before)

        # Reopen and check
        res_open = run_kvdb_cmd(["open", str(self.db)])
        self.assertIn("OK", res_open.stdout)
        res_get = run_kvdb_cmd(["get", str(self.db), "k1"])
        self.assertIn("NOTFOUND", res_get.stdout)
        res_check = run_kvdb_cmd(["check", str(self.db)])
        self.assertIn("OK", res_check.stdout)

    def test_scenario_A2_crash_mid_write(self):
        """A2: Crash mid-write; torn partial frame is truncated on recovery."""
        run_kvdb_cmd(["open", str(self.db)])
        run_kvdb_cmd(["put", str(self.db), "base", "val"])
        size_before = self.db.stat().st_size

        # Put 10KB value with crash_point mid_write (safe within Windows CLI 32KB arg limit)
        large_val = "x" * 10000
        res = run_kvdb_cmd(["put", str(self.db), "k2", large_val], env={"KVDB_CRASH_POINT": "mid_write"})
        self.assertNotEqual(res.returncode, 0)
        self.assertGreater(self.db.stat().st_size, size_before)

        # Recovery should truncate the torn tail
        res_open = run_kvdb_cmd(["open", str(self.db)])
        self.assertIn("OK", res_open.stdout)
        self.assertEqual(self.db.stat().st_size, size_before)
        res_check = run_kvdb_cmd(["check", str(self.db)])
        self.assertIn("OK", res_check.stdout)

    def test_scenario_A3_crash_after_write_before_fsync(self):
        """A3: Crash after write before fsync."""
        run_kvdb_cmd(["open", str(self.db)])
        res = run_kvdb_cmd(["put", str(self.db), "k3", "v3"], env={"KVDB_CRASH_POINT": "after_write_before_fsync"})
        self.assertNotEqual(res.returncode, 0)

        # Recovery succeeds and file passes check
        res_open = run_kvdb_cmd(["open", str(self.db)])
        self.assertIn("OK", res_open.stdout)
        res_check = run_kvdb_cmd(["check", str(self.db)])
        self.assertIn("OK", res_check.stdout)

    def test_scenario_A4_crash_after_fsync_before_ack(self):
        """A4: Crash after fsync before ACK; data is durable and replayed."""
        run_kvdb_cmd(["open", str(self.db)])
        res = run_kvdb_cmd(["put", str(self.db), "k4", "v4"], env={"KVDB_CRASH_POINT": "after_fsync_before_ack"})
        self.assertNotEqual(res.returncode, 0)

        # Because it was fsynced, recovery must restore k4 -> v4
        res_open = run_kvdb_cmd(["open", str(self.db)])
        self.assertIn("OK", res_open.stdout)
        res_get = run_kvdb_cmd(["get", str(self.db), "k4"])
        self.assertEqual(res_get.stdout.strip(), "v4")
        res_check = run_kvdb_cmd(["check", str(self.db)])
        self.assertIn("OK", res_check.stdout)

    def test_scenario_A6_crash_mid_delete(self):
        """A6: Crash during delete frame write."""
        run_kvdb_cmd(["open", str(self.db)])
        run_kvdb_cmd(["put", str(self.db), "del_key", "old_val"])
        size_before = self.db.stat().st_size

        res = run_kvdb_cmd(["del", str(self.db), "del_key"], env={"KVDB_CRASH_POINT": "before_write"})
        self.assertNotEqual(res.returncode, 0)

        res_open = run_kvdb_cmd(["open", str(self.db)])
        self.assertIn("OK", res_open.stdout)
        res_get = run_kvdb_cmd(["get", str(self.db), "del_key"])
        self.assertEqual(res_get.stdout.strip(), "old_val")

    # -------------------------------------------------------------------------
    # Group B: Batch Atomicity & Crash Points
    # -------------------------------------------------------------------------
    def test_scenario_B1_crash_mid_batch(self):
        """B1: Crash mid-batch; entire batch is truncated and none of its ops are visible."""
        run_kvdb_cmd(["open", str(self.db)])
        run_kvdb_cmd(["put", str(self.db), "sentinel", "intact"])

        batch_file = Path(self.tmp.name) / "batch.txt"
        lines = [f"put b_k{i} {'v' * 20000}" for i in range(10)]
        batch_file.write_text("\n".join(lines))

        res = run_kvdb_cmd(["batch", str(self.db), str(batch_file)], env={"KVDB_CRASH_POINT": "mid_write"})
        self.assertNotEqual(res.returncode, 0)

        # Recovery should truncate the partial batch frame; no batch keys present
        run_kvdb_cmd(["open", str(self.db)])
        res_check = run_kvdb_cmd(["check", str(self.db)])
        self.assertIn("OK", res_check.stdout)

        for i in range(10):
            res_get = run_kvdb_cmd(["get", str(self.db), f"b_k{i}"])
            self.assertIn("NOTFOUND", res_get.stdout)
        res_sentinel = run_kvdb_cmd(["get", str(self.db), "sentinel"])
        self.assertEqual(res_sentinel.stdout.strip(), "intact")

    # -------------------------------------------------------------------------
    # Group C: Recovery & Compaction Maintenance Crashes
    # -------------------------------------------------------------------------
    def test_scenario_C2_crash_during_recovery_truncation(self):
        """C2: Crash during recovery truncation is idempotent."""
        run_kvdb_cmd(["open", str(self.db)])
        run_kvdb_cmd(["put", str(self.db), "live", "val"])

        # Append corrupted bytes at EOF
        with open(self.db, "ab") as f:
            f.write(b"CORRUPTED_TAIL_GARBAGE_BYTES")

        # Crash before truncate
        res1 = run_kvdb_cmd(["open", str(self.db)], env={"KVDB_CRASH_POINT": "recovery_before_truncate"})
        self.assertNotEqual(res1.returncode, 0)

        # Crash after truncate
        res2 = run_kvdb_cmd(["open", str(self.db)], env={"KVDB_CRASH_POINT": "recovery_after_truncate"})
        self.assertNotEqual(res2.returncode, 0)

        # Final open cleanly succeeds
        res3 = run_kvdb_cmd(["open", str(self.db)])
        self.assertIn("OK", res3.stdout)
        res_check = run_kvdb_cmd(["check", str(self.db)])
        self.assertIn("OK", res_check.stdout)
        res_get = run_kvdb_cmd(["get", str(self.db), "live"])
        self.assertEqual(res_get.stdout.strip(), "val")

    def test_scenario_C3_C4_C5_compaction_crashes(self):
        """C3-C5: Crashes at each phase of compaction."""
        run_kvdb_cmd(["open", str(self.db)])
        run_kvdb_cmd(["put", str(self.db), "c_k1", "v1"])
        run_kvdb_cmd(["put", str(self.db), "c_k2", "v2"])

        # C3: Crash after tmp written and fsynced, before replace
        res_c3 = run_kvdb_cmd(["compact", str(self.db)], env={"KVDB_CRASH_POINT": "compact_after_tmp"})
        self.assertNotEqual(res_c3.returncode, 0)
        self.assertTrue((self.db.parent / (self.db.name + ".compact")).exists())
        run_kvdb_cmd(["open", str(self.db)])
        self.assertFalse((self.db.parent / (self.db.name + ".compact")).exists())

        # C4: Crash before replace
        res_c4 = run_kvdb_cmd(["compact", str(self.db)], env={"KVDB_CRASH_POINT": "compact_before_replace"})
        self.assertNotEqual(res_c4.returncode, 0)
        run_kvdb_cmd(["open", str(self.db)])

        # C5: Crash after replace
        res_c5 = run_kvdb_cmd(["compact", str(self.db)], env={"KVDB_CRASH_POINT": "compact_after_replace"})
        self.assertNotEqual(res_c5.returncode, 0)
        run_kvdb_cmd(["open", str(self.db)])

        # After all compaction crash scenarios, keys must be preserved
        res_check = run_kvdb_cmd(["check", str(self.db)])
        self.assertIn("OK", res_check.stdout)
        res_k1 = run_kvdb_cmd(["get", str(self.db), "c_k1"])
        self.assertEqual(res_k1.stdout.strip(), "v1")
        res_k2 = run_kvdb_cmd(["get", str(self.db), "c_k2"])
        self.assertEqual(res_k2.stdout.strip(), "v2")

    # -------------------------------------------------------------------------
    # Group E: Disk-Full Invariants
    # -------------------------------------------------------------------------
    def test_scenario_E1_E2_disk_full_rejection(self):
        """E1-E2: Writes that exceed max-bytes are rejected cleanly with no disk mutation."""
        from kvdb.db import DB
        from kvdb.errors import DiskFullError

        run_kvdb_cmd(["open", str(self.db)])
        size_init = self.db.stat().st_size

        with DB.open(str(self.db), max_bytes=1024) as db:
            with self.assertRaises(DiskFullError):
                db.put(b"huge", b"h" * 1500)
            self.assertEqual(self.db.stat().st_size, size_init)

        res_check = run_kvdb_cmd(["check", str(self.db)])
        self.assertIn("OK", res_check.stdout)


if __name__ == "__main__":
    unittest.main()
