"""PLOOP-12 shared real-work helper (no mocks; all real machinery).

Produces a REAL completion candidate the honest way:
  1. induce_arithmetic_from_examples genuinely synthesizes an arithmetic
     technique from training worked examples (real synthesis, N candidates
     tried, not hand-written);
  2. the induced expression tree is compiled to a real composer plan;
  3. the composer GENUINELY EXECUTES the plan (execute_sync);
  4. AcceptanceDriver._auth_gate GENUINELY authenticates: held-out args the
     attempt was not built against, claim re-execution, a negative control
     that must fail closed, and a counterfactual alternative plan that must
     diverge.
Returns (attempt, auth, info). Raises AssertionError if any leg is not real.
"""

import os
import sys

WT = os.environ.get("PLOOP12_WT",
                    os.path.expanduser("~/workspace/remor_convergence/worktrees/ploop12"))
sys.path.insert(0, os.path.join(WT, "pylib"))

from swarm_engine.services.acceptance import Attempt, AuthReport
from swarm_engine.services.acceptance_driver import AcceptanceDriver
from swarm_engine.acquisition.atomic_operators import induce_arithmetic_from_examples

# Fixed training examples: two-input addition. Deterministic induction.
TRAIN = [
    {"fields": {"x": 1.0, "y": 2.0}, "output": 3.0},
    {"fields": {"x": 5.0, "y": 7.0}, "output": 12.0},
    {"fields": {"x": 0.0, "y": 0.0}, "output": 0.0},
    {"fields": {"x": -3.0, "y": 8.0}, "output": 5.0},
    {"fields": {"x": 2.5, "y": 2.5}, "output": 5.0},
]

# Held-out: NEVER shown to induction; the attempt was not built against these.
HELD_OUT = [
    {"args": {"x": 10.0, "y": 20.0}, "expected": 30.0},
    {"args": {"x": -4.0, "y": 4.0}, "expected": 0.0},
    {"args": {"x": 100.0, "y": 0.5}, "expected": 100.5},
]

CLAIM_ARGS = {"x": 5.0, "y": 7.0}
CLAIM_VALUE = 12.0


def _compile_node(node, steps, counter):
    """Compile an induced expr tree to composer-DSL steps (real plan)."""
    if isinstance(node, tuple):
        op = node[0]
        if op == "var":
            return {"$param": node[1]}
        if op == "const":
            return float(node[1])
        if op == "one_minus":
            a = _compile_node(node[1], steps, counter)
            sid = f"s{counter[0]}"
            counter[0] += 1
            steps.append({"id": sid, "op": "one_minus", "args": {"a": a}})
            return {"$step": sid}
        a = _compile_node(node[1], steps, counter)
        b = _compile_node(node[2], steps, counter)
        sid = f"s{counter[0]}"
        counter[0] += 1
        steps.append({"id": sid, "op": op, "args": {"a": a, "b": b}})
        return {"$step": sid}
    return float(node)


def do_real_work(engine):
    """Run the real work; return (attempt, auth, info)."""
    info = {}
    # 1. real synthesis
    ind = induce_arithmetic_from_examples(TRAIN)
    info["induce_status"] = ind["status"]
    info["candidates_tried"] = ind["candidates_tried"]
    info["expr"] = ind["expr"]
    assert ind["status"] == "ok" and ind["expr"] is not None, ind

    # 2. compile to a real composer plan
    steps, counter = [], [0]
    out_ref = _compile_node(ind["expr"], steps, counter)
    plan = {"params": {"x": "any", "y": "any"},
            "steps": steps, "output": out_ref}
    info["plan"] = plan
    analysis = engine.composer.analyze(plan)
    info["analyze_ok"] = bool(analysis.ok)
    assert analysis.ok, "induced plan failed static check"

    # 3. genuine execution of the claim
    claim = engine.composer.execute_sync(plan, dict(CLAIM_ARGS))
    info["claim"] = {"success": claim.get("success"),
                     "value": claim.get("value")}
    assert claim.get("success") is True, claim
    assert claim.get("value") == CLAIM_VALUE, claim

    attempt = Attempt(
        approach_signature=["induce_arithmetic_from_examples",
                            "composer.execute_sync"],
        plan=plan, args=dict(CLAIM_ARGS),
        result_summary=claim.get("value"),
        exec_ok=bool(claim.get("success")))
    info["exec_ok"] = attempt.exec_ok
    assert attempt.exec_ok is True

    # 4. genuine authentication: held-out + claim re-execution +
    #    negative control (must fail closed) + counterfactual (must diverge)
    neg = {"args": {"x": 5.0}}  # missing y: execution must fail, not guess
    alt_plan = {"params": {"x": "any", "y": "any"},
                "steps": [{"id": "s0", "op": "add",
                           "args": {"a": {"$param": "x"},
                                    "b": {"$param": "x"}}}],
                "output": {"$step": "s0"}}  # add(x,x): genuinely wrong plan
    driver = AcceptanceDriver(_LoopShim(engine))
    auth = driver._auth_gate(attempt, HELD_OUT,
                            negative_control=neg,
                            alt_plan=alt_plan)
    info["held_out"] = {k: v.get("passed")
                        for k, v in auth.held_out.items()
                        if k.startswith("held_out")}
    info["claim_check"] = auth.held_out.get("claim_check", {}).get("passed")
    info["neg_passed"] = auth.negative_controls.get("passed")
    info["cf"] = auth.counterfactuals
    info["auth_passed"] = bool(auth.passed)
    assert auth.passed is True, info
    return attempt, auth, info


class _LoopShim:
    """Minimal loop facade: _auth_gate only needs .engine."""
    def __init__(self, engine):
        self.engine = engine
