# Prompts Log: Torture Tool Implementation & Hardening

This log records the core user prompts and corresponding engineering deliverables executed during the implementation and hardening of the championship-grade independent torture tool.

---

### 1. Independent Module Architecture & Package Separation
**User Prompt:**
> "make the separate folder for the torture tool and procced with the implementation with perfection and use the torture branch for the implementation , ensure no errors in the implementation."

**Key Deliverables:**
- Created dedicated `torture_tool/` package directory:
  - `torture_tool/__init__.py`
  - `torture_tool/__main__.py`
  - `torture_tool/oracle.py` (Independent state verifier and candidate-set invariant engine)
  - `torture_tool/workload.py` (Deterministic multi-threaded workload generator)
  - `torture_tool/client.py` (Low-overhead binary pipe client with live D4 reader thread)
  - `torture_tool/modes.py` (Mode configurations and crash-point mappings)
  - `torture_tool/runner.py` (Lifecycle runner, sub-millisecond kill loop, artifact generator)
- Built top-level executable launcher `torture`.
- Ensured 100% isolation from `src/kvdb/` (zero imports of engine internals).

---

### 2. Comprehensive Deep Audit Against Competition Specification
**User Prompt:**
> "is the torture tool implmented perfectly ? do the deepaudit and give me the insights what is left to implement in the torture tool part , go through the problem statement uploaded as the pdf format and refer it to audit about the torture tool."

**Key Deliverables:**
- Conducted deep audit against `Kill_It_and_Prove_It.pdf`.
- Identified remaining high-value requirements:
  - Scenario B5: Conflicting intra-batch operation ordering (`put-del-put`, `del-put-del`, `overwrite-put`).
  - Scenario F2: Chained recovery crash injections (killing `open` up to 3 times sequentially).
  - Scenario F4: Lost tail truncation checks.
  - Scenario E1-E5: Disk-full invariant assertions and readability verification.
  - Compaction shrink verification (100k keys write/delete/compact shrinking to < 5% peak size).
  - Scale mode (1,000,000 keys in 100 batches of 10,000).

---

### 3. Championship-Grade Elevation & Maximum Score Hardening
**User Prompts:**
> "make the torture tool more robust and strong as it is mnetioned in the uploaded problem statement pdf , as the more powerful or the strong torture tool gets the more point , so make it more strong"
> "engine is being built by another developer. what is left other than that ?"
> "now work on the torture tool itself , plan how can we make more good , best which scores high"

**Key Deliverables:**
- **In-Flight Telemetry:** Terminal output surfaces exact active operations in flight at the instant of SIGKILL (e.g. `inflight_at_kill=32 [PUT(s:0), DEL(s:1)]`).
- **Independent Raw Binary Auditor:** Implemented `validate_raw_binary_db()` in `torture_tool/oracle.py` verifying header magic (`KVDBLOG\0`), version 1, generation, CRC32, sequential frames, and zero trailing tail bytes without relying on the engine's `check` command.
- **Judge-Facing Self-Test Suite (`--self-test`):** Created `torture_tool/self_test.py` running 16 automated tests in ~6.4s verifying all §10 failure modes, binary format corruptions, and live fault injections against mock broken engines.
- **Contention Mode (`--mode contention`):** Implemented Scenario D2 with 4 concurrent writers rapidly alternating PUT and DEL on identical keys (`s:0` and `s:1`) to stress concurrent race conditions.
- **Enhanced Standalone Reproducer (`reproduce.py`):** Automatically packages binary byte inspection, key offset finder, hex dump preview, and `oracle.log` chronological timeline replay into `failure-<seed>-<iter>/`.
- **Documentation:** Updated `README.md` with CLI options and usage instructions.

