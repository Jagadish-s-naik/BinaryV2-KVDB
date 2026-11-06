"""Utilitarian visualizer and telemetry HUD for the kvdb torture tool.

Provides clean, tasteful CLI visual flare with zero external dependencies:
- ANSI color palette (dim cyan, bold white, bright green, bright yellow, bright blue, muted gray)
- Compact Unicode box frames (┌─┐, │, └─┘)
- Dynamic progress bar and pipe buffer saturation gauge
- Live iteration log stream formatter with status badges (✔, ⚡, ↻)
- Post-run executive proof scorecard with ASCII strike distribution bar chart
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

    # Cursor control
    CLEAR_LINE = "\033[2K\r"
    CURSOR_UP = lambda n=1: f"\033[{n}A"
    CURSOR_DOWN = lambda n=1: f"\033[{n}B"
    SAVE_CURSOR = "\033[s"
    RESTORE_CURSOR = "\033[u"


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
    """Zero-dependency utilitarian telemetry HUD with tasteful CLI visual flare."""

    def __init__(
        self,
        mode: str,
        seed: str,
        total_iterations: int,
        writers: int,
        inflight_cap: int,
        ui_mode: str = "auto",
    ) -> None:
        self.mode = mode
        self.seed = seed
        self.total_iterations = total_iterations
        self.writers = writers
        self.inflight_cap = inflight_cap
        self.ui_mode = ui_mode.lower().strip()

        # Determine if interactive ANSI rendering should be enabled
        if self.ui_mode == "plain":
            self.interactive = False
        elif self.ui_mode == "live":
            self.interactive = True
        else:  # auto
            is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
            # Windows terminal handles ANSI if VT processing is active
            self.interactive = is_tty and os.environ.get("TERM") != "dumb"

        self.start_time = time.monotonic()
        self.last_render_time = 0.0
        self._header_rendered = False
        self._hud_lines_count = 6

    def _c(self, text: str, color_code: str) -> str:
        """Apply color code if interactive ANSI mode is active."""
        if not self.interactive:
            return text
        return f"{color_code}{text}{ANSI.RESET}"

    def format_bar(self, current: int, total: int, width: int = 20, fill_char: str = "█", empty_char: str = "░") -> str:
        if total <= 0:
            pct = 0.0
            filled = 0
        else:
            pct = max(0.0, min(1.0, current / total))
            filled = int(round(pct * width))
        empty = width - filled
        return f"[{fill_char * filled}{empty_char * empty}] {int(pct * 100):2d}%"

    def format_buffer_gauge(self, inflight: int, max_cap: int, width: int = 16) -> str:
        cap = max(1, max_cap)
        pct = max(0.0, min(1.0, inflight / cap))
        filled = int(round(pct * width))
        empty = width - filled
        bar = f"[{'■' * filled}{'·' * empty}] {int(pct * 100):2d}%"
        if pct > 0.8:
            return self._c(bar, ANSI.B_RED)
        elif pct > 0.5:
            return self._c(bar, ANSI.B_YELLOW)
        return self._c(bar, ANSI.B_GREEN)

    def print_banner(self) -> None:
        """Render the initial run banner."""
        box_w = 78
        sep_top = self._c("┌" + "─" * (box_w - 2) + "┐", ANSI.CYAN)
        sep_mid = self._c("├" + "─" * (box_w - 2) + "┤", ANSI.CYAN)
        sep_bot = self._c("└" + "─" * (box_w - 2) + "┘", ANSI.CYAN)

        title = self._c(" KVDB TORTURE TEST & VERIFICATION HARNESS ", ANSI.B_WHITE)
        meta1 = f"Mode: {self._c(self.mode, ANSI.B_CYAN)}  │  Seed: {self._c(self.seed, ANSI.B_YELLOW)}  │  Iterations: {self._c(str(self.total_iterations), ANSI.B_WHITE)}"
        meta2 = f"Writers: {self.writers}  │  In-Flight Cap: {self.inflight_cap}/writer  │  UI: {self.ui_mode}"

        if self.interactive:
            print(sep_top)
            print(f"{self._c('│', ANSI.CYAN)}{pad_vis(title, box_w - 2, align='^')}{self._c('│', ANSI.CYAN)}")
            print(sep_mid)
            print(f"{self._c('│', ANSI.CYAN)}  {pad_vis(meta1, box_w - 4)}  {self._c('│', ANSI.CYAN)}")
            print(f"{self._c('│', ANSI.CYAN)}  {pad_vis(meta2, box_w - 4)}  {self._c('│', ANSI.CYAN)}")
            print(sep_bot)
        else:
            print(
                f"kvdb torture runner started: seed={self.seed}, mode={self.mode}, "
                f"iterations={self.total_iterations}, writers={self.writers}, inflight={self.inflight_cap}"
            )

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
        """Format and print a dense, structured iteration line to the log stream."""
        status_badge = self._c("✔ PASS", ANSI.B_GREEN) if passed else self._c("✘ FAIL", ANSI.B_RED)
        strike_badge = self._c("⚡", ANSI.B_YELLOW)
        recovery_badge = self._c("↻", ANSI.B_BLUE)

        safe_summaries = [s.encode("ascii", errors="backslashreplace").decode("ascii") for s in inflight_summaries]
        desc_str = f" [{', '.join(safe_summaries[:2])}]" if safe_summaries else ""

        strike_info = f"{strike_badge} strike={strike_delay_us}µs({strike_phase})" if strike_delay_us > 0 else f"{strike_badge} kill_ms={kill_ms}"
        rec_info = f"{recovery_badge} rec={recovery_ms}ms(keys={keys_count})"

        line = (
            f"iter {iteration:3d}/{self.total_iterations}  {status_badge}  "
            f"acked={acked} pend={pending} inflight={total_inflight}{desc_str} "
            f"{strike_info} {rec_info}"
        )
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
    ) -> None:
        """Render the post-run executive proof scorecard."""
        box_w = 78
        sep_top = self._c("┌" + "─" * (box_w - 2) + "┐", ANSI.CYAN)
        sep_mid = self._c("├" + "─" * (box_w - 2) + "┤", ANSI.CYAN)
        sep_bot = self._c("└" + "─" * (box_w - 2) + "┘", ANSI.CYAN)

        title = self._c(" KVDB TORTURE TELEMETRY & PROOF SUMMARY ", ANSI.B_WHITE)
        tot_iters = iterations_passed + iterations_failed
        pct_pass = (iterations_passed / max(1, tot_iters)) * 100.0
        status_str = self._c(f"{iterations_passed}/{tot_iters} [{pct_pass:.1f}% PASS]", ANSI.B_GREEN if iterations_failed == 0 else ANSI.B_RED)

        db_mb = final_db_size / (1024 * 1024)
        db_str = f"{final_db_size:,} B ({db_mb:.2f} MiB)" if final_db_size > 0 else "0 B"

        print()
        print(sep_top)
        print(f"{self._c('│', ANSI.CYAN)}{pad_vis(title, box_w - 2, align='^')}{self._c('│', ANSI.CYAN)}")
        print(sep_mid)
        line1 = f"Mode: {self._c(self.mode, ANSI.B_CYAN)}   Seed: {self._c(self.seed, ANSI.B_YELLOW)}   Iterations: {status_str}"
        line2 = f"DB Size: {db_str}   Live Keys: {live_keys:,}   Verified Ops: {total_acked:,}"
        print(f"{self._c('│', ANSI.CYAN)}  {pad_vis(line1, box_w - 4)}  {self._c('│', ANSI.CYAN)}")
        print(f"{self._c('│', ANSI.CYAN)}  {pad_vis(line2, box_w - 4)}  {self._c('│', ANSI.CYAN)}")
        print(sep_mid)
        print(f"{self._c('│', ANSI.CYAN)}  {pad_vis(self._c('STRIKE TARGETING DISTRIBUTION (Microsecond Sniper Telemetry)', ANSI.BOLD), box_w - 4)}  {self._c('│', ANSI.CYAN)}")

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
            delay_str = f"{avg_us:,} µs avg" if avg_us > 0 else "N/A"
            bar_w = 20
            bar_filled = int(round((cnt / total_strikes) * bar_w))
            bar_empty = bar_w - bar_filled
            bar_str = f"[{'█' * bar_filled}{'░' * bar_empty}]"
            colored_bar = self._c(bar_str, col)
            row = f"  {display_name:<16} {colored_bar} {pct:5.1f}% ({cnt:2d})   {delay_str:>12}"
            print(f"{self._c('│', ANSI.CYAN)}  {pad_vis(row, box_w - 4)}  {self._c('│', ANSI.CYAN)}")

        print(sep_mid)
        print(f"{self._c('│', ANSI.CYAN)}  {pad_vis(self._c('INVARIANT VERIFICATION AUDIT', ANSI.BOLD), box_w - 4)}  {self._c('│', ANSI.CYAN)}")

        for code, desc, passed in invariants_verified:
            badge = self._c("✔", ANSI.B_GREEN) if passed else self._c("✘", ANSI.B_RED)
            tag = self._c(code, ANSI.B_WHITE)
            status_text = self._c("PASSED", ANSI.GREEN) if passed else self._c("VIOLATED", ANSI.B_RED)
            row = f"  {badge} {tag:<5} {desc:<48} [{status_text}]"
            print(f"{self._c('│', ANSI.CYAN)}  {pad_vis(row, box_w - 4)}  {self._c('│', ANSI.CYAN)}")

        print(sep_bot)
        print()
        sys.stdout.flush()
