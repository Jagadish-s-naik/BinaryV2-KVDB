"""Hardware-level storage fault injector for the kvdb torture tool.

Simulates physical storage degradation and power-loss phenomena at EOF:
- Torn 4KB disk sector writes (partial hardware block flushes)
- Corrupted frame with invalid CRC32 (NAND cell decay / cosmic-ray bit-rot)
- Zero-byte unbuffered hole allocation at EOF
- Random noise bytes appended at EOF

Strictly independent: imports zero code from src/kvdb/.
"""

from __future__ import annotations

import os
import random
from pathlib import Path


class FaultType:
    TORN_SECTOR = "TORN_4KB_SECTOR"
    INVALID_CRC = "BIT_ROT_INVALID_CRC"
    ZERO_HOLE = "ZERO_BLOCK_HOLE"
    BAD_TAIL = "DIRTY_MEDIA_NOISE"


class FaultInjector:
    """Independent byte-level mutator for data.db."""

    @staticmethod
    def inject_random_fault(db_path: Path, rng: random.Random) -> tuple[str, str]:
        """Select and inject a random hardware fault at the EOF boundary.
        
        Returns:
            (fault_type, description)
        """
        if not db_path.exists():
            return "SKIPPED", "Database file does not exist"

        file_sz = db_path.stat().st_size
        if file_sz < 24:
            return "SKIPPED", "File smaller than database header"

        choice = rng.random()
        if choice < 0.30:
            return FaultType.TORN_SECTOR, FaultInjector.inject_torn_sector(db_path, rng)
        elif choice < 0.60:
            return FaultType.INVALID_CRC, FaultInjector.inject_corrupted_frame_at_tail(db_path, rng)
        elif choice < 0.85:
            return FaultType.ZERO_HOLE, FaultInjector.inject_zero_hole(db_path, rng=rng)
        else:
            return FaultType.BAD_TAIL, FaultInjector.inject_bad_tail(db_path, rng)

    @staticmethod
    def inject_torn_sector(db_path: Path, rng: random.Random) -> str:
        """Simulate torn write across 4KB disk sector boundary by appending partial frame and truncating."""
        file_sz = db_path.stat().st_size
        extra_len = rng.randint(64, 512)
        partial = rng.randbytes(extra_len)
        with open(db_path, "ab") as f:
            f.write(partial)
            f.flush()

        new_sz = db_path.stat().st_size
        cut_pos = new_sz - rng.randint(1, extra_len - 1)
        with open(db_path, "r+b") as f:
            f.truncate(cut_pos)
            f.flush()
            os.fsync(f.fileno())

        return f"Injected torn write (expanded to {new_sz:,} then truncated to {cut_pos:,})"

    @staticmethod
    def inject_corrupted_frame_at_tail(db_path: Path, rng: random.Random) -> str:
        """Append a plausible frame with an invalid CRC32 at EOF to test bit-rot detection."""
        k = rng.randbytes(rng.randint(4, 16))
        v = rng.randbytes(rng.randint(8, 64))
        magic = b"KVDB\x02\x00\x00\x00"
        epoch = (1).to_bytes(8, "little")
        kind = b"\x00"  # PUT
        klen = len(k).to_bytes(2, "little")
        vlen = len(v).to_bytes(4, "little")
        bad_crc = rng.randbytes(4)
        frame = magic + epoch + kind + klen + vlen + bad_crc + k + v

        with open(db_path, "ab") as f:
            f.write(frame)
            f.flush()
            os.fsync(f.fileno())

        return f"Appended {len(frame)}B frame with corrupted CRC32 at EOF"

    @staticmethod
    def inject_zero_hole(db_path: Path, size: int | None = None, rng: random.Random | None = None) -> str:
        """Append null bytes simulating unbuffered filesystem block preallocation."""
        if size is None:
            size = rng.choice([512, 1024, 4096]) if rng else 4096

        with open(db_path, "ab") as f:
            f.write(b"\x00" * size)
            f.flush()
            os.fsync(f.fileno())

        return f"Appended {size:,} null bytes (\\x00) simulating power-loss preallocation hole"

    @staticmethod
    def inject_bad_tail(db_path: Path, rng: random.Random) -> str:
        """Append 1 to 64 random noise bytes to EOF simulating dirty media / torn write."""
        noise_len = rng.randint(1, 64)
        noise = rng.randbytes(noise_len)
        with open(db_path, "ab") as f:
            f.write(noise)
            f.flush()
            os.fsync(f.fileno())

        return f"Appended {noise_len} random noise bytes at EOF"
