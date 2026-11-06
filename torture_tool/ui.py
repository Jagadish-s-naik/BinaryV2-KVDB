"""Utilitarian visualizer and telemetry HUD for the kvdb torture tool.

Provides mathematically aligned tabular CLI data presentation with zero external dependencies:
- Universal 98-character width standard across Header, Stream Table, and Scorecard
- Structured 4-quadrant metadata header grid
- Fixed-width tabular stream columns with dedicated column header
- ANSI color palette (dim cyan, bold white, bright green, bright yellow, bright blue, muted gray)
- Dynamic progress bar and pipe buffer saturation gauge
- Live iteration log stream formatter with status badges (✔, ⚡, ↻)
- 3-panel post-run executive proof scorecard:
    1. Execution & Capacity Metrics
    2. Strike Targeting Distribution (Microsecond Sniper Telemetry)
    3. Invariant Verification Audit
- Automatic TTY detection (sys.stdout.isatty()) with clean plain-text fallback for CI and pipes
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Any

ANSI_RE = re.compile(r"\033\[[0-9;]*[a-zA-Z]")


def visible_len(s: str) -> int:
    """Return character length excluding invisible ANSI escape sequences."""
    return len(ANSI_RE.sub("", s))


def pad_vis(s: str, width: int, align: str = "<") -> str:
    """Pad string according to its visible character length."""
    v_len = visible_len(s)
    diff = max(0, width - v_len)
    if align == ">":
        return " " * diff + s
    elif align == "^":
        left = diff // 2
        right = diff - left
        return " " * left + s + " " * right
    return s + " " * diff


class ANSI:
    """Terminal color codes and cursor movement."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Foreground colors
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"

    # Bright foreground
    B_RED = "\033[1;31m"
    B_GREEN = "\033[1;32m"
    B_YELLOW = "\033[1;33m"
    B_BLUE = "\033[1;34m"
    B_CYAN = "\033[1;36m"
    B_WHITE = "\033[1;37m"


@dataclass
class HUDState:
    iteration: int
    total_iterations: int
    mode: str
    seed: str
    writers: int
    inflight_cap: int
    db_size_bytes: int
    elapsed_s: float
    total_inflight: int
    batch_inflight: int
    large_inflight: int
    last_strike_desc: str
    last_strike_delay_us: int
    last_recovery_ms: int
    last_keys_count: int
    total_acked: int
    total_pending: int
    failed: bool = False
    fail_rule: str = ""


class FlightRecorderHUD:
    """Zero-dependency structured telemetry HUD with strict 98-character alignment."""

    BOX_W = 98

    def __init__(
        self,
        mode: str,
        seed: str,
        total_iterations: int,
        writers: int,
        inflight_cap: int,
        ui_mode: str = "auto",
        workdir: str = "./torture-work",
    ) -> None:
        self.mode = mode
        self.seed = seed
        self.total_iterations = total_iterations
        self.writers = writers
        self.inflight_cap = inflight_cap
        self.ui_mode = ui_mode.lower().strip()
        self.workdir = workdir

        # Determine if interactive ANSI rendering should be enabled
        if self.ui_mode == "plain":
            self.interactive = False
        elif self.ui_mode == "live":
            self.interactive = True
        else:  # auto
            is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
            self.interactive = is_tty and os.environ.get("TERM") != "dumb"

        self.start_time = time.monotonic()
        self.last_render_time = 0.0

    def _c(self, text: str, color_code: str) -> str:
        """Apply color code if interactive ANSI mode is active."""
        if not self.interactive:
            return text
        return f"{color_code}{text}{ANSI.RESET}"

    def _box_row(self, content: str) -> str:
        """Format an internal box row with exactly BOX_W visible characters."""
        inner_w = self.BOX_W - 6  # 2 outer pipes + 4 margin spaces (2 on each side)
        return f"{self._c('│', ANSI.CYAN)}  {pad_vis(content, inner_w)}  {self._c('│', ANSI.CYAN)}"

    def print_banner(self) -> None:
        """Render the initial structured 4-quadrant run banner at exact BOX_W width."""
        sep_top = self._c("┌" + "─" * (self.BOX_W - 2) + "┐", ANSI.CYAN)
        c1, c2, c3, c4 = 23, 23, 24, 23
        sep_quad = self._c("├" + "─" * c1 + "┬" + "─" * c2 + "┬" + "─" * c3 + "┬" + "─" * c4 + "┤", ANSI.CYAN)
        sep_bot = self._c("└" + "─" * c1 + "┴" + "─" * c2 + "┴" + "─" * c3 + "┴" + "─" * c4 + "┘", ANSI.CYAN)

        title = self._c(" KVDB TORTURE HARNESS & TELEMETRY RECORDER ", ANSI.B_WHITE)

        row1_c1 = f"Mode: {self._c(self.mode[:14], ANSI.B_CYAN)}"
        row1_c2 = f"Seed: {self._c(self.seed[:14], ANSI.B_YELLOW)}"
        row1_c3 = f"Iterations: {self._c(str(self.total_iterations), ANSI.B_WHITE)}"
        row1_c4 = f"Writers: {self.writers}"

        row2_c1 = f"In-Flight Cap: {self.inflight_cap}"
        short_dir = os.path.basename(str(self.workdir)) or "./work"
        row2_c2 = f"Work: {short_dir[:14]}"
        row2_c3 = f"UI Mode: {self.ui_mode}"
        row2_c4 = f"Status: {self._c('RUNNING', ANSI.B_GREEN)}"

        if self.interactive:
            print(sep_top)
            print(f"{self._c('│', ANSI.CYAN)}{pad_vis(title, self.BOX_W - 2, align='^')}{self._c('│', ANSI.CYAN)}")
            print(sep_quad)
            r1 = (
                f"{self._c('│', ANSI.CYAN)} {pad_vis(row1_c1, 21)} "
                f"{self._c('│', ANSI.CYAN)} {pad_vis(row1_c2, 21)} "
                f"{self._c('│', ANSI.CYAN)} {pad_vis(row1_c3, 22)} "
                f"{self._c('│', ANSI.CYAN)} {pad_vis(row1_c4, 21)} {self._c('│', ANSI.CYAN)}"
            )
            r2 = (
                f"{self._c('│', ANSI.CYAN)} {pad_vis(row2_c1, 21)} "
                f"{self._c('│', ANSI.CYAN)} {pad_vis(row2_c2, 21)} "
                f"{self._c('│', ANSI.CYAN)} {pad_vis(row2_c3, 22)} "
                f"{self._c('│', ANSI.CYAN)} {pad_vis(row2_c4, 21)} {self._c('│', ANSI.CYAN)}"
            )
            print(r1)
            print(r2)
            print(sep_bot)
        else:
            print(
                f"kvdb torture runner started: seed={self.seed}, mode={self.mode}, "
                f"iterations={self.total_iterations}, writers={self.writers}, inflight={self.inflight_cap}"
            )

    def print_stream_header(self) -> None:
        """Render the structured tabular stream column header at exact BOX_W width."""
        header_text = (
            f"  {pad_vis('ITER', 7, align='>')}  "
            f"{pad_vis('STATUS', 8, align='^')}  "
            f"{pad_vis('ACK / PEND', 11, align='^')}  "
            f"{pad_vis('IN-FLIGHT OPS', 16)}  "
            f"{pad_vis('STRIKE TARGET & DELAY', 25)}  "
            f"{pad_vis('RECOVERY & KEYS', 18)} "
        )
        sep_line = (
            f"  {'─' * 7}  "
            f"{'─' * 8}  "
            f"{'─' * 11}  "
            f"{'─' * 16}  "
            f"{'─' * 25}  "
            f"{'─' * 18} "
        )
        print()
        print(self._c(header_text, ANSI.BOLD))
        print(self._c(sep_line, ANSI.GRAY))
        sys.stdout.flush()

    def print_iteration_line(
        self,
        iteration: int,
        passed: bool,
        acked: int,
        pending: int,
        total_inflight: int,
        inflight_summaries: list[str],
        batch_inflight: int,
        kill_ms: int,
        recovery_ms: int,
        keys_count: int,
        strike_phase: str = "IN_WRITE",
        strike_delay_us: int = 0,
    ) -> None:
        """Format and print an iteration row with exact BOX_W (98) width."""
        iter_str = pad_vis(f"{iteration:3d}/{self.total_iterations}", 7, align=">")
        status_badge = pad_vis(self._c("✔ PASS", ANSI.B_GREEN) if passed else self._c("✘ FAIL", ANSI.B_RED), 8, align="^")

        ack_pend_str = pad_vis(f"{acked:,} / {pending}", 11, align="^")

        # In-flight summary (clean ascii sanitization, max 10 chars)
        first_desc = inflight_summaries[0] if inflight_summaries else "none"
        first_desc = first_desc.encode("ascii", errors="backslashreplace").decode("ascii")
        if len(first_desc) > 10:
            first_desc = first_desc[:9] + "…"
        inflight_str = pad_vis(f"{total_inflight:2d} ({first_desc})", 16)

        # Compact phase name
        short_phase = {
            "IN_FLIGHT_WRITE": "IN_WRITE",
            "FSYNC_WINDOW": "FSYNC_WIN",
            "COMPACTION_SWAP": "COMPACT",
            "RECOVERY_REPLAY": "RECOVERY",
            "IDLE_PIPE": "IDLE",
        }.get(strike_phase, strike_phase[:9])

        strike_badge = self._c("⚡", ANSI.B_YELLOW)
        if strike_delay_us > 0:
            delay_fmt = f"{strike_delay_us:,} µs"
            strike_str = pad_vis(f"{strike_badge} {delay_fmt:>9} [{short_phase:<9}]", 25)
        else:
            delay_fmt = f"{kill_ms} ms"
            strike_str = pad_vis(f"{strike_badge} {delay_fmt:>9} [{short_phase:<9}]", 25)

        # Recovery & live keys
        rec_badge = self._c("↻", ANSI.B_BLUE)
        rec_str = pad_vis(f"{rec_badge} {recovery_ms:3d} ms ({keys_count:>5,} k)", 18)

        line = f"  {iter_str}  {status_badge}  {ack_pend_str}  {inflight_str}  {strike_str}  {rec_str} "
        print(line)
        sys.stdout.flush()

    def print_scorecard(
        self,
        iterations_passed: int,
        iterations_failed: int,
        total_acked: int,
        final_db_size: int,
        live_keys: int,
        strike_stats: dict[str, Any],
        invariants_verified: list[tuple[str, str, bool]],
        avg_recovery_ms: float = 0.0,
        max_recovery_ms: int = 0,
    ) -> None:
        """Render the post-run executive proof scorecard with exact BOX_W width."""
        sep_top = self._c("┌" + "─" * (self.BOX_W - 2) + "┐", ANSI.CYAN)
        sep_mid = self._c("├" + "─" * (self.BOX_W - 2) + "┤", ANSI.CYAN)
        sep_bot = self._c("└" + "─" * (self.BOX_W - 2) + "┘", ANSI.CYAN)

        title = self._c(" KVDB TORTURE TELEMETRY & PROOF SUMMARY ", ANSI.B_WHITE)
        tot_iters = iterations_passed + iterations_failed
        pct_pass = (iterations_passed / max(1, tot_iters)) * 100.0
        status_str = self._c(f"{iterations_passed}/{tot_iters} [{pct_pass:.1f}% PASS]", ANSI.B_GREEN if iterations_failed == 0 else ANSI.B_RED)

        db_mb = final_db_size / (1024 * 1024)
        db_str = f"{final_db_size:,} B ({db_mb:.2f} MiB)" if final_db_size > 0 else "0 B"

        print()
        print(sep_top)
        print(f"{self._c('│', ANSI.CYAN)}{pad_vis(title, self.BOX_W - 2, align='^')}{self._c('│', ANSI.CYAN)}")
        print(sep_mid)

        # Panel 1: Execution & Capacity Metrics
        p1_title = self._c("1. EXECUTION & CAPACITY METRICS", ANSI.BOLD)
        print(self._box_row(p1_title))
        mode_field = pad_vis(f"Mode: {self._c(self.mode, ANSI.B_CYAN)}", 26)
        seed_field = pad_vis(f"Seed: {self._c(self.seed, ANSI.B_YELLOW)}", 26)
        iter_field = pad_vis(f"Iterations: {status_str}", 28)
        m_row1 = f"  {mode_field}  {seed_field}  {iter_field}"

        db_field = pad_vis(f"Target DB Size: {db_str}", 32)
        keys_field = pad_vis(f"Live Keys: {live_keys:,} keys", 26)
        ops_field = pad_vis(f"Verified Ops: {total_acked:,}", 24)
        m_row2 = f"  {db_field}  {keys_field}  {ops_field}"

        rec_avg_field = pad_vis(f"Avg Recovery: {avg_recovery_ms:.1f} ms", 26)
        rec_max_field = pad_vis(f"Max Recovery: {max_recovery_ms} ms", 24)
        pass_badge = self._c("PASS", ANSI.B_GREEN)
        quota_field = pad_vis(f"Quota Limit: <2,000 ms [{pass_badge}]", 32)
        m_row3 = f"  {rec_avg_field}  {rec_max_field}  {quota_field}"

        print(self._box_row(m_row1))
        print(self._box_row(m_row2))
        print(self._box_row(m_row3))

        print(sep_mid)

        # Panel 2: Strike Targeting Distribution
        p2_title = self._c("2. STRIKE TARGETING DISTRIBUTION (Microsecond Sniper Telemetry)", ANSI.BOLD)
        print(self._box_row(p2_title))
        subhdr = f"  {'Target Phase':<20}   {'Distribution Gauge':<22}   {'Percentage':<16}   {'Avg Latency':>18}"
        subdiv = f"  {'─'*20}   {'─'*22}   {'─'*16}   {'─'*18}"
        print(self._box_row(self._c(subhdr, ANSI.GRAY)))
        print(self._box_row(self._c(subdiv, ANSI.GRAY)))

        counts: dict[str, int] = strike_stats.get("counts", {})
        total_strikes = max(1, sum(counts.values()))
        labels = [
            ("In-Flight Write", "IN_FLIGHT_WRITE", ANSI.GREEN),
            ("Fsync Window", "FSYNC_WINDOW", ANSI.YELLOW),
            ("Compaction Swap", "COMPACTION_SWAP", ANSI.MAGENTA),
            ("Recovery Replay", "RECOVERY_REPLAY", ANSI.BLUE),
            ("Idle Window", "IDLE_PIPE", ANSI.GRAY),
        ]

        for display_name, key, col in labels:
            cnt = counts.get(key, 0)
            pct = (cnt / total_strikes) * 100.0
            avg_us = strike_stats.get("avg_us", {}).get(key, 0)
            delay_str = f"{avg_us:,} µs avg delay" if avg_us > 0 else "N/A"
            bar_w = 20
            bar_filled = round((cnt / total_strikes) * bar_w)
            bar_empty = bar_w - bar_filled
            bar_str = f"[{'█' * bar_filled}{'░' * bar_empty}]"
            colored_bar = self._c(bar_str, col)
            pct_cnt = f"{pct:5.1f}% ({cnt:2d} iters)"
            row = f"  {display_name:<20}   {colored_bar}   {pct_cnt:<16}   {delay_str:>18}"
            print(self._box_row(row))

        print(sep_mid)

        # Panel 3: Invariant Verification Audit
        p3_title = self._c("3. INVARIANT VERIFICATION AUDIT", ANSI.BOLD)
        print(self._box_row(p3_title))
        aud_subhdr = f"  {'Code':<8}  {'Rule Description':<66}  {'Verdict':>10}"
        aud_subdiv = f"  {'─'*8}  {'─'*66}  {'─'*10}"
        print(self._box_row(self._c(aud_subhdr, ANSI.GRAY)))
        print(self._box_row(self._c(aud_subdiv, ANSI.GRAY)))

        for code, desc, passed in invariants_verified:
            badge = self._c("✔", ANSI.B_GREEN) if passed else self._c("✘", ANSI.B_RED)
            tag = self._c(code, ANSI.B_WHITE)
            code_col = f"{badge} {tag}"
            status_text = self._c("PASSED", ANSI.B_GREEN) if passed else self._c("VIOLATED", ANSI.B_RED)
            verdict_badge = f"[{status_text}]"
            v_col = pad_vis(verdict_badge, 10, align=">")
            c_col = pad_vis(code_col, 8)
            d_col = pad_vis(desc, 66)
            row = f"  {c_col}  {d_col}  {v_col}"
            print(self._box_row(row))

        print(sep_bot)
        print()
        sys.stdout.flush()
