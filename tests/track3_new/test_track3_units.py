"""Track 3 unit tests: dispatch_learning, dispatch_by_id, review wiring.

Standalone script (no pytest dependency): builds a fresh deployment,
runs focused unit checks on the new Track 3 runtime pieces, exits
nonzero on the first failure.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pylib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from driver_track3 import (boot_engine, deploy_org, install_auto_anchor,
                           SORT_GOAL, SORT_PLAN)

PASS_N = 0


def check(name, cond, detail=""):
    global PASS_N
    PASS_N += 1
    print(("PASS " if cond else "FAIL ") + name +
          (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise SystemExit(f"UNIT FAILED at: {name} {detail}")


def main(workdir):
    import shutil
    # Fresh workdir: remove prior state (journal lives in workdir_anchor)
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    anchor_dir = os.path.abspath(workdir) + "_anchor"
    if os.path.exists(anchor_dir):
        shutil.rmtree(anchor_dir)
    os.makedirs(workdir, exist_ok=True)
    from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher
    from swarm_engine.agent_org.dispatch_learning import (
        build_evidence_doc, capture_dispatch_evidence, get_evidence,
        DISPATCH_EVIDENCE_KIND)
    from swarm_engine.agent_org.store import digest
    from swarm_engine.agent_org.exceptions import VerificationFailed

    eng = boot_engine(workdir)
    org = deploy_org(workdir)
    org.review.dispatch_engine = eng
    dispatcher = NLToolDispatcher(eng)

    adm = eng.admission.admit(goal=SORT_GOAL, plan=dict(SORT_PLAN),
                              name="sort_numbers", caller=eng.oracle)
    check("unit: capability admitted", adm.ok)
    cap_id = adm.capability_id
    rec = eng.capabilities.get(cap_id)

    # -- evidence doc determinism -----------------------------------------
    d1 = build_evidence_doc(
        evidence_id="e1", dispatch_id="d1", ts=1.0, agent_id="a1",
        assignment_id="s1", request_text="sort numbers", route_via="exact_goal",
        route_score="1.0", capability_id=cap_id, capability_version="1",
        plan_fingerprint="fp", args={"items": [3, 1]},
        input_digest="in", result_value=[1, 3], result_digest="out",
        ok=True, error=None)
    d2 = build_evidence_doc(
        evidence_id="e1", dispatch_id="d1", ts=1.0, agent_id="a1",
        assignment_id="s1", request_text="sort numbers", route_via="exact_goal",
        route_score="1.0", capability_id=cap_id, capability_version="1",
        plan_fingerprint="fp", args={"items": [3, 1]},
        input_digest="in", result_value=[1, 3], result_digest="out",
        ok=True, error=None)
    check("unit: evidence doc deterministic", d1 == d2)
    check("unit: evidence doc parses", json.loads(d1)["evidence_id"] == "e1")

    # -- agent/assignment fixture ------------------------------------------
    A = org.factory.create("tpl_callable_coder_v1")
    asg = org.assignments.create(
        A.agent_id, objective={"goal": "x"}, constraints={},
        authority_scope={"workspace": A.workspace_path,
                         "allowed_capability_patterns": ["dispatch:*"]},
        expected_outputs={}, validation_requirements={},
        originating_decision="track3:unit")
    asg = org.assignments.activate(asg.assignment_id)

    # -- dispatch_by_id: governed direct dispatch ---------------------------
    r = dispatcher.dispatch_by_id(cap_id, {"items": [4, 2, 9]},
                                  producer="track3:unit")
    check("unit: dispatch_by_id ok", r.ok and r.result == [2, 4, 9],
          f"{r.result} {r.refusal}")
    check("unit: dispatch_by_id records route_via",
          r.route_via == "direct_by_id", r.route_via)
    check("unit: dispatch_by_id names the capability",
          r.capability_id == cap_id)

    r_bad = dispatcher.dispatch_by_id(cap_id, {"items": "notalist"},
                                      producer="track3:unit")
    check("unit: dispatch_by_id validates args",
          not r_bad.ok and r_bad.refusal == "bad_arguments", r_bad.refusal)

    r_unknown = dispatcher.dispatch_by_id("cap_doesnotexist", {"items": [1]},
                                          producer="track3:unit")
    check("unit: dispatch_by_id refuses unknown id",
          not r_unknown.ok and r_unknown.refusal == "capability_vanished",
          r_unknown.refusal)

    # quarantine the capability: direct dispatch must refuse (governed)
    from swarm_engine.synthesis.integrity import quarantine_everywhere
    quarantine_everywhere(eng, cap_id, reason="track3:unit",
                          caller=eng.oracle)
    r_q = dispatcher.dispatch_by_id(cap_id, {"items": [1]},
                                    producer="track3:unit")
    check("unit: dispatch_by_id refuses quarantined",
          not r_q.ok and r_q.refusal == "capability_unavailable", r_q.refusal)
    from swarm_engine.synthesis.integrity import restore_everywhere
    restore_everywhere(eng, cap_id, caller=eng.oracle,
                       reason="track3:unit")
    r_re = dispatcher.dispatch_by_id(cap_id, {"items": [2, 1]},
                                     producer="track3:unit")
    check("unit: dispatch_by_id works after governed restore",
          r_re.ok and r_re.result == [1, 2], str(r_re.result))

    # -- capture: unknown agent / wrong assignment ---------------------------
    try:
        capture_dispatch_evidence(eng, org.store, r.dispatch_id, "agt_nope",
                                  asg.assignment_id, {"items": [4, 2, 9]},
                                  [2, 4, 9])
        check("unit: capture refuses unknown agent", False, "captured")
    except ValueError as exc:
        check("unit: capture refuses unknown agent", True, str(exc)[:80])

    # -- review without an attached engine -----------------------------------
    org2_review_engine = org.review.dispatch_engine
    org.review.dispatch_engine = None
    ev_id = capture_dispatch_evidence(
        eng, org.store, r.dispatch_id, A.agent_id, asg.assignment_id,
        {"items": [4, 2, 9]}, [2, 4, 9])
    try:
        org.review.verify_dispatch_evidence(ev_id)
        check("unit: review without engine refused", False, "verified")
    except VerificationFailed as exc:
        check("unit: review without engine refused", "engine" in str(exc),
              str(exc)[:100])
    finally:
        org.review.dispatch_engine = org2_review_engine

    # -- evidence round-trip ---------------------------------------------------
    ev = get_evidence(org.store, ev_id)
    check("unit: evidence round-trips", ev.evidence_id == ev_id and
          ev.dispatch_id == r.dispatch_id and ev.agent_id == A.agent_id)
    check("unit: evidence version normalized to string",
          ev.capability_version == "1", repr(ev.capability_version))
    try:
        get_evidence(org.store, "dsp_ev_nope")
        check("unit: unknown evidence KeyError", False, "returned")
    except KeyError:
        check("unit: unknown evidence KeyError", True)

    # -- duplicate capture refused ---------------------------------------------
    try:
        capture_dispatch_evidence(eng, org.store, r.dispatch_id, A.agent_id,
                                  asg.assignment_id, {"items": [4, 2, 9]},
                                  [2, 4, 9])
        check("unit: duplicate capture refused", False, "captured twice")
    except ValueError as exc:
        check("unit: duplicate capture refused", "already exists" in str(exc),
              str(exc)[:80])

    # -- producer string never establishes provenance --------------------------
    r_imp = dispatcher.dispatch("sort numbers", {"items": [5, 1]},
                                producer="agent:mallory")
    check("unit: impersonating dispatch executes", r_imp.ok)
    ev_imp = capture_dispatch_evidence(
        eng, org.store, r_imp.dispatch_id, A.agent_id, asg.assignment_id,
        {"items": [5, 1]}, [1, 5])
    ev_imp_row = get_evidence(org.store, ev_imp)
    check("unit: producer string ignored for attribution",
          ev_imp_row.agent_id == A.agent_id, ev_imp_row.agent_id)

    print(f"UNIT COMPLETE: {PASS_N} checks", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/t3unit")
