"""Binary framing, header, and op encoding/decoding according to FORMAT.md."""

from __future__ import annotations

import struct
import zlib
from typing import NamedTuple

from kvdb.errors import CorruptFileError

# Constants defined in FORMAT.md
MAGIC = b"KVDBLOG\x00"
HEADER_SIZE = 24
VERSION = 1

# Struct formats (little-endian unsigned)
# Header: magic (8), version (4), generation (8), header_crc (4)
HEADER_STRUCT = struct.Struct("<8sIQI")
# Frame header: frame_len (4), crc32 (4)
FRAME_HDR_STRUCT = struct.Struct("<II")
# Op header: type (1), key_len (2), val_len (4)
OP_HDR_STRUCT = struct.Struct("<BHI")

# FORMAT limits
MIN_PAYLOAD_LEN = 13
MIN_CHECKPOINT_PAYLOAD_LEN = 9
MAX_PAYLOAD_LEN = 268_435_456  # 256 MiB
MIN_KEY_LEN = 1
MAX_KEY_LEN = 1024
MAX_VAL_LEN = 1_048_576  # 1 MiB
MAX_BATCH_OPS = 10_000
MAX_CHECKPOINT_ENTRIES = 2_000_000

KIND_OPS = 1
KIND_CHECKPOINT = 2

OP_TYPE_PUT = 1
OP_TYPE_DEL = 2

CHECKPOINT_HDR_STRUCT = struct.Struct("<BII")  # kind (1), entry_count (4), keys_bytes_total (4)
CHECKPOINT_RECORD_STRUCT = struct.Struct("<QH")  # packed (8), key_len (2)
CHECKPOINT_ENTRY_HDR = struct.Struct("<HQI")  # backward compat helper if needed


class ParsedCheckpointEntry:
    __slots__ = ("key", "val_offset", "val_len")

    def __init__(self, key: bytes, val_offset: int, val_len: int) -> None:
        self.key = key
        self.val_offset = val_offset
        self.val_len = val_len

    def __getitem__(self, item: int):
        if item == 0:
            return self.key
        elif item == 1:
            return self.val_offset
        elif item == 2:
            return self.val_len
        raise IndexError(f"ParsedCheckpointEntry index out of range: {item}")


class ParsedOp:
    __slots__ = ("op_type", "key", "val", "val_offset_in_frame", "val_len")

    def __init__(
        self,
        op_type: int,
        key: bytes,
        val: bytes,
        val_offset_in_frame: int,
        val_len: int,
    ) -> None:
        self.op_type = op_type
        self.key = key
        self.val = val
        self.val_offset_in_frame = val_offset_in_frame
        self.val_len = val_len

    def __getitem__(self, item: int):
        if item == 0:
            return self.op_type
        elif item == 1:
            return self.key
        elif item == 2:
            return self.val
        elif item == 3:
            return self.val_offset_in_frame
        elif item == 4:
            return self.val_len
        raise IndexError(f"ParsedOp index out of range: {item}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, (ParsedOp, tuple)):
            return False
        return (
            self.op_type == other[0]
            and self.key == other[1]
            and self.val == other[2]
            and self.val_offset_in_frame == other[3]
            and self.val_len == other[4]
        )


class ParsedFrame:
    __slots__ = ("frame_len", "crc32", "kind", "op_count", "ops")

    def __init__(
        self,
        frame_len: int,
        crc32: int,
        kind: int,
        op_count: int,
        ops: list[ParsedOp],
    ) -> None:
        self.frame_len = frame_len
        self.crc32 = crc32
        self.kind = kind
        self.op_count = op_count
        self.ops = ops

    def __getitem__(self, item: int):
        if item == 0:
            return self.frame_len
        elif item == 1:
            return self.crc32
        elif item == 2:
            return self.kind
        elif item == 3:
            return self.op_count
        elif item == 4:
            return self.ops
        raise IndexError(f"ParsedFrame index out of range: {item}")


def encode_header(generation: int = 1) -> bytes:
    """Encode a 24-byte database header with CRC32."""
    if generation < 1:
        raise ValueError("Generation must be >= 1")
    prefix = struct.pack("<8sIQ", MAGIC, VERSION, generation)
    header_crc = zlib.crc32(prefix) & 0xFFFFFFFF
    return prefix + struct.pack("<I", header_crc)


def parse_header(buf: bytes | memoryview) -> tuple[int, int]:
    """Validate and parse a 24-byte database header.
    
    Returns (generation, version).
    Raises CorruptFileError if invalid.
    """
    if len(buf) < HEADER_SIZE:
        raise CorruptFileError(f"File too short for header: {len(buf)} bytes (expected {HEADER_SIZE})")

    magic, version, generation, header_crc = HEADER_STRUCT.unpack_from(buf, 0)
    if magic != MAGIC:
        raise CorruptFileError(f"Invalid magic: {magic!r}")
    if version != VERSION:
        raise CorruptFileError(f"Unsupported version: {version}")

    prefix = buf[:20]
    expected_crc = zlib.crc32(prefix) & 0xFFFFFFFF
    if header_crc != expected_crc:
        raise CorruptFileError(f"Header CRC mismatch: got {header_crc:#010x}, expected {expected_crc:#010x}")

    return generation, version


def encode_op(op_type: int, key: bytes, val: bytes = b"") -> bytes:
    """Validate and encode a single operation."""
    if op_type not in (OP_TYPE_PUT, OP_TYPE_DEL):
        raise ValueError(f"Invalid op_type: {op_type}")
    if not (MIN_KEY_LEN <= len(key) <= MAX_KEY_LEN):
        raise ValueError(f"Key length {len(key)} out of bounds [{MIN_KEY_LEN}, {MAX_KEY_LEN}]")

    if op_type == OP_TYPE_PUT:
        if len(val) > MAX_VAL_LEN:
            raise ValueError(f"Value length {len(val)} exceeds maximum {MAX_VAL_LEN}")
    elif op_type == OP_TYPE_DEL:
        if len(val) != 0:
            raise ValueError("Delete operation must have value length 0")

    return OP_HDR_STRUCT.pack(op_type, len(key), len(val)) + key + val


def encode_frame(ops: list[tuple[int, bytes, bytes]]) -> bytes:
    """Encode a list of operations into a single frame with CRC32 commit marker."""
    op_count = len(ops)
    if not (1 <= op_count <= MAX_BATCH_OPS):
        raise ValueError(f"Batch op count {op_count} out of bounds [1, {MAX_BATCH_OPS}]")

    payload_parts = [struct.pack("<BI", 1, op_count)]  # kind = 1, op_count
    for op_type, key, val in ops:
        payload_parts.append(encode_op(op_type, key, val))

    payload = b"".join(payload_parts)
    frame_len = len(payload)
    if not (MIN_PAYLOAD_LEN <= frame_len <= MAX_PAYLOAD_LEN):
        raise ValueError(f"Payload size {frame_len} out of bounds [{MIN_PAYLOAD_LEN}, {MAX_PAYLOAD_LEN}]")

    len_bytes = struct.pack("<I", frame_len)
    crc = zlib.crc32(payload, zlib.crc32(len_bytes)) & 0xFFFFFFFF
    return len_bytes + struct.pack("<I", crc) + payload


def encode_checkpoint_frame(index: dict[bytes, int]) -> bytes:
    """Encode an in-memory index snapshot into a kind=2 checkpoint frame with CRC32.
    
    Layout (Option A - Split Table + String Pool):
    kind: 1B = 2
    entry_count: 4B (uint32)
    keys_bytes_total: 4B (uint32)
    table: entry_count * 10B (<QH: packed uint64, key_len uint16)
    keys_pool: contiguous raw key bytes
    """
    entry_count = len(index)
    if not (0 <= entry_count <= MAX_CHECKPOINT_ENTRIES):
        raise ValueError(f"Checkpoint entry count {entry_count} out of bounds")

    table_parts = []
    key_parts = []
    keys_bytes_total = 0

    for key, packed in index.items():
        k_len = len(key)
        if not (MIN_KEY_LEN <= k_len <= MAX_KEY_LEN):
            raise ValueError(f"Key length {k_len} out of bounds [{MIN_KEY_LEN}, {MAX_KEY_LEN}]")
        if isinstance(packed, tuple):
            val_offset, val_len = packed
            packed_val = pack_val(val_offset, val_len)
        else:
            packed_val = packed
            val_offset, val_len = unpack_val(packed_val)

        if val_len > MAX_VAL_LEN:
            raise ValueError(f"Value length {val_len} exceeds maximum {MAX_VAL_LEN}")

        table_parts.append(CHECKPOINT_RECORD_STRUCT.pack(packed_val, k_len))
        key_parts.append(key)
        keys_bytes_total += k_len

    payload = (
        CHECKPOINT_HDR_STRUCT.pack(KIND_CHECKPOINT, entry_count, keys_bytes_total)
        + b"".join(table_parts)
        + b"".join(key_parts)
    )
    frame_len = len(payload)
    if not (MIN_CHECKPOINT_PAYLOAD_LEN <= frame_len <= MAX_PAYLOAD_LEN):
        raise ValueError(f"Checkpoint payload size {frame_len} out of bounds")

    len_bytes = struct.pack("<I", frame_len)
    crc = zlib.crc32(payload, zlib.crc32(len_bytes)) & 0xFFFFFFFF
    return len_bytes + struct.pack("<I", crc) + payload


def pack_val(val_offset: int, val_len: int) -> int:
    """Pack value offset and length into a single 64-bit integer."""
    return (val_offset << 24) | (val_len & 0xFFFFFF)


def unpack_val(packed: int) -> tuple[int, int]:
    """Unpack a packed int into (val_offset, val_len)."""
    return packed >> 24, packed & 0xFFFFFF


def parse_frame(
    buf: bytes | memoryview,
    pos: int,
    file_size: int,
    extract_values: bool = True,
    target_index: dict[bytes, int] | None = None,
    frame_file_offset: int = 0,
    validate_crc: bool = True,
) -> tuple[ParsedFrame, int] | None:
    """Parse a frame at byte position `pos` using FORMAT.md Section 4 validity rules.
    
    Supports both kind=1 (ops) and kind=2 (checkpoint) frames.
    If target_index is provided, operations/entries are applied directly to the index without
    allocating intermediate objects (enabling high-throughput 1M key recovery).
    Returns (ParsedFrame, next_pos) if valid, or None if invalid (torn tail).
    """
    buf_len = len(buf)

    # Rule 1: At least 8 bytes remain after p
    if pos + 8 > buf_len:
        return None

    # Use memoryview to prevent copying multi-megabyte payloads
    mv = buf if isinstance(buf, memoryview) else memoryview(buf)

    frame_len, expected_crc = FRAME_HDR_STRUCT.unpack_from(mv, pos)

    # Rule 2: Check bounds (prevents zeros from validating)
    if not (MIN_CHECKPOINT_PAYLOAD_LEN <= frame_len <= MAX_PAYLOAD_LEN):
        return None

    # Rule 3: p + 8 + frame_len <= file_size (and within provided buffer)
    if (pos + 8 + frame_len > file_size) or (pos + 8 + frame_len > buf_len):
        return None

    # Rule 4: CRC matches (zero-copy memoryview slice)
    payload = mv[pos + 8 : pos + 8 + frame_len]
    if validate_crc:
        len_bytes = mv[pos : pos + 4]
        calc_crc = zlib.crc32(payload, zlib.crc32(len_bytes)) & 0xFFFFFFFF
        if calc_crc != expected_crc:
            return None

    # Rule 5: kind in (KIND_OPS, KIND_CHECKPOINT)
    kind, count = struct.unpack_from("<BI", payload, 0)
    if kind not in (KIND_OPS, KIND_CHECKPOINT):
        return None

    if kind == KIND_OPS:
        op_count = count
        # Rule 6: 1 <= op_count <= 10,000
        if not (1 <= op_count <= MAX_BATCH_OPS):
            return None
        if frame_len < MIN_PAYLOAD_LEN:
            return None

        b_payload = bytes(payload) if not isinstance(payload, bytes) else payload
        unpack_op = OP_HDR_STRUCT.unpack_from
        p_pos = 5  # past kind (1) and op_count (4)

        if target_index is not None:
            base_offset = frame_file_offset + 8
            for _ in range(op_count):
                if p_pos + 7 > frame_len:
                    return None

                op_type, k_len, v_len = unpack_op(b_payload, p_pos)
                if not (1 <= op_type <= 2 and MIN_KEY_LEN <= k_len <= MAX_KEY_LEN):
                    return None
                if op_type == OP_TYPE_PUT:
                    if v_len > MAX_VAL_LEN:
                        return None
                elif v_len != 0:
                    return None

                p_pos += 7
                val_pos = p_pos + k_len
                p_pos = val_pos + v_len
                if p_pos > frame_len:
                    return None

                key = b_payload[val_pos - k_len : val_pos]
                if op_type == OP_TYPE_PUT:
                    target_index[key] = ((base_offset + val_pos) << 24) | v_len
                else:
                    target_index.pop(key, None)

            # Rule 7 check: must parse exactly to the end of the payload
            if p_pos != frame_len:
                return None

            next_pos = pos + 8 + frame_len
            return ParsedFrame(frame_len, expected_crc, kind, op_count, []), next_pos

        ops: list[ParsedOp] = []
        for _ in range(op_count):
            if p_pos + 7 > frame_len:
                return None

            op_type, k_len, v_len = unpack_op(b_payload, p_pos)
            if op_type not in (OP_TYPE_PUT, OP_TYPE_DEL) or not (MIN_KEY_LEN <= k_len <= MAX_KEY_LEN):
                return None
            if op_type == OP_TYPE_PUT:
                if v_len > MAX_VAL_LEN:
                    return None
            elif v_len != 0:
                return None

            p_pos += 7
            if p_pos + k_len + v_len > frame_len:
                return None

            key = b_payload[p_pos : p_pos + k_len]
            p_pos += k_len

            val_offset_in_frame = 8 + p_pos
            val = b_payload[p_pos : p_pos + v_len] if extract_values else b""
            p_pos += v_len
            ops.append(ParsedOp(op_type, key, val, val_offset_in_frame, v_len))

        # Rule 7 check: must parse exactly to the end of the payload
        if p_pos != frame_len:
            return None

        next_pos = pos + 8 + frame_len
        return ParsedFrame(frame_len, expected_crc, kind, op_count, ops), next_pos

    else:
        # KIND_CHECKPOINT (kind == 2)
        if frame_len < 9:
            return None
        kind, entry_count, keys_bytes_total = CHECKPOINT_HDR_STRUCT.unpack_from(payload, 0)
        if not (0 <= entry_count <= MAX_CHECKPOINT_ENTRIES):
            return None

        expected_table_len = 10 * entry_count
        if 9 + expected_table_len + keys_bytes_total != frame_len:
            return None

        table_bytes = payload[9 : 9 + expected_table_len]
        keys_pool = bytes(payload[9 + expected_table_len :])

        if target_index is not None:
            target_index.clear()
            k_pos = 0
            for packed, k_len in CHECKPOINT_RECORD_STRUCT.iter_unpack(table_bytes):
                if not (MIN_KEY_LEN <= k_len <= MAX_KEY_LEN) or (packed & 0xFFFFFF) > MAX_VAL_LEN:
                    return None
                next_k_pos = k_pos + k_len
                if next_k_pos > keys_bytes_total:
                    return None
                target_index[keys_pool[k_pos:next_k_pos]] = packed
                k_pos = next_k_pos

            if k_pos != keys_bytes_total:
                return None

            next_pos = pos + 8 + frame_len
            return ParsedFrame(frame_len, expected_crc, kind, entry_count, []), next_pos

        checkpoint_entries: list[ParsedCheckpointEntry] = []
        k_pos = 0
        for packed, k_len in CHECKPOINT_RECORD_STRUCT.iter_unpack(table_bytes):
            if not (MIN_KEY_LEN <= k_len <= MAX_KEY_LEN) or (packed & 0xFFFFFF) > MAX_VAL_LEN:
                return None
            next_k_pos = k_pos + k_len
            if next_k_pos > keys_bytes_total:
                return None
            key = keys_pool[k_pos:next_k_pos]
            k_pos = next_k_pos
            v_off = packed >> 24
            v_len = packed & 0xFFFFFF
            checkpoint_entries.append(ParsedCheckpointEntry(key, v_off, v_len))

        if k_pos != keys_bytes_total:
            return None

        next_pos = pos + 8 + frame_len
        return ParsedFrame(frame_len, expected_crc, kind, entry_count, checkpoint_entries), next_pos
    return ParsedFrame(frame_len, expected_crc, kind, op_count, ops), next_pos
