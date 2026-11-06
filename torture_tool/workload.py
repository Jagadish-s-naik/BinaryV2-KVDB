"""Workload generator for the kvdb torture tool.

Per docs/TORTURE_SPEC.md Section 5 and problem statement limits:
- Keys: 1 to 1,024 bytes (including 1-byte, 1024-byte, and multi-byte UTF-8).
- Values: 0 bytes (empty value '-') to 1 MB.
- Scenario B5: batches with internal overwrites and put-del / del-put sequences.
- Deterministic random streams per writer: Random(f"{seed}:{iteration}:{writer}").
- Op IDs: i<iteration>.w<writer>.<n>.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from torture_tool.oracle import generate_value


@dataclass
class GeneratedOp:
    op_type: str  # "PUT", "DEL", "BATCH"
    op_id: str
    key: str = ""
    val: bytes | None = None
    sub_ops: list[tuple[str, str, bytes | None]] | None = None


class WorkloadGenerator:
    def __init__(
        self,
        seed: str | int,
        iteration: int,
        writer_id: int,
        shared_key_count: int = 20,
        mode: str = "default",
    ):
        self.seed = str(seed)
        self.iteration = iteration
        self.writer_id = writer_id
        self.shared_key_count = shared_key_count
        self.mode = mode
        self.rng = random.Random(f"{self.seed}:{self.iteration}:{self.writer_id}")
        self.op_seq = 0

    def next_op_id(self) -> str:
        self.op_seq += 1
        return f"i{self.iteration}.w{self.writer_id}.{self.op_seq}"

    def pick_shared_key(self) -> str:
        """Picks shared key among high-contention keys, 1-byte/2-byte keys, max boundary keys (1023/1024), embedded NUL keys, and utf8 keys."""
        roll = self.rng.random()
        if roll < 0.65:
            # High contention shared keys (s:0 to s:19)
            idx = self.rng.randint(0, self.shared_key_count - 1)
            return f"s:{idx}"
        elif roll < 0.75:
            # 1-byte and 2-byte short keys (boundary testing)
            short_keys = ["k", "z", "1", "9", "x", "k2", "s1"]
            return short_keys[self.rng.randint(0, len(short_keys) - 1)]
        elif roll < 0.80:
            # Exact boundary keys: 1023 bytes and 1024 bytes
            if self.rng.random() < 0.5:
                prefix = "s:b1023:"
                return prefix + ("A" * (1023 - len(prefix)))
            else:
                prefix = "s:b1024:"
                return prefix + ("B" * (1024 - len(prefix)))
        elif roll < 0.90:
            # Embedded NUL byte keys (tests C-string and null-termination vulnerabilities)
            nul_keys = [
                "s:nul:\x00:key",
                "s:nul:mid\x00end",
                "s:\x00",
                "s:zero:\x00\x00:trail",
            ]
            return nul_keys[self.rng.randint(0, len(nul_keys) - 1)]
        else:
            # UTF-8 multi-byte unicode key
            unicode_keys = [
                "s:utf8:⚡",
                "s:utf8:🚀",
                "s:utf8:αβγ",
                "s:sym:!@#$_-+.",
            ]
            return unicode_keys[self.rng.randint(0, len(unicode_keys) - 1)]

    def pick_value_size(self, large_values: bool = False) -> int | None:
        """Selects value sizes covering 0 bytes, page boundaries, and 1 MB."""
        if large_values:
            return self.rng.randint(512 * 1024, 1024 * 1024)

        roll = self.rng.random()
        if roll < 0.05:
            # Empty value (0 bytes, tests '-' representation)
            return 0
        elif roll < 0.10:
            # 1-byte value
            return 1
        elif roll < 0.15:
            # Page / buffer boundary sizes (511, 512, 4095, 4096, 65535, 65536)
            boundaries = [511, 512, 4095, 4096, 65535, 65536]
            return self.rng.choice(boundaries)
        elif roll < 0.20:
            # Medium sizes (1 KiB to 32 KiB)
            return self.rng.randint(1024, 32 * 1024)
        else:
            # Default small padding
            return None

    def next_op(self) -> GeneratedOp:
        op_id = self.next_op_id()

        if self.mode == "large-values":
            return self._generate_large_values_op(op_id)
        elif self.mode == "large-batches":
            return self._generate_large_batches_op(op_id)
        elif self.mode == "compaction":
            return self._generate_compaction_op(op_id)
        elif self.mode == "contention":
            return self._generate_contention_op(op_id)
        else:
            return self._generate_default_op(op_id)

    def _generate_default_op(self, op_id: str) -> GeneratedOp:
        roll = self.rng.random()
        if roll < 0.60:
            # Single PUT
            key = self.pick_shared_key()
            v_size = self.pick_value_size()
            val = generate_value(op_id, key.encode("utf-8").hex(), size=v_size)
            return GeneratedOp(op_type="PUT", op_id=op_id, key=key, val=val)
        elif roll < 0.75:
            # Single DEL
            key = self.pick_shared_key()
            return GeneratedOp(op_type="DEL", op_id=op_id, key=key)
        else:
            # BATCH (2 to 50 operations)
            return self._generate_batch(op_id, min_ops=2, max_ops=50)

    def _generate_large_values_op(self, op_id: str) -> GeneratedOp:
        roll = self.rng.random()
        if roll < 0.75:
            key = self.pick_shared_key()
            # 50% chance of 512 KiB to 1 MiB value
            if self.rng.random() < 0.50:
                size = self.rng.randint(512 * 1024, 1024 * 1024)
            else:
                size = self.pick_value_size()
            val = generate_value(op_id, key.encode("utf-8").hex(), size=size)
            return GeneratedOp(op_type="PUT", op_id=op_id, key=key, val=val)
        elif roll < 0.85:
            key = self.pick_shared_key()
            return GeneratedOp(op_type="DEL", op_id=op_id, key=key)
        else:
            return self._generate_batch(op_id, min_ops=1, max_ops=5, large_values=True)

    def _generate_large_batches_op(self, op_id: str) -> GeneratedOp:
        if self.writer_id == 0:
            count = self.rng.randint(1000, 10000)
            return self._generate_batch(op_id, min_ops=count, max_ops=count)
        else:
            key = self.pick_shared_key()
            val = generate_value(op_id, key.encode("utf-8").hex())
            return GeneratedOp(op_type="PUT", op_id=op_id, key=key, val=val)

    def _generate_compaction_op(self, op_id: str) -> GeneratedOp:
        roll = self.rng.random()
        if roll < 0.40:
            key = self.pick_shared_key()
            return GeneratedOp(op_type="DEL", op_id=op_id, key=key)
        elif roll < 0.80:
            key = self.pick_shared_key()
            val = generate_value(op_id, key.encode("utf-8").hex())
            return GeneratedOp(op_type="PUT", op_id=op_id, key=key, val=val)
        else:
            return self._generate_batch(op_id, min_ops=2, max_ops=30)

    def _generate_contention_op(self, op_id: str) -> GeneratedOp:
        """Scenario D2: rapid concurrent PUT/DEL on identical shared keys."""
        idx = self.rng.randint(0, max(1, self.shared_key_count) - 1)
        key = f"s:{idx}"
        if self.rng.random() < 0.50:
            val = generate_value(op_id, key.encode("utf-8").hex())
            return GeneratedOp(op_type="PUT", op_id=op_id, key=key, val=val)
        else:
            return GeneratedOp(op_type="DEL", op_id=op_id, key=key)

    def _generate_batch(
        self,
        op_id: str,
        min_ops: int,
        max_ops: int,
        large_values: bool = False,
    ) -> GeneratedOp:
        batch_size = self.rng.randint(min_ops, max_ops)
        sub_ops: list[tuple[str, str, bytes | None]] = []

        # Guarantee at least one surviving PUT
        key_0 = f"b:{op_id}:0"
        val_size = self.pick_value_size(large_values=large_values)
        val_0 = generate_value(op_id, key_0.encode("utf-8").hex(), size=val_size)
        sub_ops.append(("PUT", key_0, val_0))

        cur_idx = 1
        patterns = [
            "simple_put",
            "put_del",
            "del_put",
            "put_del_put",
            "del_put_del",
            "overwrite_put",
        ]

        while len(sub_ops) < batch_size:
            pattern = self.rng.choice(patterns)
            k = f"b:{op_id}:{cur_idx}"
            cur_idx += 1
            v_size = self.pick_value_size(large_values=large_values)
            v1 = generate_value(op_id + ":1", k.encode("utf-8").hex(), size=v_size)
            v2 = generate_value(op_id + ":2", k.encode("utf-8").hex(), size=v_size)

            if pattern == "simple_put":
                sub_ops.append(("PUT", k, v1))
            elif pattern == "put_del":
                sub_ops.append(("PUT", k, v1))
                if len(sub_ops) < batch_size:
                    sub_ops.append(("DEL", k, None))
            elif pattern == "del_put":
                sub_ops.append(("DEL", k, None))
                if len(sub_ops) < batch_size:
                    sub_ops.append(("PUT", k, v1))
            elif pattern == "put_del_put":
                # Scenario B5: put, delete, put again (last op wins: present with v2)
                sub_ops.append(("PUT", k, v1))
                if len(sub_ops) < batch_size:
                    sub_ops.append(("DEL", k, None))
                if len(sub_ops) < batch_size:
                    sub_ops.append(("PUT", k, v2))
            elif pattern == "del_put_del":
                # Scenario B5: del, put, delete again (last op wins: absent)
                sub_ops.append(("DEL", k, None))
                if len(sub_ops) < batch_size:
                    sub_ops.append(("PUT", k, v1))
                if len(sub_ops) < batch_size:
                    sub_ops.append(("DEL", k, None))
            elif pattern == "overwrite_put":
                # Multiple puts to same key inside batch (last op wins: present with v2)
                sub_ops.append(("PUT", k, v1))
                if len(sub_ops) < batch_size:
                    sub_ops.append(("PUT", k, v2))

        return GeneratedOp(op_type="BATCH", op_id=op_id, sub_ops=sub_ops)
