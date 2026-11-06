# Bug Log

This log tracks bugs discovered by the torture tool or test suite, as required by AGENTS.md.
Every bug must be recorded here with its reproducer seed/mode before it is fixed.

| ID | Date | Mode & Seed | Symptom / Description | Fix Commit / PR |
|---|---|---|---|---|
| BUG-001 | 2026-11-06 | `recovery-crash --seed 12345` | `fake_engine open` crashed with `UnicodeDecodeError` when reading state file with injected torn bytes at EOF; engine failed to truncate torn tail during recovery | Added defensive splitlines binary scan with recovery truncation in `tests/fake_engine.py` |
| BUG-002 | 2026-11-06 | `disk-full --seed 12345` | `subprocess.run(text=True)` raised `UnicodeDecodeError` in `torture_tool/runner.py` when CLI `get` returned raw binary bytes; caused `'NoneType' object has no attribute 'strip'` | Added `errors="replace"` across all `subprocess.run` CLI invocations in `torture_tool/runner.py` |
| BUG-003 | 2026-11-06 | `compaction --seed 12345` | Compaction shrink check failed (`WARN: file did not shrink under 5% of peak`) because live keys from prior torture iterations were never deleted, remaining live after deleting the 100,000 test keys; and `fake_engine` sent `ACK` before `_persist()` on `COMPACT` | Delete prior live baseline keys during shrink check in `torture_tool/runner.py`; persist before `ACK` in `tests/fake_engine.py` |


