# CUR-P4A mission report — Activation request → take-up (C-9, chunk 4a)

**Mission:** build the curiosity side of the C-9 Primary-requested activation
path: a Primary-issued activation request reaches the Curiosity Executive
through the real path; the executive accepts or refuses on kill state,
grants, fit — refusal named, never silent (C-9.1/A29); the Primary may
withdraw mid-flight with clean termination and cause recorded (A28).

**Worktree:** `~/workspace/worktrees/cur-p4a`, branch `cur-p4a`, base
canonical `84b2a80` (pin verified fail-closed at mission start).
**Battery:** `proofs/cur_p4a/gate_run.sh` → `cur_p4a_proof.py`,
**44/44 checks green** (fresh processes, real machinery throughout, no
mocks; the Primary side is real `runtime/core/` code consumed read-only).

## What was built (new files only)

`runtime/curiosity/activation/` — the curiosity-side take-up wiring:
- `request.py` — `ActivationRequest` record per the C-9 design §1 (field
  ownership split enforced by construction: `validate()` checks only
  Primary-written fields; take-up writes only curiosity-owned fields).
  Fail-closed validation → `REQUEST_MALFORMED` (R1).
- `takeup.py` — `ActivationTakeUp`: `receive()` (validate + index,
  `DUPLICATE_ACTIVE_REQUEST` on id reuse, R2), `decide()` (the §2 take-up
  procedure: translate to a `CuriosityTrigger` with `origin=
  PRIMARY_REQUESTED`, call the REAL `executive.request_activation`, map
  refusals to the C-9 names, dispatch on accept), `withdraw()` (A28),
  `get_request()` (the defined return channel the Primary side reads).
- `__init__.py` — exports.

## One frozen-file addition (integration-exposed gap, disclosed)

`runtime/curiosity/run_controller/controller.py`:
- `CKPT_STOP = "STOPPED_ORDINARY"` label.
- `stop_inquiry(inquiry_id, *, cause)` — the **ordinary termination path
  (A13/A18)** the authorized C-9 design requires for A28 withdrawal. The
  as-built run controller had kill/pause/resume/natural-terminal only;
  withdrawal needs "ordinary stop, not a kill", and no such path existed.

Root cause of the gap: the C-9 design (Phase 0b, gate-verified) specifies
withdrawal "through the Curiosity Run Controller's ordinary termination
path (A18 — inquiry termination execution; A13 — ordinary stop, not a
kill)"; Phase 2 never built that path (its terminations are converge,
kill, resource-suspend, pause). This mission is the first consumer, so
integration exposed it.

The method is minimal and additive — no existing behavior changed:
checkpoint first (lineage carries the stop), abort the loop's live
machinery, persist partial evidence as `INCONCLUSIVE` with the cause via
the fenced writer, attribute `outcome="partial"` (the frozen Result
vocabulary is success|partial|failed; the withdrawal is named in detail,
stop_cause, and lineage), release the loop, `ST_TERMINATED` with
`stop_cause` (never `ST_KILLED`, no kill ledger, `kill_requested` never
set). The loop's `abort()` primitive stops machinery in both paths —
stopping machinery is stopping machinery; the RECORDS are what
distinguish withdrawal (operational) from kill (enforcement), per the
design's load-bearing distinction. Verified disjoint by T09.

## Per-mandate verdicts

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map | **PROVEN** — pin verified; C-9 design §§1–6, gate-record seam map, real `LivePath`, executive `request_activation`, run-controller lifecycle all read at landed state. |
| 2 | Take-up wiring (new files) | **PROVEN** — `ActivationTakeUp` translates a Primary-shaped request to a real trigger and drives the real executive → run controller → questioning loop chain (T03: ACCEPTED → QUESTION_RESOLVED, provenance-stamped finding with `origin=PRIMARY_REQUESTED` in the fenced store). |
| 3 | Withdrawal (A28) | **PROVEN** — before take-up: WITHDRAWN, nothing committed (T07); mid-flight: ordinary stop, partial evidence INCONCLUSIVE with cause, named to the request record (T08). |
| 4 | Adversarial | **PROVEN** — killed → CURIOSITY_DISABLED (T04); no grant → REFUSED_GRANT, non-negotiating (T05); unfit → REFUSED_FIT incl. LOOP_ABSENT (T06); withdrawal/kill record paths disjoint (T09); curiosity side cannot write Primary acceptance records (T10: no such capability in `activation/`, `runtime/core/` untouched per git). |
| 5 | Primary-side gaps → named boundary | **PROVEN** — the ActivationRequestStore is UNBUILT (Primary-track-owned per design); no issuance hook exists in `runtime/core/`. Recorded in BOUNDARY.md, not built here. |
| 6 | Battery discipline | **PROVEN** — gate_queue.md checked (no competing battery), load < 2.0, niced, halt-on-first-fail. |
| 7 | `gate_run.sh` | **PROVEN** — 44/44, exit 0, one fresh invocation; ancestry pin check; no hardcoded paths. |
| 8 | Report-don't-land | **PROVEN** — branch `cur-p4a`, neutral commit message, canonical untouched. |

## Observed deviation (not a defect)

The design's §2 specifies the take-up evaluation order as kill →
**grant** → **fit**. The as-built `request_activation` (landed Phase 2,
gated) evaluates kill → **ownership** → grant → fit. Every refusal is
named either way and kill stays first; the take-up consumes the as-built
order and records it here rather than re-litigating landed behavior.

## Incidents

1. **Frozen dataclass `CuriosityTrigger`** — first battery run crashed
   assigning `trigger_id` post-construction; fixed by constructing with
   the derived id (mechanism untouched).
2. **`FrmPolicy` rejects `total_budget_s=0`** — the no-grant test now
   states zero curiosity demand instead (a real FRM round granting zero;
   `CountingFRM` proves the evaluation layer was consulted, rounds==1
   proves non-negotiation).
3. **`Result` vocabulary rejects `outcome="withdrawn"`** — attribution
   uses `outcome="partial"` (frozen vocabulary); the withdrawal is named
   in detail, stop_cause, and lineage. Test bug, not mechanism.
4. No Primary-side file written (verified via `git status
   runtime/core/`); no frozen file written beyond the disclosed
   `stop_inquiry` addition; no parallel batteries.

## What remains unproven / out of scope

- The **return leg** (chunk 4c): Primary Acceptance inspecting the
  curiosity finding and recording a verdict — the finding is in the
  fenced store with triage; acceptance is Primary-track/4c work.
- The **Primary-side binding**: issuance through a real
  ActivationRequestStore and the Primary reading back decisions —
  Primary-track boundary (BOUNDARY.md).
- Kill during a Primary-requested inquiry (C-9.4/R7) — chunk 4b.

## Exact next executable boundary

**CUR-P4B — Disabled-domain and kill paths** (plan chunk 4b): request
while disabled → CURIOSITY_DISABLED (already proven at take-up in T04;
4b owns the domain-level behavior), kill during a Primary-requested
inquiry → named KILLED termination to the Primary side (not a finding),
checkpointed partial evidence surviving as INCONCLUSIVE with the kill
cause recorded (C-9.4). The `stop_inquiry`/`kill_inquiry` record
disjointness proven here is 4b's foundation.
