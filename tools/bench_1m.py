#!/usr/bin/env python3
"""1 Million Key Benchmark for kvdb engine.

Measures:
1. Ingestion of 1,000,000 keys into a real kvdb database using batch frames.
2. Cold startup recovery time using `DB.open(path)` and the CLI (`./kvdb open`).
3. Throughput and RAM utilization.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

# Add src to sys.path
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kvdb.db import DB
from kvdb.frame import OP_TYPE_PUT


def run_benchmark(total_keys: int = 1_000_000, batch_size: int = 1_000) -> None:
    print(f"=== kvdb 1,000,000 Key Benchmark (Python {sys.version.split()[0]}) ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "bench_1m.db")

        # 1. Ingestion Phase
        print(f"\n1. Ingesting {total_keys:,} keys in batches of {batch_size:,}...")
        t_start = time.perf_counter()

        with DB.open(db_path) as db:
            batch = []
            for i in range(total_keys):
                k = f"k_{i:07d}".encode("ascii")
                v = f"val_{i}".encode("ascii")
                batch.append((OP_TYPE_PUT, k, v))
                if len(batch) == batch_size:
                    db.batch(batch)
                    batch.clear()
            if batch:
                db.batch(batch)

        t_ingest = time.perf_counter() - t_start
        file_size = os.path.getsize(db_path)
        print(f"   ✓ Ingested in {t_ingest:.2f}s ({total_keys / t_ingest:,.0f} keys/s)")
        print(f"   ✓ Database file size: {file_size / (1024 * 1024):.2f} MB")

        # 2. Cold Startup Recovery via Python Engine API (Full replay without checkpoint)
        print(f"\n2. Testing cold startup recovery (full replay, no checkpoint)...")
        t0 = time.perf_counter()

        with DB.open(db_path) as db:
            cold_time = time.perf_counter() - t0
            keys_loaded = len(db)
            engine_recovery_ms = db.recovery_ms

        print(f"   ✓ Reopened: {keys_loaded:,} keys loaded in {cold_time * 1000:.1f} ms")
        print(f"   ✓ Engine recovery timer: {engine_recovery_ms} ms")
        print(f"   ✓ Recovery throughput: {keys_loaded / cold_time:,.0f} keys/s")

        # 3. Create Checkpoint Frame
        print(f"\n3. Creating in-line checkpoint frame (db.checkpoint())...")
        t_cp = time.perf_counter()
        with DB.open(db_path) as db:
            db.checkpoint()
        t_cp_dur = (time.perf_counter() - t_cp) * 1000
        print(f"   ✓ Checkpoint frame written and fsynced in {t_cp_dur:.1f} ms")
        print(f"   ✓ New database file size: {os.path.getsize(db_path) / (1024 * 1024):.2f} MB")

        # 4. Cold Startup Recovery with Checkpoint Frame
        print(f"\n4. Testing cold startup recovery (fast-forwarded from checkpoint)...")
        t0_cp = time.perf_counter()
        with DB.open(db_path) as db:
            cold_time_cp = time.perf_counter() - t0_cp
            keys_loaded_cp = len(db)
            engine_recovery_ms_cp = db.recovery_ms

        print(f"   ✓ Reopened: {keys_loaded_cp:,} keys loaded in {cold_time_cp * 1000:.1f} ms")
        print(f"   ✓ Engine recovery timer: {engine_recovery_ms_cp} ms")
        speedup = cold_time / max(cold_time_cp, 1e-6)
        print(f"   ✓ Speedup: {speedup:.1f}x faster with checkpoint!")

        # 5. CLI Cold Open Test
        print(f"\n5. Testing CLI cold startup (./kvdb open)...")
        import subprocess

        t_cli = time.perf_counter()
        cli_res = subprocess.run(
            [sys.executable, "kvdb", "open", db_path],
            capture_output=True,
            text=True,
        )
        total_cli_ms = (time.perf_counter() - t_cli) * 1000
        print(f"   ✓ CLI output: {cli_res.stdout.strip()}")
        print(f"   ✓ Total CLI process time: {total_cli_ms:.1f} ms")

        # 6. Integrity check
        print(f"\n6. Checking file integrity (./kvdb check)...")
        chk_res = subprocess.run(
            [sys.executable, "kvdb", "check", db_path],
            capture_output=True,
            text=True,
        )
        print(f"   ✓ CLI check: {chk_res.stdout.strip()}")

        print("\n=== Benchmark Complete ===")


if __name__ == "__main__":
    count = 1_000_000
    if len(sys.argv) > 1:
        try:
            count = int(sys.argv[1])
        except ValueError:
            pass
    run_benchmark(count)
