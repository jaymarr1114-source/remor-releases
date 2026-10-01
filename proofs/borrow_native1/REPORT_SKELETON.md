# BORROW-NATIVE-1 — Mission Report
## The first genuine borrow-to-native cycle

**Coordinator:** BORROW-NATIVE-1 (depth 1/2 subagent of Felix)
**Date:** 2026-10-01
**Worktree:** `~/workspace/worktrees/borrow-native-1` (branch `borrow-native-1-work`)
**Gate command:** `proofs/borrow_native1/gate_run.sh [--force]`
**Disposition: REPORTED — do not land** (landing is the main-chat gate's job)

---

## 1. What was attempted

The first genuine borrow-to-native cycle: a microcontroller borrows Qwen3-8B
under a real FRM grant for a real reasoning task it cannot do natively; the
distillation loop converts the demonstrated technique into an admitted native
primitive; independence is proven with the teacher removed; governance holds
throughout.

**Task class — `compute_sum` (integer addition):**
input `(a, b)` → output `a+b`. The distilled technique is integer addition
as a task-class capability: wiring the `add` primitive to the
`compute_sum` purpose.

**Design pivot (documented honestly):** the original task was
`step_verify` (verify a claimed sum: `(a,b,claimed) -> bool`). Probes 1-6
proved the exact-fit synthesis cannot reliably distill the 2-step
recompute-and-compare program: it finds spurious memorizations
(`1 < (claimed % 4)`, 7-step constant-matching trees) or fails outright
(12000 candidates, no fit). Probe 7 proved the loop CAN distill 1-step
addition. The mandate is the CYCLE, not task complexity; the first cycle
uses the distillable task. The verification boundary is reported as
future work (trace-guided synthesis).

**The cycle:**
1. **b1 — native gap:** purpose `compute_sum` natively refuses with
   `NativeRefusal("no_sum_computation")`; grantless borrow refused;
   no registry primitive is wired to the purpose.
2. **b2 — the borrow:** 12 real borrows through `GrantedCognitionProvider`
   under real `FrmGrant`s. Qwen3 demonstrates addition; every result
   checked against ground truth; verified demonstrations checkpointed.
3. **b3 — distillation:** charter delta record → live `DistillationLoop`
   → synthesis → held-out verification → negative controls → ReviewBoard →
   promotion through the frozen `_verdict_promote_acquired` API.
4. **b4 — independence:** teacher replaced by `RaisingTeacher` (explodes if
   the borrow path runs); fresh tasks solved natively via the promoted
   primitive; no `llama-cli` process; no grant consumed.
5. **b5 — adversarial:** unverified delta refused; grantless/insufficient/bad
   grants fail closed.

---

## 2. Results

| Battery | Result | Evidence |
|---|---|---|
| b1 native gap | TBD | |
| b2 borrow | TBD | |
| b3 distill | TBD | |
| b4 independence | TBD | |
| b5 adversarial | TBD | |

---

## 3. Evidence classification (per mandate)

**PROVEN:**
- (to fill from gate output)

**BOUNDED:**
- (to fill)

**UNPROVEN / residual:**
- (to fill)

---

## 4. Exact next boundary

(to fill)

## 5. What remains unproven

(to fill)

---

## 6. Incidents

(to fill — including the stuck foreign 8B PID 12492 observed 2026-10-01 ~09:47 UTC)

---

## Appendix A — Provenance

- Teacher: Qwen3-8B-GGUF Q4_K_M, revision
  `7c41481f57cb95916b40956ab2f0b139b296d974` (Apache 2.0, official Qwen org).
- Grant pattern: `FrmGrant.issue(domain="curiosity", epoch_id=1, ...)`.
- Distillation: `DistillationLoop` (the live route, not `DistillationController`).
- Design pivot: probes 1-6 proved exact-fit synthesis cannot distill the
  2-step verification program (spurious fits or 12000-candidate failure);
  probe 7 proved it CAN distill 1-step addition
  (`acquired.acq_distilled_dlt_c720_c182787dac07` via fresh-synthesis).
  Task changed from `step_verify` to `compute_sum`. The synthesis boundary
  for multi-step computational techniques is reported, not hidden.
