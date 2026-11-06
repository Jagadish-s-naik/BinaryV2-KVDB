"""Torture mode specifications and configurations.

Per docs/TORTURE_SPEC.md Section 8:
Modes:
- default
- large-values
- large-batches
- compaction
- recovery-crash
- disk-full
- crash-points
- scale
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WRITE_CRASH_POINTS = [
    "before_write",
    "mid_write",
    "after_write_before_fsync",
    "after_fsync_before_index",
    "after_fsync_before_ack",
]

COMPACT_CRASH_POINTS = [
    "compact_after_tmp",
    "compact_before_replace",
    "compact_after_replace",
    "compact_after_dirsync",
]

RECOVERY_CRASH_POINTS = [
    "recovery_before_truncate",
    "recovery_after_truncate",
]

CRASH_POINTS = WRITE_CRASH_POINTS + COMPACT_CRASH_POINTS + RECOVERY_CRASH_POINTS


@dataclass
class ModeConfig:
    name: str
    writers: int = 2
    shared_key_count: int = 20
    kill_min_ms: int = 5
    kill_max_ms: int = 600
    max_bytes: int | None = None
    enable_compaction_thread: bool = False
    compaction_interval_ms_range: tuple[int, int] = (50, 300)
    enable_live_reader: bool = True
    is_crash_point_mode: bool = False
    is_recovery_crash_mode: bool = False
    is_scale_mode: bool = False
    is_fault_injection_mode: bool = False


def get_mode_config(mode_name: str, base_writers: int = 2) -> ModeConfig:
    m = mode_name.lower().strip()
    if m == "default":
        return ModeConfig(name="default", writers=max(2, base_writers))
    elif m == "large-values":
        return ModeConfig(name="large-values", writers=max(2, base_writers))
    elif m == "large-batches":
        return ModeConfig(name="large-batches", writers=max(2, base_writers))
    elif m == "compaction":
        return ModeConfig(
            name="compaction",
            writers=max(2, base_writers),
            enable_compaction_thread=True,
        )
    elif m == "recovery-crash":
        return ModeConfig(
            name="recovery-crash",
            writers=max(2, base_writers),
            is_recovery_crash_mode=True,
        )
    elif m == "disk-full":
        return ModeConfig(
            name="disk-full",
            writers=max(2, base_writers),
            max_bytes=2 * 1024 * 1024,  # 2 MiB default within 1-4 MiB
        )
    elif m == "crash-points":
        return ModeConfig(
            name="crash-points",
            writers=max(2, base_writers),
            is_crash_point_mode=True,
        )
    elif m == "scale":
        return ModeConfig(
            name="scale",
            writers=1,
            is_scale_mode=True,
        )
    elif m == "contention":
        return ModeConfig(
            name="contention",
            writers=max(4, base_writers),
            shared_key_count=2,
            kill_min_ms=5,
            kill_max_ms=400,
        )
    elif m == "fuzz-corrupt":
        return ModeConfig(
            name="fuzz-corrupt",
            writers=max(2, base_writers),
            is_fault_injection_mode=True,
        )
    else:
        raise ValueError(f"Unknown torture mode: {mode_name}")

