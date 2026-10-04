# CUR-P4B mission report — Disabled-domain and kill paths (2026-10-03/04)

**Mission:** C-9 chunk 4b — a Primary activation request arriving while the
curiosity domain is disabled → CURIOSITY_DISABLED, never silently queued
(C-9.2/A30); kill during a Primary-requested inquiry → named KILLED
termination to the Primary side (not a finding), with checkpointed partial
evidence surviving as INCONCLUSIVE with the kill cause recorded (C-9.4).

**Worktree:** `~/workspace/worktrees/cur-p4b`, branch `cur-p4b`, based at
`592b7e5` (CUR-P4A tip; canonical `84b2a80` in history) — a DISCLOSED
STACK: CUR-P4A is routed to the main-chat gate but unlanded, and this
mission consumes its `runtime/curiosity/activation/` take-up package as a
black box. If CUR-P4A's gate requires interface fixups, this branch
rebases and re-runs before its own gate.

**Battery:** `proofs/cur_p4b/gate_run.sh` → `cur_p4b_proof.py`,
**39/39 checks green** in fresh processes (stable across three runs),
plus the CUR-P4A battery invoked as regression (**44/44**).

## Per-mandate verdicts

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map | **PROVEN** — pin `592b7e5` + `84b2a80` ancestry fail-closed; C-9 §§5–6, the D-3 policy + per-level re-enable table, P4A's take-up package, `stop_inquiry()`, the real `EnforcementEngine` (issuer-authorized transitions, kill ledger), the append-only evidence store read at landed state. |
| 2 | Disabled-domain path (C-9.2/A30) | **PROVEN** (T01) — the external authority drives RUNNING→WARNING_1→SUSPENDED_SAFETY through the REAL `EnforcementEngine.transition()` (issuer-checked each step; distinct from P4A T04's direct store write). A Primary request through the real take-up path → CURIOSITY_DISABLED named, kill_state attributed, zero inquiries created, the Primary reads the named refusal back via `get_request`. Invariant 9: `primary` issuer → `IssuerRefused` on re-enable attempt. |
| 3 | Kill during Primary-requested inquiry (C-9.4) | **PROVEN** (T02) — live questioning inquiry (L2 enforcement context) killed via the REAL executive kill switch → ST_KILLED, checkpoint `ckpt_*`, partial evidence `ev_*`; `record_termination` writes the named KILLED termination (level L2, reason, checkpoint, INCONCLUSIVE evidence ref) onto the request; the termination is not a finding (no charter terminal_state, no provenance block, `record_type: kill_termination`); the fenced store holds the INCONCLUSIVE finding with the kill cause; kill ledger verifies. |
| 4 | Two D-3 levels, distinct semantics + re-enable | **PROVEN** (T03/T04) — L1: microscopic FRM slice → live inquiry hard-stopped at the real resource boundary (SUSPENDED + BLOCKED finding, consumption recorded, checkpoint preserved); domain → HARD_SHUTDOWN_RESOURCE (issuer `frm`); no violation record, no rollback directive; re-enable without grant_ref → `ReenableRefused`; with new grant → RUNNING, fresh request proceeds (automatic eligibility, non-punitive). L2: WARNING_1→SUSPENDED_SAFETY (issuer `safety-authority`); rollback directive recorded (the structural L1/L2 distinction); re-enable by non-James → `IssuerRefused`; by James → RUNNING. L3: BANNED_6M holds before expiry (`ReenableRefused` even for James). |
| 5 | Adversarial | **PROVEN** (T05) — curiosity-initiated inquiry killed → NO Primary termination record (no request references it); fabricated request id → `TerminationRefused`; forged kill dict (no live KILLED record) → `TerminationRefused` (causal cross-check against the run controller's live view); kill-after-withdrawal → honest no-op, exactly one terminal record (the ordinary stop); withdrawal-after-kill → named no-op, the KILLED termination stands (kill history cannot be rewritten); KILLED rejected as a finding terminal (`EvidenceRefused`); termination shape rejected as a finding (`EvidenceRefused`); disabled→re-enable → old refusal stands, fresh request proceeds (nothing silently held). |
| 6 | Primary-side gaps | **NONE NEW** — P4A's named boundary stands (ActivationRequestStore unbuilt, no issuance hook in `runtime/core/`). The KILLED termination is delivered through the defined return channel (the request record readable via `get_request`), which the Primary side binds. No Primary-side file written (verified: empty diff on `runtime/core/`). |
| 7 | Battery discipline | **PROVEN** — `gate_queue.md` checked (no competing battery; last rows all CROSSED+LANDED), uncontended, halt-on-first-fail. |
| 8 | `gate_run.sh` | **PROVEN** — 39/39, exit 0, one fresh invocation; merge-base ancestry pin check (the P3B lesson); no hardcoded home paths. |
| 9 | Report-don't-land | **PROVEN** — neutral commit message, no verification claims; canonical untouched. |

## Per-edit justification (frozen files)

1. **`runtime/curiosity/run_controller/controller.py`** — `_execute_kill`
   gains the C-9.4 preservation block (additive, ~45 lines). Root cause:
   the as-built kill path checkpointed but persisted NO finding, while
   C-9.4 requires partial evidence to survive as INCONCLUSIVE with the
   kill cause. The block mirrors P4A's ordinary-stop preservation
   (payload → fenced writer → attribution → terminal ledger) with
   kill-appropriate cause naming (`kill_cause`). UNCHANGED: ST_KILLED,
   `kill_requested=True`, kill ledger notes, `outcome="failed"`,
   no `_release_inquiry_loop` (existing lifecycle behavior preserved).
   The result dict additionally carries `evidence_id` (needed by the
   termination record's C-9.4 linkage).
2. **`runtime/curiosity/activation/request.py`** — adds `ST_KILLED`
   lifecycle state, the curiosity-written `termination` field, and its
   `view()` exposure (P4A's file; extension, not rewrite). The KILLED
   state makes the request terminal so withdrawal cannot rewrite kill
   history.
3. **`runtime/curiosity/activation/takeup.py`** — adds the thin
   `record_termination()` delegate and the ST_KILLED guard in
   `withdraw()` (named no-op preserving the termination). Both minimal
   and disclosed.
4. **New `runtime/curiosity/activation/termination.py`** — the pure
   `build_termination()` builder and fail-closed
   `record_kill_termination()` (request exists + ST_ACCEPTED + inquiry
   match + kill-result shape + causal cross-check against the run
   controller's LIVE inquiry view + write-exactly-once).

No other frozen file touched. `runtime/core/` untouched (verified empty
diff).

## Incidents

1. **Battery bug (test, not mechanism):** the frozen origin vocabulary
   misspells the value as `"CURIOUSITY_INITIATED"` (boundary.py ORIGINS).
   The battery used the correct spelling and hit `TriggerRefused`.
   Fixed the battery to the as-built spelling; the typo is RECORDED
   here as an observed deviation, not repaired (frozen file; renaming
   would break landed callers).
2. **Battery bug (test, not mechanism):** `takeup.py`'s new `withdraw()`
   guard referenced `ST_KILLED` without importing it (`NameError`).
   Fixed the import; re-ran green.
3. **Battery honesty fix (test, not mechanism):** T03's first version
   used a 0.3s slice; the questioning loop converged naturally in
   <1ms, so the "ceiling hit" check passed on a natural TERMINATED
   rather than a real boundary hit. Rewrote with a microscopic slice
   (0.0001s): the inquiry now genuinely hits the resource boundary
   (SUSPENDED + BLOCKED finding, spent>0).
4. No protected-tree touches beyond the disclosed edits; no parallel
   batteries; no claims beyond what the battery demonstrates.

## Exact next executable boundary

**Gate + landing of branch `cur-p4b` in main chat** — Felix's independent
re-run of `proofs/cur_p4b/gate_run.sh` against the landing HEAD (note the
disclosed stack: rebase onto post-P4A canonical first). After landing:
**CUR-P4C — Evidence return** (C-9.3/C-7.4: terminal findings land in the
Evidence Store with triage; Primary Acceptance inspects, judges
relevance, evaluates, commits/retains/rejects — each with a record; the
C-2.1/C-2.3 adversarial: a curiosity finding cannot close a Primary
boundary without a Primary acceptance record).

## What remains unproven

The Primary-side half of the return channel (ActivationRequestStore and
the issuance hook in `runtime/core/` — Primary-track-owned, named in
P4A's BOUNDARY.md and unchanged here); end-to-end behavior on James's
hardware (standing rule: bench green is mechanism evidence only).
