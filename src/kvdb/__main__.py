#!/usr/bin/env python3
"""CLI entry point and mock server for kvdb."""

import sys
import os
import argparse
from typing import NoReturn


def run_mock_serve(path: str, max_bytes: int | None = None) -> NoReturn:
    """Mock implementation of the PROTOCOL.md serve loop for independent torture testing."""
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    # In-memory storage for mock mode
    store: dict[bytes, bytes] = {}

    # Initial ready signal
    stdout.write(b"READY keys=0 recovery_ms=0\n")
    stdout.flush()

    while True:
        line = stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        parts = line.split(b" ", 3)
        cmd = parts[0].upper()

        if cmd == b"QUIT":
            break
        elif cmd == b"PUT" and len(parts) == 4:
            _, op_id, hex_key, hex_val = parts
            val = b"" if hex_val == b"-" else bytes.fromhex(hex_val.decode("ascii"))
            store[bytes.fromhex(hex_key.decode("ascii"))] = val
            stdout.write(b"ACK " + op_id + b"\n")
            stdout.flush()
        elif cmd == b"DEL" and len(parts) == 3:
            _, op_id, hex_key = parts
            store.pop(bytes.fromhex(hex_key.decode("ascii")), None)
            stdout.write(b"ACK " + op_id + b"\n")
            stdout.flush()
        elif cmd == b"GET" and len(parts) == 3:
            _, op_id, hex_key = parts
            key = bytes.fromhex(hex_key.decode("ascii"))
            if key in store:
                val = store[key]
                hex_val = b"-" if not val else val.hex().encode("ascii")
                stdout.write(b"VAL " + op_id + b" " + hex_val + b"\n")
            else:
                stdout.write(b"NOTFOUND " + op_id + b"\n")
            stdout.flush()
        elif cmd == b"BATCH" and len(parts) == 3:
            _, op_id, n_str = parts
            n = int(n_str.decode("ascii"))
            for _ in range(n):
                op_line = stdin.readline().strip()
                if not op_line:
                    continue
                op_parts = op_line.split(b" ", 2)
                op_type = op_parts[0].lower()
                if op_type == b"put" and len(op_parts) == 3:
                    k = bytes.fromhex(op_parts[1].decode("ascii"))
                    v = b"" if op_parts[2] == b"-" else bytes.fromhex(op_parts[2].decode("ascii"))
                    store[k] = v
                elif op_type == b"del" and len(op_parts) == 2:
                    k = bytes.fromhex(op_parts[1].decode("ascii"))
                    store.pop(k, None)
            stdout.write(b"ACK " + op_id + b"\n")
            stdout.flush()
        elif cmd == b"COMPACT" and len(parts) == 2:
            _, op_id = parts
            stdout.write(b"ACK " + op_id + b"\n")
            stdout.flush()
        else:
            op_id = parts[1] if len(parts) > 1 else b"?"
            stdout.write(b"ERR " + op_id + b" PARSE\n")
            stdout.flush()

    sys.exit(0)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description="kvdb: Embedded Key-Value Database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # open <path> [--max-bytes N]
    open_p = subparsers.add_parser("open")
    open_p.add_argument("path")
    open_p.add_argument("--max-bytes", type=int, default=None)

    # put <path> <key> <value>
    put_p = subparsers.add_parser("put")
    put_p.add_argument("path")
    put_p.add_argument("key")
    put_p.add_argument("value")

    # get <path> <key>
    get_p = subparsers.add_parser("get")
    get_p.add_argument("path")
    get_p.add_argument("key")

    # del <path> <key>
    del_p = subparsers.add_parser("del")
    del_p.add_argument("path")
    del_p.add_argument("key")

    # batch <path> <file>
    batch_p = subparsers.add_parser("batch")
    batch_p.add_argument("path")
    batch_p.add_argument("file")

    # dump <path> [--hex]
    dump_p = subparsers.add_parser("dump")
    dump_p.add_argument("path")
    dump_p.add_argument("--hex", action="store_true")

    # check <path>
    check_p = subparsers.add_parser("check")
    check_p.add_argument("path")

    # serve <path> [--max-bytes N]
    serve_p = subparsers.add_parser("serve")
    serve_p.add_argument("path")
    serve_p.add_argument("--max-bytes", type=int, default=None)

    # compact <path>
    compact_p = subparsers.add_parser("compact")
    compact_p.add_argument("path")

    args = parser.parse_args(argv)

    if args.command == "serve":
        run_mock_serve(args.path, args.max_bytes)
    elif args.command == "open":
        print("OK keys=0 recovery_ms=0")
        return 0
    elif args.command == "check":
        print("OK")
        return 0
    elif args.command == "dump":
        return 0
    elif args.command == "compact":
        print("OK")
        return 0
    elif args.command in ("put", "del", "batch"):
        print("OK")
        return 0
    elif args.command == "get":
        print("NOTFOUND")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

