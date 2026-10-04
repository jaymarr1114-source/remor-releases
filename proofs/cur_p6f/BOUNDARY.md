# CUR-P6F Boundary Notes

Genuine boundaries and named residuals from the Phase 6f persistence proof.
None block this mission's verdict (all stores proven crash-safe; no torn
write ever presented as valid). They are recorded for the final Phase 6
gate.

## 1. Silent data corruption is not detected by three stores (PROVEN BUT BOUNDED)

Single-byte payload corruption, empirically demonstrated in this mission:

- **Evidence Store** (`curiosity_evidence.db`): no per-record integrity
  hash. `PRAGMA integrity_check` passes; the store reads back wrong data
  silently. (Demonstrated: `bounded_objective` read back as
  `X6f-persistence-probe-seed1`.)
- **FRM epoch ledger** (`frm_epochs.db`): no per-record hash on
  `payload_json`. Same silent behavior. (Demonstrated: payload differs
  from pristine, integrity_check ok.)
- **`enforcement_state.json`**: no per-value integrity check. A byte flip
  that keeps the JSON valid reads back silently wrong
  (`issuer=Xames`). Enum-valued fields (`state`) self-validate loudly via
  the `EnforcementState` enum; free-text/numeric fields do not.

Contrast (detection PROVEN): the kill ledger's SHA-256 hash chain and the
checkpoint store's per-record `integrity_sha256` (verified on the
handoff/resume path) both caught the equivalent tampering loudly.

Whether to add per-record tamper-evidence to the three stores is James's
call — it is new mechanism, not a repair of something that regressed.

## 2. `verify_chain` violates its tuple contract on a torn tail (observation)

`KillLedger.verify_chain()` raises `JSONDecodeError` on a torn last line
instead of returning `(False, reason)`. Detection is loud (never silent),
but callers expecting the `(bool, str)` tuple get an exception. Small
robustness fix if James wants it; not a silent-acceptance defect.

## 3. Current-record-only enforcement history (carried, unchanged)

After a crash, what recovers: the current enforcement record (atomic
write) + the kill ledger (terminal transitions only). What does not: the
full detail of non-terminal episodes (demonstrated: the WARNING_1
episode's `reason_refs`/`entered_at` — only `prev_state="WARNING_1"`
survives). No history table exists. This is the architecture as decided,
not a defect — recorded so the gate does not mistake it for one.

## 4. Kill-ledger append is not atomic (carried, unchanged)

A SIGKILL landing inside the `write()` syscall could tear the last line.
30 real mid-write kills in this mission produced zero tears (the window is
nanoseconds), and a torn tail is detected loudly (see §2). No automatic
recovery exists — truncating a provably-incomplete tail is a manual
operation. If James wants crash-atomic appends, that is new mechanism
(temp-file + rename per append, or a length-prefixed record format).

## 5. Landing reconciliation: four versions of `hardening/__init__.py`

cur-p6a, cur-p6c, cur-p6d, and cur-p6f each created
`runtime/curiosity/hardening/__init__.py` with different docstring content
on their unlanded branches. All four are docstring-only. Felix reconciles
at landing.

## 6. Carry-forward residuals for the final Phase 6 gate (unchanged)

- **P6C:** post-ban checkpoint resume is not enforcement-gated
  (PROVEN BUT BOUNDED) — a retained checkpoint empirically resumed to
  ACTIVE under BANNED_6M. The ban still starves it (activation refused,
  FRM zero allocation), but the state inconsistency is real.
- **P6D:** no engine-side refusal audit — refusals raise and leave no
  store trace; evidenced by exception + fresh-process state read + the
  battery's attempt log.
- **P6E:** two PROVEN-stale battery exemptions documented with causal
  chains (P1A A5b creativity-FRM scan; P3B t10 gate-3 post-P3A-INT refusal
  shape); P0 battery pin-locked to 44e19e4.

## 7. Outstanding James decisions (unchanged)

- The two-part `novel_pattern` / `discovery_novelty` decision (declare in
  frozen boundary.py; admit to loop vocabulary, classification-only).
- James's hardware acceptance (standing — nothing is "working" until he
  confirms on his hardware).
- Whether to add per-record tamper-evidence to the evidence store, FRM
  ledger, and enforcement state file (§1); whether to harden
  `verify_chain`'s torn-tail path (§2); whether to make kill-ledger
  appends crash-atomic (§4).
