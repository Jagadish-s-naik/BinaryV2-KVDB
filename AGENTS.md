# Documentation rules

- Any change to the record format, write order, recovery,
  compaction, locking, or disk-full logic must update
  docs/FORMAT.md and the affected rows in ARCHITECTURE.md
  in the SAME commit.
- Describe only what the code actually does. Read the
  function before documenting it. If unsure, write
  "not verified". Never claim a mechanism that doesn't exist.
- Every proof-table row needs: disk state, mechanism,
  file:function, and the torture mode + seed that exercises it.
  Only fill "Verified by" after you have actually run it.
- When the torture tool finds a bug, append an entry to
  docs/BUGLOG.md before fixing it.
- Crash-point tables must cite exact function names and
  line-level steps (e.g. "after fsync(log), before ACK").
- Keep BREAKING.md within 2 pages.
- Update README commands whenever the CLI changes.
