# CUR-P6F Mission Report — Fresh-Process Persistence of the Three Stores

**Branch:** `cur-p6f` · **Base:** `86d5376` (clean; canonical unmoved during run)
**Gate:** `proofs/cur_p6f/gate_run.sh` — one invocation, fresh sequential processes, exit 0
**Result:** battery 27/27 + verifier 12/12, ALL GREEN

## Objective

Prove the layer beneath the P6B–D fresh-process reads: that the enforcement
store, the Evidence Store (checkpoints, findings, index), and the FRM
grant/allocation records survive REAL process death (SIGKILL, not graceful
shutdown) with byte-level integrity — and that a crash mid-write never
presents a torn write as valid.

## On-disk representations (re-map, verified at the pin)

| Store | File(s) | Write atomicity |
|---|---|---|
| Enforcement state | `enforcement/enforcement_state.json` | atomic temp-file + `os.replace` |
| Kill ledger | `enforcement/kill_ledger.jsonl` | append-only, SHA-256 hash-chained; append is NOT atomic |
| Evidence Store | `curiosity_evidence.db` | SQLite (crash-safe via journal) |
| Checkpoint store | `transition_checkpoints.db` | SQLite + per-record `integrity_sha256` |
| Checkpoint index | `transition_checkpoints.db.index.json` | `Path.write_text` — NOT atomic |
| FRM epoch ledger | `frm_epochs.db` | SQLite (crash-safe via journal) |

## Per-mandate results

**M1 re-map — PROVEN.** Pin verified fail-closed; all six files located;
chain/hash verification code read; crash-recovery path identified per store.

**M2 SIGKILL battery — PROVEN** (`p6f_kill_writer.py` + `cur_p6f_proof.py`
T02). 30 real SIGKILLs (6 per store kind: enforcement state, kill ledger,
evidence, checkpoint, FRM), each kill proven to land mid-write via a
heartbeat fresher than the READY signal. After every kill, from a brand-new
process: zero torn writes presented as valid in all 30 cases.

- `enforcement_state.json`: always complete JSON (atomic rename) — PROVEN.
- `kill_ledger.jsonl`: every line parses; chain valid; seq contiguous — PROVEN.
- All three SQLite stores: open clean, `integrity_check` ok, committed rows
  intact — PROVEN (SQLite journal recovery).

**M2 torn-write detection (deterministic, on copies) — PROVEN.**
Truncating the kill-ledger tail mid-line: the store's real `records()` /
`verify_chain()` raise `JSONDecodeError` — loud, never silent.
Observation (not a defect): `verify_chain` raises instead of returning
`(False, ...)`, violating its tuple contract.

**M2 byte-corruption adversarial — PROVEN / PROVEN BUT BOUNDED** (T04).
- Kill ledger, one validity-preserving byte flip mid-record: `verify_chain`
  → `False, "line 1: hash mismatch (record tampered)"` — PROVEN.
- Checkpoint store, one byte flip in a payload: the store's own
  `verify_checkpoint_integrity` raises `CheckpointError` — PROVEN.
- Evidence store, one byte flip in a payload: `integrity_check` ok, wrong
  data read back silently — PROVEN BUT BOUNDED (no per-record hash).
- FRM epoch ledger, one byte flip in a payload: `integrity_check` ok,
  payload differs from pristine silently — PROVEN BUT BOUNDED.
- `enforcement_state.json`, one byte flip in a string value (JSON stays
  valid): read back silently wrong (`issuer=Xames`) — PROVEN BUT BOUNDED
  (no per-value integrity check; enum fields self-validate loudly).
- Checkpoint index, torn mid-JSON: the real `_latest_checkpoint_handoff`
  returns `None` (graceful, never a torn entry); the next real
  `_write_index_entry` self-heals by replacing the file — PROVEN.

**M3 cross-store consistency — PROVEN** (T05 + verifier). Fresh process,
both directions: enforcement RUNNING←SUSPENDED_SAFETY ↔ 1 kill-ledger
entry (SUSPENDED_SAFETY, probe `p6f-t01-susp`) ↔ 4 evidence rows ↔
4 checkpoint rows (all with 64-hex integrity hashes) ↔ FRM 2 rounds + 1
epoch close. Nothing missing, nothing extra.

**M4 honest residual — PROVEN, named** (T06). After WARNING_1 →
SUSPENDED_SAFETY + crash: the current record (state, prev_state, issuer)
and the one terminal kill-ledger entry recover. Unrecoverable: the
WARNING_1 episode's `reason_refs`/`entered_at` — only
`prev_state="WARNING_1"` survives. No history table exists; nothing was
invented.

**M5 discipline — followed.** Gate queue clear (no VERIFYING battery);
load < 2.0 on two consecutive checks; niced; halt-on-first-fail.

**M6/M7 gate + report-don't-land — PROVEN.** `gate_run.sh` exit 0
post-commit (see output below); committed with explicit pathspec, neutral
message, no verification claims.

## Incidents (all battery bugs, none tree defects)

1. **I1 — heartbeat race:** the parent mistook the writer's pre-READY
   heartbeat touch for a write-loop touch and SIGKILLed during startup
   (0-byte DB, "no such table"). Fixed: wait for a touch NEWER than READY.
2. **I2 — writer desync:** the SIGKILL-restarted writer kept an in-memory
   toggle that disagreed with the persisted state
   (`WARNING_1 -> WARNING_1` refused). Fixed: read the actual state each
   iteration. This also proved the transition table refuses out-of-ladder
   jumps for real.
3. **I3 — ladder discipline:** RUNNING → SUSPENDED_SAFETY is not a decided
   transition (the ladder is RUNNING → WARNING_1 → SUSPENDED_SAFETY).
   Fixed the ledger writer to walk the ladder.
4. **I4 — corruption must preserve validity:** XOR flips produced invalid
   UTF-8 (parser failure, not chain failure). Fixed: validity-preserving
   single-byte substitutions so the hash chain is the component under test.

## Artifacts

- Harness: `runtime/curiosity/hardening/p6f_drill.py` (+ `__init__.py`
  with the landing-reconciliation note — 4th version of the file)
- Battery: `proofs/cur_p6f/` — `cur_p6f_proof.py`, `p6f_kill_writer.py`,
  `verify_p6f.py`, `gate_run.sh`, `CUR-P6F-REPORT.md`, `BOUNDARY.md`

## Exact next boundary

The **Phase 6 final gate**. It must adjudicate the complete residual list
(see BOUNDARY.md): P6C post-ban checkpoint-resume hole, P6D no-refusal-audit,
P6F silent-corruption residuals (evidence/FRM/state-file), the
`verify_chain` tuple-contract observation, the four-way `__init__.py`
landing reconciliation, the unlanded phase landings (P4A–C, P5A–C,
P6A–F), and James's outstanding decisions (novel_pattern/discovery_novelty
two-parter; hardware acceptance).
