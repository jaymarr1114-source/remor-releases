# CUR-P3A-INT mission report — James-authorized integration (2026-10-03)

**Mission:** unblock the stage-proven Scientific Inquiry loop by applying
James's U-1-class vocabulary decision (2026-10-01 ~12:00 EDT): admit
`"scientific_inquiry"` to the curiosity loop vocabulary, integrate it
through the three frozen gates, and prove the previously
UNPROVEN/PARTIAL items end-to-end.
**Worktree:** `~/workspace/worktrees/cur-p3a`, branch `cur-p3a`,
base `44e19e4` (fail-closed verified 2026-10-03; tip contained the
stage commit `15a183b`).
**Battery:** `proofs/cur_p3a/gate_run.sh` → stage battery (62 checks)
+ integration battery (35 checks) — **97/97 green in one fresh
invocation**, stable across three runs.

## Per-mandate verdicts

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map | **PROVEN** — pin `44e19e4` fail-closed (branch tip `15a183b` on top); hardlink equivalence `runtime/` ≡ `pylib/swarm_engine/` confirmed by inode; all three frozen files read at their landed state before editing. |
| 2 | Authorized frozen-file edits only | **PROVEN** — exactly three files edited (see per-edit justifications below); the diff touches nothing else. One integration-exposed defect in the stage-built loop repaired (NODE_GOALS, below). |
| 3 | End-to-end inquiry through the full chain | **PROVEN** — `trigger → executive → run controller → scientific inquiry → substrate/cognition → evidence → verification → terminal → fenced store → return`, under a real FRM grant (CountingFRM: 1 round), converging to HYPOTHESIS_SUPPORTED (T15) and HYPOTHESIS_REFUTED (T16) with provenance-stamped findings, ledger routes, and attribution records. |
| 3b | Executive selection (C-6.1 executive side) | **PROVEN** — hypothesis_candidate and novel_observation select scientific_inquiry; imprecise_question still selects questioning; generative_prompt refused LOOP_ABSENT; empty presentation refused FIT (T14). |
| 3c | C-6.2 forest containment | **PROVEN** — after termination `loop_view("scientific_inquiry")` shows state=resolved, active=0, spawned=2, retired=2; a second inquiry converges with no leaked state (T19). |
| 3d | C-4.2 | **PROVEN** — demand 0 → NO_BUDGET refusal; the inquiry does not start (T17). |
| 3e | C-4.3 | **PROVEN** — forced breach → BLOCKED with exact consumption externalized (spent_s == the accumulator, not an estimate), checkpoint preserved, inquiry SUSPENDED, unresolved boundary named (T18). Two real breach modes observed (slice-clock and MC-exhaustion); both satisfy the contract. |
| 3f | Kill/resume lineage | **PROVEN** — kill after one tick in process A (real death); resume from the durable checkpoint in fresh process B; inquiry converges to HYPOTHESIS_SUPPORTED under the same inquiry_id with killed + resumed_from_checkpoint in its lineage (T20). |
| 3g | Terminal routing + attribution | **PROVEN** — TerminalLedger route (loop=scientific_inquiry, consumer=curiosity_evidence_store, evidence ref); ChainLedger work/result/admission (outcome=success, verdict=retained) (T15). |
| 4 | Questioning regression | **PROVEN** — imprecise_question → QUESTION_RESOLVED through the refactored registry; payload envelope key-set identical to Phase 2 (T21). |
| 5 | Adversarial | **PROVEN** — foreign boundary not selected (T14); provenance-stripped finding refused at the integrated store via the loop's write path (T23); forged `creative_exploration` loop refused at dispatch by the registry fence (T24). |
| 6 | Heavy-battery discipline | **PROVEN** — gate_queue.md checked before the final battery (no competing battery; COLAB-PROOFPATH-1's 2026-10-01 registration long finished); load 1.18–1.36 < 2.0; niced; halt-on-first-fail. |
| 7 | Extended gate_run.sh | **PROVEN** — one invocation, fresh sequential processes, exit 0, 97/97 (see output below). |
| 8 | Report-don't-land | **PROVEN** — committed on `cur-p3a`, no canonical landing, no verification claims in commit messages. |

## Per-edit justification (frozen files)

1. **`runtime/curiosity/substrate.py`** — `CURIOSITY_LOOPS` gains
   `"scientific_inquiry"` (+ `LOOP_SCIENTIFIC_INQUIRY` constant). The
   fenced structure (closed tuple + frozenset + ValueError) is intact;
   it is not generalized into an open registry. Justification: this is
   the literal content of James's U-1-class decision.
2. **`runtime/curiosity/executive/executive.py`** — `LOOP_OWNERSHIP`
   gains the two inquiry boundary classes (removed from
   `ABSENT_OWNERSHIP`, which keeps the genuinely absent Phase-3 loops);
   `_check_fit` gains the inquiry branch (non-empty presentation +
   bounded objective — the loop itself judges falsifiability, mirroring
   the questioning division of labor); `executive_view` gains the
   inquiry loop entry. Justification: the executive can only select
   loops the ownership map names; the map is the declared authority.
3. **`runtime/curiosity/run_controller/controller.py`** — `__init__`
   builds `self._loops` / `self._inlets` registries dispatched by
   `inq.loop` (questioning construction byte-identical); `_activate`,
   `_step_inquiry`, `_execute_kill`, `resume_inquiry` dispatch through
   the registry; `_build_ctx` builds the per-loop LoopContext
   (questioning: CorpusIndex; inquiry: presented evidence lines = the
   corpus documents, mechanically); `_persist_terminal` keeps the
   loop-agnostic envelope with honest per-loop extras (no fabricated
   relevance score for verdict loops; triage from the terminal with the
   score rule as fallback — questioning payloads byte-identical);
   `_handle_resource_boundary` names the actual boundary class and
   carries node summaries for inquiry; `dispatch` fail-closes on
   unregistered loops. Justification: the hard-coded single loop was
   the third frozen gate; the registry is the minimal generalization
   the packet authorized.

## Integration-exposed defect repaired (stage-built file)

**`runtime/curiosity/loops/scientific_inquiry/loop.py` — NODE_GOALS.**
The stage author worded the goals for the GraphController's relevance
selection but missed the actual mechanism: the controller stems with
`_tokens` ("scientifically" ≠ "scientific") and requires relevance ≥
0.34, while the goals scored 0.083–0.333 — so `open_region` refused
every inquiry with "no graph nodes relevant to objective" and the
first tick died at the resource boundary. The stage battery never
exercised `open_region` (its T12 called the pure stages directly), so
only integration could expose this. Repair: reworded the five goals in
the objective's vocabulary (the same technique the questioning loop
uses); all five now score ≥ 0.41 against the real `_relevance`.
Verified empirically, not by assertion.

## Incidents

1. **Stage-battery T10 went stale** (expected): it encoded the old
   frozen-gate refusals as pass conditions. Rewrote it as
   `t10_authorized_admission` asserting the post-decision structure
   (ownership maps, vocabulary admission, registry contents, fence
   still closed to unadmitted loops). The old boundary evidence is
   preserved in `proofs/cur_p3a/BOUNDARY.md`.
2. **t18 timing sensitivity** (battery, not mechanism): the forced
   breach fires via two real modes (slice-clock vs MC-exhaustion)
   depending on timing; the first assertion assumed one mode. Fixed
   the assertion to the mechanism's actual contract (disjunction +
   exactness of the externalized consumption).
3. No protected-tree touches beyond the three authorized files; no
   parallel batteries; no mid-run failures; no defects in the frozen
   machinery itself.

## What remains unproven

- James's hardware acceptance (standing: nothing "works" until he
  confirms on his device).
- The inquiry loop against a live borrowed cognition provider
  (proven here against the deterministic mechanical native tier).
- Long-horizon inquiry behavior (multi-epoch FRM contention,
  concurrent inquiry admission ordering under load).
- Canonical-tree note: this branch is based at `44e19e4` (2026-10-01);
  canonical has since moved (2026-10-02 reconciliation,
  `v10-runtime-reconciled`). The gate will need to rebase and re-run.

## Exact next executable boundary

**Gate + landing of this branch in main chat** (Felix's independent
re-run of `proofs/cur_p3a/gate_run.sh` against the landing HEAD).
Then **CUR-P3B — Creative Exploration loop controller** (stage build,
new files only under `runtime/curiosity/loops/creative_exploration/`;
its vocabulary admission will be a fresh U-1-class decision for
James at its own integration boundary — this mission's authorization
covered `scientific_inquiry` only).
