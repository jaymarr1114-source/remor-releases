# BORROW-NATIVE-1 — Mission Report
## The first genuine borrow-to-native cycle

**Coordinator:** BORROW-NATIVE-1 (depth 1/2 subagent of Felix)
**Date:** 2026-10-01
**Worktree:** `~/workspace/worktrees/borrow-native-1` (branch `borrow-native-1-work`, at canonical HEAD `73b38a1`)
**Gate command:** `proofs/borrow_native1/gate_run.sh [--force]`
**Disposition: REPORTED — do not land** (landing is the main-chat gate's job)

---

## 1. What was attempted

The first genuine borrow-to-native cycle: a microcontroller borrows Qwen3-8B
under a real FRM grant for a real reasoning task it cannot do natively; the
distillation loop converts the demonstrated technique into an admitted native
primitive; independence is proven with the teacher removed; governance holds
throughout.

**Task class — `compute_sum` (integer addition):** input `(a, b)` → output
`a+b`. The distilled technique is integer addition as a task-class
capability: the native tier goes from refusing `compute_sum` to answering it
from an owned, promoted primitive.

**The cycle (all five batteries green):**
1. **b1 — native gap:** purpose `compute_sum` natively refuses with
   `NativeRefusal("no_sum_computation")`; grantless borrow refused with the
   teacher untouched; no registry primitive is wired to the purpose.
2. **b2 — the borrow:** 12 real borrows through `GrantedCognitionProvider`
   under 12 real `FrmGrant`s. Qwen3-8B demonstrates addition; every result
   checked against ground truth; 12/12 verified; demonstrations checkpointed
   to `demos.json`.
3. **b3 — distillation:** charter delta record (`delta_record.json`) → live
   `DistillationLoop.distill` → fresh-synthesis route → held-out verification
   → negative controls → ReviewBoard → promotion through the frozen
   `_verdict_promote_acquired` API. Promoted:
   `acquired.acq_distilled_dlt_e976_8c7453f73496`.
4. **b4 — independence:** teacher replaced by `RaisingTeacher` (raises
   `AssertionError` if the borrow path runs); 6 fresh tasks solved natively
   via the promoted primitive; `native:` provenance; zero grant consumed;
   no `llama-cli` process before or after.
5. **b5 — adversarial:** random-output delta refused by the loop (not
   admitted, reason named); grantless borrow refused fast with no inference;
   insufficient grant deferred with zero charge; non-`FrmGrant` refused
   naming the confusion.

---

## 2. Results

| Battery | Result | Checks |
|---|---|---|
| b1 native gap | PASS | 6/6 |
| b2 borrow (12 real 8B calls) | PASS | 53/53, 12/12 verified |
| b3 distill (live DistillationLoop) | PASS | 7/7 |
| b4 independence (teacher removed) | PASS | 29/29 |
| b5 adversarial | PASS | 11/11 |
| **Gate** (`gate_run.sh`) | **ALL PASS** | |

---

## 3. Evidence classification

**PROVEN:**
- The native tier genuinely cannot compute sums: `NativeRefusal`
  (`no_sum_computation`) raised by name, pre-borrow, in a fresh process (b1).
- The borrow is genuine: 12 completions ran through real `llama-cli`
  subprocesses against the pinned Qwen3-8B weights
  (`7c41481f57cb95916b40956ab2f0b139b296d974`), each under its own
  `FrmGrant` (budget 400 s), each charged (70–191 s consumed), each
  carrying provenance `borrowed:qwen3@7c41481f…`, each verdict checked
  against locally computed ground truth (b2, `demos.json`).
- The distillation is the live loop, not a bypass: `DistillationLoop`
  (Route B fresh-synthesis; Route A correctly refused for no near-miss),
  held-out verification in fresh processes, negative controls, ReviewBoard
  admission, promotion only through `_verdict_promote_acquired` (b3,
  `distill_result.json`).
- Independence is causal, not asserted: with the teacher slot occupied by
  `RaisingTeacher` (explodes on any borrow attempt) and no grant in context,
  6 fresh `(a,b)` pairs (never in the delta evidence) return correct sums
  with `native:` provenance; `pgrep -x llama-cli` empty before and after
  (b4).
- Governance fails closed: unverified (random-output) delta → distill
  refuses with named reason; grantless → fast refusal naming `no_grant`
  (no inference, <20 s); 1 s grant → `insufficient_grant` deferral with
  exactly 0.0 s charged; string "grant" → `grant_not_frmgrant` (b5).

**BOUNDED:**
- The distilled technique is simple (integer addition wired to a purpose).
  The CYCLE is what this mission proves; technique sophistication grows in
  later cycles. The promoted primitive composes the existing `add`
  vocabulary op — the newly owned part is the task-class wiring, verified
  by held-out generalization to novel `(a,b)` pairs.
- Bench evidence only: 8B inference at ~1–3 min/call on this host; the
  cycle is proven on the bench, not on James's hardware.

**UNPROVEN / residual:**
- The original `step_verify` task (recompute-and-compare verification,
  `(a,b,claimed) → bool`) does NOT distill via the current loop — see §4.
- `demos.json`/`distill_result.json` checkpoints: the gate reuses them;
  `--force` re-runs everything from scratch (b2 ~30 min, b3 ~15 s).

---

## 4. Exact next boundary

**The exact-fit synthesis cannot distill multi-step computational
techniques from input-output examples.** Probes 1–6 (in
`proofs/borrow_native1/_probe*.py`, kept as evidence):

- Probe 1 (6 examples, 3 ops): synthesis returned the spurious
  `1 < (claimed % 4)` — fits the examples, ignores the inputs.
- Probes 3/5 (12 examples, add-only): synthesis returned 7-step
  constant-memorizing `if_else` trees — fits build examples, fails novel
  held-out.
- Probe 4 (4 examples): synthesis returned `claimed % 2` (candidates: 0 —
  analogy stage), exploiting a parity correlation in my example design.
- Probe 6 (16 examples, novel held-out pairs, full `DistillationLoop`):
  **failed honestly** — 12,000 candidates, no fit; the loop refused with a
  named reason. The mechanism works; the search cannot find the 2-step
  `equals(add(a,b), claimed)` program.
- Probe 7 (11 examples, `(a,b) → a+b`): **succeeded** —
  `acquired.acq_distilled_dlt_c720_c182787dac07` via fresh-synthesis.

**Why the mechanism can't cross it:** the synthesizer's heuristic search
prefers correlational/memorization patterns over computational programs;
with 12k candidates it never generates the 2-step recompute-and-compare.
The teacher's `recompute:` trace *contains* the program, but the loop only
sees `(input, output)` pairs — the reasoning trace is discarded at the
delta boundary.

**Crossing mechanism (future work, not built here):** trace-guided
synthesis — a distillation route that parses the teacher's demonstrated
work (the `recompute:` line) instead of relying solely on input-output
exact fit. The architecture permits it (a new route in `DistillationLoop`
alongside adaptation/fresh-synthesis); building it is a separate mission.

---

## 5. What remains unproven

- Distillation of techniques the exact-fit search cannot find
  (the §4 boundary).
- The borrow-to-native cycle on James's hardware (bench only).
- Whether the promoted primitive survives engine upgrades / registry
  migrations (persistence across versions untested).
- Multi-step technique composition (second-order distillation).

---

## 6. Incidents

- **Stuck foreign 8B (2026-10-01 ~09:47 UTC):** PID 12492
  (`p2_substrate_run.py` → `llama-cli`, another coordinator's) wedged for
  23+ min at ~0% CPU on a trivial prompt. Not mine; left untouched. Gone
  after the host reboot (~10:28 UTC).
- **Host reboot (~10:28 UTC):** mid-mission reboot; all proof state lives
  under `proofs/` (never `/tmp`), so nothing was lost; b2 re-ran cleanly.
- **Teacher prompt echo:** Qwen3 echoes the prompt including the
  `result: <placeholder>` line; the strict parser initially matched the
  placeholder and returned None for all 4 first tasks. Fixed by parsing
  from the end backwards and skipping `<…>` placeholders
  (`common.py:parse_result`). 12/12 verified after the fix.
- **Intermittent D-state stalls:** several probe/synthesis processes
  blocked in disk sleep (`write_all_supers`) for minutes at a time under
  load 12; all eventually completed. Not investigated further (host-level).

---

## Appendix A — Provenance

- Teacher: Qwen3-8B-GGUF Q4_K_M, revision
  `7c41481f57cb95916b40956ab2f0b139b296d974` (Apache 2.0, official Qwen
  org); weights used in place at `~/workspace/models/qwen3-8b/`, never
  copied.
- Grant pattern: `FrmGrant.issue(domain="curiosity", epoch_id=1, …)`
  (from `proofs/qwen3_acquire1_proof.py`).
- Distillation: `DistillationLoop` (`pylib/swarm_engine/acquisition/`);
  `DistillationController` is dead code, never used.
- All battery imports use `swarm_engine.*` (pylib), matching the proven
  Qwen3 acquisition proof.
- Deliverables: `demos.json` (12 verified demonstrations),
  `delta_record.json` (charter delta), `distill_result.json`
  (promoted `acquired.acq_distilled_dlt_e976_8c7453f73496`).
