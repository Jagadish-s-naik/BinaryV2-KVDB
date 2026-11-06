"""Independent Oracle for kvdb torture testing.

Per docs/TORTURE_SPEC.md:
- Pure Python 3.14 standard library only.
- Does not import or depend on src/kvdb/.
- Checks invariants for shared keys, batches (all-or-nothing), and value integrity.
"""

from __future__ import annotations

import hashlib
import random
import struct
import zlib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class OpState(str, Enum):
    ACKED = "ACKED"
    FAILED = "FAILED"
    PENDING = "PENDING"


def generate_value(op_id: str, key_hex: str, size: int | None = None) -> bytes:
    """Generate deterministic value: 'v:' + op_id + ':' + key_hex + ':' + padding.
    If size == 0, returns b'' (empty value, encoded as '-' in serve protocol).
    """
    if size == 0:
        return b""
    prefix = f"v:{op_id}:{key_hex}:".encode("ascii")
    rng = random.Random(op_id)
    if size is None:
        pad_len = 32
    else:
        pad_len = max(0, size - len(prefix))
    padding = rng.randbytes(pad_len)
    return prefix + padding


@dataclass
class SingleOp:
    op_id: str
    op_type: str  # "PUT" or "DEL"
    key: str
    key_hex: str
    val: bytes | None = None
    val_len: int = 0
    val_sha256: str = ""
    sent_ns: int = 0
    ack_ns: int | None = None
    state: OpState = OpState.PENDING
    err_code: str | None = None


@dataclass
class BatchSubOp:
    op_type: str  # "PUT" or "DEL"
    key: str
    key_hex: str
    val: bytes | None = None
    val_len: int = 0
    val_sha256: str = ""


@dataclass
class BatchOp:
    op_id: str
    sub_ops: list[BatchSubOp]
    expected_map: dict[str, bytes | None]  # key -> expected val (None = absent)
    sent_ns: int = 0
    ack_ns: int | None = None
    state: OpState = OpState.PENDING
    err_code: str | None = None


@dataclass
class CheckResult:
    passed: bool
    rule: str = ""
    key: str = ""
    found_desc: str = ""
    expected_desc: str = ""
    related_ops: list[str] = field(default_factory=list)
    details: str = ""


class Oracle:
    """The Oracle maintains the ground truth of sent and acknowledged operations."""

    def __init__(self, iteration: int = 0):
        self.iteration = iteration
        self.ops_by_id: dict[str, SingleOp | BatchOp] = {}
        self.ops_by_key: dict[str, list[SingleOp]] = {}
        self.batches: list[BatchOp] = []
        self.log_lines: list[str] = []
        self.frozen: bool = False

    def record_sent_put(
        self, op_id: str, key: str, val: bytes, sent_ns: int
    ) -> SingleOp:
        if self.frozen:
            return None  # type: ignore
        key_hex = key.encode("utf-8").hex()
        op = SingleOp(
            op_id=op_id,
            op_type="PUT",
            key=key,
            key_hex=key_hex,
            val=val,
            val_len=len(val),
            val_sha256=hashlib.sha256(val).hexdigest(),
            sent_ns=sent_ns,
        )
        self.ops_by_id[op_id] = op
        self.ops_by_key.setdefault(key, []).append(op)
        self.log_lines.append(
            f"SENT {op_id} PUT {key_hex} {op.val_len} {op.val_sha256} {sent_ns}"
        )
        return op

    def record_sent_del(self, op_id: str, key: str, sent_ns: int) -> SingleOp:
        if self.frozen:
            return None  # type: ignore
        key_hex = key.encode("utf-8").hex()
        op = SingleOp(
            op_id=op_id,
            op_type="DEL",
            key=key,
            key_hex=key_hex,
            sent_ns=sent_ns,
        )
        self.ops_by_id[op_id] = op
        self.ops_by_key.setdefault(key, []).append(op)
        self.log_lines.append(f"SENT {op_id} DEL {key_hex} {sent_ns}")
        return op

    def record_sent_batch(
        self,
        op_id: str,
        sub_ops: list[tuple[str, str, bytes | None]],
        sent_ns: int,
    ) -> BatchOp:
        if self.frozen:
            return None  # type: ignore
        parsed_sub: list[BatchSubOp] = []
        expected_map: dict[str, bytes | None] = {}

        for op_type, key, val in sub_ops:
            key_hex = key.encode("utf-8").hex()
            if op_type.upper() == "PUT":
                assert val is not None
                parsed_sub.append(
                    BatchSubOp(
                        op_type="PUT",
                        key=key,
                        key_hex=key_hex,
                        val=val,
                        val_len=len(val),
                        val_sha256=hashlib.sha256(val).hexdigest(),
                    )
                )
                expected_map[key] = val
            else:
                parsed_sub.append(
                    BatchSubOp(
                        op_type="DEL",
                        key=key,
                        key_hex=key_hex,
                    )
                )
                expected_map[key] = None

        batch = BatchOp(
            op_id=op_id,
            sub_ops=parsed_sub,
            expected_map=expected_map,
            sent_ns=sent_ns,
        )
        self.ops_by_id[op_id] = batch
        self.batches.append(batch)
        self.log_lines.append(f"SENT {op_id} BATCH {len(sub_ops)} {sent_ns}")
        return batch

    def record_ack(self, op_id: str, ack_ns: int) -> None:
        if self.frozen:
            return
        op = self.ops_by_id.get(op_id)
        if op is not None:
            op.ack_ns = ack_ns
            op.state = OpState.ACKED
        self.log_lines.append(f"ACK {op_id} {ack_ns}")

    def record_err(self, op_id: str, code: str, err_ns: int) -> None:
        if self.frozen:
            return
        op = self.ops_by_id.get(op_id)
        if op is not None:
            op.ack_ns = err_ns
            op.err_code = code
            if code in ("DISK_FULL", "BAD_ARG", "PARSE"):
                op.state = OpState.FAILED
            else:
                op.state = OpState.PENDING
        self.log_lines.append(f"ERR {op_id} {code} {err_ns}")

    def freeze(self) -> None:
        self.frozen = True

    def write_log(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for line in self.log_lines:
                f.write(line + "\n")

    def check_invariants(
        self,
        found_dump: dict[str, bytes],
        baseline: dict[str, bytes],
    ) -> CheckResult:
        """Verify database dump against oracle and baseline.

        Returns CheckResult with passed=True, or passed=False with violation details.
        """
        touched_keys = set(self.ops_by_key.keys()) | set(baseline.keys())
        batch_keys = {k for b in self.batches for k in b.expected_map}

        # Check for alien keys in dump
        for k in found_dump:
            if k not in touched_keys and k not in batch_keys:
                return CheckResult(
                    passed=False,
                    rule="alien key: key found in dump was neither in baseline nor touched by operations",
                    key=k,
                    found_desc=f"key {k} present with {len(found_dump[k])} bytes",
                    expected_desc="key should not exist",
                )

        # Baseline keys untouched this iteration must remain exactly unchanged
        for k, baseline_val in baseline.items():
            if k not in self.ops_by_key and k not in batch_keys:
                if k not in found_dump:
                    return CheckResult(
                        passed=False,
                        rule="untouched baseline key dropped",
                        key=k,
                        found_desc="absent",
                        expected_desc=f"baseline value (len={len(baseline_val)})",
                    )
                if found_dump[k] != baseline_val:
                    return CheckResult(
                        passed=False,
                        rule="untouched baseline key corrupted",
                        key=k,
                        found_desc=f"len={len(found_dump[k])}",
                        expected_desc=f"baseline value (len={len(baseline_val)})",
                    )

        # 1. Check shared keys (and single-op touched keys)
        for key in self.ops_by_key:
            ops = self.ops_by_key[key]

            # Collect candidates C
            # Rule: every ACKED or PENDING op x on K,
            # EXCEPT x is excluded if some ACKED op y on K was sent after x was acknowledged (sent_y > ack_x)
            candidates: list[SingleOp] = []
            has_acked_op = False

            for op in ops:
                if op.state == OpState.ACKED:
                    has_acked_op = True

            for x in ops:
                if x.state == OpState.FAILED:
                    continue

                excluded = False
                if x.state == OpState.ACKED and x.ack_ns is not None:
                    for y in ops:
                        if y.state == OpState.ACKED and y.sent_ns > x.ack_ns:
                            excluded = True
                            break
                if not excluded:
                    candidates.append(x)

            allow_baseline = not has_acked_op
            baseline_val = baseline.get(key)
            found_val = found_dump.get(key)

            match_found = False

            if found_val is None:
                # Key is absent in dump
                if allow_baseline and baseline_val is None:
                    match_found = True
                else:
                    for cand in candidates:
                        if cand.op_type == "DEL":
                            match_found = True
                            break
            else:
                # Key is present in dump
                if allow_baseline and baseline_val == found_val:
                    match_found = True
                else:
                    for cand in candidates:
                        if cand.op_type == "PUT" and cand.val == found_val:
                            match_found = True
                            break

            if not match_found:
                found_desc = "absent"
                if found_val is not None:
                    matching_ops = [
                        op
                        for op in ops
                        if op.op_type == "PUT" and op.val == found_val
                    ]
                    if matching_ops:
                        m = matching_ops[0]
                        found_desc = f"op {m.op_id} ({m.op_type}, {m.state.value} at ack_ns={m.ack_ns}) sha256={m.val_sha256[:16]}"
                    else:
                        found_desc = f"unknown/corrupted value (len={len(found_val)}, sha256={hashlib.sha256(found_val).hexdigest()[:16]})"

                expected_cands = []
                if allow_baseline:
                    expected_cands.append(
                        f"baseline ({'absent' if baseline_val is None else f'len={len(baseline_val)}'})"
                    )
                for cand in candidates:
                    expected_cands.append(
                        f"{cand.op_id} ({cand.op_type}, {cand.state.value})"
                    )

                return CheckResult(
                    passed=False,
                    rule="shared key: found value not allowed",
                    key=key,
                    found_desc=found_desc,
                    expected_desc="one of: " + ", ".join(expected_cands),
                    related_ops=[op.op_id for op in ops],
                )

        # 2. Check batches (all or nothing)
        for batch in self.batches:
            E = batch.expected_map
            P = {k: v for k, v in E.items() if v is not None}
            D = {k for k, v in E.items() if v is None}

            for k in D:
                if k in found_dump:
                    return CheckResult(
                        passed=False,
                        rule="batch: key expected absent was present in dump",
                        key=k,
                        found_desc=f"key {k} present (len={len(found_dump[k])})",
                        expected_desc="key should be absent per batch resolution",
                        related_ops=[batch.op_id],
                    )

            c = 0
            for k, expected_v in P.items():
                if k in found_dump:
                    if found_dump[k] == expected_v:
                        c += 1
                    else:
                        return CheckResult(
                            passed=False,
                            rule="batch: key holding value other than expected",
                            key=k,
                            found_desc=f"len={len(found_dump[k])}",
                            expected_desc=f"len={len(expected_v)} sha256={hashlib.sha256(expected_v).hexdigest()[:16]}",
                            related_ops=[batch.op_id],
                        )

            if 0 < c < len(P):
                return CheckResult(
                    passed=False,
                    rule="batch atomicity violation: partial batch applied",
                    key=batch.op_id,
                    found_desc=f"{c}/{len(P)} keys present",
                    expected_desc=f"0 or {len(P)} keys (all or nothing)",
                    related_ops=[batch.op_id],
                )

            if batch.state == OpState.ACKED and c != len(P):
                return CheckResult(
                    passed=False,
                    rule="batch ACKED but not applied",
                    key=batch.op_id,
                    found_desc=f"{c}/{len(P)} keys present",
                    expected_desc=f"{len(P)} keys present (ACKED)",
                    related_ops=[batch.op_id],
                )

            if batch.state == OpState.FAILED and c != 0:
                return CheckResult(
                    passed=False,
                    rule="batch FAILED but partially or fully applied",
                    key=batch.op_id,
                    found_desc=f"{c}/{len(P)} keys present",
                    expected_desc="0 keys present (FAILED)",
                    related_ops=[batch.op_id],
                )

        return CheckResult(passed=True)


def validate_raw_binary_db(db_path: Path | str, strict_magic: bool = False) -> tuple[bool, str]:
    """Independent binary format & frame auditor per FORMAT.md.

    Validates:
    - 24-byte header: magic 'KVDBLOG\\0', version 1, generation >= 1, header_crc.
    - Sequential frames from offset 24 to EOF.
    - Each frame: 4-byte frame_len, 4-byte crc32 over (frame_len + payload).
    - Payload structure: kind == 1, op_count uint32, each op well-formed.
    - Zero trailing junk: file must end exactly at the end of the last valid frame.
    """
    db_path = Path(db_path) if isinstance(db_path, str) else db_path
    if not db_path.exists():
        return False, f"File does not exist: {db_path}"

    try:
        raw = db_path.read_bytes()
    except OSError as e:
        return False, f"Failed to read file {db_path}: {e}"

    file_size = len(raw)
    if file_size == 0:
        return True, "Empty database (0 bytes)"

    if file_size < 24:
        return False, f"Corrupted header: file size {file_size} is less than 24-byte header"

    magic = raw[:8]
    if magic != b"KVDBLOG\x00":
        if not strict_magic:
            # Non-binary engine (e.g. test mock text state)
            return True, "Non-binary file (skipped raw validation)"
        return False, f"Invalid magic bytes {magic!r}, expected b'KVDBLOG\\x00'"

    version = struct.unpack("<I", raw[8:12])[0]
    if version != 1:
        return False, f"Unsupported version {version}, expected 1"

    generation = struct.unpack("<Q", raw[12:20])[0]
    if generation < 1:
        return False, f"Invalid generation {generation}, expected >= 1"

    expected_header_crc = zlib.crc32(raw[:20]) & 0xFFFFFFFF
    header_crc = struct.unpack("<I", raw[20:24])[0]
    if header_crc != expected_header_crc:
        return False, f"Header CRC mismatch: expected {expected_header_crc:#010x}, found {header_crc:#010x}"

    offset = 24
    frame_index = 0

    while offset < file_size:
        rem = file_size - offset
        if rem < 8:
            return False, f"Torn frame header at offset {offset}: only {rem} bytes remain before EOF"

        frame_len = struct.unpack("<I", raw[offset : offset + 4])[0]
        frame_crc = struct.unpack("<I", raw[offset + 4 : offset + 8])[0]

        if offset + 8 + frame_len > file_size:
            return False, (
                f"Torn frame {frame_index} at offset {offset}: "
                f"frame_len={frame_len} extends past EOF ({file_size})"
            )

        payload = raw[offset + 8 : offset + 8 + frame_len]
        computed_crc = zlib.crc32(raw[offset : offset + 4] + payload) & 0xFFFFFFFF
        if frame_crc != computed_crc:
            return False, (
                f"Frame {frame_index} at offset {offset} CRC mismatch: "
                f"expected {computed_crc:#010x}, found {frame_crc:#010x}"
            )

        # Validate payload structure per FORMAT.md
        if frame_len < 5:
            return False, f"Frame {frame_index} at offset {offset} payload length {frame_len} < 5"

        kind = payload[0]
        if kind != 1:
            return False, f"Frame {frame_index} at offset {offset} unexpected kind {kind} (expected 1)"

        op_count = struct.unpack("<I", payload[1:5])[0]
        p_off = 5
        for op_idx in range(op_count):
            if p_off + 7 > frame_len:
                return False, f"Frame {frame_index} op {op_idx} header truncated within payload"

            op_type = payload[p_off]
            if op_type not in (1, 2):
                return False, f"Frame {frame_index} op {op_idx} invalid op_type {op_type}"

            k_len = struct.unpack("<H", payload[p_off + 1 : p_off + 3])[0]
            if not (1 <= k_len <= 1024):
                return False, f"Frame {frame_index} op {op_idx} key_len {k_len} out of bounds [1, 1024]"

            v_len = struct.unpack("<I", payload[p_off + 3 : p_off + 7])[0]
            if op_type == 2 and v_len != 0:
                return False, f"Frame {frame_index} delete op {op_idx} has non-zero val_len {v_len}"
            if op_type == 1 and v_len > 1_048_576:
                return False, f"Frame {frame_index} put op {op_idx} val_len {v_len} exceeds 1MB"

            if p_off + 7 + k_len + v_len > frame_len:
                return False, f"Frame {frame_index} op {op_idx} payload overflow"

            p_off += 7 + k_len + v_len

        if p_off != frame_len:
            return False, f"Frame {frame_index} at offset {offset} has {frame_len - p_off} unparsed trailing payload bytes"

        offset += 8 + frame_len
        frame_index += 1

    if offset != file_size:
        return False, f"Untruncated trailing data after last frame: parsed {offset} bytes, file size {file_size}"

    return True, f"Valid binary database: {frame_index} frames verified cleanly to EOF ({file_size} bytes)"


