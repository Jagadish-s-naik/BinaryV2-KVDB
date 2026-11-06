#!/usr/bin/env python3
"""Binary WAL & Timeline Visualizer for kvdb.

Inspects on-disk database files (.db) and correlates them with oracle logs (.log):
- Analyzes 24-byte binary headers and frame CRCs per FORMAT.md
- Decodes all frames, payloads, operations (PUT/DEL), and byte offsets
- Computes dead space and tombstone ratios to evaluate compaction health
- Reconstructs chronological operation timelines from oracle.log
- Outputs human-readable terminal dashboards or machine-readable JSON
"""

import os
import sys
import json
import zlib
import struct
import argparse
from pathlib import Path
from typing import Any

MAGIC = b"KVDBLOG\x00"
HEADER_FMT = "<8sIQI"
OP_HDR_FMT = "<BHI"


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.2f} KiB"
    else:
        return f"{n / (1024 * 1024):.2f} MiB"


def inspect_db(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Database file not found: {path}")

    raw = path.read_bytes()
    file_size = len(raw)

    result: dict[str, Any] = {
        "file_path": str(path),
        "file_size": file_size,
        "file_size_formatted": format_bytes(file_size),
        "header": {},
        "frames": [],
        "summary": {
            "total_frames": 0,
            "valid_frames": 0,
            "corrupt_frames": 0,
            "total_puts": 0,
            "total_dels": 0,
            "live_keys_count": 0,
            "tombstones_count": 0,
            "dead_payload_bytes": 0,
            "total_payload_bytes": 0,
            "fragmentation_percent": 0.0,
            "torn_tail_bytes": 0,
        },
        "live_index": {},
    }

    if file_size == 0:
        result["status"] = "EMPTY"
        return result

    if file_size < 24:
        result["status"] = "CORRUPT_HEADER_TOO_SHORT"
        return result

    # Parse Header
    magic = raw[:8]
    version, generation, header_crc = struct.unpack("<IQI", raw[8:24])
    calc_header_crc = zlib.crc32(raw[:20]) & 0xFFFFFFFF
    header_valid = (magic == MAGIC) and (calc_header_crc == header_crc) and (version == 1)

    result["header"] = {
        "magic": magic.decode("latin1", errors="replace"),
        "version": version,
        "generation": generation,
        "header_crc": f"{header_crc:#010x}",
        "calculated_crc": f"{calc_header_crc:#010x}",
        "valid": header_valid,
    }

    if not header_valid:
        result["status"] = "INVALID_HEADER"
        return result

    # Parse Frames
    offset = 24
    frame_idx = 0
    live_index: dict[str, dict[str, Any]] = {}
    history_per_key: dict[str, list[dict[str, Any]]] = {}

    while offset < file_size:
        rem = file_size - offset
        if rem < 8:
            result["summary"]["torn_tail_bytes"] = rem
            break

        frame_len, frame_crc = struct.unpack("<II", raw[offset : offset + 8])
        if offset + 8 + frame_len > file_size:
            result["summary"]["torn_tail_bytes"] = rem
            break

        payload = raw[offset + 8 : offset + 8 + frame_len]
        calc_crc = zlib.crc32(raw[offset : offset + 4] + payload) & 0xFFFFFFFF
        crc_ok = (frame_crc == calc_crc)

        frame_info: dict[str, Any] = {
            "index": frame_idx,
            "offset": offset,
            "frame_len": frame_len,
            "crc": f"{frame_crc:#010x}",
            "crc_valid": crc_ok,
            "ops": [],
        }

        if not crc_ok:
            result["summary"]["corrupt_frames"] += 1
            result["summary"]["torn_tail_bytes"] = file_size - offset
            result["frames"].append(frame_info)
            break

        result["summary"]["valid_frames"] += 1
        result["summary"]["total_payload_bytes"] += frame_len

        if frame_len >= 5:
            kind = payload[0]
            op_count = struct.unpack("<I", payload[1:5])[0]
            frame_info["kind"] = kind
            frame_info["op_count"] = op_count

            if kind == 1:
                p_off = 5
                for op_i in range(op_count):
                    if p_off + 7 > frame_len:
                        break
                    op_type, k_len, v_len = struct.unpack("<BHI", payload[p_off : p_off + 7])
                    p_off += 7
                    key_bytes = payload[p_off : p_off + k_len]
                    p_off += k_len
                    val_bytes = payload[p_off : p_off + v_len]
                    p_off += v_len

                    key_str = key_bytes.decode("utf-8", errors="replace")
                    is_put = (op_type == 1)

                    op_meta = {
                        "frame_index": frame_idx,
                        "op_index": op_i,
                        "type": "PUT" if is_put else "DEL",
                        "key": key_str,
                        "val_len": v_len,
                        "val_preview": val_bytes[:32].decode("utf-8", errors="replace"),
                        "val_offset": offset + 8 + p_off - v_len,
                    }
                    frame_info["ops"].append(op_meta)

                    if is_put:
                        result["summary"]["total_puts"] += 1
                        live_index[key_str] = op_meta
                    else:
                        result["summary"]["total_dels"] += 1
                        live_index.pop(key_str, None)

                    if key_str not in history_per_key:
                        history_per_key[key_str] = []
                    history_per_key[key_str].append(op_meta)

        result["frames"].append(frame_info)
        offset += 8 + frame_len
        frame_idx += 1

    result["summary"]["total_frames"] = frame_idx
    result["summary"]["live_keys_count"] = len(live_index)

    # Calculate dead bytes (overwrites and deletes)
    dead_bytes = 0
    for key, history in history_per_key.items():
        if key not in live_index:
            # Entire key was deleted: all its past PUTs are dead
            for op in history:
                if op["type"] == "PUT":
                    dead_bytes += 7 + len(key.encode("utf-8")) + op["val_len"]
        else:
            # Key is alive: all but the latest PUT are dead
            for op in history[:-1]:
                if op["type"] == "PUT":
                    dead_bytes += 7 + len(key.encode("utf-8")) + op["val_len"]

    result["summary"]["dead_payload_bytes"] = dead_bytes
    total_payload = result["summary"]["total_payload_bytes"]
    if total_payload > 0:
        frag_pct = (dead_bytes / total_payload) * 100
        result["summary"]["fragmentation_percent"] = round(frag_pct, 1)

    result["live_index"] = live_index
    result["status"] = "OK"
    return result


def inspect_timeline(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        raise FileNotFoundError(f"Oracle log not found: {log_path}")

    timeline = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(" ")
            action = parts[0]
            if action in ("SENT", "ACK", "ERR"):
                entry = {
                    "action": action,
                    "id": parts[1] if len(parts) > 1 else "?",
                    "timestamp_ns": int(parts[-1]) if parts[-1].isdigit() else 0,
                    "raw": line,
                }
                timeline.append(entry)

    # Sort by timestamp
    timeline.sort(key=lambda x: x["timestamp_ns"])
    return timeline


def print_cli_report(data: dict[str, Any], show_frames: bool = False, show_keys: bool = False) -> None:
    print("=" * 70)
    print("             KVDB BINARY WAL & STORAGE INSPECTOR")
    print("=" * 70)
    print(f"Database File: {data['file_path']}")
    print(f"File Size:     {data['file_size_formatted']} ({data['file_size']} bytes)")
    print(f"Status:        {data['status']}")

    if "header" in data and data["header"]:
        hdr = data["header"]
        valid_mark = "[VALID]" if hdr["valid"] else "[CORRUPTED]"
        print(f"\n--- 24-Byte Header {valid_mark} ---")
        print(f"  Magic:       {hdr['magic']!r}")
        print(f"  Version:     {hdr['version']}")
        print(f"  Generation:  {hdr['generation']}")
        print(f"  Header CRC:  {hdr['header_crc']} (calculated: {hdr['calculated_crc']})")

    s = data["summary"]
    print(f"\n--- Storage Health & Metrics ---")
    print(f"  Total Frames:       {s['total_frames']} (Valid: {s['valid_frames']}, Corrupt: {s['corrupt_frames']})")
    print(f"  Operations:         PUT: {s['total_puts']} | DEL: {s['total_dels']}")
    print(f"  Live Keys:          {s['live_keys_count']}")
    print(f"  Dead Space:         {format_bytes(s['dead_payload_bytes'])} ({s['fragmentation_percent']}% fragmented)")
    print(f"  Torn Tail Bytes:    {s['torn_tail_bytes']} bytes at EOF")

    if s["fragmentation_percent"] >= 30.0:
        print(f"  Compaction advice:  RECOMMENDED (fragmentation > 30%)")
    else:
        print(f"  Compaction advice:  CLEAN (compaction not urgent)")

    if show_frames and data["frames"]:
        print(f"\n--- Frame Breakdown ({len(data['frames'])} frames) ---")
        print(f"{'Idx':>4} | {'Offset':>8} | {'Len':>8} | {'CRC':>10} | {'Ops':>4} | {'Ops Preview'}")
        print("-" * 70)
        for f in data["frames"]:
            crc_badge = "OK" if f["crc_valid"] else "FAIL"
            ops_preview = ", ".join(f"{op['type']}({op['key']})" for op in f["ops"][:3])
            if len(f["ops"]) > 3:
                ops_preview += f" ... (+{len(f['ops'])-3})"
            print(f"{f['index']:>4} | {f['offset']:>8} | {f['frame_len']:>8} | {f['crc']:>10} | {len(f['ops']):>4} | {ops_preview}")

    if show_keys and data["live_index"]:
        print(f"\n--- Live Key Index ({len(data['live_index'])} keys) ---")
        for k, meta in sorted(data["live_index"].items()):
            print(f"  {k} -> len={meta['val_len']} offset={meta['val_offset']} val='{meta['val_preview']}'")

    print("=" * 70)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect kvdb binary storage and oracle logs.")
    parser.add_argument("db_path", help="Path to .db file")
    parser.add_argument("--frames", action="store_true", help="Print table of all frames")
    parser.add_argument("--keys", action="store_true", help="Print table of live keys")
    parser.add_argument("--timeline", type=str, default=None, help="Path to oracle.log to correlate")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")

    args = parser.parse_args(argv)

    db_path = Path(args.db_path)
    try:
        report = inspect_db(db_path)
    except Exception as e:
        print(f"Error inspecting database: {e}", file=sys.stderr)
        return 1

    if args.timeline:
        try:
            tl = inspect_timeline(Path(args.timeline))
            report["timeline_events"] = len(tl)
            report["timeline_sample"] = tl[:20]
        except Exception as e:
            print(f"Warning: failed to read timeline: {e}", file=sys.stderr)

    if args.json:
        # Don't serialize non-json structures
        print(json.dumps(report, indent=2))
    else:
        print_cli_report(report, show_frames=args.frames, show_keys=args.keys)
        if args.timeline:
            print(f"\n--- Timeline Log Analysis ({report.get('timeline_events', 0)} events) ---")
            for evt in report.get("timeline_sample", []):
                print(f"  [{evt['timestamp_ns']}] {evt['action']:<4} {evt['id']}")
            if report.get("timeline_events", 0) > 20:
                print(f"  ... (+{report['timeline_events'] - 20} more events)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

