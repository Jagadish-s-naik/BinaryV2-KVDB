"""Testing hooks for deterministic crash-point injection."""

import os
import sys
import signal

_CRASH_COUNTS: dict[str, int] = {}


def reset_crash_points() -> None:
    """Reset crash point hit counters."""
    _CRASH_COUNTS.clear()


def crash_point(name: str) -> None:
    """Trigger a crash if KVDB_CRASH_POINT matches the given name and nth count.
    
    Format of KVDB_CRASH_POINT: '<name>' or '<name>:<nth>' (where nth defaults to 1).
    """
    env_val = os.environ.get("KVDB_CRASH_POINT")
    if not env_val:
        return

    parts = env_val.split(":", 1)
    target_name = parts[0]
    nth = 1
    if len(parts) == 2:
        try:
            nth = int(parts[1])
        except ValueError:
            nth = 1

    if name != target_name:
        return

    count = _CRASH_COUNTS.get(name, 0) + 1
    _CRASH_COUNTS[name] = count

    if count == nth:
        # Immediate uncatchable kill without atexit or buffer flushes
        if sys.platform == "win32":
            os._exit(137)
        else:
            os.kill(os.getpid(), signal.SIGKILL)
