#!/usr/bin/env python3
"""CLI entry point and server launcher for kvdb."""

import argparse
import sys

from kvdb.cli import (
    cmd_batch,
    cmd_check,
    cmd_compact,
    cmd_del,
    cmd_dump,
    cmd_get,
    cmd_open,
    cmd_put,
)
from kvdb.serve import run_serve


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
        run_serve(args.path, args.max_bytes)
    elif args.command == "open":
        return cmd_open(args.path, args.max_bytes)
    elif args.command == "put":
        return cmd_put(args.path, args.key, args.value)
    elif args.command == "get":
        return cmd_get(args.path, args.key)
    elif args.command == "del":
        return cmd_del(args.path, args.key)
    elif args.command == "batch":
        return cmd_batch(args.path, args.file)
    elif args.command == "dump":
        return cmd_dump(args.path, args.hex)
    elif args.command == "check":
        return cmd_check(args.path)
    elif args.command == "compact":
        return cmd_compact(args.path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
