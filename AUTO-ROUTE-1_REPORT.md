# AUTO-ROUTE-1 — Mission Report (coordinator)

## Objective
One chat interface; fast FRM-governed student answer in ~seconds; deep
FRM-governed 8B path on escalation with the stronger answer arriving as a
labeled follow-up. The user never chooses a brain.

## Implementation
- `distill1/router.py` (new, ~340 lines): `AutoRouter` — the controller
  layer of the hidden execution hierarchy. Consumes `governed_student.py`
  (fast) and `GrantedCognitionProvider` + `Qwen3Teacher` (deep) without
  modifying either. Ownership: AUTO-ROUTE-1.
- `proofs/auto_route1/`: `common.py` harness + 9 batteries
  (p1–p9) + `gate_run.sh` (one command, fresh sequential processes).

## Per-mandate results

### 1. Consume, don't duplicate — PROVEN
Fresh re-map: no difficulty classifier exists in `runtime/` (only vendored
NLTK and `gap_reasoner.py`'s static acquisition gap-node field, neither of
which routes inference). `metareasoning.should_escalate` is confidence/
action escalation for the acceptance loop, not inference-path routing.
`distill1/router.py` is the single module referencing both inference paths
for routing. The escalation heuristic is transparent surface rules
(explicit deep-think phrases; >=2 of: long prompt, deep interrogatives,
multi-part, show-work) — not a learned classifier. Battery p9 proves all
of this structurally.

### 2. Both paths FRM-governed — PROVEN
Fast: `GovernedStudent.turn` with FrmGrant (FRM-STUDENT-1). Deep:
`GrantedCognitionProvider.request_cognition` with `context["frm_grant"]`
(native=None, so it always takes the grant-gated borrow path). Battery p4:
grantless on both paths → `student_refused:no_grant` and
`cognition_refused:no_grant` with the real reasons; zero charges;
`substrate.calls == []`.

### 3. Silent escalation, real — PROVEN (pending p3)
Battery p2 (explicit "think hard"): fast answer served first with the
provisional label; deep path ran the REAL Qwen3-8B (98.0s charged,
provenance `borrowed:qwen3@7c41481f...`); diff_ratio 0.804 ≥ 0.70 →
materially better → follow-up served labeled `[deeper result]`.
Battery p3 (heuristic escalation): heuristic triggered on
`deep-interrogatives:compare,how,why` + `multiple`; deep path ran the REAL
8B; diff_ratio 0.82 ≥ 0.70 → materially better → follow-up served labeled
`[deeper result]`.

### 4. Anti-masquerade — PROVEN
Battery p8: the provisional label is IN the served fast text when
escalation triggers (`[provisional — deeper reasoning running] ...`) and
absent (`[final] ...`) when it doesn't. The label is user-visible output,
not internal logic.

### 5. Adversarial — PROVEN
- p5 grant revoked mid-escalation: live enforcement check refuses with
  `route_refused:escalation_blocked` naming `SUSPENDED_SAFETY`; deep never
  attempted; zero deep charge; the served fast answer's charge stands.
- p6 deep transport down (corrupt GGUF → real llama-cli failure):
  `deep_failed:transport` with the genuine loader error; zero deep charge;
  served falls back to the fast answer.
- p7 budget exhausted: deep defers with `cognition_deferred:
  insufficient_grant` (estimate 251.0s > 1.0s budget); zero charge.
- p4 both grantless: both refuse, zero charges everywhere.

### 6. No coming-soon — honored
Everything not working is classified below; nothing is labeled as if it
works.

## Classifications
- Single-interface chat with silent escalation: PROVEN (p1, p2, p8)
- Heuristic escalation path: PROVEN BUT BOUNDED (p3 pending; heuristic is
  surface rules — a learned router is future work for the distill loop)
- Both-path FRM governance with real refusals: PROVEN (p4)
- Adversarial fail-closed: PROVEN (p5, p6, p7)
- No-duplication: PROVEN (p9)
- "Materially better" threshold 0.70: PROVEN BUT BOUNDED — calibrated on
  two measured pairs (rephrase 0.625, genuine depth 0.804); re-anchor on
  data if production pairs land in the ambiguous band.
- True concurrent fast+deep execution: BOUNDED by the 2-core host (the 8B
  saturates both cores); the router runs deep sequentially after the fast
  answer. Architecturally supported where cores allow — UNPROVEN here.
- Student answer quality: the 0.6B occasionally emits odd third-person
  meta-text (observed in p8: "The user is a young adult, male, who has
  bee..."). Router serves it faithfully; quality is the student's
  known bound (DISTILL-1: 1.833/3.0), not a router defect. Noted, not
  repaired — repair belongs to the distillation track.

## Exact next boundary
Wire the router as the default chat-serving inlet (consumer mission):
today `governed_student.py` is an available inlet and the router is proven
in the worktree, but no production call path invokes either. The boundary
is consumer integration, not router capability.

## What remains unproven
- p3 battery (heuristic escalation with real 8B) — PROVEN (diff 0.82).
- Felix's independent gate re-run (this report is REPORTED until then).
- Real-device behavior; concurrent fast+deep on many-core hardware.
- Learned escalation (replacing surface rules) via the distill loop.

## Gate
`proofs/auto_route1/gate_run.sh` — one command, 9 batteries in fresh
sequential processes, visible pass/fail. Refuses to run if a foreign
llama-cli is active. Manages the student server lifecycle itself.
Final full-gate run 2026-10-01: **9 passed, 0 failed** (exit 0),
including both real-8B escalation batteries.
