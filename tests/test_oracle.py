"""Unit tests for the independent torture Oracle.

Per docs/TORTURE_SPEC.md Section 10:
A passing result is meaningless until the tool is shown to fail on a broken engine.
Tests cover each check in section 6 using synthetic histories (no engine needed):
1. ACKs before writing (lost write / dropped ACKed write).
2. Drops an ACKed write after restart.
3. Applies only some operations of a batch.
4. Returns a value truncated by one byte.
5. Returns another key's value.
6. A put-then-delete in a batch resolves to the wrong order.
"""

import sys
import unittest
from pathlib import Path

# Add repo root to sys.path so torture_tool can be imported
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from torture_tool.oracle import Oracle, generate_value


class TestOracleSyntheticHistories(unittest.TestCase):
    def setUp(self):
        self.oracle = Oracle(iteration=1)
        self.baseline: dict[str, bytes] = {}

    def test_shared_key_lost_acked_write(self):
        """Case 1 & 2: ACKs before writing / drops an ACKed write after restart."""
        val = generate_value("i1.w0.1", "s:1".encode().hex())
        self.oracle.record_sent_put("i1.w0.1", "s:1", val, sent_ns=100)
        self.oracle.record_ack("i1.w0.1", ack_ns=200)

        # Engine dropped the write: s:1 is absent in dump
        dump = {}
        res = self.oracle.check_invariants(dump, self.baseline)
        self.assertFalse(res.passed)
        self.assertEqual(res.key, "s:1")
        self.assertIn("shared key", res.rule)

    def test_shared_key_stale_overwritten_acked_write_rejected(self):
        """Candidate set exclusion: if op y was sent after x was acked, x cannot win."""
        val1 = generate_value("i1.w0.1", "s:1".encode().hex())
        val2 = generate_value("i1.w1.2", "s:1".encode().hex())

        # Op 1 sent at 100, acked at 200
        self.oracle.record_sent_put("i1.w0.1", "s:1", val1, sent_ns=100)
        self.oracle.record_ack("i1.w0.1", ack_ns=200)

        # Op 2 sent at 300 (> ack 200), acked at 400
        self.oracle.record_sent_put("i1.w1.2", "s:1", val2, sent_ns=300)
        self.oracle.record_ack("i1.w1.2", ack_ns=400)

        # Dump contains stale val1!
        dump = {"s:1": val1}
        res = self.oracle.check_invariants(dump, self.baseline)
        self.assertFalse(res.passed)
        self.assertEqual(res.key, "s:1")

        # But if dump contains val2, it passes!
        dump_valid = {"s:1": val2}
        res_valid = self.oracle.check_invariants(dump_valid, self.baseline)
        self.assertTrue(res_valid.passed)

    def test_partial_batch_applied(self):
        """Case 3: Applies only some operations of a batch."""
        k1 = "b:batch1:0"
        k2 = "b:batch1:1"
        v1 = generate_value("i1.w0.1", k1.encode().hex())
        v2 = generate_value("i1.w0.1", k2.encode().hex())

        batch = self.oracle.record_sent_batch(
            "i1.w0.1",
            [("PUT", k1, v1), ("PUT", k2, v2)],
            sent_ns=100,
        )
        # Even if PENDING or ACKED, partial application is illegal
        dump = {k1: v1}  # only k1 present, k2 missing!
        res = self.oracle.check_invariants(dump, self.baseline)
        self.assertFalse(res.passed)
        self.assertIn("partial batch", res.rule)

    def test_truncated_value(self):
        """Case 4: Returns a value truncated by one byte."""
        val = generate_value("i1.w0.1", "s:2".encode().hex())
        self.oracle.record_sent_put("i1.w0.1", "s:2", val, sent_ns=100)
        self.oracle.record_ack("i1.w0.1", ack_ns=200)

        truncated_val = val[:-1]
        dump = {"s:2": truncated_val}
        res = self.oracle.check_invariants(dump, self.baseline)
        self.assertFalse(res.passed)
        self.assertEqual(res.key, "s:2")
        self.assertIn("corrupted", res.found_desc)

    def test_value_from_another_key(self):
        """Case 5: Returns another key's value."""
        val_for_s1 = generate_value("i1.w0.1", "s:1".encode().hex())
        val_for_s2 = generate_value("i1.w0.2", "s:2".encode().hex())

        self.oracle.record_sent_put("i1.w0.1", "s:1", val_for_s1, sent_ns=100)
        self.oracle.record_ack("i1.w0.1", ack_ns=200)

        self.oracle.record_sent_put("i1.w0.2", "s:2", val_for_s2, sent_ns=150)
        self.oracle.record_ack("i1.w0.2", ack_ns=250)

        # Cross-contamination: s:1 gets s:2's value
        dump = {"s:1": val_for_s2, "s:2": val_for_s2}
        res = self.oracle.check_invariants(dump, self.baseline)
        self.assertFalse(res.passed)
        self.assertEqual(res.key, "s:1")

    def test_batch_put_del_wrong_order(self):
        """Case 6: A put-then-delete in a batch resolves to the wrong order."""
        k = "b:batch1:0"
        v = generate_value("i1.w0.1", k.encode().hex())

        # Put followed by Del -> expected absent
        self.oracle.record_sent_batch(
            "i1.w0.1",
            [("PUT", k, v), ("DEL", k, None)],
            sent_ns=100,
        )
        self.oracle.record_ack("i1.w0.1", ack_ns=200)

        # If engine resolved in wrong order (del then put), k would be present
        dump = {k: v}
        res = self.oracle.check_invariants(dump, self.baseline)
        self.assertFalse(res.passed)
        self.assertIn("expected absent", res.rule)

    def test_batch_del_put_order_correct(self):
        """A del-then-put in a batch resolves to present."""
        k = "b:batch2:0"
        v = generate_value("i1.w0.2", k.encode().hex())

        # Del followed by Put -> expected present with v
        self.oracle.record_sent_batch(
            "i1.w0.2",
            [("DEL", k, None), ("PUT", k, v)],
            sent_ns=100,
        )
        self.oracle.record_ack("i1.w0.2", ack_ns=200)

        dump = {k: v}
        res = self.oracle.check_invariants(dump, self.baseline)
        self.assertTrue(res.passed)

    def test_alien_key_rejected(self):
        """Keys appearing out of nowhere are rejected."""
        dump = {"alien:key": b"ghost_value"}
        res = self.oracle.check_invariants(dump, self.baseline)
        self.assertFalse(res.passed)
        self.assertIn("alien key", res.rule)

    def test_failed_op_must_not_apply(self):
        """Operations that received ERR DISK_FULL / BAD_ARG must not be applied."""
        val = generate_value("i1.w0.1", "s:5".encode().hex())
        self.oracle.record_sent_put("i1.w0.1", "s:5", val, sent_ns=100)
        self.oracle.record_err("i1.w0.1", "DISK_FULL", err_ns=200)

        dump = {"s:5": val}
        res = self.oracle.check_invariants(dump, self.baseline)
        self.assertFalse(res.passed)
        self.assertEqual(res.key, "s:5")

    def test_embedded_nul_key_integrity(self):
        """Keys containing embedded NUL bytes must not be truncated or corrupted."""
        k = "s:nul:\x00:test"
        v = generate_value("i1.w0.1", k.encode().hex())
        self.oracle.record_sent_put("i1.w0.1", k, v, sent_ns=100)
        self.oracle.record_ack("i1.w0.1", ack_ns=200)

        # Engine truncated key at NUL: "s:nul:" instead of "s:nul:\x00:test"
        corrupted_dump = {"s:nul:": v}
        res = self.oracle.check_invariants(corrupted_dump, self.baseline)
        self.assertFalse(res.passed)

        # Clean roundtrip matches exactly
        clean_dump = {k: v}
        res_ok = self.oracle.check_invariants(clean_dump, self.baseline)
        self.assertTrue(res_ok.passed)


class TestBrokenEngineTortureIntegration(unittest.TestCase):
    """End-to-end test of the TortureRunner against fake broken engines.

    Per docs/TORTURE_SPEC.md Section 10:
    The tool must report FAIL (exit code 1) for each:
    1. ACKs before writing (lost write).
    2. Drops an ACKed write after restart.
    3. Applies only some operations of a batch.
    4. Returns a value truncated by one byte.
    5. Returns another key's value.
    6. A put-then-delete in a batch resolves to the wrong order.
    """

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workdir = Path(self.temp_dir.name)
        self.fake_engine = str(REPO_ROOT / "tests" / "fake_engine.py")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_with_bug(self, bug_name: str) -> int:
        import os
        from torture_tool.runner import TortureRunner
        import argparse

        old_env = os.environ.get("FAKE_ENGINE_BUG")
        os.environ["FAKE_ENGINE_BUG"] = bug_name
        try:
            args = argparse.Namespace(
                seed="meta-test-seed",
                iterations=1,
                mode="default",
                workdir=str(self.workdir / f"run-{bug_name}"),
                writers=2,
                inflight=8,
                kill_min_ms=100,
                kill_max_ms=200,
                kvdb=self.fake_engine,
                zip_failure=False,
            )
            runner = TortureRunner(args)
            return runner.run()
        finally:
            if old_env is None:
                os.environ.pop("FAKE_ENGINE_BUG", None)
            else:
                os.environ["FAKE_ENGINE_BUG"] = old_env

    def test_broken_engine_lost_write(self):
        code = self._run_with_bug("lost_write")
        self.assertEqual(code, 1, "Torture tool must report FAIL (1) on lost_write engine")

    def test_broken_engine_partial_batch(self):
        code = self._run_with_bug("partial_batch")
        self.assertEqual(code, 1, "Torture tool must report FAIL (1) on partial_batch engine")

    def test_broken_engine_truncate_val(self):
        code = self._run_with_bug("truncate_val")
        self.assertEqual(code, 1, "Torture tool must report FAIL (1) on truncate_val engine")

    def test_broken_engine_another_key_val(self):
        code = self._run_with_bug("another_key_val")
        self.assertEqual(code, 1, "Torture tool must report FAIL (1) on another_key_val engine")

    def test_broken_engine_wrong_batch_order(self):
        code = self._run_with_bug("wrong_batch_order")
        self.assertEqual(code, 1, "Torture tool must report FAIL (1) on wrong_batch_order engine")


if __name__ == "__main__":
    unittest.main()

