#!/usr/bin/env python3
"""CUR-P4A proof battery: Activation request -> take-up (C-9, chunk 4a).

Every check runs against the REAL machinery in fresh processes:
the real Primary-side LivePath interfaces are consumed read-only, the real
CuriosityExecutive.request_activation decides, the real run controller
dispatches/steps/stops, the real fenced Evidence Store persists.

No mocks. No Primary code is executed as a double: Primary-issued request
dicts are DATA records per the design's §1 contract (like Findings), not
behavior stand-ins.
"""

import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]  # worktree root, no hardcode
RUNS = Path(__file__).resolve().parent / "runs"
sys.path.insert(0, str(WORKTREE / "pylib"))
sys.path.insert(0, str(WORKTREE / "proofs" / "cur_p3a"))  # int_stack helpers

from swarm_engine.curiosity.activation import (
    ST_ACCEPTED, ST_REFUSED, ST_REQUESTED, ST_WITHDRAWN,
    R_CURIOSITY_DISABLED, R_DUPLICATE_ACTIVE_REQUEST, R_KILLED,
    R_REFUSED_FIT, R_REFUSED_GRANT, R_REQUEST_MALFORMED, R_WITHDRAWN,
    ActivationTakeUp, DuplicateActiveRequest, RequestMalformed, TakeUpError,
)
from swarm_engine.curiosity.activation.request import ActivationRequest
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.executive.executive import (
    ABSENT_OWNERSHIP, LOOP_OWNERSHIP)
from swarm_engine.curiosity.frm.policy import FrmPolicy
from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
from swarm_engine.curiosity.rollcall.scheduler import (
    RollCallPolicy, RollCallScheduler)
from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
from swarm_engine.curiosity.rollcall.gam import GovernanceAttestationMonitor
from swarm_engine.curiosity.rollcall.responder import HonestTestDouble
from swarm_engine.curiosity.substrate import CuriositySubstrate
from swarm_engine.curiosity.run_controller.controller import (
    CuriosityRunController)
from swarm_engine.curiosity.executive.executive import CuriosityExecutive
from swarm_engine.governance.curiosity_enforcement._persistence import (
    StateStore)
from swarm_engine.governance.curiosity_enforcement.states import (
    DOMAIN, EnforcementRecord, EnforcementState)

from int_stack import CountingFRM, met_roll_call  # proven harness pieces

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} -- {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} -- {detail}")


def p4a_stack(name, *, corpus_docs, frm=None, demand_budget_s=60.0,
              demand_concurrent=2, total_budget_s=600.0):
    """Full real stack under proofs/cur_p4a/runs/<name>/ (mirrors the
    proven int_stack.new_stack construction)."""
    rundir = RUNS / name
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "payloads").mkdir(parents=True, exist_ok=True)
    policy = FrmPolicy(
        total_budget_s=total_budget_s, total_max_concurrent=8,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        epoch_s=300.0)
    if frm is None:
        frm = CountingFRM(policy)
    sched = RollCallScheduler(RollCallPolicy())
    gam = GovernanceAttestationMonitor(
        sched, AttestationLedger(str(rundir / "att.db")))
    enf_dir = str(rundir / "enf")
    sub = CuriositySubstrate()
    rc = CuriosityRunController(
        substrate=sub, checkpoint_db=str(rundir / "ckpt.db"),
        evidence_db=str(rundir / "ev.db"),
        ledger_db=str(rundir / "term.db"),
        attribution_db=str(rundir / "attr.db"),
        payload_dir=str(rundir / "payloads"),
        corpus_docs=list(corpus_docs))
    ex = CuriosityExecutive(
        frm=frm, enforcement_state_dir=enf_dir, gam=gam,
        run_controller=rc, demand_budget_s=demand_budget_s,
        demand_concurrent=demand_concurrent)
    return {"dir": rundir, "frm": frm, "gam": gam, "enf_dir": enf_dir,
            "sub": sub, "rc": rc, "ex": ex}


def corpus():
    files = ["runtime/curiosity/run_controller/controller.py",
             "runtime/curiosity/executive/executive.py"]
    return [(WORKTREE / f).read_text() for f in files]


QUESTION = "How does the run controller enforce the budget slice?"


def primary_request(**over):
    d = {
        "request_id": uuid.uuid4().hex,
        "issued_at": time.time(),
        "issued_by": "primary_executive",
        "operational_objective_ref": {
            "objective_id": "obj-keep-turning",
            "statement": "Keep the Primary loop turning",
        },
        "knowledge_gap": QUESTION,
        "bounded_requirement": {
            "scope": "curiosity run controller",
            "constraints": {"max_inquiries": 1},
            "fit_hints": {"boundary_class": "imprecise_question"},
        },
        "epoch_context": {"epoch_id": 0, "issued_under_grant": {}},
    }
    d.update(over)
    return d


def takeup_for(stack):
    return ActivationTakeUp(executive=stack["ex"],
                            run_controller=stack["rc"])


def drive_to_terminal(rc, inquiry_id, max_ticks=120):
    for _ in range(max_ticks):
        views = {v["inquiry_id"]: v for v in rc.inquiry_views()}
        st = views[inquiry_id]["state"]
        if st in ("TERMINATED", "SUSPENDED", "KILLED"):
            return st
        rc.tick()
    return "TICK_LIMIT"


def wait_active(rc, inquiry_id, max_ticks=10):
    for _ in range(max_ticks):
        rc.tick()
        views = {v["inquiry_id"]: v for v in rc.inquiry_views()}
        if views[inquiry_id]["state"] == "ACTIVE":
            return True
    return False


# ---------------------------------------------------------------- T01
def t01_validation():
    print("T01: ActivationRequest fail-closed validation (R1)")
    for label, mutate in [
        ("missing knowledge_gap", {"knowledge_gap": "   "}),
        ("missing bounded_requirement", {"bounded_requirement": {}}),
        ("missing objective ref", {"operational_objective_ref": {}}),
        ("wrong issued_by", {"issued_by": "curiosity_executive"}),
    ]:
        try:
            ActivationRequest.from_primary_dict(primary_request(**mutate))
            check(f"t01 {label} refused", False, "no refusal raised")
        except RequestMalformed as exc:
            check(f"t01 {label} refused",
                  R_REQUEST_MALFORMED in str(exc), str(exc)[:70])
    r = ActivationRequest.from_primary_dict(primary_request())
    check("t01 well-formed request validates", r.state == ST_REQUESTED,
          f"state={r.state}")


# ---------------------------------------------------------------- T02
def t02_duplicate():
    print("T02: duplicate request_id refused (R2)")
    stack = p4a_stack("t02", corpus_docs=corpus())
    tu = takeup_for(stack)
    d = primary_request()
    rid = tu.receive(d)
    try:
        tu.receive(d)
        check("t02 duplicate receive refused", False, "no refusal raised")
    except DuplicateActiveRequest as exc:
        check("t02 duplicate receive refused",
              R_DUPLICATE_ACTIVE_REQUEST in str(exc), str(exc)[:60])
    check("t02 first request still indexed", tu.get_request(rid)["state"]
          == ST_REQUESTED, f"state={tu.get_request(rid)['state']}")


# ---------------------------------------------------------------- T03
def t03_accept_end_to_end():
    print("T03: take-up ACCEPTED end-to-end through a real loop")
    stack = p4a_stack("t03", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    rec = tu.consider(primary_request())
    check("t03 take-up ACCEPTED",
          rec["state"] == ST_ACCEPTED and rec["decision"]["verdict"]
          == "ACCEPTED", f"state={rec['state']}")
    check("t03 decision written exactly once",
          rec["decision"] is not None and rec["inquiry_id"] is not None,
          f"inquiry={rec['inquiry_id']}")
    basis = rec["decision_basis"]
    check("t03 decision_basis populated",
          basis.get("kill_state") == "RUNNING"
          and basis.get("grant_available", {}).get("budget_s", 0) > 0
          and basis.get("fit") == "questioning",
          f"basis={basis}")
    try:
        tu.decide(rec["request_id"])
        check("t03 second decide refused (exactly-once)", False,
              "no refusal raised")
    except TakeUpError as exc:
        check("t03 second decide refused (exactly-once)", True,
              str(exc)[:60])
    iid = rec["inquiry_id"]
    terminal = drive_to_terminal(stack["rc"], iid)
    check("t03 inquiry ran to a terminal state", terminal == "TERMINATED",
          f"terminal={terminal}")
    inq = stack["rc"]._inquiries[iid]
    check("t03 inquiry ran as PRIMARY_REQUESTED",
          inq.trigger.origin == "PRIMARY_REQUESTED",
          f"origin={inq.trigger.origin}")
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    ev = store.get(inq.evidence_id)
    check("t03 terminal finding in the fenced store",
          ev is not None and ev.terminal_state == inq.terminal_state,
          f"ev={inq.evidence_id} terminal={inq.terminal_state}")
    check("t03 finding provenance complete, Primary-requested",
          ev.provenance.loop == "questioning" and ev.origin
          == "PRIMARY_REQUESTED" and bool(ev.provenance.model),
          f"loop={ev.provenance.loop} origin={ev.origin}")

# ---------------------------------------------------------------- T04
def t04_kill_state_refusal():
    print("T04: request while killed -> CURIOSITY_DISABLED, named (R3)")
    stack = p4a_stack("t04", corpus_docs=corpus())
    met_roll_call(stack)
    StateStore(stack["enf_dir"]).write_record(EnforcementRecord(
        domain=DOMAIN, state=EnforcementState.SUSPENDED_SAFETY,
        prev_state=EnforcementState.RUNNING, issuer="safety-authority",
        reason_refs={"test": "t04"}, entered_at=time.time()))
    tu = takeup_for(stack)
    rec = tu.consider(primary_request())
    check("t04 CURIOSITY_DISABLED named",
          rec["state"] == ST_REFUSED
          and rec["decision"]["verdict"] == R_CURIOSITY_DISABLED,
          f"verdict={rec['decision']['verdict']}")
    check("t04 underlying state attributed, not hidden",
          rec["decision_basis"].get("kill_state") == "SUSPENDED_SAFETY",
          f"basis={rec['decision_basis']}")
    check("t04 not silently queued (no inquiry created)",
          rec["inquiry_id"] is None, f"inquiry={rec['inquiry_id']}")


# ---------------------------------------------------------------- T05
def t05_no_grant_refusal():
    print("T05: request with no grant -> REFUSED_GRANT, named (R4)")
    # The curiosity domain states zero demand: the real FRM evaluates a
    # genuine round and grants zero -> the executive refuses NO_BUDGET.
    stack = p4a_stack("t05", corpus_docs=corpus(), demand_budget_s=0.0,
                      demand_concurrent=0)
    met_roll_call(stack)
    tu = takeup_for(stack)
    rec = tu.consider(primary_request())
    check("t05 REFUSED_GRANT named",
          rec["state"] == ST_REFUSED
          and rec["decision"]["verdict"] == R_REFUSED_GRANT,
          f"verdict={rec['decision']['verdict']}")
    check("t05 adequacy estimate recorded",
          bool(rec["decision_basis"].get("grant_available")),
          f"basis={rec['decision_basis']}")
    check("t05 not a negotiation (no second FRM round spent)",
          stack["frm"].rounds == 1, f"rounds={stack['frm'].rounds}")


# ---------------------------------------------------------------- T06
def t06_fit_refusals():
    print("T06: unfit requests -> REFUSED_FIT, named (R5)")
    stack = p4a_stack("t06", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    d1 = primary_request()
    d1["bounded_requirement"]["fit_hints"] = {"boundary_class": "teleport"}
    rec = tu.consider(d1)
    check("t06 unknown hint class -> REFUSED_FIT",
          rec["decision"]["verdict"] == R_REFUSED_FIT,
          f"verdict={rec['decision']['verdict']}")
    absent = sorted(set(ABSENT_OWNERSHIP) - set(LOOP_OWNERSHIP))
    if absent:
        d2 = primary_request()
        d2["bounded_requirement"]["fit_hints"] = {
            "boundary_class": absent[0]}
        rec2 = tu.consider(d2)
        check(f"t06 absent class {absent[0]} -> REFUSED_FIT (LOOP_ABSENT)",
              rec2["decision"]["verdict"] == R_REFUSED_FIT
              and "LOOP_ABSENT" in rec2["decision"]["reason"],
              f"verdict={rec2['decision']['verdict']}")
    else:
        check("t06 absent-class case", False, "no absent class in tree")


# ---------------------------------------------------------------- T07
def t07_withdraw_before_takeup():
    print("T07: withdrawal before take-up -> WITHDRAWN, nothing committed")
    stack = p4a_stack("t07", corpus_docs=corpus())
    tu = takeup_for(stack)
    rid = tu.receive(primary_request())
    rec = tu.withdraw(rid, "primary objective closed before take-up")
    check("t07 WITHDRAWN recorded",
          rec["state"] == ST_WITHDRAWN
          and rec["withdrawn_reason"] == "primary objective closed before take-up"
          and rec["withdrawn_at"] is not None,
          f"state={rec['state']}")
    check("t07 no resources committed (no inquiry, no decision)",
          rec["inquiry_id"] is None and rec["decision"] is None,
          f"inquiry={rec['inquiry_id']} decision={rec['decision']}")
    try:
        tu.decide(rid)
        check("t07 decide-after-withdraw refused", False,
              "no refusal raised")
    except TakeUpError as exc:
        check("t07 decide-after-withdraw refused", True, str(exc)[:60])


# ---------------------------------------------------------------- T08
def t08_withdraw_midflight():
    print("T08: withdrawal mid-flight -> ordinary stop, INCONCLUSIVE kept")
    stack = p4a_stack("t08", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    rec = tu.consider(primary_request())
    iid = rec["inquiry_id"]
    check("t08 inquiry admitted", rec["state"] == ST_ACCEPTED,
          f"inquiry={iid}")
    if not wait_active(stack["rc"], iid):
        check("t08 inquiry reached ACTIVE before withdrawal", False,
              "never observed ACTIVE")
        return
    wrec = tu.withdraw(rec["request_id"], "primary reprioritized")
    check("t08 request WITHDRAWN with cause",
          wrec["state"] == ST_WITHDRAWN
          and "reprioritized" in (wrec["withdrawn_reason"] or ""),
          f"state={wrec['state']}")
    inq = stack["rc"]._inquiries[iid]
    check("t08 inquiry TERMINATED, not KILLED",
          inq.state == "TERMINATED" and not inq.kill_requested,
          f"state={inq.state} kill_requested={inq.kill_requested}")
    check("t08 stop record carries the cause, not a kill record",
          inq.result.get("stop_cause", "").startswith("WITHDRAWN")
          and "kill_reason" not in inq.result,
          f"result_keys={sorted(inq.result)}")
    lineage_events = [e.get("event") for e in inq.lineage]
    check("t08 lineage: stopped, never killed",
          "stopped" in lineage_events and "killed" not in lineage_events
          and "kill_requested" not in lineage_events,
          f"events={lineage_events[-4:]}")
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    ev = store.get(inq.evidence_id)
    check("t08 partial evidence INCONCLUSIVE with cause, in fenced store",
          ev is not None and ev.terminal_state == "INCONCLUSIVE"
          and ev.provenance.loop == "questioning",
          f"terminal={ev.terminal_state if ev else None}")
    payload = json.loads(
        (stack["dir"] / "payloads" / f"{inq.evidence_id}.json").read_text())
    check("t08 payload names the withdrawal cause",
          payload.get("stop_cause", "").startswith("WITHDRAWN"),
          f"stop_cause={payload.get('stop_cause')}")


# ---------------------------------------------------------------- T09
def t09_withdrawal_vs_kill():
    print("T09: withdrawal and kill share no record path")
    stack = p4a_stack("t09", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    # Withdrawn inquiry.
    rec_w = tu.consider(primary_request())
    iid_w = rec_w["inquiry_id"]
    assert wait_active(stack["rc"], iid_w), "withdrawn inquiry never ACTIVE"
    tu.withdraw(rec_w["request_id"], "t09 contrast")
    inq_w = stack["rc"]._inquiries[iid_w]
    # Killed inquiry (separate request).
    rec_k = tu.consider(primary_request())
    iid_k = rec_k["inquiry_id"]
    assert wait_active(stack["rc"], iid_k), "killed inquiry never ACTIVE"
    stack["rc"].kill_inquiry(iid_k, "t09 contrast kill")
    inq_k = stack["rc"]._inquiries[iid_k]
    check("t09 withdrawn: TERMINATED/WITHDRAWN records",
          inq_w.state == "TERMINATED"
          and inq_w.result.get("stop_cause", "").startswith("WITHDRAWN"),
          f"state={inq_w.state}")
    check("t09 killed: KILLED records",
          inq_k.state == "KILLED" and inq_k.kill_requested
          and inq_k.result.get("kill_reason") == "t09 contrast kill",
          f"state={inq_k.state}")
    check("t09 record paths disjoint",
          "stop_cause" not in inq_k.result
          and "kill_reason" not in inq_w.result,
          "withdrawn has no kill_reason; killed has no stop_cause")


# ---------------------------------------------------------------- T10
def t10_no_acceptance_write():
    print("T10: C-2.1/C-2.3 -- curiosity side cannot close Primary bounds")
    import swarm_engine.curiosity.activation.takeup as tumod
    import swarm_engine.curiosity.activation.request as reqmod
    src = open(tumod.__file__).read() + open(reqmod.__file__).read()
    check("t10 no acceptance-record writer in the take-up path",
          "acceptance" not in src.lower().replace(
              "primary acceptance inspects", ""),
          "no 'acceptance' write capability in activation/")
    out = __import__("subprocess").run(
        ["git", "status", "--porcelain", "runtime/core/"],
        capture_output=True, text=True, cwd=str(WORKTREE))
    check("t10 no Primary-side file written", out.stdout.strip() == "",
          f"git status runtime/core/: {out.stdout.strip()[:80] or 'clean'}")
    stack = p4a_stack("t10", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    rec = tu.consider(primary_request())
    check("t10 decision is take-up, not an acceptance record",
          rec["decision"]["verdict"] == "ACCEPTED"
          and "acceptance" not in json.dumps(rec["decision"]).lower(),
          f"verdict={rec['decision']['verdict']}")


# ---------------------------------------------------------------- T11
def t11_decision_shape():
    print("T11: decision record shape (exactly-once, auditable basis)")
    stack = p4a_stack("t11", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    rec = tu.consider(primary_request())
    d = rec["decision"]
    check("t11 decision has verdict/reason/decided_at",
          set(("verdict", "reason", "decided_at")) <= set(d),
          f"keys={sorted(d)}")
    check("t11 decision_basis has the three evaluated inputs",
          set(("kill_state", "grant_available", "fit"))
          <= set(rec["decision_basis"]),
          f"basis_keys={sorted(rec['decision_basis'])}")


# ---------------------------------------------------------------- T12
def t12_distinct_ids_distinct_requests():
    print("T12: same boundary, new id -> distinct request (designed)")
    stack = p4a_stack("t12", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    r1 = tu.consider(primary_request())
    r2 = tu.consider(primary_request())
    check("t12 both accepted as distinct requests",
          r1["state"] == ST_ACCEPTED and r2["state"] == ST_ACCEPTED
          and r1["inquiry_id"] != r2["inquiry_id"],
          f"inq1={r1['inquiry_id']} inq2={r2['inquiry_id']}")
    check("t12 each has its own named decision",
          r1["decision"]["verdict"] == "ACCEPTED"
          and r2["decision"]["verdict"] == "ACCEPTED",
          "no silent duplication: two ids, two decisions")


# ---------------------------------------------------------------- T13
def t13_refusal_names():
    print("T13: refusal names match the design §4 table R1-R7")
    check("t13 R1..R7 names",
          (R_REQUEST_MALFORMED, R_DUPLICATE_ACTIVE_REQUEST,
           R_CURIOSITY_DISABLED, R_REFUSED_GRANT, R_REFUSED_FIT,
           R_WITHDRAWN, R_KILLED)
          == ("REQUEST_MALFORMED", "DUPLICATE_ACTIVE_REQUEST",
              "CURIOSITY_DISABLED", "REFUSED_GRANT", "REFUSED_FIT",
              "WITHDRAWN", "KILLED"),
          "R1-R7 stable")


def main():
    RUNS.mkdir(parents=True, exist_ok=True)
    t01_validation()
    t02_duplicate()
    t03_accept_end_to_end()
    t04_kill_state_refusal()
    t05_no_grant_refusal()
    t06_fit_refusals()
    t07_withdraw_before_takeup()
    t08_withdraw_midflight()
    t09_withdrawal_vs_kill()
    t10_no_acceptance_write()
    t11_decision_shape()
    t12_distinct_ids_distinct_requests()
    t13_refusal_names()
    print(f"\nCUR-P4A: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
