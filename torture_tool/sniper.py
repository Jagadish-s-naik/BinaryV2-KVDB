"""Microsecond latency-calibrated sniper strike engine for the kvdb torture tool.

Profiles real-time write/fsync cycle durations and times SIGKILL strikes
to land directly inside active kernel I/O windows rather than during idle wait loops.
Maintains strike phase statistics and formats proof distribution metrics.
"""

from __future__ import annotations

import random
import time
from collections import defaultdict
from typing import Any


class StrikePhase:
    IN_FLIGHT_WRITE = "IN_FLIGHT_WRITE"
    FSYNC_WINDOW = "FSYNC_WINDOW"
    COMPACTION_SWAP = "COMPACTION_SWAP"
    RECOVERY_REPLAY = "RECOVERY_REPLAY"
    IDLE_PIPE = "IDLE_PIPE"


class SniperCalibrator:
    """Profiles write/fsync latencies and executes surgical microsecond kills."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self._latencies_ns: list[int] = []
        self._strike_counts: dict[str, int] = defaultdict(int)
        self._strike_delays_ns: dict[str, list[int]] = defaultdict(list)
        self._last_phase: str = StrikePhase.IN_FLIGHT_WRITE
        self._last_delay_us: int = 0

    def record_write_latency_ns(self, latency_ns: int) -> None:
        """Record completed write cycle latency in nanoseconds."""
        self._latencies_ns.append(latency_ns)
        if len(self._latencies_ns) > 200:
            self._latencies_ns.pop(0)

    def get_p50_latency_ns(self) -> int:
        if not self._latencies_ns:
            return 250_000  # Default 250 µs baseline
        sorted_l = sorted(self._latencies_ns)
        return sorted_l[len(sorted_l) // 2]

    def get_p95_latency_ns(self) -> int:
        if not self._latencies_ns:
            return 800_000  # Default 800 µs baseline
        sorted_l = sorted(self._latencies_ns)
        idx = int(len(sorted_l) * 0.95)
        return sorted_l[min(idx, len(sorted_l) - 1)]

    def compute_strike_timing(
        self,
        base_kill_ms: int,
        is_compact_active: bool = False,
        is_recovery_active: bool = False,
    ) -> tuple[int, str]:
        """Compute targeted microsecond delay and anticipated phase.
        
        Returns:
            (target_delay_ns, anticipated_phase)
        """
        if is_recovery_active:
            delay_ns = self.rng.randint(2_000_000, 20_000_000)
            self._last_phase = StrikePhase.RECOVERY_REPLAY
            self._last_delay_us = delay_ns // 1000
            return delay_ns, StrikePhase.RECOVERY_REPLAY

        if is_compact_active:
            delay_ns = self.rng.randint(5_000_000, 40_000_000)
            self._last_phase = StrikePhase.COMPACTION_SWAP
            self._last_delay_us = delay_ns // 1000
            return delay_ns, StrikePhase.COMPACTION_SWAP

        p50 = self.get_p50_latency_ns()
        roll = self.rng.random()

        if roll < 0.65:
            # Phase 1: 65% target in-flight write syscall (penetrate 20% to 80% into write)
            penetration = self.rng.uniform(0.20, 0.80)
            delay_ns = max(20_000, int(p50 * penetration))
            phase = StrikePhase.IN_FLIGHT_WRITE
        elif roll < 0.90:
            # Phase 2: 25% target fsync window immediately following write submission
            delay_ns = max(50_000, int(p50 * self.rng.uniform(0.90, 1.40)))
            phase = StrikePhase.FSYNC_WINDOW
        else:
            # Phase 3: 10% broad sub-millisecond precision window
            delay_ns = self.rng.randint(100_000, max(500_000, base_kill_ms * 1_000_000))
            phase = StrikePhase.IN_FLIGHT_WRITE

        self._last_phase = phase
        self._last_delay_us = delay_ns // 1000
        return delay_ns, phase

    def record_strike_result(
        self,
        phase: str,
        delay_ns: int,
        inflight_ops_count: int,
    ) -> None:
        """Record the verified strike event after SIGKILL."""
        actual_phase = phase
        if inflight_ops_count == 0 and phase not in (StrikePhase.COMPACTION_SWAP, StrikePhase.RECOVERY_REPLAY):
            actual_phase = StrikePhase.IDLE_PIPE

        self._strike_counts[actual_phase] += 1
        self._strike_delays_ns[actual_phase].append(delay_ns)
        self._last_phase = actual_phase
        self._last_delay_us = delay_ns // 1000

    def get_summary_stats(self) -> dict[str, Any]:
        """Generate structured stats dictionary for scorecard and reports."""
        avg_us: dict[str, int] = {}
        for phase, delays in self._strike_delays_ns.items():
            if delays:
                avg_us[phase] = (sum(delays) // len(delays)) // 1000
            else:
                avg_us[phase] = 0

        return {
            "counts": dict(self._strike_counts),
            "avg_us": avg_us,
            "total_strikes": sum(self._strike_counts.values()),
        }

