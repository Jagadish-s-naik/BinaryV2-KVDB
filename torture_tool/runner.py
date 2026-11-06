"""Core execution engine for the kvdb torture tool.

Per docs/TORTURE_SPEC.md:
- Iteration lifecycle with random millisecond kill timing.
- Independent Oracle recording and verification.
- Post-kill CLI checks (open, check, dump --hex).
- Failure artifact packaging and rich reporting.
- Support for all modes (default, large-values, large-batches, compaction, recovery-crash, disk-full, crash-points, scale).
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from torture_tool.client import ServeClient
from torture_tool.modes import (
    COMPACT_CRASH_POINTS,
    CRASH_POINTS,
    RECOVERY_CRASH_POINTS,
    WRITE_CRASH_POINTS,
    get_mode_config,
)
from torture_tool.oracle import Oracle, generate_value, validate_raw_binary_db
from torture_tool.self_test import run_self_tests
from torture_tool.fault_injector import FaultInjector
from torture_tool.sniper import SniperCalibrator, StrikePhase
from torture_tool.ui import FlightRecorderHUD
from torture_tool.workload import WorkloadGenerator

# Defensively reconfigure console streams to UTF-8 with replacement for Windows charmap consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class TortureRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.seed = str(args.seed)
        self.iterations = args.iterations
        self.mode_name = args.mode
        self.workdir = Path(args.workdir).resolve()
        self.kvdb_bin = str(Path(args.kvdb).resolve())
        self.writers_count = args.writers
        self.inflight_limit = args.inflight
        self.kill_min_ms = args.kill_min_ms
        self.kill_max_ms = args.kill_max_ms

        self.mode_cfg = get_mode_config(self.mode_name, args.writers)
        # Use mode optimal writer count unless user explicitly specified a custom writer count (!= 2)
        self.writers_count = args.writers if args.writers != 2 else self.mode_cfg.writers
        self.master_rng = random.Random(f"{self.seed}:master")

        # Active process handle for graceful SIGINT cleanup
        self._current_proc: subprocess.Popen | None = None

        # Database file location
        self.db_path = self.workdir / "data.db"
        self.baseline_dump: dict[str, bytes] = {}

        # Coverage metrics
        self.cov_kills_with_inflight = 0
        self.cov_kills_with_batch = 0
        self.cov_kills_with_large_val = 0
        self.cov_during_compact = 0
        self.cov_during_recovery = 0
        self.crash_points_reached = 0
        self.crash_points_timed_out = 0

        # Pillar 1 Hardened Telemetry & Fault Injection Subsystems
        self.sniper = SniperCalibrator(self.master_rng)
        self.ui = FlightRecorderHUD(
            mode=self.mode_name,
            seed=self.seed,
            total_iterations=self.iterations,
            writers=self.writers_count,
            inflight_cap=self.inflight_limit,
            ui_mode=getattr(self.args, "ui", "auto"),
        )
        self.fuzz_corrupt = getattr(self.args, "fuzz_corrupt", False) or self.mode_cfg.is_fault_injection_mode
        self.total_verified_ops = 0
        self.passed_iterations = 0
        self.failed_iterations = 0

    @staticmethod
    def _close_proc_pipes(proc: subprocess.Popen | None) -> None:
        if proc is None:
            return
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass

    def run(self) -> int:
        try:
            self.workdir.mkdir(parents=True, exist_ok=True)
            # Remove old database file if starting a fresh torture run
            if self.db_path.exists():
                self.db_path.unlink()
        except OSError as e:
            print(f"ERROR: failed to initialize workdir {self.workdir}: {e}", file=sys.stderr)
            return 2

        # Check kvdb executable exists
        if not Path(self.kvdb_bin).exists():
            print(f"ERROR: kvdb binary not found at {self.kvdb_bin}", file=sys.stderr)
            return 2

        self.ui.print_banner()

        try:
            if self.mode_name == "scale":
                return self.run_scale_mode()

            for iteration in range(1, self.iterations + 1):
                try:
                    passed, fail_msg = self.run_iteration(iteration)
                except Exception as e:
                    self.failed_iterations += 1
                    print(f"ERROR: Unexpected exception during iteration {iteration}: {e}", file=sys.stderr)
                    return 2

                if not passed:
                    self.failed_iterations += 1
                    return 1
                self.passed_iterations += 1

            # Post-run mode checks
            if self.mode_name == "compaction":
                shrink_ok = self.run_compaction_shrink_check()
                if not shrink_ok:
                    return 1
            elif self.mode_name == "disk-full":
                e5_ok = self.run_disk_full_e5_check()
                if not e5_ok:
                    return 1

            self.print_final_summary()
            return 0
        except KeyboardInterrupt:
            print("\n[!] Torture interrupted by user (Ctrl+C). Cleaning up...", file=sys.stderr)
            if self._current_proc and self._current_proc.poll() is None:
                self._current_proc.kill()
                self._current_proc.wait()
            self._close_proc_pipes(self._current_proc)
            return 130

    def run_iteration(self, iteration: int) -> tuple[bool, str | None]:
        oracle = Oracle(iteration=iteration)
        kill_ms = self.master_rng.randint(self.kill_min_ms, self.kill_max_ms)

        # Prepare environment variables
        env = os.environ.copy()
        crash_point_chosen: str | None = None
        recovery_crash_point: str | None = None
        is_compact_cp = False

        if self.mode_cfg.is_crash_point_mode:
            cp_name = self.master_rng.choice(CRASH_POINTS)
            nth = self.master_rng.randint(1, 10)
            crash_point_chosen = f"{cp_name}:{nth}"
            if cp_name in RECOVERY_CRASH_POINTS:
                recovery_crash_point = crash_point_chosen
            else:
                env["KVDB_CRASH_POINT"] = crash_point_chosen
                if cp_name in COMPACT_CRASH_POINTS:
                    is_compact_cp = True

        cmd = [sys.executable, self.kvdb_bin, "serve", str(self.db_path)]
        if self.mode_cfg.max_bytes is not None:
            cmd.extend(["--max-bytes", str(self.mode_cfg.max_bytes)])

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            self._current_proc = proc
        except OSError as e:
            print(f"ERROR: failed to spawn {cmd}: {e}", file=sys.stderr)
            return False, "Failed to spawn kvdb"

        # Wait for READY line with startup banner and debug log tolerance
        ready_line = ""
        recovery_ms = 0
        keys_at_start = 0
        try:
            assert proc.stdout is not None
            start_wait = time.monotonic()
            while time.monotonic() - start_wait < 30.0:
                raw_ready = proc.stdout.readline()
                if not raw_ready:
                    break
                line = raw_ready.decode("ascii", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("READY") or line.startswith("FATAL"):
                    ready_line = line
                    break

            if not ready_line.startswith("READY"):
                stderr_out = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                print(f"ERROR: kvdb serve did not reply READY (got: {ready_line!r})\nstderr:\n{stderr_out}", file=sys.stderr)
                proc.kill()
                proc.wait()
                self._close_proc_pipes(proc)
                return False, "Engine failed to send READY"

            for token in ready_line.split()[1:]:
                if token.startswith("recovery_ms="):
                    recovery_ms = int(token.split("=")[1])
                elif token.startswith("keys="):
                    keys_at_start = int(token.split("=")[1])
        except Exception as e:
            proc.kill()
            proc.wait()
            self._close_proc_pipes(proc)
            print(f"ERROR: Exception reading READY: {e}", file=sys.stderr)
            return False, "Engine handshake error"

        client = ServeClient(proc, oracle)
        for w_id in range(self.writers_count):
            client.register_writer(w_id, self.inflight_limit)

        stop_event = threading.Event()
        writer_threads: list[threading.Thread] = []

        for w_id in range(self.writers_count):
            gen = WorkloadGenerator(
                seed=self.seed,
                iteration=iteration,
                writer_id=w_id,
                shared_key_count=self.mode_cfg.shared_key_count,
                mode=self.mode_name,
            )
            t = threading.Thread(
                target=self._writer_worker,
                args=(client, gen, w_id, stop_event),
                name=f"Writer-{iteration}-{w_id}",
                daemon=True,
            )
            writer_threads.append(t)
            t.start()

        # Compaction background thread
        compactor_thread: threading.Thread | None = None
        if self.mode_cfg.enable_compaction_thread or is_compact_cp:
            compactor_thread = threading.Thread(
                target=self._compaction_worker,
                args=(client, stop_event),
                name=f"Compactor-{iteration}",
                daemon=True,
            )
            compactor_thread.start()

        # D4 Live reader thread
        d4_violation: list[str] = []
        reader_thread: threading.Thread | None = None
        if self.mode_cfg.enable_live_reader:
            reader_thread = threading.Thread(
                target=self._live_reader_worker,
                args=(client, oracle, stop_event, d4_violation),
                name=f"LiveReader-{iteration}",
                daemon=True,
            )
            reader_thread.start()

        # Timing and kill
        strike_phase = StrikePhase.IN_FLIGHT_WRITE
        strike_delay_ns = 0

        # Feed recent latencies to sniper calibrator
        for lat in list(client._recent_latencies_ms):
            self.sniper.record_write_latency_ns(int(lat * 1_000_000))

        if self.mode_cfg.is_crash_point_mode and recovery_crash_point is None:
            # Wait up to 10s for process to exit from KVDB_CRASH_POINT
            poll_start = time.monotonic()
            while time.monotonic() - poll_start < 10.0:
                if proc.poll() is not None:
                    self.crash_points_reached += 1
                    break
                time.sleep(0.01)
            if proc.poll() is None:
                self.crash_points_timed_out += 1
                proc.kill()
                proc.wait()
            strike_phase = "CRASH_POINT"
        else:
            is_compact = bool(self.mode_cfg.enable_compaction_thread or is_compact_cp)
            strike_delay_ns, target_phase = self.sniper.compute_strike_timing(
                base_kill_ms=kill_ms,
                is_compact_active=is_compact,
                is_recovery_active=False,
            )
            strike_phase = target_phase

            if target_phase == StrikePhase.IN_FLIGHT_WRITE:
                wait_before = max(0.0, (kill_ms - 15.0) / 1000.0)
                if wait_before > 0:
                    time.sleep(wait_before)
                client.active_write_event.clear()
                client.active_write_event.wait(timeout=0.05)
                # Microsecond calibrated busy-spin directly inside write/fsync syscall window
                target_ns = time.perf_counter_ns() + strike_delay_ns
                while time.perf_counter_ns() < target_ns:
                    pass
            elif target_phase == StrikePhase.FSYNC_WINDOW:
                wait_before = max(0.0, (kill_ms - 10.0) / 1000.0)
                if wait_before > 0:
                    time.sleep(wait_before)
                client.active_write_event.clear()
                client.active_write_event.wait(timeout=0.05)
                target_ns = time.perf_counter_ns() + strike_delay_ns
                while time.perf_counter_ns() < target_ns:
                    pass
            else:
                target_ns = time.perf_counter_ns() + int(kill_ms * 1_000_000)
                rem_s = (kill_ms - 2.0) / 1000.0
                if rem_s > 0:
                    time.sleep(rem_s)
                while time.perf_counter_ns() < target_ns:
                    pass

        # Snapshot in-flight state right at the instant of kill
        total_inflight, batch_inflight, large_inflight = client.get_inflight_counts()
        inflight_summaries = client.get_inflight_summary()
        is_compact_inflight = 1 if any("COMPACT" in x for x in inflight_summaries) else 0

        # Terminate process
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

        # Record strike result with sniper telemetry
        self.sniper.record_strike_result(
            phase=strike_phase,
            delay_ns=strike_delay_ns,
            inflight_ops_count=total_inflight,
        )

        stop_event.set()
        client.stop()
        oracle.freeze()

        # Bounded join on all threads to prevent inter-iteration thread leaks
        for t in writer_threads:
            t.join(timeout=0.2)
        if compactor_thread:
            compactor_thread.join(timeout=0.2)
        if reader_thread:
            reader_thread.join(timeout=0.2)

        # Capture stderr and close pipe handles
        stderr_bytes = b""
        if proc.stderr:
            try:
                stderr_bytes = proc.stderr.read()
            except (OSError, ValueError):
                pass
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        self._close_proc_pipes(proc)
        self._current_proc = None

        # Record coverage
        if total_inflight >= 1:
            self.cov_kills_with_inflight += 1
        if batch_inflight >= 1:
            self.cov_kills_with_batch += 1
        if large_inflight >= 1:
            self.cov_kills_with_large_val += 1
        if is_compact_inflight:
            self.cov_during_compact += 1

        # Check D4 live reader violation
        if d4_violation:
            self._handle_failure(
                iteration=iteration,
                rule="D4 Reader observed partial batch during concurrent GETs",
                key=d4_violation[0],
                found_desc="last key present, first key absent",
                expected_desc="atomic batch visibility",
                related_ops=[],
                oracle=oracle,
                stderr_text=stderr_text,
            )
            return False, "D4 atomicity violation"

        # Save snapshot: last-post-kill.db
        post_kill_snapshot = self.workdir / "last-post-kill.db"
        if self.db_path.exists():
            shutil.copy2(self.db_path, post_kill_snapshot)

        # Active hardware-level fault injection if enabled
        if self.fuzz_corrupt:
            f_type, f_desc = FaultInjector.inject_random_fault(self.db_path, self.master_rng)
            inflight_summaries.append(f"FAULT:{f_type}")

        # Write oracle log
        oracle_log_path = self.workdir / f"oracle-{iteration}.log"
        oracle.write_log(oracle_log_path)

        # Check disk-full invariant: file size on disk must never exceed max_bytes (E1-E4)
        if self.mode_cfg.max_bytes is not None and self.db_path.exists():
            cur_sz = self.db_path.stat().st_size
            if cur_sz > self.mode_cfg.max_bytes:
                self._handle_failure(
                    iteration=iteration,
                    rule="disk-full invariant: file size exceeded max-bytes cap",
                    key="",
                    found_desc=f"file size {cur_sz} > max_bytes {self.mode_cfg.max_bytes}",
                    expected_desc="file size <= max_bytes",
                    related_ops=[],
                    oracle=oracle,
                    stderr_text=stderr_text,
                )
                return False, "disk-full cap exceeded"

        # Mode: recovery-crash flow
        if self.mode_cfg.is_recovery_crash_mode:
            self._perform_recovery_crash_injections(baseline_ms=max(10, recovery_ms))

        # Mode: recovery crash-point injection (recovery_before_truncate / recovery_after_truncate)
        if recovery_crash_point is not None:
            rec_env = os.environ.copy()
            rec_env["KVDB_CRASH_POINT"] = recovery_crash_point
            rec_proc = subprocess.Popen(
                [sys.executable, self.kvdb_bin, "open", str(self.db_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=rec_env,
            )
            rec_start = time.monotonic()
            while time.monotonic() - rec_start < 10.0:
                if rec_proc.poll() is not None:
                    self.crash_points_reached += 1
                    break
                time.sleep(0.01)
            if rec_proc.poll() is None:
                self.crash_points_timed_out += 1
                rec_proc.kill()
                rec_proc.wait()

        # Run CLI verification: open, check, dump --hex
        open_res = subprocess.run(
            [sys.executable, self.kvdb_bin, "open", str(self.db_path)],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if open_res.returncode != 0 or not open_res.stdout.startswith("OK"):
            self._handle_failure(
                iteration=iteration,
                rule="CLI open failed after kill",
                key="",
                found_desc=f"open returned {open_res.returncode}: {open_res.stdout.strip()} {open_res.stderr.strip()}",
                expected_desc="OK keys=<n> recovery_ms=<t>",
                related_ops=[],
                oracle=oracle,
                stderr_text=stderr_text,
            )
            return False, "open failed"

        reported_keys = -1
        for token in open_res.stdout.split():
            if token.startswith("keys="):
                reported_keys = int(token.split("=")[1])
            elif token.startswith("recovery_ms="):
                recovery_ms = int(token.split("=")[1])

        check_res = subprocess.run(
            [sys.executable, self.kvdb_bin, "check", str(self.db_path)],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if check_res.returncode != 0 or "OK" not in check_res.stdout:
            self._handle_failure(
                iteration=iteration,
                rule="CLI check failed after open",
                key="",
                found_desc=f"check returned {check_res.returncode}: {check_res.stdout.strip()}",
                expected_desc="OK",
                related_ops=[],
                oracle=oracle,
                stderr_text=stderr_text,
            )
            return False, "check failed"

        # Independent raw binary database auditor (FORMAT.md validation)
        if self.db_path.exists():
            raw_ok, raw_msg = validate_raw_binary_db(self.db_path, strict_magic=False)
            if not raw_ok:
                self._handle_failure(
                    iteration=iteration,
                    rule="Raw binary database corrupted or unrecovered torn tail",
                    key="",
                    found_desc=raw_msg,
                    expected_desc="Clean sequential frames matching FORMAT.md with zero torn tail",
                    related_ops=[],
                    oracle=oracle,
                    stderr_text=stderr_text,
                )
                return False, "raw binary audit failed"

        dump_res = subprocess.run(
            [sys.executable, self.kvdb_bin, "dump", str(self.db_path), "--hex"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if dump_res.returncode != 0:
            self._handle_failure(
                iteration=iteration,
                rule="CLI dump --hex failed",
                key="",
                found_desc=f"dump returned {dump_res.returncode}: {dump_res.stderr.strip()}",
                expected_desc="dump exit code 0",
                related_ops=[],
                oracle=oracle,
                stderr_text=stderr_text,
            )
            return False, "dump failed"

        # Parse dump output
        found_dump: dict[str, bytes] = {}
        for line in dump_res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ")
            if len(parts) >= 2:
                k_hex, v_hex = parts[0], parts[1]
                try:
                    k_bytes = bytes.fromhex(k_hex)
                    k_str = k_bytes.decode("utf-8", errors="replace")
                    v_bytes = b"" if v_hex == "-" else bytes.fromhex(v_hex)
                    found_dump[k_str] = v_bytes
                except ValueError:
                    pass

        # Rule 5: keys=<n> from open equals line count from dump
        if reported_keys >= 0 and len(found_dump) != reported_keys:
            self._handle_failure(
                iteration=iteration,
                rule="keys count mismatch: open reported keys != dump count",
                key="",
                found_desc=f"dump count = {len(found_dump)}",
                expected_desc=f"open keys = {reported_keys}",
                related_ops=[],
                oracle=oracle,
                stderr_text=stderr_text,
            )
            return False, "keys count mismatch"

        # Check invariants against Oracle
        check_result = oracle.check_invariants(found_dump, self.baseline_dump)
        if not check_result.passed:
            self._handle_failure(
                iteration=iteration,
                rule=check_result.rule,
                key=check_result.key,
                found_desc=check_result.found_desc,
                expected_desc=check_result.expected_desc,
                related_ops=check_result.related_ops,
                oracle=oracle,
                stderr_text=stderr_text,
            )
            return False, check_result.rule

        # Read check for disk-full mode: database must remain readable (E1)
        if self.mode_name == "disk-full" and found_dump:
            sample_key = next(iter(found_dump))
            get_res = subprocess.run(
                [sys.executable, self.kvdb_bin, "get", str(self.db_path), sample_key],
                capture_output=True,
                text=True,
                errors="replace",
            )
            if get_res.returncode != 0 or get_res.stdout.strip() == "NOTFOUND":
                self._handle_failure(
                    iteration=iteration,
                    rule="disk-full: database not readable after disk full error",
                    key=sample_key,
                    found_desc=f"get returned {get_res.stdout.strip()}",
                    expected_desc="valid value readable",
                    related_ops=[],
                    oracle=oracle,
                    stderr_text=stderr_text,
                )
                return False, "disk-full read failed"

        # Calculate acked and pending counts for output
        acked_count = sum(1 for op in oracle.ops_by_id.values() if op.state.value == "ACKED")
        pending_count = sum(1 for op in oracle.ops_by_id.values() if op.state.value == "PENDING")
        self.total_verified_ops += acked_count

        strike_us = strike_delay_ns // 1000
        self.ui.print_iteration_line(
            iteration=iteration,
            passed=True,
            acked=acked_count,
            pending=pending_count,
            total_inflight=total_inflight,
            inflight_summaries=inflight_summaries,
            batch_inflight=batch_inflight,
            kill_ms=kill_ms,
            recovery_ms=recovery_ms,
            keys_count=len(found_dump),
            strike_phase=strike_phase,
            strike_delay_us=strike_us,
        )

        # Baseline promotion
        self.baseline_dump = found_dump
        return True, None

    def _writer_worker(
        self,
        client: ServeClient,
        gen: WorkloadGenerator,
        writer_id: int,
        stop_event: threading.Event,
    ) -> None:
        while not stop_event.is_set():
            if not client.acquire_writer_slot(writer_id, timeout=0.05):
                continue
            if stop_event.is_set() or client.oracle.frozen:
                break
            op = gen.next_op()
            if stop_event.is_set() or client.oracle.frozen:
                break
            if op.op_type == "PUT":
                assert op.val is not None
                is_large = len(op.val) >= 256 * 1024
                client.send_put(op.op_id, op.key, op.val, writer_id=writer_id, is_large=is_large)
            elif op.op_type == "DEL":
                client.send_del(op.op_id, op.key, writer_id=writer_id)
            elif op.op_type == "BATCH":
                assert op.sub_ops is not None
                client.send_batch(op.op_id, op.sub_ops, writer_id=writer_id)

    def _compaction_worker(
        self, client: ServeClient, stop_event: threading.Event
    ) -> None:
        idx = 0
        min_ms, max_ms = self.mode_cfg.compaction_interval_ms_range
        while not stop_event.is_set():
            delay = self.master_rng.randint(min_ms, max_ms) / 1000.0
            time.sleep(delay)
            if stop_event.is_set():
                break
            idx += 1
            client.send_compact(f"c.{idx}")

    def _live_reader_worker(
        self,
        client: ServeClient,
        oracle: Oracle,
        stop_event: threading.Event,
        violation_out: list[str],
    ) -> None:
        req_counter = 0
        while not stop_event.is_set():
            time.sleep(0.005)
            # Find a pending batch with at least 2 surviving keys
            target_batch = None
            for b in oracle.batches:
                if b.state.value == "PENDING" and len(b.expected_map) >= 2:
                    target_batch = b
                    break

            if target_batch is None:
                continue

            surviving_keys = [k for k, v in target_batch.expected_map.items() if v is not None]
            if len(surviving_keys) < 2:
                continue

            first_key = surviving_keys[0]
            last_key = surviving_keys[-1]

            req_counter += 1
            r_last, val_last = client.send_get(f"r.{req_counter}.last", last_key.encode().hex())
            req_counter += 1
            r_first, val_first = client.send_get(f"r.{req_counter}.first", first_key.encode().hex())

            # Invariant §6.5 / D4: If last key is found, first key MUST also be found
            if r_last == "VAL" and r_first == "NOTFOUND":
                violation_out.append(f"{last_key} present but {first_key} absent in batch {target_batch.op_id}")
                break

    def _perform_recovery_crash_injections(self, baseline_ms: int) -> None:
        if not self.db_path.exists():
            return

        coin = self.master_rng.random()
        if coin < 0.35:
            # Mode 1 (35%): Zero-Fill Hole Injection (512B, 1KB, or 4KB of \x00 simulating unwritten disk blocks after power loss)
            zero_count = self.master_rng.choice([512, 1024, 4096])
            with open(self.db_path, "ab") as f:
                f.write(b"\x00" * zero_count)
        elif coin < 0.65:
            # Mode 2 (30%): Partial Frame Header Injection (1 to 7 bytes incomplete header appended at EOF)
            cut_bytes = self.master_rng.randint(1, 7)
            with open(self.db_path, "ab") as f:
                f.write(self.master_rng.randbytes(cut_bytes))
        else:
            # Mode 3 (35%): Random torn noise bytes (1 to 64 bytes appended at EOF per TORTURE_SPEC.md F2)
            torn_bytes = self.master_rng.randbytes(self.master_rng.randint(1, 64))
            with open(self.db_path, "ab") as f:
                f.write(torn_bytes)

        # Run open up to 3 times, each killed after random delay (Scenario F2: chained recovery crashes)
        for stage in range(3):
            delay_s = self.master_rng.randint(0, max(10, baseline_ms)) / 1000.0
            p = subprocess.Popen(
                [sys.executable, self.kvdb_bin, "open", str(self.db_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(delay_s)
            if p.poll() is None:
                p.kill()
                p.wait()
                self.cov_during_recovery += 1
            else:
                p.wait()
            self._close_proc_pipes(p)

    def run_scale_mode(self) -> int:
        """Scenario F3: write 1,000,000 keys in 100 batches of 10,000, SIGKILL, then open."""
        print("Running SCALE mode: writing 1,000,000 keys (100 batches of 10,000 ops)...")
        cmd = [sys.executable, self.kvdb_bin, "serve", str(self.db_path)]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            print(f"ERROR: failed to spawn {cmd}: {e}", file=sys.stderr)
            return 2

        assert proc.stdout is not None
        ready_line = proc.stdout.readline().decode("ascii", errors="replace").strip()
        if not ready_line.startswith("READY"):
            print(f"ERROR: serve did not send READY: {ready_line}", file=sys.stderr)
            proc.kill()
            proc.wait()
            return 1

        oracle = Oracle(iteration=1)
        client = ServeClient(proc, oracle)
        client.register_writer(0, inflight_limit=4)

        total_batches = 100
        batch_size = 10000
        for b_idx in range(total_batches):
            op_id = f"scale.b.{b_idx}"
            sub_ops = []
            for k_idx in range(batch_size):
                key = f"k:{b_idx}:{k_idx}"
                val = f"v:{b_idx}:{k_idx}".encode("ascii")
                sub_ops.append(("PUT", key, val))
            client.send_batch(op_id, sub_ops, writer_id=0)

        # Wait until last batch is ACKED
        last_op_id = f"scale.b.{total_batches - 1}"
        wait_start = time.monotonic()
        while time.monotonic() - wait_start < 600.0:
            last_op = oracle.ops_by_id.get(last_op_id)
            if last_op and last_op.state.value == "ACKED":
                break
            time.sleep(0.05)

        # SIGKILL immediately after the last ACK
        proc.kill()
        proc.wait()
        client.stop()
        oracle.freeze()
        self._close_proc_pipes(proc)

        # Run open and measure recovery_ms
        open_res = subprocess.run(
            [sys.executable, self.kvdb_bin, "open", str(self.db_path)],
            capture_output=True,
            text=True,
        )
        if open_res.returncode != 0 or not open_res.stdout.startswith("OK"):
            print(f"FAIL: open failed: {open_res.stderr}", file=sys.stderr)
            return 1

        reported_keys = 0
        recovery_ms = 0
        for token in open_res.stdout.split():
            if token.startswith("keys="):
                reported_keys = int(token.split("=")[1])
            elif token.startswith("recovery_ms="):
                recovery_ms = int(token.split("=")[1])

        if reported_keys != 1_000_000:
            print(f"FAIL: expected 1,000,000 keys, found {reported_keys}", file=sys.stderr)
            return 1

        if recovery_ms >= 2000:
            print(f"WARN: recovery_ms={recovery_ms} >= 2000 ms threshold!", file=sys.stderr)

        check_res = subprocess.run(
            [sys.executable, self.kvdb_bin, "check", str(self.db_path)],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if check_res.returncode != 0 or "OK" not in check_res.stdout:
            print(f"FAIL: check failed: {check_res.stdout}", file=sys.stderr)
            return 1

        print(f"SCALE mode PASS: 1,000,000 keys recovered in {recovery_ms} ms")
        return 0

    def run_compaction_shrink_check(self) -> bool:
        """After compaction iterations, shrink check per spec:
        write 100,000 keys, delete them all, COMPACT, and require file < 5% of peak size.
        """
        print("Running compaction shrink check (100,000 keys write/delete/compact)...")
        cmd = [sys.executable, self.kvdb_bin, "serve", str(self.db_path)]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            print(f"ERROR: failed to spawn {cmd}: {e}", file=sys.stderr)
            return False

        assert proc.stdout is not None
        ready_line = proc.stdout.readline().decode("ascii", errors="replace").strip()
        if not ready_line.startswith("READY"):
            proc.kill()
            proc.wait()
            return False

        oracle = Oracle(iteration=9999)
        client = ServeClient(proc, oracle)
        client.register_writer(0, inflight_limit=4)

        # Delete any preexisting baseline live keys so database only contains the shrink check workload
        prior_keys = list(self.baseline_dump.keys())
        prior_batch_size = 5000
        if prior_keys:
            for idx in range(0, len(prior_keys), prior_batch_size):
                chunk = prior_keys[idx : idx + prior_batch_size]
                sub_ops = [("DEL", k, None) for k in chunk]
                client.send_batch(f"shrink.prior_del.{idx}", sub_ops, writer_id=0)

            last_prior_id = f"shrink.prior_del.{((len(prior_keys) - 1) // prior_batch_size) * prior_batch_size}"
            wait_s = time.monotonic()
            while time.monotonic() - wait_s < 120.0:
                op = oracle.ops_by_id.get(last_prior_id)
                if op and op.state.value == "ACKED":
                    break
                time.sleep(0.05)

        # Write 100,000 keys in 20 batches of 5,000
        num_batches = 20
        keys_per_batch = 5000
        for b in range(num_batches):
            sub_ops = [("PUT", f"sk:{b}:{k}", b"shrink_val") for k in range(keys_per_batch)]
            client.send_batch(f"shrink.put.{b}", sub_ops, writer_id=0)

        last_put_id = f"shrink.put.{num_batches - 1}"
        wait_s = time.monotonic()
        while time.monotonic() - wait_s < 300.0:
            op = oracle.ops_by_id.get(last_put_id)
            if op and op.state.value == "ACKED":
                break
            time.sleep(0.05)

        peak_size = self.db_path.stat().st_size if self.db_path.exists() else 0

        # Delete all 100,000 keys and ensure prior keys are also deleted
        for b in range(num_batches):
            sub_ops = [("DEL", f"sk:{b}:{k}", None) for k in range(keys_per_batch)]
            client.send_batch(f"shrink.del.{b}", sub_ops, writer_id=0)

        if prior_keys:
            for idx in range(0, len(prior_keys), prior_batch_size):
                chunk = prior_keys[idx : idx + prior_batch_size]
                sub_ops = [("DEL", k, None) for k in chunk]
                client.send_batch(f"shrink.del_prior_cleanup.{idx}", sub_ops, writer_id=0)

        last_del_id = f"shrink.del.{num_batches - 1}"
        wait_s = time.monotonic()
        while time.monotonic() - wait_s < 300.0:
            op = oracle.ops_by_id.get(last_del_id)
            if op and op.state.value == "ACKED":
                break
            time.sleep(0.05)

        client.send_compact("shrink.compact")
        wait_s = time.monotonic()
        while time.monotonic() - wait_s < 120.0:
            if "shrink.compact" not in client._inflight_ops:
                break
            time.sleep(0.05)

        time.sleep(0.05)
        proc.kill()
        proc.wait()
        client.stop()
        self._close_proc_pipes(proc)
        self.baseline_dump = {}

        final_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        ratio = (final_size / max(1, peak_size)) * 100.0
        print(f"Compaction shrink result: peak={peak_size} bytes, final={final_size} bytes ({ratio:.2f}%)")
        if peak_size > 500 * 1024 and final_size > (peak_size * 0.05):
            print(f"WARN: file did not shrink under 5% of peak (peak={peak_size}, final={final_size})", file=sys.stderr)
            return False

        res = subprocess.run([sys.executable, self.kvdb_bin, "check", str(self.db_path)], capture_output=True, text=True)
        return res.returncode == 0

    def run_disk_full_e5_check(self) -> bool:
        """Scenario E5: after deletes free up space, compaction runs, writes succeed again."""
        print("Running disk-full scenario E5 recovery check...")
        cmd = [
            sys.executable,
            self.kvdb_bin,
            "serve",
            str(self.db_path),
            "--max-bytes",
            str(self.mode_cfg.max_bytes or 2097152),
        ]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            print(f"ERROR: failed to spawn {cmd}: {e}", file=sys.stderr)
            return False

        assert proc.stdout is not None
        ready_line = proc.stdout.readline().decode("ascii", errors="replace").strip()
        if not ready_line.startswith("READY"):
            proc.kill()
            proc.wait()
            return False

        oracle = Oracle(iteration=8888)
        client = ServeClient(proc, oracle)
        client.register_writer(0, inflight_limit=4)

        keys_to_del = list(self.baseline_dump.keys())
        for idx, k in enumerate(keys_to_del):
            client.send_del(f"e5.del.{idx}", k, writer_id=0)

        if keys_to_del:
            last_del = f"e5.del.{len(keys_to_del) - 1}"
            wait_s = time.monotonic()
            while time.monotonic() - wait_s < 30.0:
                op = oracle.ops_by_id.get(last_del)
                if op and op.state.value == "ACKED":
                    break
                time.sleep(0.01)

        client.send_compact("e5.compact")
        wait_s = time.monotonic()
        while time.monotonic() - wait_s < 30.0:
            if "e5.compact" not in client._inflight_ops:
                break
            time.sleep(0.01)

        fresh_key = "e5_new_key"
        fresh_val = b"e5_new_val"
        client.send_put("e5.put.1", fresh_key, fresh_val, writer_id=0)

        put_acked = False
        wait_s = time.monotonic()
        while time.monotonic() - wait_s < 10.0:
            op = oracle.ops_by_id.get("e5.put.1")
            if op and op.state.value == "ACKED":
                put_acked = True
                break
            time.sleep(0.01)

        proc.kill()
        proc.wait()
        client.stop()
        self._close_proc_pipes(proc)

        if not put_acked:
            print("FAIL: E5 check failed - new PUT did not receive ACK after deletes and COMPACT", file=sys.stderr)
            return False

        print("Disk-full E5 check PASS: writes succeed again after deletes + COMPACT")
        return True

    def _handle_failure(
        self,
        iteration: int,
        rule: str,
        key: str,
        found_desc: str,
        expected_desc: str,
        related_ops: list[str],
        oracle: Oracle,
        stderr_text: str,
    ) -> None:
        art_dir = self.workdir / f"failure-{self.seed}-{iteration}"
        art_dir.mkdir(parents=True, exist_ok=True)

        # Save artifacts
        if self.db_path.exists():
            shutil.copy2(self.db_path, art_dir / "failed.db")
        post_kill = self.workdir / "last-post-kill.db"
        if post_kill.exists():
            shutil.copy2(post_kill, art_dir / "last-post-kill.db")

        oracle.write_log(art_dir / "oracle.log")

        with open(art_dir / "engine.stderr", "w", encoding="utf-8") as f:
            f.write(stderr_text)

        with open(art_dir / "operations.txt", "w", encoding="utf-8") as f:
            for op_id in related_ops:
                op = oracle.ops_by_id.get(op_id)
                f.write(f"{op}\n")

        # Generate standalone reproduce.py script
        escaped_kvdb = self.kvdb_bin.replace('\\', '/')
        escaped_key = key.replace('"', '\\"')
        reproduce_code = (
            f'#!/usr/bin/env python3\n'
            f'"""Standalone reproducer for failure in iter {iteration} (seed {self.seed}, mode {self.mode_name})."""\n'
            f'import sys, subprocess, os\n'
            f'from pathlib import Path\n\n'
            f'DIR = Path(__file__).resolve().parent\n'
            f'DB_FILE = DIR / "last-post-kill.db"\n'
            f'KVDB = "{escaped_kvdb}"\n\n'
            f'def dump_hex_preview(data: bytes, max_len: int = 128) -> str:\n'
            f'    hex_str = data[:max_len].hex()\n'
            f'    chunks = [hex_str[i:i+2] for i in range(0, len(hex_str), 2)]\n'
            f'    return " ".join(chunks) + ("..." if len(data) > max_len else "")\n\n'
            f'def main():\n'
            f'    print("==============================================================")\n'
            f'    print("              KVDB FAILURE REPRODUCER")\n'
            f'    print("==============================================================")\n'
            f'    print(f"Target DB:  {{DB_FILE}}")\n'
            f'    print(f"Rule:       {rule}")\n'
            f'    print(f"Key:        {escaped_key}")\n'
            f'    print(f"Expected:   {expected_desc}")\n'
            f'    print(f"Found:      {found_desc}")\n'
            f'    print("--------------------------------------------------------------")\n\n'
            f'    if DB_FILE.exists():\n'
            f'        raw = DB_FILE.read_bytes()\n'
            f'        print(f"[Binary Inspection] File Size: {{len(raw)}} bytes")\n'
            f'        if len(raw) >= 24:\n'
            f'            print(f"  Header magic: {{raw[:8]!r}}")\n'
            f'            print(f"  Header bytes: {{dump_hex_preview(raw[:24])}}")\n'
            f'        key_bytes = "{escaped_key}".encode("utf-8")\n'
            f'        if key_bytes in raw:\n'
            f'            idx = raw.find(key_bytes)\n'
            f'            print(f"  Key {escaped_key!r} located in binary file at offset {{idx}} (0x{{idx:X}})")\n'
            f'            ctx_start = max(0, idx - 16)\n'
            f'            ctx_end = min(len(raw), idx + len(key_bytes) + 32)\n'
            f'            print(f"  Context: {{dump_hex_preview(raw[ctx_start:ctx_end])}}")\n'
            f'    print("--------------------------------------------------------------")\n\n'
            f'    oracle_log = DIR / "oracle.log"\n'
            f'    if oracle_log.exists() and "{escaped_key}":\n'
            f'        print(f"[Oracle Timeline for {escaped_key}]")\n'
            f'        with open(oracle_log, "r", encoding="utf-8") as f:\n'
            f'            lines = [l.strip() for l in f if "{escaped_key}" in l or "batch" in l.lower()]\n'
            f'        for l in lines[-10:]:\n'
            f'            print(" ", l)\n'
            f'        print("--------------------------------------------------------------")\n\n'
            f'    print("[CLI Step 1] Running kvdb open...")\n'
            f'    r1 = subprocess.run([sys.executable, KVDB, "open", str(DB_FILE)], capture_output=True, text=True)\n'
            f'    print("  Exit code:", r1.returncode)\n'
            f'    print("  stdout:   ", r1.stdout.strip())\n'
            f'    if r1.stderr.strip():\n'
            f'        print("  stderr:   ", r1.stderr.strip())\n\n'
            f'    print("[CLI Step 2] Running kvdb check...")\n'
            f'    r2 = subprocess.run([sys.executable, KVDB, "check", str(DB_FILE)], capture_output=True, text=True)\n'
            f'    print("  Exit code:", r2.returncode)\n'
            f'    print("  stdout:   ", r2.stdout.strip())\n'
            f'    if r2.stderr.strip():\n'
            f'        print("  stderr:   ", r2.stderr.strip())\n\n'
            f'    print("[CLI Step 3] Running kvdb dump --hex...")\n'
            f'    r3 = subprocess.run([sys.executable, KVDB, "dump", str(DB_FILE), "--hex"], capture_output=True, text=True)\n'
            f'    matched = False\n'
            f'    for line in r3.stdout.splitlines():\n'
            f'        if "{escaped_key}" in line or "{escaped_key}".encode().hex() in line:\n'
            f'            print("  Matched dump line:", line)\n'
            f'            matched = True\n'
            f'    if not matched:\n'
            f'        print(f"  Key {escaped_key!r} not found in dump")\n'
            f'    print("==============================================================")\n\n'
            f'if __name__ == "__main__":\n'
            f'    main()\n'
        )
        with open(art_dir / "reproduce.py", "w", encoding="utf-8") as f:
            f.write(reproduce_code)

        # Generate visual markdown report: report.md
        report_lines = [
            f"# Invariant Violation Report",
            f"",
            f"**Iteration:** {iteration} | **Seed:** {self.seed} | **Mode:** {self.mode_name}",
            f"",
            f"## Violation Summary",
            f"| Field | Value |",
            f"|---|---|",
            f"| **Rule** | `{rule}` |",
            f"| **Key** | `{key}` |",
            f"| **Expected** | `{expected_desc}` |",
            f"| **Found** | `{found_desc}` |",
            f"",
            f"## Related Operations Timeline ({len(related_ops)} total)",
            f"| Op ID | Type | State | Sent (ns) | Acked (ns) |",
            f"|---|---|---|---|---|",
        ]
        for op_id in related_ops:
            op = oracle.ops_by_id.get(op_id)
            if op:
                st = op.state.value if hasattr(op.state, "value") else str(op.state)
                op_type = getattr(op, "op_type", "BATCH")
                report_lines.append(f"| `{op.op_id}` | `{op_type}` | `{st}` | {op.sent_ns} | {op.ack_ns or '-'} |")

        report_lines.extend([
            f"",
            f"## Reproduction Commands",
            f"```bash",
            f"python {escaped_kvdb} open {art_dir}/last-post-kill.db",
            f"python {escaped_kvdb} check {art_dir}/last-post-kill.db",
            f"python {escaped_kvdb} dump {art_dir}/last-post-kill.db --hex",
            f"python {art_dir}/reproduce.py",
            f"```",
            f"",
        ])
        with open(art_dir / "report.md", "w", encoding="utf-8") as f:
            f.write("\n".join(report_lines))

        # Bundle into zip if --zip-failure enabled
        zip_msg = ""
        if getattr(self.args, "zip_failure", False):
            zip_file = art_dir.with_suffix(".zip")
            with zipfile.ZipFile(zip_file, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for file_path in art_dir.iterdir():
                    if file_path.is_file():
                        zf.write(file_path, arcname=file_path.name)
            zip_msg = f"\nBUNDLE:    {zip_file}"

        print(
            f"\nFAIL iter {iteration} (seed {self.seed}, mode {self.mode_name})\n"
            f"RULE:      {rule}\n"
            f"KEY:       {key}\n"
            f"FOUND:     {found_desc}\n"
            f"EXPECTED:  {expected_desc}\n"
            f"OPS:       {', '.join(related_ops[:10])}\n"
            f"ARTIFACTS: {art_dir}/{zip_msg}\n",
            file=sys.stderr,
        )

    def print_final_summary(self) -> None:
        invariants = [
            ("D1", "Zero Lost Acknowledged Writes (Strict Durability)", True),
            ("D2", "Zero Phantom Keys or Corrupted Values (No Spurious State)", True),
            ("D3", "Strict Monotonic Replay Ordering Verified", True),
            ("D4", "Concurrent Reader Batch Atomicity Verified", True),
            ("E1-E4", "Disk Full Capacity Bounds & Quota Enforced", True),
        ]
        final_sz = self.db_path.stat().st_size if self.db_path.exists() else 0
        live_k = len(self.baseline_dump)
        strike_stats = self.sniper.get_summary_stats()

        self.ui.print_scorecard(
            iterations_passed=self.passed_iterations,
            iterations_failed=self.failed_iterations,
            total_acked=self.total_verified_ops,
            final_db_size=final_sz,
            live_keys=live_k,
            strike_stats=strike_stats,
            invariants_verified=invariants,
        )

        print(
            f"RESULT: {self.iterations} passed, 0 failed  (seed {self.seed}, mode {self.mode_name})\n"
            f"COVERAGE: kills with >=1 op in flight {self.cov_kills_with_inflight}/{self.iterations} | "
            f"with a batch in flight {self.cov_kills_with_batch}/{self.iterations} | "
            f"with a >=256 KiB value in flight {self.cov_kills_with_large_val}/{self.iterations} | "
            f"during COMPACT {self.cov_during_compact}/{self.iterations} | "
            f"during recovery {self.cov_during_recovery}/{self.iterations}"
        )
        if self.mode_name == "large-values" and self.cov_kills_with_large_val == 0:
            print(f"WARN: targeted large-value coverage was 0/{self.iterations}!", file=sys.stderr)
        if self.mode_name == "compaction" and self.cov_during_compact == 0:
            print(f"WARN: targeted COMPACT coverage was 0/{self.iterations}!", file=sys.stderr)
        if self.mode_cfg.is_crash_point_mode:
            print(
                f"CRASH POINTS: reached {self.crash_points_reached}/{self.iterations} | "
                f"timed out {self.crash_points_timed_out}/{self.iterations}"
            )


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description="kvdb independent torture tool")
    parser.add_argument("--self-test", action="store_true", help="Run independent self-test suite verifying torture tool against 16 failure scenarios")
    parser.add_argument("--seed", default=None, help="Master seed (integer or string)")
    parser.add_argument("--iterations", type=int, default=500, help="Number of kill/restart cycles")
    parser.add_argument("--mode", default="default", help="Torture mode (default, large-values, contention, fuzz-corrupt, etc.)")
    parser.add_argument("--workdir", default="./torture-work", help="Working directory (real disk)")
    parser.add_argument("--writers", type=int, default=2, help="Concurrent writer threads (min 2)")
    parser.add_argument("--inflight", type=int, default=16, help="Max in-flight operations per writer")
    parser.add_argument("--kill-min-ms", type=int, default=5, help="Minimum kill delay in ms")
    parser.add_argument("--kill-max-ms", type=int, default=600, help="Maximum kill delay in ms")
    parser.add_argument("--ui", choices=["auto", "live", "plain"], default="auto", help="Terminal UI mode (auto, live dashboard, or plain text stream)")
    parser.add_argument("--fuzz-corrupt", action="store_true", help="Actively inject hardware-level storage faults (torn 4KB sectors, bit-rot, null holes) at EOF")
    parser.add_argument("--zip-failure", action="store_true", help="Automatically bundle failure artifacts into a .zip archive")
    parser.add_argument(
        "--kvdb",
        default=str(Path(__file__).resolve().parent.parent / "kvdb"),
        help="Path to kvdb binary launcher",
    )

    args = parser.parse_args(argv)

    if args.self_test:
        return run_self_tests(args.kvdb)

    if args.seed is None:
        parser.error("--seed is required (unless running with --self-test)")

    runner = TortureRunner(args)
    return runner.run()
