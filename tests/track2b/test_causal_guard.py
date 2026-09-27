"""Causal proof for the deliberate-quarantine guard (admission.py 0c).

Claim: the 0c guard -- not anything else -- is what stops silent
resurrection of a deliberately-quarantined capability via re-admission.

Method: the guard's decision keys on
CapabilityStore._quarantine_was_deliberate. Monkeypatching that predicate
to False reproduces the exact pre-guard code path (guard present but
inert); restoring it reproduces the guarded path. Both run against the
same engine/DB lineage.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pylib"))

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.synthesis.admission import Verdict
from swarm_engine.synthesis.capability_store import CapabilityStore
from swarm_engine.synthesis.integrity import quarantine_everywhere, effective_status

SCRATCH = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


ADD_PLAN = {
    "name": "add_two", "params": {"a": "num", "b": "num"},
    "steps": [{"id": "s1", "op": "add", "args": {"a": {"$param": "a"}, "b": {"$param": "b"}}}],
    "output": {"$step": "s1"},
}
GOAL = "add two numbers together"


def main():
    db = os.path.join(tempfile.mkdtemp(prefix="causal_", dir=SCRATCH), "eng.db")
    eng = SwarmEngine(db_path=db)
    r = eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN), caller=eng.oracle)
    assert r.verdict == Verdict.ADMITTED
    cap_id = r.capability_id
    quarantine_everywhere(eng, cap_id, "causal test", caller=eng.oracle)

    real_pred = CapabilityStore._quarantine_was_deliberate

    # -- guard inert (pre-guard behavior): silent resurrection happens --
    CapabilityStore._quarantine_was_deliberate = lambda self, cid: False
    try:
        r2 = eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN), caller=eng.oracle)
        resurrected = (r2.verdict == Verdict.ADMITTED
                       and eng.capabilities.get(cap_id).status == "active"
                       and eng.primitives.get(f"acquired.{cap_id}") is not None)
        eff = effective_status(eng, cap_id)
        check("causal: guard inert -> identical re-admission resurrects",
              resurrected, f"verdict={r2.verdict}")
        check("causal: resurrected primitive is executable",
              eng.primitives.get(f"acquired.{cap_id}").fn(a=2, b=3) == 5)
        check("causal: resurrection leaves tri-system inconsistent "
              "(store active, trust/lifecycle quarantined)",
              eff["store_status"] == "active" and not eff["consistent"],
              str({k: eff.get(k) for k in ("store_status", "trust", "lifecycle", "consistent")}))
    finally:
        CapabilityStore._quarantine_was_deliberate = real_pred

    # -- guard active: re-admission refused, primitive stays dead --
    # (re-quarantine deliberately: the inert run left store active)
    quarantine_everywhere(eng, cap_id, "causal test re-quarantine", caller=eng.oracle)
    r3 = eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN), caller=eng.oracle)
    check("causal: guard active -> identical re-admission REJECTED",
          r3.verdict == Verdict.REJECTED, f"verdict={r3.verdict}")
    check("causal: primitive stays unregistered",
          eng.primitives.get(f"acquired.{cap_id}") is None)
    eff3 = effective_status(eng, cap_id)
    check("causal: tri-system stays consistently quarantined",
          eff3["effective"] == "quarantined" and eff3["consistent"])

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
