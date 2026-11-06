# BREAKING.md: Crash Analysis & Invariant Proofs

This document analyzes the exact disk states, crash behavior, and recovery guarantees of `kvdb` across all failure boundaries.

---

## 1. The Solitary Durability Point

Durability in `kvdb` rests on a single barrier: `os.fsync(fd)` inside `src/kvdb/db.py:DB._append_frame`.

```
0. Lock -> 1. Validate -> 2. Frame in RAM -> 3. Cap check -> 4. os.write -> 5. os.fsync -> 6. Index -> 7. Unlock -> 8. ACK
                                                                            ^^^^^^^^^^^^
                                                                         DURABILITY POINT
```

- **Before Step 5 (`fsync`):** The write is unacknowledged. The OS page cache or disk buffer may hold partial or complete frame bytes, but the client has received no durability promise. Recovery truncates torn bytes to the last intact frame.
- **After Step 5 (`fsync`):** The frame is on disk with a valid CRC32. Even if the process is killed before Step 6 (index update) or Step 8 (`ACK` transmission), recovery replays the valid frame into the index. The write is permanently preserved.
- **Fail-Safe Poisoning:** If `os.fsync` raises `OSError`, the instance poisons itself (`DB._poisoned = True`). Future writes fail with `DBPoisonedError`, preventing corrupt or partial transactions from ever being acknowledged.

---

## 2. Crash-Point Injection Matrix (11 Named Points)

Triggered deterministically via `KVDB_CRASH_POINT=<name>[:<nth>]`, executing instantaneous exit (`os._exit(137)` on Windows, `SIGKILL` on POSIX).

| Name | Location & Line-Level Step | Disk State at Termination | Recovery & Invariant Guarantee |
|---|---|---|---|
| `before_write` | `src/kvdb/db.py:155` (after step 3 cap check, before `os.write`) | File ends at previous frame; zero new bytes written. | Clean log. Unacknowledged write never reached disk. Prior data fully intact. |
| `mid_write` | `src/kvdb/db.py:171` (inside step 4 write loop, after writing half) | Tail contains short frame or partial payload. | Fails `FORMAT 4` Rule 3/4 (CRC mismatch). Recovery truncates torn tail (`ftruncate` + `fsync`). Zero torn bytes returned. |
| `after_write_before_fsync` | `src/kvdb/db.py:184` (after step 4 write, before step 5 `fsync`) | Complete frame in OS buffer, un-fsynced. | Process death preserves OS buffers. If intact, CRC validates and recovery applies it; if partial, CRC fails and tail is truncated. Legal either way because no ACK was sent. |
| `after_fsync_before_index` | `src/kvdb/db.py:193` (after step 5 `fsync`, before step 6 index) | Complete, durable frame fsynced on disk. | Replay validates frame CRC and inserts keys into index. Data survives despite missing caller notification. |
| `after_fsync_before_ack` | `src/kvdb/db.py:202` (after step 6 index, before stdout `ACK`) | Complete, durable frame fsynced on disk. | Same as above. Client experiences connection loss; restart guarantees all written keys are present. |
| `compact_after_tmp` | `src/kvdb/compaction.py:105` (after writing and fsyncing `.compact`) | Main file intact at `path`; complete new file at `path.compact`. | Main file is authoritative. `init_db`/`open` deletes leftover `*.compact`. Zero live data lost. |
| `compact_before_replace` | `src/kvdb/compaction.py:111` (after closing main fd, before `os.replace`) | Main file intact; `path.compact` complete; fd closed. | Main file untouched. Next `open` cleans up temporary file and resumes from original database. |
| `compact_after_replace` | `src/kvdb/compaction.py:117` (after atomic `os.replace`, before dirsync) | `path` atomically swapped to new file; old file gone. | Atomic filesystem rename (`rename(2)` / `MoveFileExW`). New generation database active and authoritative. |
| `compact_after_dirsync` | `src/kvdb/compaction.py:130` (after directory `fsync`, before unlock) | New file durable; directory durable; index not yet rebuilt. | Reopened database immediately reads new generation file and builds fresh in-memory index. |
| `recovery_before_truncate` | `src/kvdb/recovery.py:205` (after detecting torn tail, before `ftruncate`) | File contains valid prefix + corrupt trailing tail bytes. | Read-only scan changed nothing on disk. Subsequent `open` repeats identical scan and completes truncation. |
| `recovery_after_truncate` | `src/kvdb/recovery.py:208` (after `ftruncate` + `fsync`) | File truncated to exact frame boundary and fsynced. | File is clean. Subsequent `open` replays valid prefix with zero bytes to truncate. Fully idempotent. |

---

## 3. Platform Concurrency & Filesystem Edge Cases

### Windows Handle Discipline & Atomic Replacement
On Windows (NTFS), `os.replace(src, dst)` raises `PermissionError` (Win32 `ERROR_SHARING_VIOLATION` or `ERROR_ACCESS_DENIED`) if any process holds an open file handle to `dst`.
- **Engine Protocol (`src/kvdb/compaction.py:compact`):** Before executing `os.replace`, `compact()` takes the global write lock, flushes and closes `db._fd`, performs `os.replace(compact_path, path)`, and immediately reopens `path` with `os.open(..., O_RDWR | O_BINARY)`.
- **Directory Durability:** POSIX requires `fsync` on the parent directory to commit directory entries. On Windows, directory file descriptors cannot be opened or fsynced; the engine guards directory fsync with `sys.platform != "win32"`. Crash recovery relies on NTFS transactional metadata guarantees.

### Idempotent Torn-Tail Truncation
Recovery never writes new records. Its sole mutation is `os.ftruncate(fd, valid_end)` followed by `os.fsync(fd)`. If a power loss or crash kills the system during truncation, the file size is either its pre-truncation length (torn tail present) or post-truncation length (clean). The next recovery scan reaches the exact same logical stop point.

### Auto-Compaction on Disk-Full (Scenario E5)
When a write encounters `DiskFullError` (`end_offset + frame_size > max_bytes - reserve`), `DB._append_frame` automatically triggers `DB.compact()`. If obsolete versions or deletes exist, compaction shrinks the database below `max_bytes`, and the write is retried within the locked critical section. Deletes are permitted into the safety margin (`reserve = min(65536, max_bytes // 8)`), ensuring full reclaimability.
