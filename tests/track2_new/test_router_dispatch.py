"""Intent router (§3) + NL tool dispatch (§4) tests.

Causal chain: admit real capabilities -> route NL text (exact + structural)
-> dispatch through the real Composer path -> persisted dispatch records.
Adversarial battery: unknown/ambiguous/quarantined intents, arg injection,
callable args, oversized/malformed input, TOCTOU quarantine, replay.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pylib"))

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.synthesis.admission import Verdict
from swarm_engine.synthesis.integrity import quarantine_everywhere, effective_status
from swarm_engine.synthesis.intent_router import IntentRouter
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher

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
CONCAT_PLAN = {
    "name": "concat_two", "params": {"a": "str", "b": "str"},
    "steps": [{"id": "s1", "op": "concat", "args": {"a": {"$param": "a"}, "b": {"$param": "b"}}}],
    "output": {"$step": "s1"},
}


def main():
    db = os.path.join(tempfile.mkdtemp(prefix="router_", dir=SCRATCH), "eng.db")
    eng = SwarmEngine(db_path=db)
    r1 = eng.admission.admit(goal="add two numbers together", plan=dict(ADD_PLAN))
    r2 = eng.admission.admit(goal="concatenate two strings", plan=dict(CONCAT_PLAN))
    assert r1.verdict == Verdict.ADMITTED and r2.verdict == Verdict.ADMITTED
    add_id, concat_id = r1.capability_id, r2.capability_id

    router = IntentRouter(eng)

    # ---- routing ----
    rt = router.route("add two numbers together")
    check("route: exact goal hit", rt.ok and rt.via == "exact_goal" and rt.capability_id == add_id,
          str(rt.as_dict()))
    rt2 = router.route("concatenate two strings")
    check("route: exact goal hit (concat)", rt2.ok and rt2.capability_id == concat_id)

    # wrong-kind arg refused while add is still active (str does not coerce to num)
    disp_early = NLToolDispatcher(eng, router)
    d4b = disp_early.dispatch("add two numbers together", {"a": "not_a_number", "b": 2})
    check("dispatch: non-numeric string for num param refused",
          not d4b.ok and d4b.refusal == "bad_arguments", str(d4b.as_dict()))
    d4c = disp_early.dispatch("add two numbers together", {"a": 40, "b": 2})
    check("dispatch: add works", d4c.ok and d4c.result == 42, str(d4c.as_dict()))

    rt3 = router.route("sum a pair of numbers")
    print("   info: paraphrase route ->", rt3.ok, rt3.via, rt3.refusal,
          (rt3.capability_id or "")[:12], f"score={rt3.score:.3f}")
    # Honest assertion: whatever it decides must be consistent (no crash, valid shape)
    check("route: paraphrase returns well-formed result",
          isinstance(rt3.ok, bool) and (rt3.ok or rt3.refusal in (
              "unknown_intent", "ambiguous_intent", "capability_unavailable")))

    rt4 = router.route("bake a chocolate cake at 350 degrees")
    check("route: unrelated intent refused",
          not rt4.ok and rt4.refusal == "unknown_intent", str(rt4.as_dict()))

    for bad, code in [("", "empty_intent"), ("   ", "empty_intent"),
                      ("x" * 5000, "oversized_input")]:
        r = router.route(bad)
        check(f"route: {code} refused", not r.ok and r.refusal == code)
    r = router.route(None)
    check("route: non-string refused", not r.ok and r.refusal == "invalid_input")
    r = router.route(123)
    check("route: int input refused", not r.ok and r.refusal == "invalid_input")

    # quarantined capability is not routable (goal binding was dropped by
    # quarantine_everywhere, so the router reports unknown_intent --
    # either way it must not route)
    quarantine_everywhere(eng, add_id, "router test quarantine")
    rq = router.route("add two numbers together")
    check("route: quarantined capability refused",
          not rq.ok and rq.refusal in ("capability_unavailable", "unknown_intent"),
          str(rq.as_dict()))

    # ---- dispatch ----
    disp = NLToolDispatcher(eng, router)
    d1 = disp.dispatch("concatenate two strings", {"a": "foo", "b": "bar"},
                       producer="test:router")
    check("dispatch: concat works", d1.ok and d1.result == "foobar", str(d1.as_dict()))
    check("dispatch: dispatch_id assigned", d1.dispatch_id is not None)
    hist = disp.history()
    concat_recs = [h for h in hist if h["capability_id"] == concat_id and h["ok"] == 1]
    check("dispatch: record persisted", len(concat_recs) >= 1)
    check("dispatch: digests recorded",
          bool(concat_recs[0]["input_digest"]) and bool(concat_recs[0]["result_digest"]))

    d2 = disp.dispatch("concatenate two strings", {"a": "foo"})
    check("dispatch: missing arg refused",
          not d2.ok and d2.refusal == "bad_arguments", str(d2.as_dict()))
    d3 = disp.dispatch("concatenate two strings", {"a": "foo", "b": "bar", "c": 1})
    check("dispatch: extra arg refused", not d3.ok and d3.refusal == "bad_arguments")
    d4 = disp.dispatch("concatenate two strings", {"a": "foo", "b": 42})
    check("dispatch: int->str coerces per engine semantics",
          d4.ok and d4.result == "foo42", str(d4.as_dict()))

    # arg injection attempts
    d5 = disp.dispatch("concatenate two strings", {"a": "__import__('os').system('x')", "b": "y"})
    check("dispatch: code-string arg coerced/refused safely",
          not d5.ok or isinstance(d5.result, str), str(d5.as_dict()))
    d6 = disp.dispatch("concatenate two strings", {"a": lambda x: x, "b": "y"})
    check("dispatch: callable arg refused",
          not d6.ok and d6.refusal == "bad_arguments", str(d6.as_dict()))
    d7 = disp.dispatch("concatenate two strings", {"__proto__": "x", "a": "p", "b": "q"})
    check("dispatch: dunder arg refused", not d7.ok and d7.refusal == "bad_arguments")
    d8 = disp.dispatch("concatenate two strings", {"a": "x" * 9000, "b": "y"})
    check("dispatch: oversized string arg refused",
          not d8.ok and d8.refusal == "bad_arguments")
    d9 = disp.dispatch("concatenate two strings", ["not", "a", "dict"])
    check("dispatch: non-dict args refused", not d9.ok and d9.refusal == "bad_arguments")

    # dispatch to quarantined capability -> refused (router won't route)
    dq = disp.dispatch("add two numbers together", {"a": 1, "b": 2})
    check("dispatch: quarantined capability refused",
          not dq.ok and dq.refusal in ("capability_unavailable", "unknown_intent"),
          str(dq.as_dict()))

    # unknown intent -> refused, nothing executed, nothing recorded
    n_before = len(disp.history())
    du = disp.dispatch("bake a chocolate cake", {"temp": 350})
    check("dispatch: unknown intent refused",
          not du.ok and du.refusal == "unknown_intent")
    check("dispatch: refused dispatch not recorded",
          len(disp.history()) == n_before)

    # numeric coercion still works through the honest path (concat needs strings;
    # use a fresh engine check for add via direct dispatcher on unquarantined)
    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
