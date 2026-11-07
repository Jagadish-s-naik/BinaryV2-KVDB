"""Multi-threaded serve protocol implementation for kvdb (PROTOCOL.md)."""

from __future__ import annotations

import queue
import sys
import threading
from typing import NoReturn

from kvdb.db import DB
from kvdb.errors import DBPoisonedError, DiskFullError
from kvdb.frame import OP_TYPE_DEL, OP_TYPE_PUT


def run_serve(path: str, max_bytes: int | None = None) -> NoReturn:
    """Run the multi-threaded serve command loop on binary stdin/stdout."""
    stdout_lock = threading.Lock()

    def reply(data: bytes) -> None:
        with stdout_lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    try:
        db = DB.open(path, max_bytes=max_bytes)
    except Exception as e:
        reply(f"FATAL OPEN {e}\n".encode("ascii"))
        sys.exit(1)

    # Initial ready signal (PROTOCOL.md Section 2)
    reply(f"READY keys={len(db)} recovery_ms={db.recovery_ms}\n".encode("ascii"))

    work_queue: queue.Queue[tuple | None] = queue.Queue(maxsize=256)

    def worker() -> None:
        while True:
            item = work_queue.get()
            if item is None:
                work_queue.task_done()
                break

            cmd, op_id, args = item
            try:
                if cmd == "PUT":
                    k, v = args
                    db.put(k, v)
                    reply(b"ACK " + op_id + b"\n")
                elif cmd == "DEL":
                    k = args[0]
                    db.delete(k)
                    reply(b"ACK " + op_id + b"\n")
                elif cmd == "GET":
                    k = args[0]
                    val = db.get(k)
                    if val is None:
                        reply(b"NOTFOUND " + op_id + b"\n")
                    else:
                        hex_v = b"-" if not val else val.hex().encode("ascii")
                        reply(b"VAL " + op_id + b" " + hex_v + b"\n")
                elif cmd == "BATCH":
                    ops = args[0]
                    db.batch(ops)
                    reply(b"ACK " + op_id + b"\n")
                elif cmd == "COMPACT":
                    db.compact()
                    reply(b"ACK " + op_id + b"\n")
                elif cmd == "CHECKPOINT":
                    db.checkpoint()
                    reply(b"ACK " + op_id + b"\n")
            except DiskFullError:
                reply(b"ERR " + op_id + b" DISK_FULL\n")
            except ValueError:
                reply(b"ERR " + op_id + b" BAD_ARG\n")
            except DBPoisonedError:
                reply(b"ERR " + op_id + b" POISONED\n")
            except Exception:
                reply(b"ERR " + op_id + b" INTERNAL\n")
            finally:
                work_queue.task_done()

    # 8 worker threads per PROTOCOL.md Section 4
    num_workers = 8
    workers = [threading.Thread(target=worker, daemon=True) for _ in range(num_workers)]
    for w in workers:
        w.start()

    stdin = sys.stdin.buffer

    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        parts = line.split(b" ")
        cmd = parts[0].upper()

        if cmd == b"QUIT":
            break
        elif cmd == b"PUT":
            if len(parts) != 4:
                op_id = parts[1] if len(parts) > 1 else b"?"
                reply(b"ERR " + op_id + b" PARSE\n")
                continue
            _, op_id, hex_key, hex_val = parts
            try:
                k = bytes.fromhex(hex_key.decode("ascii"))
                v = b"" if hex_val == b"-" else bytes.fromhex(hex_val.decode("ascii"))
                work_queue.put(("PUT", op_id, (k, v)))
            except Exception:
                reply(b"ERR " + op_id + b" PARSE\n")

        elif cmd == b"DEL":
            if len(parts) != 3:
                op_id = parts[1] if len(parts) > 1 else b"?"
                reply(b"ERR " + op_id + b" PARSE\n")
                continue
            _, op_id, hex_key = parts
            try:
                k = bytes.fromhex(hex_key.decode("ascii"))
                work_queue.put(("DEL", op_id, (k,)))
            except Exception:
                reply(b"ERR " + op_id + b" PARSE\n")

        elif cmd == b"GET":
            if len(parts) != 3:
                op_id = parts[1] if len(parts) > 1 else b"?"
                reply(b"ERR " + op_id + b" PARSE\n")
                continue
            _, op_id, hex_key = parts
            try:
                k = bytes.fromhex(hex_key.decode("ascii"))
                work_queue.put(("GET", op_id, (k,)))
            except Exception:
                reply(b"ERR " + op_id + b" PARSE\n")

        elif cmd == b"COMPACT":
            if len(parts) != 2:
                op_id = parts[1] if len(parts) > 1 else b"?"
                reply(b"ERR " + op_id + b" PARSE\n")
                continue
            _, op_id = parts
            work_queue.put(("COMPACT", op_id, ()))

        elif cmd == b"CHECKPOINT":
            if len(parts) != 2:
                op_id = parts[1] if len(parts) > 1 else b"?"
                reply(b"ERR " + op_id + b" PARSE\n")
                continue
            _, op_id = parts
            work_queue.put(("CHECKPOINT", op_id, ()))

        elif cmd == b"BATCH":
            if len(parts) != 3:
                op_id = parts[1] if len(parts) > 1 else b"?"
                reply(b"ERR " + op_id + b" PARSE\n")
                continue
            _, op_id, n_str = parts
            try:
                n = int(n_str.decode("ascii"))
            except ValueError:
                reply(b"ERR " + op_id + b" PARSE\n")
                continue

            if n <= 0 or n > 10000:
                reply(b"ERR " + op_id + b" BAD_ARG\n")
                continue

            batch_ops: list[tuple[int, bytes, bytes]] = []
            parse_error = False

            for _ in range(n):
                op_line = stdin.readline()
                if not op_line:
                    parse_error = True
                    break
                op_line = op_line.strip()
                op_parts = op_line.split(b" ")
                sub_cmd = op_parts[0].lower()

                try:
                    if sub_cmd == b"put" and len(op_parts) == 3:
                        sk = bytes.fromhex(op_parts[1].decode("ascii"))
                        sv = b"" if op_parts[2] == b"-" else bytes.fromhex(op_parts[2].decode("ascii"))
                        batch_ops.append((OP_TYPE_PUT, sk, sv))
                    elif sub_cmd == b"del" and len(op_parts) == 2:
                        sk = bytes.fromhex(op_parts[1].decode("ascii"))
                        batch_ops.append((OP_TYPE_DEL, sk, b""))
                    else:
                        parse_error = True
                except Exception:
                    parse_error = True

            if parse_error:
                reply(b"ERR " + op_id + b" PARSE\n")
            else:
                work_queue.put(("BATCH", op_id, (batch_ops,)))

        else:
            op_id = parts[1] if len(parts) > 1 else b"?"
            reply(b"ERR " + op_id + b" PARSE\n")

    # Clean shutdown: drain queue, stop workers, close database
    for _ in range(num_workers):
        work_queue.put(None)
    for w in workers:
        w.join()

    db.close()
    sys.exit(0)
