# FORMAT.md: On-disk format and engine rules

**Status:** v1 design, written before any code. Update this file in the same commit as any change to the format, write order, recovery, compaction, locking or disk-full logic. If the code and this file disagree, one of them is a bug.

**Target:** Python 3.14, standard library only, Linux and Windows.

---

## 1. The one-paragraph summary

The database is one file: a 24-byte header followed by a sequence of **frames**. Every write (a single put, a delete, or a whole batch) is exactly one frame. A frame carries a CRC32 over its length and payload. A frame that validates is applied in full; a frame that does not validate is a torn tail and is cut off. So **a valid CRC means the whole batch is on disk, and the CRC acts as the commit marker.** The CRC does not replace `fsync`: durability comes from writing the frame and calling `fsync` *before* sending ACK.

---

## 2. Conventions

- All integers are **little-endian, unsigned**.
- CRC is `zlib.crc32(data) & 0xFFFFFFFF`.
- "Offset" means a byte offset from the start of the file.
- Keys and values are raw bytes.

---

## 3. File layout

```
offset 0                                                        end of file
+----------+---------+---------+-----+---------+ ... +---------+
|  Header  | Frame 1 | Frame 2 | ... | Frame N |     | (torn   |
|  24 B    |         |         |     |         |     |  tail?) |
+----------+---------+---------+-----+---------+ ... +---------+
```

### 3.1 Header (24 bytes)

| Offset | Size | Field        | Value / meaning                                    |
|-------:|-----:|--------------|----------------------------------------------------|
| 0      | 8    | `magic`      | `4B 56 44 42 4C 4F 47 00` (`"KVDBLOG\0"`)          |
| 8      | 4    | `version`    | `1`                                                |
| 12     | 8    | `generation` | Starts at 1. Compaction writes `generation + 1`.   |
| 20     | 4    | `header_crc` | CRC32 of bytes 0..19                               |

### 3.2 Frame

| Size        | Field       | Meaning                                         |
|------------:|-------------|-------------------------------------------------|
| 4           | `frame_len` | Length of `payload` in bytes                    |
| 4           | `crc32`     | CRC32 of `frame_len` (4 bytes) **followed by** `payload` |
| `frame_len` | `payload`   | See 3.3                                         |

The CRC covers the length field too, so a corrupted length is caught by the CRC, not just by bounds checks.

### 3.3 Payload

| Size | Field      | Meaning                                              |
|-----:|------------|------------------------------------------------------|
| 1    | `kind`     | `1` = ops frame. `2` = reserved for checkpoint (not used in v1). |
| 4    | `op_count` | Number of ops that follow                            |
| ...  | `ops`      | `op_count` ops, back to back                         |

### 3.4 Op

| Size    | Field     | Meaning                                       |
|--------:|-----------|-----------------------------------------------|
| 1       | `type`    | `1` = put, `2` = delete                       |
| 2       | `key_len` | 1 to 1024                                     |
| 4       | `val_len` | 0 to 1,048,576 for put; **must be 0 for delete** |
| key_len | `key`     |                                               |
| val_len | `value`   |                                               |

A put with an empty value (`val_len = 0`) is still a put. A delete is identified by `type`, never by an empty value.

### 3.5 Worked example

`put("a", "1")` on a fresh database (generation 1):

```
Header:
4B 56 44 42 4C 4F 47 00   magic "KVDBLOG\0"
01 00 00 00               version = 1
01 00 00 00 00 00 00 00   generation = 1
95 A2 C2 97               header_crc

Frame:
0E 00 00 00               frame_len = 14
93 78 1A 03               crc32 over (0E 00 00 00 || payload)
01                        kind = 1 (ops frame)
01 00 00 00               op_count = 1
01                        type = put
01 00                     key_len = 1
01 00 00 00               val_len = 1
61                        key "a"
31                        value "1"
```

Use this as a test vector for your parser. Check it against a hex dump of a real file.

---

## 4. Frame validity rules

A frame starting at offset `p` is **valid** only if **all** of these hold. If any fails, the frame is a torn tail.

1. At least 8 bytes remain after `p` (room for `frame_len` and `crc32`).
2. `13 <= frame_len <= 268,435,456` (256 MiB). 13 is the smallest legal payload: 1 kind + 4 op_count + one op of 8 bytes (1 + 2 + 4 + a 1-byte key).
3. `p + 8 + frame_len <= file_size`.
4. The CRC matches.
5. `kind == 1`.
6. `1 <= op_count <= 10,000`.
7. The ops parse **exactly** to the end of the payload, with no bytes missing and none left over.
8. Every op has `type` 1 or 2, `1 <= key_len <= 1024`, `val_len <= 1,048,576`, and `val_len == 0` for deletes.

**Why rule 2 matters:** a file extended with zeros reads as `frame_len = 0, crc32 = 0`, and the CRC32 of empty input is 0, so without a minimum length a block of zeros would "validate."

**Why checks come before reading the payload:** a garbage `frame_len` must never cause a huge allocation or a read past the end of the file.

---

## 5. Semantics

### 5.1 Operations

| Operation | Frame written |
|-----------|---------------|
| `put(k, v)` | one frame, one put op |
| `delete(k)` | one frame, one delete op (writing it even if `k` is absent is fine) |
| `batch(ops)` | one frame, all ops in order |
| empty batch | **no frame written**, returns success |

### 5.2 Batch ordering (scenario B5)

Ops in a batch apply **in order, and the last op on a key wins.**
- `put X, del X` leaves X absent.
- `del X, put X` leaves X present with the put's value.

The same rule applies when recovery replays a frame.

### 5.3 Limits

| Item | Limit |
|------|-------|
| Key length | 1 to 1,024 bytes |
| Value length | 0 to 1,048,576 bytes |
| Ops per batch | 10,000 |
| Frame payload | 256 MiB (see Limitations) |
| Keys per database | at least 1,000,000 (memory is the practical bound) |

Violations raise `ValueError` **before anything is written.**

---

## 6. In-memory state

- `index`: a dict mapping `key -> packed int`, where `packed = (value_offset << 24) | value_len`. `value_len` is at most 2^20, so it fits in 24 bits. A single int per key keeps 1M keys light.
- `end_offset`: where the next frame will be written (the end of the last valid frame).
- `generation`: from the header.
- `poisoned`: a flag, see 7.2.
- One `threading.Lock` (`lock`) protecting all of the above.

Deleted keys are removed from `index`. The index is **never** persisted in v1; it is rebuilt from the log on `open()`.

---

## 7. Write path

All of this runs while holding `lock`.

```
 1. validate inputs (sizes, counts)            -> ValueError, nothing written
 2. build the complete frame in memory         (header + payload + CRC)
 3. check size cap (section 10)                -> DiskFullError, nothing written
 4. seek to end_offset; write the whole frame  (loop until every byte is written)
 5. os.fsync(fd)
 6. update index and end_offset                (apply ops in order)
 7. release lock
 8. return success  ->  serve prints "ACK <id>" and flushes stdout
```

**The ordering rule:** the index is updated only after `fsync` returns, so no reader ever sees data that a crash could erase. ACK is sent only after `fsync`.

### 7.1 Failed or partial write

If step 4 raises, call `ftruncate(fd, end_offset)` to remove any partial bytes and re-raise. If the truncate also fails, set `poisoned` (7.2).

### 7.2 Poisoning

If `fsync` fails, or a cleanup `ftruncate` fails, the instance sets `poisoned = True` and every later write raises `DBPoisonedError`. **We never retry a failed `fsync`**: after a failure the OS may have dropped the dirty pages, so a retry that "succeeds" would be lying. Reopening the database runs recovery, which finds the true state.

### 7.3 Reading

`get(k)` takes `lock`, looks up `index`, and reads the value with `os.lseek` + `os.read` on the same fd. Because `get` takes the same lock as writes, it can never observe a half-applied batch.

> **Windows note:** `os.pread` and `os.pwrite` do **not exist on Windows**. Use `os.lseek` + `os.read`/`os.write` under the lock. This is safe because the lock already serialises everything. Also open files with `os.O_BINARY` (defined on Windows, absent on Linux, so use `getattr(os, "O_BINARY", 0)`), or Windows will translate line endings and corrupt data.

---

## 8. Recovery (`open`)

```
 1. delete leftover  path.compact  and  path.init   (section 9.3)
 2. if path is missing or 0 bytes: create a new database (section 8.1)
 3. read the header; wrong magic -> error "not a kvdb file", file untouched;
    bad header_crc or unsupported version -> error, file untouched
 4. scan frames from offset 24:
      read in 16 MiB chunks, parse with struct.unpack_from on a memoryview
      for each frame, check validity rules (section 4)
      valid   -> apply its ops, in order, to the index
      invalid -> stop
 5. if the stopping offset < file_size: ftruncate(fd, stopping_offset), fsync
 6. set end_offset, ready
```

### 8.1 Creating a new database

Build `path.init` containing just the header, `fsync` it, `os.replace` it onto `path`, then `fsync` the directory (skipped on Windows). So `path` either does not exist or has a complete header. A crash mid-creation leaves at worst a stray `path.init`, deleted by the next open.

### 8.2 Why recovery is idempotent

Recovery's only change to disk is a truncate to a frame boundary. A crash during recovery leaves either the old file or the truncated file. Both are a valid prefix of frames, so the next `open()` reaches the same state. Recovery never writes new data.

### 8.3 What "valid prefix" means for each crash

| Where the kill lands | On disk | What `open()` does |
|---|---|---|
| Before any bytes of a frame | Previous frames only | Nothing to repair |
| Mid-write | Partial frame at the end | Frame fails rule 3 or 4, truncate to its start |
| After write, before fsync | Full frame, maybe not durable | Either it validates (kept) or is partial (cut). Both acceptable, as it was never ACKed |
| After fsync, before ACK | Full valid frame | Kept. The caller never heard back, but the data is durable |
| After ACK | Full valid frame | Kept |

---

## 9. Compaction

Rewrites the file with only live data, so it can shrink.

### 9.1 Sequence (holding `lock` the whole time)

```
 1. create  path.compact  with header, generation + 1
 2. write all live keys as frames of up to 1,000 puts each (reading values from the old file)
 3. fsync  path.compact
 4. close the old fd           (required on Windows, harmless elsewhere)
 5. os.replace(path.compact, path)           <-- the atomic swap
 6. fsync the directory                      (skipped on Windows)
 7. reopen the fd on path; rebuild index offsets from the new file
```

### 9.2 Crash analysis

| Kill during | On disk | Result after next `open()` |
|---|---|---|
| Steps 1-3 | `path` is the complete old file; `path.compact` is partial | Temp deleted, old file used |
| Step 5 (during the replace) | Either old or new `path`, both complete | Whichever is there is used |
| After step 5, before 6 | New `path` is in place; the rename may not be durable | New or old file, both complete; leftovers deleted |
| After step 6 | New file, durable | Normal |

The key property: **`os.replace` is atomic, and the file at `path` is always complete.** The temp file is never read as data.

### 9.3 Leftover files

On every `open()`, `path.compact` and `path.init` are deleted unconditionally. This is safe because `path` is always authoritative.

### 9.4 Triggers

- Manual: `compact` CLI command and a `COMPACT` serve command (so the torture tool can trigger it).
- Automatic: when `file_size > 4 MiB` and `file_size > 2 * live_bytes`, checked after a write.
- On `DiskFullError` from a write, compact once and retry.

---

## 10. Disk-full (`max_bytes`)

`open(path, max_bytes=N)`. `None` means unlimited.

- `reserve = min(65536, N // 8)`.
- A write frame containing any put must satisfy `end_offset + frame_size <= N - reserve`.
- A frame containing **only deletes** may use the reserve: `end_offset + frame_size <= N`.
- The check happens **before any bytes are written**, so a refused write touches nothing. Failure raises `DiskFullError` with a clear message. The database stays fully readable.
- The data file and the compaction temp file are each bounded by `N` individually. If live data does not fit, compaction raises `DiskFullError`, deletes the temp file, and leaves the old file untouched.

**Why the reserve exists:** without it, a full file could not even write a delete, and E5 (deletes free space, compaction runs, writes work again) would deadlock.

---

## 11. Concurrency

- One global `threading.Lock` covers put, delete, batch, get, compact and close.
- Writes are therefore serialised: frames never interleave, and a reader sees the state before or after a batch, never between.
- No cross-process locking in v1 (see Limitations).

---

## 12. Platform notes (Linux and Windows)

| Topic | Linux | Windows |
|---|---|---|
| Positioned I/O | Could use `pread`/`pwrite` | **Not available.** Use `lseek` + `read`/`write` under the lock |
| Binary mode | Not needed | **Required:** `os.O_BINARY` in `os.open` flags |
| Directory `fsync` | `os.open(dir, O_RDONLY)` then `fsync` | **Cannot be done.** Skip it (guard with `sys.platform == "win32"`) |
| Replace | `os.replace` works with the file open | Close all handles to the target first |
| Kill the child (torture tool) | `proc.kill()` (SIGKILL) | `proc.kill()` (TerminateProcess) |
| Self-kill (crash injection) | `os.kill(os.getpid(), signal.SIGKILL)` | `os._exit(137)` |
| `serve` stdin/stdout | Use binary-safe handling; hex-encode keys and values | Same. Avoid text-mode newline translation surprises |

Use **`proc.kill()`** in the torture tool on both platforms, since `signal.SIGKILL` does not exist on Windows.

---

## 13. Limitations (documented, not hidden)

1. **Frame size cap.** A frame payload is capped at 256 MiB, and the whole frame is built in memory before writing. A 10,000-op batch of 1 MB values (about 10 GB) is **rejected** with a clear error. Large batches work as long as the total stays under the cap. The torture tool's `large-batches` mode uses small values in big batches, and `large-values` uses 1 MB values in small batches.
2. **Recovery is O(file size).** Every open replays the whole log. Replay speed at 1,000,000 keys is **unmeasured**. A checkpoint frame (reserved `kind = 2`) is the planned fix if the hour-1 benchmark exceeds 1.5 s.
3. **Mid-file corruption is not handled.** Recovery treats the first invalid frame as the tail and truncates there. That is correct for torn writes, but if a frame in the *middle* of the file were corrupted (bit rot, not tested), every frame after it would be discarded too. Detection idea: the CRCs already detect it; we could refuse to truncate when valid frames follow the bad one, and report an error instead.
4. **Power loss is not claimed.** The design assumes the process dies, not the OS. We call `fsync` before every ACK, so it likely survives power loss on honest hardware, but we have not tested it.
5. **Windows directory durability.** On Windows we skip the directory `fsync` after `os.replace`. Without it, a crash right after a swap may leave the old file in place. Both files are complete, so no data is lost, but behaviour differs from Linux. Not verified on NTFS.
6. **Compaction blocks everything.** The global lock is held for the whole compaction, so all reads and writes stall while it runs.
7. **Disk usage can reach 2x `max_bytes`** during compaction, since the temp file and the data file are each bounded separately.
8. **No cross-process locking.** Two processes opening the same file at once can corrupt it. Threads in one process are safe.
9. **Memory.** The whole key set lives in RAM. 1M keys costs roughly 100 to 200 MB (unmeasured).
10. **Deletes of missing keys** still write a frame. This is harmless but wastes space until compaction.
11. **`fsync` failure poisons the instance.** After a failed `fsync` the only recovery is closing and reopening.
12. **Wall-clock kill timing is not exactly reproducible.** A `--seed` reproduces the op stream and the chosen delays, but the OS decides where in an operation a timed kill lands. Crash-point injection (section 14) is the deterministic alternative.

---

## 14. Crash-point injection (testing hook)

`KVDB_CRASH_POINT=<name>[:<nth>]` makes the engine kill itself the `nth` time (default 1) it reaches the named point. With the variable unset, the hook does nothing.

| Point | Where |
|---|---|
| `before_write` | after step 3 of the write path, before any bytes |
| `mid_write` | after writing half of the frame |
| `after_write_before_fsync` | after step 4, before step 5 |
| `after_fsync_before_index` | after step 5, before step 6 |
| `after_fsync_before_ack` | after step 6, before the ACK is printed |
| `compact_after_tmp` | after compaction step 3 |
| `compact_before_replace` | after step 4, before step 5 |
| `compact_after_replace` | after step 5, before step 6 |
| `compact_after_dirsync` | after step 6 |
| `recovery_before_truncate` | recovery step 5, before the truncate |
| `recovery_after_truncate` | recovery step 5, after the truncate |

---

## 15. Open items

- [ ] Hour-1 benchmark: full-replay time at 1M keys, and memory. Decides whether a checkpoint is needed.
- [ ] Ask a judge: does the compaction temp file count toward `max_bytes`?
- [ ] Ask a judge: how are the 1M keys for F3 written (puts or batches)?
- [ ] Confirm `os.replace` behaviour on the Windows teammates' machines in the first hour (a 10-line test: write, fsync, close, replace over an existing file).
