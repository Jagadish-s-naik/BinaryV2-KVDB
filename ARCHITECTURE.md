# ARCHITECTURE.md

**Status: pre-implementation draft.** Written before any code exists. Every "Where in the code" entry is a *planned* name from the module map in section 2, and every "Verified by" entry is `not verified` until someone has actually run it and recorded the result. Do not change a row to "verified" without a real run (mode + seed) on record. A row that claims something the code does not do costs points.

The byte-level format lives in [`docs/FORMAT.md`](docs/FORMAT.md) and is not repeated here. Section numbers like "FORMAT 7" refer to it.

---

## 1. Architecture diagram

### 1.1 Components and locks

```mermaid
flowchart TB
    subgraph Clients["Callers"]
        CLI["CLI: put/get/del/batch/dump/check/open"]
        SERVE["serve: 8 worker threads, ACK printer"]
        LIB["Library API"]
    end

    subgraph Engine["kvdb engine (one process)"]
        LOCK{{"GLOBAL LOCK: threading.Lock<br/>covers put, delete, batch, get, compact, close"}}
        WP["Write path<br/>validate, build frame, cap check, write, fsync"]
        IDX["In-memory index<br/>dict: key to packed(value_offset, value_len)"]
        GUARD["Disk-full guard<br/>max_bytes, reserve, checked BEFORE writing"]
        REC["Recovery routine<br/>scan frames, apply, truncate torn tail"]
        COMP["Compaction routine<br/>write path.compact, fsync, os.replace, dir fsync"]
        CP["Crash-point hooks<br/>KVDB_CRASH_POINT env var"]
    end

    subgraph Disk["Disk"]
        DATA[("path: header + frames<br/>the ONLY authoritative state")]
        TMP[("path.compact / path.init<br/>temporary, deleted on open")]
    end

    CLI --> LIB
    SERVE --> LIB
    LIB --> LOCK
    LOCK --> WP
    LOCK --> IDX
    WP --> GUARD
    WP --> DATA
    WP --> IDX
    REC --> DATA
    REC --> IDX
    COMP --> TMP
    TMP -. "os.replace (atomic)" .-> DATA
    CP -. "can kill the process at named points" .-> WP
    CP -. "..." .-> COMP
    CP -. "..." .-> REC
```

**Locks:** exactly one, `DB._lock` (a `threading.Lock`). Every public operation, including `get`, `compact` and `close`, takes it. `serve` has a second lock that only serialises writes to stdout so ACK lines never interleave. It protects no database state.

### 1.2 File layout

```
+--------------------------- Header: 24 bytes ----------------------------+
| magic 8 | version 4 | generation 8 | header_crc 4  (CRC over first 20) |
+--------------------------------------------------------------------------+
+--- Frame (repeated) --------------------------------------------------------+
| frame_len 4 | crc32 4 | kind 1 | op_count 4 | op ... op                       |
|             |         |<------------ payload = frame_len bytes ------------>|
| crc32 covers: frame_len (4 bytes) || payload                                 |
+------------------------------------------------------------------------------+
  op = type 1 | key_len 2 | val_len 4 | key | value

No separate commit marker: a frame whose CRC validates IS committed.
Index is not stored on disk (v1); it is rebuilt by replaying frames.
```

### 1.3 Write ordering (numbered)

```mermaid
flowchart TB
    S0["0. acquire GLOBAL LOCK"] --> S1["1. validate inputs<br/>fail: ValueError, nothing written"]
    S1 --> S2["2. build the complete frame in memory<br/>frame_len, CRC, payload"]
    S2 --> S3["3. disk-full check<br/>fail: DiskFullError, nothing written"]
    S3 --> S4["4. os.lseek to end_offset, os.write the whole frame<br/>(loop on short writes)"]
    S4 --> S5["5. os.fsync(fd)<br/>THE DURABILITY POINT"]
    S5 --> S6["6. update index + end_offset<br/>(apply ops in order)"]
    S6 --> S7["7. release lock"]
    S7 --> S8["8. return success: serve prints ACK id and flushes stdout"]
    S4 -. "write error" .-> F1["ftruncate(end_offset); re-raise<br/>if truncate fails: POISON"]
    S5 -. "fsync error" .-> F2["POISON: never retry fsync"]
```

Crash-point hooks sit between these steps: `before_write` (after 3), `mid_write` (inside 4), `after_write_before_fsync` (after 4), `after_fsync_before_index` (after 5), `after_fsync_before_ack` (after 6). See FORMAT 14.

### 1.4 Recovery flow

```mermaid
flowchart TB
    O1["open(path)"] --> O2["delete leftover path.compact and path.init"]
    O2 --> O3{"path missing or 0 bytes?"}
    O3 -- yes --> N1["write path.init with header, fsync,<br/>os.replace to path, fsync directory"]
    N1 --> READY
    O3 -- no --> O4{"header valid?<br/>magic, version, header_crc"}
    O4 -- no --> ERR["raise error, file untouched"]
    O4 -- yes --> O5["scan frames from offset 24"]
    O5 --> O6{"next frame valid?<br/>FORMAT 4, rules 1-8"}
    O6 -- yes --> O7["apply its ops in order to index"]
    O7 --> O5
    O6 -- "no, or end of file" --> O8{"stop offset < file size?"}
    O8 -- yes --> O9["ftruncate to stop offset, fsync"]
    O8 -- no --> READY
    O9 --> READY["READY: set end_offset, accept calls"]
```

### 1.5 Compaction flow

```mermaid
flowchart TB
    C0["hold GLOBAL LOCK"] --> C1["1. create path.compact, header generation+1"]
    C1 --> C2["2. write live keys as frames of up to 1000 puts<br/>fail on cap: DiskFullError, delete temp, old file untouched"]
    C2 --> C3["3. fsync path.compact"]
    C3 --> C4["4. close old fd"]
    C4 --> C5["5. os.replace(path.compact, path)  ATOMIC SWAP"]
    C5 --> C6["6. fsync directory (skipped on Windows)"]
    C6 --> C7["7. reopen fd, rebuild index offsets, release lock"]
```

---

## 2. Planned module map (the contract)

Rows in the proof table reference these names. If the code uses different names, **update the table, not the other way round.**

| Module (`src/kvdb/`) | Planned contents |
|---|---|
| `frame.py` | `encode_frame(ops)`, `parse_frame(buf, pos, file_size)` (applies FORMAT 4 rules 1-8) |
| `db.py` | class `DB`: `open`, `put`, `delete`, `batch`, `get`, `close`, `compact`, `_append_frame`, `_check_cap`, `_apply_ops` |
| `recovery.py` | `recover(fd, file_size)`: scan, apply, truncate; `cleanup_temp_files(path)` |
| `compaction.py` | `compact(db)`: the sequence in 1.5 |
| `crashpoints.py` | `crash_point(name)`: reads `KVDB_CRASH_POINT`, kills the process |
| `errors.py` | `DiskFullError`, `DBPoisonedError`, `CorruptFileError` |
| `serve.py` | stdin parser, worker pool, `ACK` printer |
| `cli.py` | the 8 commands |
| `check.py` | `check(path)`: read-only integrity verification |

`_apply_ops` is the **single** function that applies a frame's ops to the index. The live write path and recovery both call it, so batch ordering (B5) cannot differ between "just written" and "after restart."

---

## 3. Scenario-to-mechanism proof table (32 rows)

**Legend for "Verified by":** every row currently reads `not verified`. The text after the dash is the *planned* test. When a row is actually run, replace it with the mode, seed and date, for example `torture --mode crash-points --seed 7, 2026-11-07, 100/100 passed`. "CP:" means crash-point injection via `KVDB_CRASH_POINT`.

"Where in the code" names are planned (section 2) until the code exists.

### Group A: crash during a single write

| ID | Disk state at the kill | Mechanism | Where in the code | Verified by |
|---|---|---|---|---|
| A1 | File ends at the previous frame; no bytes of the new frame written | Nothing to repair. No ACK was sent, so key is absent or holds its old value | `DB._append_frame` (before the write), `recovery.recover` | not verified. Planned: CP `before_write` |
| A2 | The tail holds part of the new frame (a short frame, or full length field but missing payload) | Frame fails FORMAT 4 rule 3 (runs past end of file) or rule 4 (CRC mismatch). Recovery stops there and truncates to the frame start. Torn bytes are never returned | `frame.parse_frame`, `recovery.recover` | not verified. Planned: CP `mid_write` |
| A3 | Frame fully written to the OS but `fsync` not yet called | Process death does not drop OS-cached data, so the frame usually validates and is kept; if it is partial it is cut. Either is allowed because no ACK was sent. Neither outcome damages earlier frames | `DB._append_frame`, `recovery.recover` | not verified. Planned: CP `after_write_before_fsync` |
| A4 | Frame fully written and `fsync` returned; index not yet updated or ACK not yet printed | Frame validates, so recovery rebuilds the key into the index. The caller never heard back, but the data is durable | `DB._append_frame`, `recovery.recover` | not verified. Planned: CP `after_fsync_before_index`, `after_fsync_before_ack` |
| A5 | Frame durable, ACK printed | Same as A4. ACK is printed only after step 5 (fsync) returns | `serve.py` ACK printer, `DB._append_frame` | not verified. Planned: default torture mode, oracle comparison |
| A6 | Delete frame torn or whole | A delete is a frame with one op of type 2. Torn: cut, key keeps its old value. Whole: key absent. Frame atomicity means a value is never corrupted | `DB.delete`, `frame.encode_frame`, `recovery.recover` | not verified. Planned: default mode with deletes; CP `mid_write` on a delete |
| A7 | A frame of about 1 MB, possibly part-written | `_append_frame` loops until every byte is written (short writes are normal for 1 MB). A torn large frame fails rule 3 or 4 and is cut. Value is stored whole inside one CRC-protected frame | `DB._append_frame`, `recovery.recover` | not verified. Planned: `torture --mode large-values` |

### Group B: crash during a batch

A batch is **one frame**, so there is no separate commit marker. B1 and B2 describe a marker-based design; the mapping to ours is stated in each row.

| ID | Disk state at the kill | Mechanism | Where in the code | Verified by |
|---|---|---|---|---|
| B1 | Part of the batch frame on disk (some ops' bytes, not all) | One frame: partial means rule 3 or 4 fails, so recovery truncates it. None of the 10 ops are visible | `frame.parse_frame`, `recovery.recover` | not verified. Planned: CP `mid_write` on a 10-op batch |
| B2 | "All ops written, no commit marker." **This state does not exist in our format.** The nearest states are (a) frame partial, so none visible, or (b) frame complete, so a valid CRC means all visible | If complete: the batch is fully present, which is legal under the invariant (it was never ACKed, so present or absent is allowed). If partial: none visible. Never some | `frame.parse_frame` | not verified. Planned: CP `after_write_before_fsync` on a batch. Note for the judge: B2's literal "none visible" assumes a marker design |
| B3 | Complete frame in the OS cache, `fsync` not called | Frame either validates (all ops) or is partial (none). The CRC makes "some" impossible | `recovery.recover` | not verified. Planned: CP `after_write_before_fsync` |
| B4 | Frame durable, ACK printed | All ops present after recovery | `DB.batch`, `serve.py` | not verified. Planned: default mode with batches |
| B5 | A frame containing `put X` and `del X` | Ops apply in order; the last op on X wins. The same `_apply_ops` runs live and in recovery, so the result is identical after a crash | `DB._apply_ops`, `recovery.recover` | not verified. Planned: a unit test for both orders, plus oracle check in default mode |
| B6 | A 10,000-op frame partly on disk | Same as B1: partial frame cut, none visible. Recovery scans file size only, so cost does not depend on the killed frame | `recovery.recover` | not verified. Planned: `torture --mode large-batches`. Recovery time under 2 s: not yet measured |

### Group C: crash during recovery and maintenance

| ID | Disk state at the kill | Mechanism | Where in the code | Verified by |
|---|---|---|---|---|
| C1 | Same as before open: replaying frames does not write to disk | Replay is read-only. The next open() sees the identical file and reaches the same state | `recovery.recover` | not verified. Planned: `torture --mode recovery-crash` |
| C2 | Mid-truncate: the file is either its old length (still has the torn tail) or the shorter length (clean) | `ftruncate` to a frame boundary is the only mutation. Either result is a valid prefix of frames (plus possibly a torn tail), so the next open() reaches the same state | `recovery.recover` | not verified. Planned: CP `recovery_before_truncate`, `recovery_after_truncate` |
| C3 | `path` is the complete old file; `path.compact` is partial or complete but not renamed | `path` is always authoritative. The next open() deletes `path.compact`. No data lost | `recovery.cleanup_temp_files`, `compaction.compact` | not verified. Planned: CP `compact_after_tmp`, `compact_before_replace`; `torture --mode compaction` |
| C4 | Kill during the `os.replace` itself | `os.replace` is a single atomic rename (rename(2) on Linux, MoveFileEx on Windows): `path` is the complete old file or the complete new file. **We cannot inject a kill inside the syscall;** we cover the points on either side and rely on the OS guarantee | `compaction.compact` | not verified. Planned: CP `compact_before_replace`, `compact_after_replace`; random kills in `--mode compaction` |
| C5 | New file in place at `path`; directory fsync may not have happened | Data intact. The temp name was consumed by the rename, so a leftover `path.compact` could only come from an earlier aborted run; any such file is deleted on open | `recovery.cleanup_temp_files` | not verified. Planned: CP `compact_after_replace`, `compact_after_dirsync` |
| C6 | Same as any kill: every write is already fsynced before ACK, so `close()` writes nothing that matters | `close()` takes the lock and closes the fd. It is not special and does not flush data that could be lost | `DB.close` | not verified. Planned: random kills at shutdown in default mode |

### Group D: concurrency

| ID | Disk state at the kill | Mechanism | Where in the code | Verified by |
|---|---|---|---|---|
| D1 | Whole frames from both writers, last frame possibly torn | The global lock serialises appends, so frames never interleave. Each ACK follows its own fsync, so every ACKed op is in the file | `DB._append_frame` (under `_lock`), `serve.py` worker pool | not verified. Planned: default mode (2+ writers) |
| D2 | Frames for the same key from both writers, in lock order | Each frame is atomic, so the final value is exactly one writer's value, never a mix. Oracle values embed their op id so the origin is checkable | `DB._append_frame`, `DB._apply_ops` | not verified. Planned: a torture phase where both writers hit a small key set |
| D3 | One batch frame and several single-put frames | The batch is one frame (all or nothing); the puts are separate frames, each with its own fsync before ACK | `DB.batch`, `DB.put` | not verified. Planned: `torture --mode large-batches` with a parallel put writer |
| D4 | No crash | `get` takes the same lock that the batch holds, and the index is updated only after the frame is durable, inside that lock. A reader sees before or after | `DB.get`, `DB._append_frame` | not verified. Planned: a non-crash unit test with a reader thread and a big batch |

### Group E: disk full

| ID | Disk state at the kill | Mechanism | Where in the code | Verified by |
|---|---|---|---|---|
| E1 | File unchanged: the check happens before any bytes are written | `_check_cap` computes `end_offset + frame_size` against `max_bytes - reserve` and raises `DiskFullError`. The index is untouched, reads keep working | `DB._check_cap` | not verified. Planned: `torture --mode disk-full` |
| E2 | File unchanged | The whole batch is one frame, so its full size is checked up front. The batch is rejected as a unit | `DB._check_cap`, `DB.batch` | not verified. Planned: `torture --mode disk-full` with batches |
| E3 | File ends at a valid frame (the refused write never touched it) | Restart is the normal recovery path; there is nothing special to repair | `recovery.recover` | not verified. Planned: `torture --mode disk-full` with random kills |
| E4 | `path` is the complete old file; `path.compact` is partial | If the temp file would exceed `max_bytes`, `compaction.compact` raises `DiskFullError`, deletes the temp, and leaves `path` untouched. A kill here is case C3 | `compaction.compact` | not verified. Planned: `torture --mode disk-full` + compaction. **Judge question open:** does the temp file count toward `max_bytes`? |
| E5 | File is full of garbage frames; deletes may use the reserve | Delete-only frames may use the `reserve`, so tombstones can be written. Compaction then shrinks the file, and writes succeed again. A write that hits the cap triggers one automatic compact-and-retry | `DB._check_cap`, `DB.compact`, `DB._append_frame` | not verified. Planned: a unit test: fill, delete, compact, put |

### Group F: repeated abuse

| ID | Disk state at the kill | Mechanism | Where in the code | Verified by |
|---|---|---|---|---|
| F1 | Any of the above | Every state is covered by the frame rules; the oracle checks the invariant after every kill | torture tool, `recovery.recover` | not verified. Planned: `torture --iterations 500` |
| F2 | The file after each kill is a valid prefix, possibly a shorter one | Recovery is idempotent (its only write is a truncate to a frame boundary) | `recovery.recover` | not verified. Planned: `torture --mode recovery-crash` |
| F3 | A log of 1,000,000 keys' frames | Full replay with a chunked, `memoryview` + `struct.unpack_from` scan. **Time is unmeasured.** If the hour-1 benchmark exceeds 1.5 s, a checkpoint frame (`kind = 2`) is the fix | `recovery.recover` | not verified. Planned: benchmark script |
| F4 | The last frame is 1 byte short | Fails FORMAT 4 rule 3, so it is truncated and open() succeeds. **We lose that whole last frame** (which could have been ACKed, since the cut was made by hand), not only unacknowledged data. This is the only loss possible from a tail cut | `recovery.recover` | not verified. Planned: a manual test |

---

## 4. The three guarantees

**Durability (once ACK is sent, the data survives).** For every put, delete or batch, the engine builds the complete frame, writes it with `os.write` (looping until every byte is accepted), and calls `os.fsync` on the file descriptor. Only after `fsync` returns does the engine update the index and release control; only then does `serve` print `ACK` and call `flush()` on stdout. So an ACK on the wire implies the frame was durable before it was printed. If `fsync` fails, the instance is poisoned and never retries, so a failed `fsync` is never reported as success. We rely on the stated assumption that once `fsync` returns, the bytes are on disk. We do not claim power-loss durability beyond that assumption.

**Atomicity (a batch is never half visible).** A batch is a single frame whose CRC32 covers its length field and all of its ops. Recovery applies a frame only if it passes all validity checks, including the CRC and an exact parse to the end of the payload, so a frame that is torn anywhere fails and is truncated whole. There is no state in which some ops of a batch are valid and others are not. In memory, the index is updated only after the frame's `fsync`, inside the same global lock that `get` takes, so a reader sees the state before the batch or after it, never in between.

**Recoverability (open() always succeeds on a file we produced).** Every state our code can leave on disk is a valid header followed by zero or more complete frames and at most one torn tail frame, because frames are only ever appended at `end_offset`, and a failed append is cut back with `ftruncate`. Recovery reads, validates, and at worst calls `ftruncate` to a frame boundary and `fsync`; it writes no new data, so a crash in the middle of it leaves a file that the next open() handles identically. New databases are created as `path.init` then `os.replace`d into place, and compaction builds `path.compact`, `fsync`s it, then `os.replace`s it over `path`: both swaps are single atomic renames, so `path` is always either the old complete file or the new complete file. Leftover temp files are deleted on open and never read as data.

---

## 5. Keeping this file honest

1. When code lands, replace every planned name in section 3 with the real `file:function`.
2. After each torture run, update the "Verified by" cell for the rows it exercises: mode, seed, date, pass count. Leave `not verified` if you did not run it.
3. Any change to the write order, recovery, compaction, locking, or disk-full logic updates FORMAT.md, sections 1.3 to 1.5 above, and the affected rows **in the same commit**.
4. Before the demo, each teammate picks six rows, reads the named function, and confirms the row is true. Fix the row or the code, not neither.
5. Known gaps to resolve: B2's wording (see its row), whether C4 can be injected, and whether the compaction temp file counts toward `max_bytes` (ask a judge).
6. Export the Mermaid diagrams to images in `diagrams/` before submission (the Mermaid live editor can export PNG or SVG), and keep the `.mmd` sources beside them.
