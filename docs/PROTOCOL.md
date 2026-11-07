# PROTOCOL.md: the `serve` protocol and CLI contract

This is the **only interface** between the engine and the torture tool. The engine owner implements it; the torture-tool owner (and their agent) writes against it without reading engine source. If you change anything here, change it in one commit with both sides.

## 1. Conventions

- Transport: the child process's stdin (commands) and stdout (replies), as **binary pipes**. Never rely on text mode (Windows would translate newlines).
- One message per line, terminated by `\n` only. ASCII only. Fields are separated by **one space**.
- **Hex:** keys and values are lowercase hex. An **empty value is written as `-`** (keys are never empty).
- **Ids:** `[A-Za-z0-9._-]{1,64}`, chosen by the client, unique per command within a run.
- A reply is **complete only if it ends with `\n`**. A reader must ignore a final line that lacks one (the process may have been killed mid-print).
- stderr is for humans (diagnostics). The torture tool captures it to a file and never parses it.

## 2. Starting up

```
python kvdb serve <path> [--max-bytes N]
```

After recovery completes, the engine prints exactly one line:

```
READY keys=<n> recovery_ms=<t>
```

The client must not send commands before `READY`. If the file cannot be opened, the engine prints `FATAL <code> <message>` to stdout and exits non-zero.

## 3. Commands and replies

| Command (stdin) | Success reply (stdout) |
|---|---|
| `PUT <id> <hexkey> <hexval>` | `ACK <id>` |
| `DEL <id> <hexkey>` | `ACK <id>` |
| `BATCH <id> <n>` followed by exactly `n` lines, each `put <hexkey> <hexval>` or `del <hexkey>` | `ACK <id>` |
| `GET <id> <hexkey>` | `VAL <id> <hexval>` or `NOTFOUND <id>` |
| `COMPACT <id>` | `ACK <id>` |
| `CHECKPOINT <id>` | `ACK <id>` |
| `QUIT` | none; the process exits 0 |

Failure reply for any command: `ERR <id> <code>`. Codes:

| Code | Meaning | Did the operation take effect? |
|---|---|---|
| `DISK_FULL` | would exceed `--max-bytes`, nothing written | **No** |
| `BAD_ARG` | key or value size out of range, bad batch size (`n` of 0 or over 10,000 is `BAD_ARG`) | **No** |
| `PARSE` | malformed line; the id may be `?` if unreadable | **No** |
| `POISONED` | a previous fsync failed; the instance refuses writes | **No** (reads still allowed) |
| `INTERNAL` | anything else | **Unknown**, treat as pending |

For a malformed `BATCH` group, the engine replies `ERR <id> PARSE`, skips the `n` announced lines if it can, and continues.

## 4. Durability and ordering rules (what the tool may assume)

1. **`ACK` is printed only after the data is fsynced**, and stdout is flushed immediately after. This is the promise the oracle checks.
2. Replies may arrive **in any order**. Match by id.
3. Commands are read in order and handed to a pool of 8 worker threads. **Two commands that overlap in time may take effect in either order.** If a client sends command B only after it has received the reply to command A, then A takes effect before B.
4. A `BATCH` group (header plus its `n` lines) must be written to stdin **contiguously**. A client with several writer threads must hold a client-side lock while writing a whole group, but must **not** wait for replies while holding it.
5. Clients may **pipeline**: send further commands without waiting for earlier replies. The torture tool relies on this so something is always in flight when the kill lands.
6. `GET` sees either the state before a batch or after it, never in between.
7. `COMPACT` blocks other operations for its duration (global lock). It replies `ACK` after the swap is durable.

## 5. Shutdown

- `QUIT`, or EOF on stdin: finish in-flight commands, close the database, exit 0.
- Any kill: no cleanup runs, and none is needed.

## 6. CLI contract used by the tools

All commands print to stdout; exit code 0 means success.

| Command | Output | Exit code |
|---|---|---|
| `open <path> [--max-bytes N]` | `OK keys=<n> recovery_ms=<t>` | 0, or non-zero with `ERROR <code> <message>` |
| `check <path>` | `OK`, or `FAIL <reason>` | 0 only if `OK`. **Read-only: must not modify the file** |
| `dump <path> --hex` | one line per key, sorted by key: `<hexkey> <hexval>`, `-` for an empty value | 0 |
| `compact <path>` | `OK` | 0 |
| `checkpoint <path>` | `OK` | 0 |

`open` runs full recovery (including truncating a torn tail), so it may modify the file. `check` and `dump` must not.

## 7. Crash-point injection

Environment variable `KVDB_CRASH_POINT=<name>[:<nth>]`, honoured by `serve`, `open` and `compact`. The process kills itself (SIGKILL on Linux, `os._exit(137)` on Windows) the `nth` time (default 1) it reaches the named point. When the variable is unset, nothing happens.

Names: `before_write`, `mid_write`, `after_write_before_fsync`, `after_fsync_before_index`, `after_fsync_before_ack`, `compact_after_tmp`, `compact_before_replace`, `compact_after_replace`, `compact_after_dirsync`, `recovery_before_truncate`, `recovery_after_truncate`. Meaning of each: FORMAT.md section 14.

## 8. Changes this makes to README.md (do these when the engine is written)

- `serve` prints `READY ...` at start.
- Empty values are `-` in `serve` and in `dump --hex`.
- `serve` error codes list.
- Torture tool gains a `scale` mode.
