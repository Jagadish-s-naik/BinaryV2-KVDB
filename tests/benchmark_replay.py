#!/usr/bin/env python3
"""Hour-1 Replay Benchmark: Replaying 1,000,000 keys from disk.

Tests both ingestion styles:
  Case A: 1,000 batch frames of 1,000 keys each (1M total keys)
  Case B: 1,000,000 individual put frames (1M total frames)

Determines whether replay time stays under the 1.5s threshold or if
checkpoint frames (kind = 2) are required.
"""

import os
import sys
import time
import zlib
import struct
import tempfile
import tracemalloc
from pathlib import Path

MAGIC = b"KVDBLOG\x00"
HEADER_FMT = "<8sIQI"  # magic (8), version (4), generation (8), header_crc (4)
FRAME_HDR_FMT = "<II"  # frame_len (4), crc32 (4)
OP_HDR_FMT = "<BHI"    # type (1), key_len (2), val_len (4)


def create_header() -> bytes:
    prefix = struct.pack("<8sIQ", MAGIC, 1, 1)
    crc = zlib.crc32(prefix) & 0xFFFFFFFF
    return prefix + struct.pack("<I", crc)


def build_frame(ops: list[tuple[int, bytes, bytes]]) -> bytes:
    # kind (1), op_count (4)
    payload_parts = [struct.pack("<BI", 1, len(ops))]
    for op_type, key, val in ops:
        payload_parts.append(struct.pack(OP_HDR_FMT, op_type, len(key), len(val)))
        payload_parts.append(key)
        payload_parts.append(val)
    payload = b"".join(payload_parts)
    frame_len = len(payload)
    len_bytes = struct.pack("<I", frame_len)
    crc = zlib.crc32(payload, zlib.crc32(len_bytes)) & 0xFFFFFFFF
    return len_bytes + struct.pack("<I", crc) + payload


def generate_benchmark_file(path: str, total_keys: int, batch_size: int) -> None:
    with open(path, "wb") as f:
        f.write(create_header())
        ops_batch = []
        for i in range(total_keys):
            key = f"key_{i:07d}".encode("ascii")
            val = f"val_{i}".encode("ascii")
            ops_batch.append((1, key, val))
            if len(ops_batch) == batch_size:
                f.write(build_frame(ops_batch))
                ops_batch = []
        if ops_batch:
            f.write(build_frame(ops_batch))


def replay_log(path: str) -> tuple[int, int]:
    """Replay log file from disk, verifying every CRC and building index."""
    index: dict[bytes, tuple[int, int]] = {}
    file_size = os.path.getsize(path)

    with open(path, "rb") as f:
        hdr_bytes = f.read(24)
        if len(hdr_bytes) < 24:
            raise ValueError("File too short for header")
        magic, ver, gen, crc = struct.unpack(HEADER_FMT, hdr_bytes)
        if magic != MAGIC or ver != 1:
            raise ValueError("Invalid header magic/version")
        expected_crc = zlib.crc32(hdr_bytes[:20]) & 0xFFFFFFFF
        if crc != expected_crc:
            raise ValueError("Corrupt header CRC")

        offset = 24
        # Read the remainder into memory or stream chunked
        # For raw speed, mmap or reading entire file
        buf = f.read()

    mv = memoryview(buf)
    pos = 0
    buf_len = len(mv)

    while pos + 8 <= buf_len:
        frame_len, frame_crc = struct.unpack_from("<II", mv, pos)
        if pos + 8 + frame_len > buf_len:
            # Torn tail at EOF
            break

        # Check CRC: covers frame_len (4 bytes) + payload (frame_len bytes)
        # mv[pos:pos+4] is frame_len bytes
        # mv[pos+8:pos+8+frame_len] is payload
        calc_crc = zlib.crc32(mv[pos + 8 : pos + 8 + frame_len], zlib.crc32(mv[pos : pos + 4])) & 0xFFFFFFFF
        if calc_crc != frame_crc:
            # Corrupted / torn frame
            break

        payload_mv = mv[pos + 8 : pos + 8 + frame_len]
        kind, op_count = struct.unpack_from("<BI", payload_mv, 0)
        if kind == 1:
            p_pos = 5
            for _ in range(op_count):
                op_type, k_len, v_len = struct.unpack_from(OP_HDR_FMT, payload_mv, p_pos)
                p_pos += 7
                k = bytes(payload_mv[p_pos : p_pos + k_len])
                p_pos += k_len
                val_offset = offset + pos + 8 + p_pos
                p_pos += v_len
                if op_type == 1:
                    index[k] = (val_offset, v_len)
                elif op_type == 2:
                    index.pop(k, None)

        pos += 8 + frame_len

    return len(index), file_size


def benchmark():
    total_keys = 1_000_000
    print(f"=== kvdb 1,000,000 Keys Replay Benchmark (Python {sys.version.split()[0]}) ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Case A: 1,000 batches of 1,000 ops
        path_a = os.path.join(tmpdir, "bench_batch.db")
        print(f"\n1. Generating Case A: {total_keys:,} keys in 1,000 batches (batch_size=1,000)...")
        t0 = time.perf_counter()
        generate_benchmark_file(path_a, total_keys, batch_size=1000)
        gen_time_a = time.perf_counter() - t0
        size_a = os.path.getsize(path_a)
        print(f"   Generated in {gen_time_a:.2f}s ({size_a / (1024*1024):.2f} MB)")

        print("   Replaying Case A (CRC validation + in-memory indexing)...")
        tracemalloc.start()
        t0 = time.perf_counter()
        count_a, _ = replay_log(path_a)
        t_replay_a = time.perf_counter() - t0
        _, peak_a = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"   Result: {count_a:,} keys replayed in {t_replay_a:.4f}s")
        print(f"   Peak RAM: {peak_a / (1024*1024):.2f} MB")
        print(f"   Throughput: {total_keys / t_replay_a:,.0f} keys/s")

        # Case B: 1,000,000 individual frames
        path_b = os.path.join(tmpdir, "bench_individual.db")
        print(f"\n2. Generating Case B: {total_keys:,} keys as 1,000,000 individual frames...")
        t0 = time.perf_counter()
        generate_benchmark_file(path_b, total_keys, batch_size=1)
        gen_time_b = time.perf_counter() - t0
        size_b = os.path.getsize(path_b)
        print(f"   Generated in {gen_time_b:.2f}s ({size_b / (1024*1024):.2f} MB)")

        print("   Replaying Case B (1M CRCs + 1M frame headers)...")
        tracemalloc.start()
        t0 = time.perf_counter()
        count_b, _ = replay_log(path_b)
        t_replay_b = time.perf_counter() - t0
        _, peak_b = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"   Result: {count_b:,} keys replayed in {t_replay_b:.4f}s")
        print(f"   Peak RAM: {peak_b / (1024*1024):.2f} MB")
        print(f"   Throughput: {total_keys / t_replay_b:,.0f} keys/s")

        print("\n=== Summary & Architectural Decision ===")
        print(f"Threshold: 1.500s (FORMAT.md §13)")
        print(f"Case A (batches):    {t_replay_a:.4f}s -> {'PASS (<1.5s)' if t_replay_a < 1.5 else 'FAIL (>1.5s)'}")
        print(f"Case B (individual): {t_replay_b:.4f}s -> {'PASS (<1.5s)' if t_replay_b < 1.5 else 'FAIL (>1.5s)'}")


if __name__ == "__main__":
    benchmark()

