# CUR-P4C mission report — C-9 evidence return (2026-10-04)

**Mission:** build the curiosity side of the C-9 evidence-return leg
(Phase 4 chunk 4c): a Primary-requested inquiry's terminal finding is
presented to Primary Acceptance through the REAL `RelevanceGate`,
which judges relevance and records the verdict — commit/admit, retain,
or reject, each with a record; with the C-2.1/C-2.3 adversarial that a
curiosity finding cannot close a Primary boundary.
**Worktree:** `~/workspace/worktrees/cur-p4c`, branch `cur-p4c`,
stacked on `cur-p4b` tip `68d99f1` (DISCLOSED STACK — P4A/P4B routed
to the main-chat gate but unlanded; their machinery consumed as a
black box; rebase + full re-run owed when they land).
**Battery:** `proofs/cur_p4c/gate_run.sh` → `cur_p4c_proof.py`,
**29/29 checks green** (fresh processes, real machinery throughout),
plus the P4B battery invoked as regression (39/39, which itself runs
P4A's 44/44).

## Per-mandate verdicts

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map | **PROVEN** — base ancestry fail-closed (68d99f1+592b7e5+84b2a80); C-9 §7 return-leg design, P4A/P4B machinery, and the REAL `RelevanceGate`/`AcceptanceLoop` interfaces read at landed state. Key discovery: `AcceptanceLoop.present` (services/) is the user-facing attempt loop, NOT the C-9 Primary Acceptance — not used, not confused (BOUNDARY.md). |
| 2 | Evidence return end-to-end | **PROVEN** — new `runtime/curiosity/activation/evidence_return.py`: `present_for_acceptance()` loads the terminal finding from the fenced store, validates (fenced), runs the causal cross-check (finding's bounded objective + payload inquiry id against the live take-up request), adapts to the gate's Finding shape, and calls the REAL `gate.decide()`. T01: Primary-requested inquiry → QUESTION_RESOLVED → ADMITTED via provenance-link, decision retrievable, `requested_by_primary=1` persisted, linkage recorded. |
| 3 | Verdict consequences | **PROVEN** — ADMITTED/RETAINED/REJECTED each executed through the real gate with records (T01/T02/T03); `get_decision` returns the recorded verdict; decisions table append-only (two decides → two rows, latest wins); re-decision re-evaluates through `decide()` — there is NO `set_verdict` API (AttributeError demonstrated); tree-wide grep proves no curiosity-side decisions writer exists. |
| 4 | Adversarial (C-2.1/C-2.3) | **PROVEN** — (a) no boundary-closing API exists on the curiosity side (absent by namespace + tree inspection; max outcome is a verdict); (b) no acceptance-record writer exists (tree grep); forged objective link → cross-check refuses BEFORE the gate (T05); (c) curiosity-initiated findings handled distinctly (`requested_by_primary=False`, judged on content-score only, T02); (d) verdict tampering impossible via API (no setter; append-only log); (e) KILLED termination presented → refused as not-a-finding through the real store read (T05). Classification: the runtime refusals are PROVEN; the structural no-writer property is PROVEN BUT BOUNDED (holds by code ownership + review, recorded in BOUNDARY.md). |
| 5 | Primary-side gaps | **PROVEN** — no new gaps; P4A's named boundary stands (ActivationRequestStore / issuance hook); the §7 inspection inlet is Primary-track-owned per the design; `runtime/core/` and `runtime/services/acceptance.py` verified untouched (empty diffs). |
| 6 | Battery discipline | **PROVEN** — gate_queue.md checked (no competing battery), load < 2.0, niced, halt-on-first-fail. |
| 7 | `gate_run.sh` | **PROVEN** — 29/29, exit 0, one fresh invocation; ancestry pin check; no hardcoded paths. |
| 8 | Report-don't-land | **PROVEN** — neutral commit message, canonical untouched. |

## Frozen-file deltas
- `runtime/curiosity/activation/__init__.py`: +2 lines (export `ReturnRefused`, `present_for_acceptance`). Additive.
- New: `runtime/curiosity/activation/evidence_return.py` (the return leg).
- No other frozen file touched. `runtime/core/`, `runtime/services/acceptance.py` untouched (verified by empty diff).

## Incidents
1. `from .return import ...` → SyntaxError (`return` is a keyword); module renamed `evidence_return.py` before any battery ran.
2. `ST_ACCEPTED` constant's VALUE is `"ACCEPTED"` — first run compared against the literal `"ST_ACCEPTED"`; fixed to the constant. Test bug, not mechanism.
3. T06's `set_objective` structural check matched the module's own comment; tightened to call-site patterns. Test bug, not mechanism.
4. No protected-tree touches beyond the disclosed additive export; no parallel batteries; no claims beyond what the battery demonstrates.

## Exact next executable boundary
**Gate + landing of branch `cur-p4c` (commit TBD) in main chat** — Felix's independent re-run of `proofs/cur_p4c/gate_run.sh` against the landing HEAD (note the disclosed stack: rebase onto post-P4A/P4B canonical first). After landing: **Phase 5 — curiosity-initiated work with relevance fencing** (per the track plan).
