# TORTURE_SPEC.md: the torture tool

Read with [PROTOCOL.md](PROTOCOL.md). The tool is written against those two documents and the invariant below. **It must not read, import or depend on engine source** (`src/kvdb/`); that is what keeps the oracle independent.

## 1. Purpose and rules

The tool tries to break the database and proves, with its own records, whether the invariant held. It is worth more than the engine in scoring and breaks ties, so it must be trustworthy.

Hard rules (any violation can be read as a rigged tool, which is a disqualification):
1. **The oracle is the tool's own record of what was sent and acknowledged.** It never trusts `dump` as truth. `dump` is the thing being judged.
2. Kills are **SIGKILL** (`proc.kill()` works on both Linux and Windows). Never SIGTERM, never `QUIT` before a kill.
3. Kill timing is a **random delay in milliseconds**, not "after N operations."
4. Kills must land **during** operations: writers keep several commands in flight (pipelined) at all times.
5. Never loosen a check to make a failure go away. A failure means fix the engine, or prove the check wrong with a written argument.
6. Standard library only. Python 3.14. No pip.

## 2. The invariant, as testable rules

After a kill and a restart:
1. `open` succeeds, and `check` prints `OK`.
2. Every operation that received `ACK` is reflected (taking later operations into account, section 6).
3. Every operation that did not receive `ACK` is either fully applied or fully absent. A batch is all-or-nothing.
4. No key holds a value that was never written to that key (no garbage, no torn value, no value from another key).
5. `keys=<n>` from `open` equals the number of lines from `dump`.

## 3. Command line

```
./torture --seed S [--iterations N] [--mode M] [--workdir DIR] [--writers W] [--inflight K]
          [--kill-min-ms A] [--kill-max-ms B] [--kvdb PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--seed` | **required** | Master seed |
| `--iterations` | 500 | Number of kill/restart cycles |
| `--mode` | `default` | See section 8 |
| `--workdir` | `./torture-work` | Must be on a real disk, **not tmpfs or a RAM disk** |
| `--writers` | 2 (minimum 2) | Concurrent writer threads |
| `--inflight` | 16 | Max unanswered commands per writer |
| `--kill-min-ms`, `--kill-max-ms` | 5, 600 | Kill delay range |
| `--kvdb` | `<repo>/kvdb` | The engine launcher; run as `[sys.executable, kvdb, ...]` |

Exit codes: **0** all iterations passed, **1** an invariant violation was found, **2** the tool itself or the environment failed (bad flags, the engine did not start, etc.). Never report a tool crash as a database failure or the reverse.

## 4. One iteration (default flow)

1. Start `kvdb serve <db>` with pipes. Wait for `READY` (timeout 30 s). Record `recovery_ms`.
2. Start the writer threads (and the reader thread, section 6.4). Each writer generates operations from its own seeded RNG, records `SENT` in the oracle, sends the command, and keeps up to `--inflight` unanswered.
3. A reader thread parses replies, timestamps each reply, and records `ACK` or `ERR` in the oracle.
4. Draw `kill_delay_ms` uniformly from `[kill-min, kill-max]`. Measured from the first command sent. At that moment call `proc.kill()`, then `proc.wait()`. Record how many commands were in flight, and of what kind.
5. Freeze the oracle (no more updates). Ignore any partial final stdout line.
6. Copy the database file to `<workdir>/last-post-kill.db` (kept only for the failure report).
7. Run `kvdb open <db>`, then `kvdb check <db>`, then `kvdb dump <db> --hex`. Check section 2 rule 1 and rule 5.
8. Run the checks in section 6. On success, the verified dump becomes the **baseline** for the next iteration.
9. Print the iteration line (section 9).

State carries across iterations: the database file persists for the whole run, and iteration 1 starts from an empty database. This tests recovery on a growing file.

## 5. Workload

**Determinism.** `Random(f"{seed}:{iteration}:{writer}")` per writer. Thread scheduling and kill timing are not deterministic, so the tool says so in its README; the operation stream and delay choices are.

**Ids.** `i<iteration>.w<writer>.<n>`, with `n` counted per writer. Unique across the run.

**Values.** Every value is a deterministic function of its op id so the oracle never has to store it:
`"v:" + op_id + ":" + key_hex + ":" + padding` where padding is pseudo-random bytes from `Random(op_id)`. The oracle records `len` and `sha256` of each value in its `SENT` line. A value found in `dump` is accepted only if its bytes match **an operation that was sent to that key**, which catches torn values, garbage, and values from other keys.

**Key spaces**
- **Shared keys:** `s:0` ... `s:19` (configurable by mode). Written by single `PUT` and `DEL` from all writers. This produces same-key contention (D2).
- **Batch keys:** `b:<batch_id>:<i>`. Written **only** by that batch. Batches never touch shared keys. This keeps all-or-nothing checkable; it is a deliberate limit.

**Batch contents.** A batch contains puts to fresh `b:` keys, and for some keys a `put` followed by a `del` of the same key (expected absent) or a `del` followed by a `put` (expected present). Every batch has at least one surviving put. Compute the **expected map** `E` by applying the ops in order (last op on a key wins). This also exercises scenario B5.

**Default mix per writer:** 60% single put, 15% single delete, 25% batch of 2 to 50 operations.

## 6. The oracle

### 6.1 Records

An append-only file `<workdir>/oracle-<iteration>.log`, owned by the tool (the tool is never killed, so it needs no fsync):
```
SENT <id> <PUT|DEL|BATCH> <details> <monotonic_ns>
ACK  <id> <monotonic_ns>
ERR  <id> <code> <monotonic_ns>
```
Timestamps use `time.monotonic_ns()`. `SENT` is recorded **before** the write to the pipe, and `ACK` is stamped when the reply line is read. Both biases only make the checker more permissive, so they cannot cause false failures.

### 6.2 Op states

| State | Rule |
|---|---|
| ACKED | `ACK` received |
| FAILED | `ERR` with `DISK_FULL`, `BAD_ARG` or `PARSE`: definitely **not** applied |
| PENDING | no reply, or `ERR INTERNAL` / `ERR POISONED` |

### 6.3 Check: shared keys

For each shared key `K`, compute the candidate set `C` of operations that could be the final writer:
- every ACKED or PENDING operation `x` on `K`, **except** `x` is excluded if some ACKED operation `y` on `K` was sent after `x` was acknowledged (`sent_y > ack_x`), because `y` must come after `x`;
- plus the **baseline** value of `K` (absent if never set), allowed only if `K` has **no** ACKED operation this iteration.

The value found in `dump` for `K` (or "absent") must equal the result of one candidate (a put's value, or absent for a delete or an absent baseline). Otherwise: **violation**. A value that matches an operation id marked FAILED is also a violation, and so is a value that matches no operation sent to `K` at all.

Also: any key in the dump that is neither in the baseline nor touched by any operation sent this iteration is a violation, and any key in the baseline untouched this iteration must be unchanged.

### 6.4 Check: batches (all or nothing)

For each batch `b` with expected map `E`, let `P` be the keys where `E` has a value, and `c` the number of keys in `P` whose found value is exactly `E`'s.
- Any `b:` key holding a value other than `E`'s, or any key in `E` that `E` says is absent but is present: **violation**.
- `c` must be `0` or `|P|`. Anything between is a **partial batch: violation**.
- If `b` is ACKED, `c` must be `|P|`.
- If `b` is FAILED, `c` must be `0`.

### 6.5 Check: reader (D4, no crash needed)

A reader thread picks a batch that is currently PENDING, sends `GET` for its **last** surviving key, waits for the reply, then `GET` for its **first** surviving key. If the first read shows the batch's value, the second **must too**. Seeing the last key present but the first absent means a reader observed a half-applied batch: **violation**. Reading in this order is what makes the test sound with non-atomic separate `GET`s.

## 7. Failure report

On the first violation the tool stops and prints:

```
FAIL iter 17 (seed 12345, mode default)
RULE:      shared key: found value not allowed
KEY:       s:3
FOUND:     op i17.w1.42 (PUT, ACKED at +183.2 ms)  sha256=ab12..
EXPECTED:  one of: i17.w0.51 (PUT, ACKED at +201.5 ms, sent after FOUND was acked), i17.w1.60 (DEL, PENDING)
OPS:       i17.w1.42, i17.w0.51, i17.w1.60
ARTIFACTS: torture-work/failure-12345-17/
```

`ARTIFACTS` holds the post-kill database copy, the oracle file, the engine's stderr, and the full list of operations on the failing key. **The saved database file is the reproducer.** Running `kvdb open`, `check` and `dump` on it reproduces open-time failures regardless of kill timing, which a seed alone cannot. Exit code 1.

## 8. Modes

| Mode | What differs from `default` |
|---|---|
| `default` | Mix in section 5, 2 writers + reader, random kill |
| `large-values` | 50% of puts carry 512 KiB to 1 MiB values; batches of 1 to 5 such ops. Verified by `sha256`. Targets A7 |
| `large-batches` | Writer 0 sends batches of 1,000 to 10,000 small operations; other writers send single puts at the same time. Targets B6 and D3 |
| `compaction` | About 40% deletes and overwrites to create garbage, plus a thread sending `COMPACT` every 50 to 300 ms. Kill at random. After the iterations, a shrink check: write 100,000 keys, delete them all, `COMPACT`, and require the file to be under 5% of its peak size. Targets C3 to C5 |
| `recovery-crash` | Run one normal iteration, then **append 1 to 64 random bytes to the file** (a simulated torn tail; never truncate, which could remove ACKed data). Then run `kvdb open` up to 3 times, each killed after a random delay in `[0, baseline_open_ms]`, and finally open normally and run the checks. Count only kills that landed **while open was still running**. Targets C1, C2 and F2 |
| `disk-full` | Start `serve` with `--max-bytes` between 1 and 4 MiB. Require: `ERR DISK_FULL` replies, no FAILED op's value ever present, the file never larger than the cap, `check` OK, reads still working. Then delete most keys, `COMPACT`, and require that a new `PUT` is `ACK`ed (E5). Random kills in some iterations (E3). Targets E1 to E5 |
| `crash-points` | Each iteration picks a point name and `nth` (1 to 20) from the seed and runs with `KVDB_CRASH_POINT`. The engine kills itself; the tool treats any exit it did not cause as that kill. Compaction points get `COMPACT` commands; recovery points use the `recovery-crash` flow with the variable set on `open`. Record whether the point was **reached** (process died) or not (timed out after 10 s) and report both. Targets A1 to A4, B1 to B3, C1 to C5 |
| `scale` | Write 1,000,000 keys using 100 batches of 10,000, then SIGKILL after the last `ACK`, then `open`. Require every key present, and `recovery_ms` under 2000 (print a loud `WARN` otherwise). Targets F3 |

## 9. Output

One line per iteration:
```
iter  17/500  PASS  acked=312 pending=9 inflight_at_kill=5 batch_inflight=1 kill_ms=237 recovery_ms=41
```
Final:
```
RESULT: 500 passed, 0 failed  (seed 12345, mode default)
COVERAGE: kills with >=1 op in flight 497/500 | with a batch in flight 211/500 |
          with a >=256 KiB value in flight 0/500 | during COMPACT 0/500 | during recovery 0/500
```
Coverage numbers are the proof that the kills landed where claimed. A mode whose targeted coverage is 0 should print a `WARN` rather than a quiet pass.

## 10. Testing the tool itself

A passing result is meaningless until the tool is shown to **fail on a broken engine**. Write `tests/test_oracle.py` using synthetic histories (no engine needed) covering each check in section 6, and a small fake `serve` with deliberately injected bugs. The tool must report FAIL for each:
1. ACKs before writing (lost write).
2. Drops an ACKed write after restart.
3. Applies only some operations of a batch.
4. Returns a value truncated by one byte.
5. Returns another key's value.
6. A put-then-delete in a batch resolves to the wrong order.

Keep this fake in `tests/`, not in the engine. If the oracle passes a broken engine, nothing else in this document matters.
