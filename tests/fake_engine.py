"""Fake broken engine for meta-testing the torture tool.

Per docs/TORTURE_SPEC.md Section 10:
The torture tool must report FAIL when run against broken engines injecting:
1. ACKs before writing (lost write / dropped write).
2. Partial batch application.
3. Truncated value byte.
4. Value from another key.
5. Put-then-delete wrong order in batch.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        sys.exit(2)

    subcmd = sys.argv[1]
    bug = os.environ.get("FAKE_ENGINE_BUG", "none")

    # File on disk stores the data directly
    db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("fake.db")
    state_file = db_path

    if subcmd == "serve":
        # Handle commands from stdin
        store: dict[str, bytes] = {}
        if state_file.exists():
            for line_raw in state_file.read_bytes().splitlines():
                try:
                    line = line_raw.decode("utf-8").strip()
                except UnicodeDecodeError:
                    break
                if not line:
                    continue
                p = line.split(" ")
                if len(p) >= 2:
                    try:
                        k = bytes.fromhex(p[0]).decode("utf-8")
                        v = bytes.fromhex(p[1]) if p[1] != "-" else b""
                        store[k] = v
                    except ValueError:
                        pass

        # Print handshake
        sys.stdout.buffer.write(f"READY keys={len(store)} recovery_ms=5\n".encode("ascii"))
        sys.stdout.buffer.flush()

        max_bytes = None
        if "--max-bytes" in sys.argv:
            idx = sys.argv.index("--max-bytes")
            if idx + 1 < len(sys.argv):
                try:
                    max_bytes = int(sys.argv[idx + 1])
                except ValueError:
                    pass

        def _can_fit(target: dict[str, bytes]) -> bool:
            if max_bytes is not None:
                sz = sum(len(k.encode().hex()) + 1 + (len(v.hex()) if v else 1) + 1 for k, v in target.items())
                if sz > max_bytes:
                    return False
            return True

        def _persist() -> None:
            tmp = state_file.with_name(state_file.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as sf:
                for k, v in store.items():
                    sf.write(f"{k.encode().hex()} {v.hex() if v else '-'}\n")
                sf.flush()
                os.fsync(sf.fileno())
            os.replace(tmp, state_file)

        while True:
            line_bytes = sys.stdin.buffer.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("ascii", errors="replace").strip()
            if not line:
                continue

            parts = line.split(" ")
            tag = parts[0].upper()

            if tag == "PUT" and len(parts) >= 4:
                op_id, k_hex, v_hex = parts[1], parts[2], parts[3]
                k = bytes.fromhex(k_hex).decode("utf-8", errors="replace")
                v = bytes.fromhex(v_hex) if v_hex != "-" else b""
                
                # Check disk full before committing
                candidate = dict(store)
                candidate[k] = v
                if not _can_fit(candidate):
                    sys.stdout.buffer.write(f"ERR {op_id} DISK_FULL\n".encode("ascii"))
                    sys.stdout.buffer.flush()
                    continue

                store[k] = v

                # Inject Bug 1: lost write (ACK but don't actually keep in store)
                if bug == "lost_write":
                    store.pop(k, None)

                # Inject Bug 3: truncated value by 1 byte
                if bug == "truncate_val" and len(v) > 0:
                    store[k] = v[:-1]

                # Inject Bug 4: assign value of another key
                if bug == "another_key_val":
                    store[k] = b"alien_key_corrupted_value_12345678"

                _persist()

                sys.stdout.buffer.write(f"ACK {op_id}\n".encode("ascii"))
                sys.stdout.buffer.flush()

            elif tag == "DEL" and len(parts) >= 3:
                op_id, k_hex = parts[1], parts[2]
                k = bytes.fromhex(k_hex).decode("utf-8", errors="replace")
                store.pop(k, None)
                _persist()
                sys.stdout.buffer.write(f"ACK {op_id}\n".encode("ascii"))
                sys.stdout.buffer.flush()

            elif tag == "BATCH" and len(parts) >= 3:
                op_id, n = parts[1], int(parts[2])
                batch_ops = []
                for _ in range(n):
                    sub_line = sys.stdin.buffer.readline().decode("ascii").strip()
                    sub_parts = sub_line.split(" ")
                    batch_ops.append(sub_parts)

                # Inject Bug 2: Partial batch (apply only first op if more than 1)
                if bug == "partial_batch" and len(batch_ops) > 1:
                    batch_ops = batch_ops[:1]

                # Apply ops
                for sub in batch_ops:
                    stype = sub[0].lower()
                    if stype == "put":
                        sk = bytes.fromhex(sub[1]).decode("utf-8")
                        sv = bytes.fromhex(sub[2]) if sub[2] != "-" else b""
                        store[sk] = sv
                    elif stype == "del":
                        sk = bytes.fromhex(sub[1]).decode("utf-8")
                        # Inject Bug 5: put-then-del wrong order (ignore del so key stays present)
                        if bug == "wrong_batch_order":
                            pass
                        else:
                            store.pop(sk, None)

                _persist()
                sys.stdout.buffer.write(f"ACK {op_id}\n".encode("ascii"))
                sys.stdout.buffer.flush()

            elif tag == "GET" and len(parts) >= 3:
                req_id, k_hex = parts[1], parts[2]
                k = bytes.fromhex(k_hex).decode("utf-8")
                if k in store:
                    sys.stdout.buffer.write(f"VAL {req_id} {store[k].hex()}\n".encode("ascii"))
                else:
                    sys.stdout.buffer.write(f"NOTFOUND {req_id}\n".encode("ascii"))
                sys.stdout.buffer.flush()

            elif tag == "COMPACT" and len(parts) >= 2:
                sys.stdout.buffer.write(f"ACK {parts[1]}\n".encode("ascii"))
                sys.stdout.buffer.flush()

            elif tag == "QUIT":
                break

            # Persist store to state_file
            _persist()

    elif subcmd == "open":
        count = 0
        if state_file.exists():
            raw_data = state_file.read_bytes()
            valid_lines = []
            for line in raw_data.splitlines():
                try:
                    text_line = line.decode("utf-8").strip()
                    if text_line and len(text_line.split(" ")) >= 2:
                        valid_lines.append(text_line)
                except UnicodeDecodeError:
                    # Torn tail at EOF truncated during recovery
                    break
            # Persist recovered data
            with open(state_file, "w", encoding="utf-8") as sf:
                for vl in valid_lines:
                    sf.write(vl + "\n")
            count = len(valid_lines)
        sys.stdout.write(f"OK keys={count} recovery_ms=5\n")
        sys.stdout.flush()

    elif subcmd == "check":
        sys.stdout.write("OK\n")
        sys.stdout.flush()

    elif subcmd == "dump":
        if state_file.exists():
            with open(state_file, "r", encoding="utf-8") as sf:
                sys.stdout.write(sf.read())
        sys.stdout.flush()

    elif subcmd == "compact":
        sys.stdout.write("OK\n")
        sys.stdout.flush()

    elif subcmd == "get":
        target_k = sys.argv[3] if len(sys.argv) > 3 else ""
        store = {}
        if state_file.exists():
            with open(state_file, "r", encoding="utf-8") as sf:
                for line in sf:
                    p = line.strip().split(" ")
                    if len(p) >= 2:
                        try:
                            k = bytes.fromhex(p[0]).decode("utf-8")
                            v = bytes.fromhex(p[1]) if p[1] != "-" else b""
                            store[k] = v
                        except ValueError:
                            pass
        if target_k in store:
            val_bytes = store[target_k]
            sys.stdout.buffer.write(val_bytes + b"\n")
        else:
            sys.stdout.write("NOTFOUND\n")
        sys.stdout.flush()

    elif subcmd == "put":
        sys.stdout.write("OK\n")
        sys.stdout.flush()

    elif subcmd == "del":
        sys.stdout.write("OK\n")
        sys.stdout.flush()

    elif subcmd == "batch":
        sys.stdout.write("OK\n")
        sys.stdout.flush()

    sys.exit(0)


if __name__ == "__main__":
    main()

