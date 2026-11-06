"""CLI command implementations for kvdb (README.md, PROTOCOL.md Section 6)."""

from __future__ import annotations

import os
import sys

from kvdb.check import check
from kvdb.db import DB
from kvdb.errors import DBPoisonedError, DiskFullError
from kvdb.frame import OP_TYPE_DEL, OP_TYPE_PUT


def cmd_open(path: str, max_bytes: int | None = None) -> int:
    """Open or create the database, run recovery, print OK keys=<n> recovery_ms=<t>."""
    try:
        db = DB.open(path, max_bytes=max_bytes)
        keys_count = len(db)
        rec_ms = db.recovery_ms
        db.close()
        print(f"OK keys={keys_count} recovery_ms={rec_ms}")
        return 0
    except Exception as e:
        print(f"ERROR INTERNAL {e}")
        return 1


def cmd_put(path: str, key: str, value: str) -> int:
    """Store a value durably."""
    try:
        k_bytes = key.encode("utf-8")
        v_bytes = value.encode("utf-8")
        with DB.open(path) as db:
            db.put(k_bytes, v_bytes)
        print("OK")
        return 0
    except DiskFullError as e:
        print(f"ERROR DISK_FULL {e}")
        return 1
    except ValueError as e:
        print(f"ERROR BAD_ARG {e}")
        return 1
    except DBPoisonedError as e:
        print(f"ERROR POISONED {e}")
        return 1
    except Exception as e:
        print(f"ERROR INTERNAL {e}")
        return 1


def cmd_get(path: str, key: str) -> int:
    """Print the value, or NOTFOUND."""
    try:
        k_bytes = key.encode("utf-8")
        with DB.open(path) as db:
            val = db.get(k_bytes)
        if val is None:
            print("NOTFOUND")
        else:
            try:
                print(val.decode("utf-8"))
            except UnicodeDecodeError:
                sys.stdout.buffer.write(val + b"\n")
                sys.stdout.buffer.flush()
        return 0
    except Exception as e:
        print(f"ERROR INTERNAL {e}")
        return 1


def cmd_del(path: str, key: str) -> int:
    """Delete a key."""
    try:
        k_bytes = key.encode("utf-8")
        with DB.open(path) as db:
            db.delete(k_bytes)
        print("OK")
        return 0
    except DiskFullError as e:
        print(f"ERROR DISK_FULL {e}")
        return 1
    except Exception as e:
        print(f"ERROR INTERNAL {e}")
        return 1


def cmd_batch(path: str, batch_file: str) -> int:
    """Apply a file of operations atomically."""
    if not os.path.exists(batch_file):
        print(f"ERROR BAD_ARG Batch file not found: {batch_file}")
        return 1

    ops: list[tuple[int, bytes, bytes]] = []
    try:
        with open(batch_file, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                parts = line.split(" ", 2)
                op_cmd = parts[0].lower()
                if op_cmd == "put" and len(parts) >= 3:
                    k = parts[1].encode("utf-8")
                    v = parts[2].encode("utf-8")
                    ops.append((OP_TYPE_PUT, k, v))
                elif op_cmd == "del" and len(parts) >= 2:
                    k = parts[1].encode("utf-8")
                    ops.append((OP_TYPE_DEL, k, b""))
                else:
                    print(f"ERROR PARSE Malformed batch operation at line {line_no}")
                    return 1

        with DB.open(path) as db:
            db.batch(ops)
        print("OK")
        return 0
    except DiskFullError as e:
        print(f"ERROR DISK_FULL {e}")
        return 1
    except ValueError as e:
        print(f"ERROR BAD_ARG {e}")
        return 1
    except Exception as e:
        print(f"ERROR INTERNAL {e}")
        return 1


def cmd_dump(path: str, hex_mode: bool = False) -> int:
    """Print every key and value, sorted by key."""
    try:
        with DB.open(path) as db:
            items = db.items()

        for k, v in items:
            if hex_mode:
                hex_k = k.hex()
                hex_v = "-" if not v else v.hex()
                print(f"{hex_k} {hex_v}")
            else:
                k_str = k.decode("utf-8", errors="backslashreplace")
                v_str = v.decode("utf-8", errors="backslashreplace")
                print(f"{k_str} {v_str}")
        return 0
    except Exception as e:
        print(f"ERROR INTERNAL {e}")
        return 1


def cmd_check(path: str) -> int:
    """Verify file integrity, print OK or reason it failed."""
    ok, msg = check(path)
    print(msg)
    return 0 if ok else 1


def cmd_compact(path: str) -> int:
    """Rewrite the file with only live data so it can shrink."""
    try:
        with DB.open(path) as db:
            db.compact()
        print("OK")
        return 0
    except Exception as e:
        print(f"ERROR INTERNAL {e}")
        return 1
