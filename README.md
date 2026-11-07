# kvdb

An embedded, single-file, crash-safe key-value database written in pure Python, plus a torture tool that kills it with SIGKILL and checks every acknowledged write.

- Python **3.14**, standard library only. No `pip install`, nothing to compile.
- Works offline. Tested on Linux and Windows.

## Build

There is no build step. Check the toolchain:

```
python3 --version      # 3.14.x  (use `python` on Windows)
```

Run the tests (optional):

```
python3 -m unittest discover tests
```

## Run

From the repository root. On Linux, `./kvdb` works directly. On Windows, or if the executable bit is lost, use `python kvdb`.

| Command | What it does |
|---|---|
| `./kvdb open <path> [--max-bytes N]` | Open or create the database, run recovery, print `OK keys=<n> recovery_ms=<t>` |
| `./kvdb put <path> <key> <value>` | Store a value. Prints `OK` once it is durable |
| `./kvdb get <path> <key>` | Print the value, or `NOTFOUND` |
| `./kvdb del <path> <key>` | Delete a key. Deleting a missing key is not an error |
| `./kvdb batch <path> <file>` | Apply a file of operations atomically (format below) |
| `./kvdb dump <path> [--hex]` | Print every key and value, sorted by key. `--hex` prints `<hexkey> <hexvalue>` per line |
| `./kvdb check <path>` | Verify file integrity, print `OK` or the reason it failed |
| `./kvdb serve <path> [--max-bytes N]` | Long-running mode: read commands on stdin, print `ACK <id>` after each durable write |
| `./kvdb compact <path>` | Rewrite the file with only live data so it can shrink |
| `./kvdb checkpoint <path>` | Append an in-line snapshot frame of the live index for sub-50ms fast startup |

`--max-bytes N` caps the file size. A write that would exceed it fails with `ERROR DISK_FULL <message>`, writes nothing, and leaves the database readable. Other errors print `ERROR <code> <message>`, and the exit code is non-zero.

CLI keys and values are text (UTF-8). A key cannot contain spaces. Bytes shown by `dump` that are not printable appear as `\xNN`.

### Batch file

One operation per line. The batch is applied as a single atomic operation.

```
put name Alice
put city New York
del oldkey
```

`put <key> <value>`: the key is the first word, the value is the rest of the line (it may contain spaces). `del <key>`. Operations apply in order, and **the last operation on a key wins**: `put X` then `del X` leaves X absent, and `del X` then `put X` leaves X present.

### `serve` protocol

Line based on binary stdin/stdout. Keys and values are lowercase hex-encoded so any bytes (including 1 MB values) are safe. An empty value is represented as `-`.

On startup, once recovery completes, `serve` prints:
```
READY keys=<n> recovery_ms=<t>
```

| Input | Reply |
|---|---|
| `PUT <id> <hexkey> <hexval>` | `ACK <id>` |
| `DEL <id> <hexkey>` | `ACK <id>` |
| `BATCH <id> <n>` then `n` lines of `put <hexkey> <hexval>` or `del <hexkey>` | `ACK <id>` |
| `GET <id> <hexkey>` | `VAL <id> <hexval>` or `NOTFOUND <id>` |
| `COMPACT <id>` | `ACK <id>` |
| `CHECKPOINT <id>` | `ACK <id>` |
| `QUIT` | (exits 0) |

Any failure replies `ERR <id> <code>` with code:
- `DISK_FULL`: would exceed `--max-bytes`, nothing written.
- `BAD_ARG`: key or value size out of range, or batch size 0 or > 10,000.
- `PARSE`: malformed line or invalid hex.
- `POISONED`: previous fsync failed; writes refused.
- `INTERNAL`: other unhandled error.

`ACK` is printed **only after the data is fsynced**. Commands run on 8 worker threads, so ids can complete out of order. All replies are flushed immediately.

### Torture tool

```
./torture --self-test
./torture --iterations 500 --seed 12345
./torture --iterations 100 --seed 999 --mode contention
./torture --iterations 100 --seed 999 --mode large-values
./torture --iterations 100 --seed 7   --mode compaction
./torture --iterations 100 --seed 42  --mode recovery-crash
./torture --iterations 1   --seed 1   --mode scale
```

Modes: `default`, `contention`, `large-values`, `large-batches`, `compaction`, `recovery-crash`, `disk-full`, `crash-points`, `scale`.
`./torture --self-test` runs an independent 16-scenario test suite proving that the oracle catches lost writes, partial batches, corrupted tail bytes, alien keys, invalid batch orderings, raw binary format/CRC corruptions, and live fault injections.
Each iteration starts `serve`, sends random operations from concurrent writers, records every ACK in its own oracle file, kills the process with SIGKILL (`TerminateProcess` on Windows) at a random moment (with microsecond-precision latency-adaptive kill strikes), then restarts it, runs `check`, and compares `dump --hex` against the oracle. It prints one line per iteration and a final pass/fail count, and stops at the first failure with expected vs found and the operation ids. When a failure occurs, the tool generates a self-contained reproducer script in `<workdir>/failure-<seed>-<iter>/reproduce.py` containing raw binary byte context inspection and an `oracle.log` timeline replay, an invariant violation report (`report.md`), and optional automatic packaging via `--zip-failure`. The seed reproduces the operation stream and the kill delays, but not the exact instruction at which a timed kill lands. The `crash-points` mode (and the `KVDB_CRASH_POINT` environment variable) makes the engine kill itself at a named point, which is deterministic. For full specification details, see [docs/TORTURE_SPEC.md](docs/TORTURE_SPEC.md).

### Diagnostic & WAL Inspector

Inspect any `.db` file's binary frames, CRC integrity, live keys, and dead payload bytes:

```
python tools/inspect_wal.py <path> [--frames] [--keys] [--timeline <oracle.log>] [--json]
```

## On-disk format

One data file. Everything is little-endian. Full details and the validity rules are in [docs/FORMAT.md](docs/FORMAT.md).

```
Header (24 bytes)
  magic "KVDBLOG\0" (8) | version = 1 (4) | generation (8) | header_crc (4)
  header_crc = CRC32 of the first 20 bytes

Then zero or more frames:
  frame_len (4) | crc32 (4) | payload (frame_len bytes)
  crc32 = CRC32 of (frame_len as 4 bytes) followed by payload

Payload
  kind (1) = 1 | op_count (4) | op ... op

Op
  type (1: 1 = put, 2 = delete) | key_len (2) | val_len (4) | key | value
```

```
+--------+---------+---------+-----+---------+------------------+
| Header | Frame 1 | Frame 2 | ... | Frame N | (torn tail?)     |
+--------+---------+---------+-----+---------+------------------+
          \_ frame_len | crc32 | kind | op_count | ops ... _/
```

A put, a delete and a batch are each **one frame**. A frame whose CRC validates is committed in full, so the CRC acts as the commit marker. Limits: keys 1 to 1,024 bytes, values 0 to 1 MiB, at most 10,000 operations per batch, at most 256 MiB per frame.

Example, `put("a", "1")` (the bytes after the header):

```
0E 00 00 00   frame_len = 14
93 78 1A 03   crc32
01            kind
01 00 00 00   op_count = 1
01            put
01 00         key_len = 1
01 00 00 00   val_len = 1
61 31         key "a", value "1"
```

## Recovery algorithm

When the database opens:

1. Delete any leftover `<path>.compact` and `<path>.init` temporary files. They are never trusted, because the main file is always complete.
2. If the file does not exist or is empty, create a new database (write a header to a temporary file, fsync it, rename it into place).
3. Check the header. If it is not ours, report an error and leave the file untouched.
4. Read frames from the start. For each one, check that the length is sane and fits in the file, that the CRC matches, and that the operations parse exactly. If it is valid, apply its operations in order to an in-memory index (key to file position of its value).
5. At the first frame that fails a check, stop. Everything before it is trusted. A frame that fails is a write that was cut off by the kill, and it was never acknowledged.
6. If there are bytes after the last valid frame, truncate the file to that point and fsync.

Recovery never writes new data. Its only change to disk is the truncation, so a crash during recovery leaves a file that the next open handles the same way.

## Durability, in one paragraph

A write builds the whole frame, writes it, calls `fsync`, and only then updates the index and returns success (`ACK`). One global lock protects all operations, so frames never interleave and a reader never sees half a batch. Compaction writes a new file containing only live data, fsyncs it, atomically renames it over the old file (`os.replace`), then fsyncs the directory (skipped on Windows).

## Known limitations

- A frame is built in memory and capped at 256 MiB, so a 10,000-operation batch of 1 MB values is rejected with a clear error.
- Recovery replays the whole log, so open time grows with file size.
- Corruption in the middle of the file (not a torn tail) is not repaired: recovery stops at the first bad frame.
- Two processes must not open the same file at once.
- On Windows the directory fsync after compaction is skipped.

The complete list is in [docs/FORMAT.md](docs/FORMAT.md) section 13. For crash analysis, see [BREAKING.md](BREAKING.md), and for the design and proof table see [ARCHITECTURE.md](ARCHITECTURE.md).
