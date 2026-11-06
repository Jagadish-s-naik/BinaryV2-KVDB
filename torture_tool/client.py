"""Process communication and protocol client for kvdb serve.

Per docs/PROTOCOL.md and docs/TORTURE_SPEC.md:
- Binary pipes for stdin and stdout.
- Contiguous BATCH writes via client-side write lock.
- Background reader thread parsing ACK/ERR lines with monotonic_ns timestamps.
- In-flight control via semaphores.
- Live reader thread testing D4 batch atomicity.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Callable

from torture_tool.oracle import Oracle


class ServeClient:
    def __init__(
        self,
        proc: subprocess.Popen,
        oracle: Oracle,
        on_violation: Callable[[str, str], None] | None = None,
    ):
        self.proc = proc
        self.oracle = oracle
        self.on_violation = on_violation

        self._write_lock = threading.Lock()
        self._running = True

        # Pending callbacks / events for GET and COMPACT
        self._get_replies: dict[str, tuple[str, str | None]] = {}  # id -> (status, val)
        self._get_events: dict[str, threading.Event] = {}

        # Semaphores or counters for in-flight operations per writer
        self._op_writers: dict[str, int] = {}  # op_id -> writer_id
        self._writer_semaphores: dict[int, threading.Semaphore] = {}

        # In-flight tracking for coverage stats
        self._inflight_lock = threading.Lock()
        self._inflight_ops: dict[str, str] = {}  # op_id -> type ("PUT", "DEL", "BATCH", "COMPACT")
        self.active_write_event = threading.Event()

        # Start background reader thread
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="ServeReader", daemon=True
        )
        self._reader_thread.start()

    def register_writer(self, writer_id: int, inflight_limit: int) -> None:
        self._writer_semaphores[writer_id] = threading.Semaphore(inflight_limit)

    def acquire_writer_slot(self, writer_id: int, timeout: float = 0.5) -> bool:
        sem = self._writer_semaphores.get(writer_id)
        if sem is None:
            return True
        return sem.acquire(timeout=timeout)

    def release_writer_slot(self, writer_id: int) -> None:
        sem = self._writer_semaphores.get(writer_id)
        if sem is not None:
            sem.release()

    def get_inflight_counts(self) -> tuple[int, int, int]:
        """Returns (total_inflight, batch_inflight, large_value_inflight)."""
        with self._inflight_lock:
            total = len(self._inflight_ops)
            batches = sum(1 for t in self._inflight_ops.values() if t.startswith("BATCH"))
            large_vals = sum(
                1 for t in self._inflight_ops.values() if t.startswith("LARGE_PUT")
            )
            return total, batches, large_vals

    def get_inflight_summary(self) -> list[str]:
        """Returns list of active op descriptions."""
        with self._inflight_lock:
            return list(self._inflight_ops.values())

    def send_put(
        self,
        op_id: str,
        key: str,
        val: bytes,
        writer_id: int | None = None,
        is_large: bool = False,
    ) -> None:
        key_hex = key.encode("utf-8").hex()
        val_hex = val.hex() if val else "-"
        cmd = f"PUT {op_id} {key_hex} {val_hex}\n".encode("ascii")

        sent_ns = time.monotonic_ns()
        self.oracle.record_sent_put(op_id, key, val, sent_ns)

        if writer_id is not None:
            self._op_writers[op_id] = writer_id

        desc = f"LARGE_PUT({key[:12]})" if is_large else f"PUT({key[:12]})"
        with self._inflight_lock:
            self._inflight_ops[op_id] = desc

        with self._write_lock:
            if not self._running or self.proc.stdin is None:
                return
            try:
                self.active_write_event.set()
                self.proc.stdin.write(cmd)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._running = False
            finally:
                self.active_write_event.clear()

    def send_del(
        self,
        op_id: str,
        key: str,
        writer_id: int | None = None,
    ) -> None:
        key_hex = key.encode("utf-8").hex()
        cmd = f"DEL {op_id} {key_hex}\n".encode("ascii")

        sent_ns = time.monotonic_ns()
        self.oracle.record_sent_del(op_id, key, sent_ns)

        if writer_id is not None:
            self._op_writers[op_id] = writer_id

        with self._inflight_lock:
            self._inflight_ops[op_id] = f"DEL({key[:12]})"

        with self._write_lock:
            if not self._running or self.proc.stdin is None:
                return
            try:
                self.active_write_event.set()
                self.proc.stdin.write(cmd)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._running = False
            finally:
                self.active_write_event.clear()

    def send_batch(
        self,
        op_id: str,
        sub_ops: list[tuple[str, str, bytes | None]],
        writer_id: int | None = None,
    ) -> None:
        header = f"BATCH {op_id} {len(sub_ops)}\n".encode("ascii")
        lines = [header]
        for op_type, k, v in sub_ops:
            k_hex = k.encode("utf-8").hex()
            if op_type.upper() == "PUT":
                assert v is not None
                v_hex = v.hex() if v else "-"
                lines.append(f"put {k_hex} {v_hex}\n".encode("ascii"))
            else:
                lines.append(f"del {k_hex}\n".encode("ascii"))

        batch_bytes = b"".join(lines)
        sent_ns = time.monotonic_ns()
        self.oracle.record_sent_batch(op_id, sub_ops, sent_ns)

        if writer_id is not None:
            self._op_writers[op_id] = writer_id

        with self._inflight_lock:
            self._inflight_ops[op_id] = f"BATCH({len(sub_ops)} ops)"

        # Multi-line batch must be written contiguously to stdin
        with self._write_lock:
            if not self._running or self.proc.stdin is None:
                return
            try:
                self.active_write_event.set()
                self.proc.stdin.write(batch_bytes)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._running = False
            finally:
                self.active_write_event.clear()

    def send_get(self, req_id: str, key_hex: str, timeout: float = 2.0) -> tuple[str, str | None]:
        cmd = f"GET {req_id} {key_hex}\n".encode("ascii")
        evt = threading.Event()
        self._get_events[req_id] = evt

        with self._write_lock:
            if not self._running or self.proc.stdin is None:
                return ("ERR", None)
            try:
                self.proc.stdin.write(cmd)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._running = False
                return ("ERR", None)

        if evt.wait(timeout=timeout):
            return self._get_replies.pop(req_id, ("ERR", None))
        return ("TIMEOUT", None)

    def send_compact(self, req_id: str) -> None:
        cmd = f"COMPACT {req_id}\n".encode("ascii")
        with self._inflight_lock:
            self._inflight_ops[req_id] = "COMPACT"

        with self._write_lock:
            if not self._running or self.proc.stdin is None:
                return
            try:
                self.active_write_event.set()
                self.proc.stdin.write(cmd)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._running = False
            finally:
                self.active_write_event.clear()

    def _reader_loop(self) -> None:
        assert self.proc.stdout is not None
        while self._running:
            try:
                line_bytes = self.proc.stdout.readline()
            except (ValueError, OSError):
                break

            if not line_bytes:
                break

            # Incomplete line check: must terminate with \n
            if not line_bytes.endswith(b"\n"):
                break

            recv_ns = time.monotonic_ns()
            line = line_bytes.decode("ascii", errors="replace").strip()
            if not line:
                continue

            parts = line.split(" ")
            tag = parts[0]

            if tag == "ACK" and len(parts) >= 2:
                op_id = parts[1]
                self.oracle.record_ack(op_id, recv_ns)
                self._finish_op(op_id)
            elif tag == "ERR" and len(parts) >= 3:
                op_id = parts[1]
                code = parts[2]
                self.oracle.record_err(op_id, code, recv_ns)
                self._finish_op(op_id)
            elif tag in ("VAL", "NOTFOUND") and len(parts) >= 2:
                req_id = parts[1]
                val = parts[2] if (tag == "VAL" and len(parts) >= 3) else None
                self._get_replies[req_id] = (tag, val)
                evt = self._get_events.pop(req_id, None)
                if evt:
                    evt.set()

    def _finish_op(self, op_id: str) -> None:
        with self._inflight_lock:
            self._inflight_ops.pop(op_id, None)
        writer_id = self._op_writers.pop(op_id, None)
        if writer_id is not None:
            self.release_writer_slot(writer_id)

    def stop(self) -> None:
        self._running = False
        with self._write_lock:
            pass

