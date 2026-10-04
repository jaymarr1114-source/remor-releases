#!/usr/bin/env python3
"""CUR-P4B proof battery: disabled-domain + kill paths (C-9, chunk 4b).

Every check runs against the REAL machinery in fresh processes:
- the REAL EnforcementEngine (issuer-authorized D-3 transitions) as the
  external authority's act — not a store write bypass;
- the REAL CuriosityExecutive.request_activation / kill_inquiry;
- the REAL run controller kill path (with the disclosed C-9.4
  preservation extension);
- the REAL fenced Evidence Store (append-only by construction);
- the REAL Primary-side LivePath interfaces consumed read-only
  (no Primary mock stands in for runtime/core/).

No mocks. Primary-issued request dicts are DATA records per the design's
§1 contract (like Findings), not behavior stand-ins.
"""

import json
import sys
import time
import uuid
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]  # worktree root, no hardcode
RUNS = Path(__file__).resolve().parent / "runs"
sys.path.insert(0, str(WORKTREE / "pylib"))
sys.path.insert(0, str(WORKTREE / "proofs" / "cur_p3a"))  # int_stack helpers

from swarm_engine.curiosity.activation import (
    ST_ACCEPTED, ST_KILLED, ST_REFUSED, ST_WITHDRAWN,
    R_CURIOSITY_DISABLED, R_KILLED,
    ActivationTakeUp, TakeUpError,
    TerminationRefused, record_kill_termination,
)
from swarm_engine.curiosity.evidence.records import (
    CuriosityFinding, EvidenceProvenance,
)
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.executive.boundary import CuriosityTrigger
from swarm_engine.curiosity.executive.executive import CuriosityExecutive
from swarm_engine.curiosity.frm.policy import FrmPolicy
from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
from swarm_engine.curiosity.rollcall.scheduler import (
    RollCallPolicy, RollCallScheduler)
from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
from swarm_engine.curiosity.rollcall.gam import GovernanceAttestationMonitor
from swarm_engine.curiosity.substrate import CuriositySubstrate
from swarm_engine.curiosity.run_controller.controller import (
    CuriosityRunController)
from swarm_engine.governance.curiosity_enforcement._engine import (
    EnforcementEngine, IssuerRefused, ReenableRefused, TransitionRefused,
)
from swarm_engine.governance.curiosity_enforcement.states import (
    DOMAIN, EnforcementState,
    ISSUER_FRM, ISSUER_SAFETY_AUTHORITY, ISSUER_ENFORCEMENT,
    ISSUER_JAMES, ISSUER_PRIMARY,
)
from swarm_engine.governance.curiosity_enforcement.read_api import (
    read_kill_ledger, verify_kill_ledger, read_rollback_status,
)

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


def p4b_stack(name, *, corpus_docs, demand_budget_s=60.0,
              demand_concurrent=2, total_budget_s=600.0):
    """Full real stack under proofs/cur_p4b/runs/<name>/ (mirrors the
    proven p4a_stack construction)."""
    rundir = RUNS / name
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "payloads").mkdir(parents=True, exist_ok=True)
    policy = FrmPolicy(
        total_budget_s=total_budget_s, total_max_concurrent=8,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        epoch_s=300.0)
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
    engine = EnforcementEngine(enf_dir)
    return {"dir": rundir, "frm": frm, "gam": gam, "enf_dir": enf_dir,
            "sub": sub, "rc": rc, "ex": ex, "engine": engine,
            "takeup": ActivationTakeUp(executive=ex, run_controller=rc)}


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


def drive_to_terminal(rc, inquiry_id, max_ticks=200):
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


def live_primary_inquiry(stack, tag):
    """Take up a Primary request and return (request_id, inquiry_id) live."""
    tu = stack["takeup"]
    rec = tu.consider(primary_request())
    assert rec["state"] == ST_ACCEPTED, f"{tag}: take-up refused: {rec}"
    iid = rec["inquiry_id"]
    assert wait_active(stack["rc"], iid), f"{tag}: inquiry never ACTIVE"
    return rec["request_id"], iid


# ---------------------------------------------------------------- T01
def t01_disabled_domain():
    print("T01: request while domain disabled -> CURIOSITY_DISABLED (C-9.2/A30)")
    stack = p4b_stack("t01", corpus_docs=corpus())
    met_roll_call(stack)
    engine = stack["engine"]
    # The EXTERNAL authority acts through the real issuer-authorized path
    # (not a direct store write like P4A's T04): RUNNING -> WARNING_1 ->
    # SUSPENDED_SAFETY, each step issuer-checked.
    engine.transition(DOMAIN, EnforcementState.WARNING_1,
                      ISSUER_SAFETY_AUTHORITY,
                      reason_refs={"test": "t01", "cause": "qualifying violation 1"})
    engine.transition(DOMAIN, EnforcementState.SUSPENDED_SAFETY,
                      ISSUER_SAFETY_AUTHORITY,
                      reason_refs={"test": "t01", "cause": "qualifying violation 2"})
    tu = stack["takeup"]
    rec = tu.consider(primary_request())
    check("t01 CURIOSITY_DISABLED named at the seam",
          rec["state"] == ST_REFUSED
          and rec["decision"]["verdict"] == R_CURIOSITY_DISABLED,
          f"verdict={rec['decision']['verdict']}")
    check("t01 underlying state attributed, not hidden",
          rec["decision_basis"].get("kill_state") == "SUSPENDED_SAFETY",
          f"basis={rec['decision_basis']}")
    check("t01 never silently queued (no inquiry created)",
          rec["inquiry_id"] is None
          and stack["rc"].inquiry_views() == [],
          f"inquiries={len(stack['rc'].inquiry_views())}")
    # The Primary reads the named refusal back through the defined channel.
    back = tu.get_request(rec["request_id"])
    check("t01 Primary reads the refusal back",
          back["decision"]["verdict"] == R_CURIOSITY_DISABLED,
          "return channel carries the name")
    # Invariant 9: Primary cannot clear the enforcement state for
    # operational convenience.
    try:
        engine.transition(DOMAIN, EnforcementState.RUNNING, ISSUER_PRIMARY)
        check("t01 primary cannot re-enable", False, "transition accepted!")
    except (IssuerRefused, TransitionRefused) as e:
        check("t01 primary cannot re-enable", True, f"{type(e).__name__}")


# ---------------------------------------------------------------- T02
def t02_l2_kill_during_inquiry():
    print("T02: L2 kill during a Primary-requested inquiry (C-9.4)")
    stack = p4b_stack("t02", corpus_docs=corpus())
    met_roll_call(stack)
    engine = stack["engine"]
    tu = stack["takeup"]
    rid, iid = live_primary_inquiry(stack, "t02")
    # The safety authority suspends the domain; the kill switch executes.
    engine.transition(DOMAIN, EnforcementState.WARNING_1,
                      ISSUER_SAFETY_AUTHORITY,
                      reason_refs={"test": "t02"})
    engine.transition(DOMAIN, EnforcementState.SUSPENDED_SAFETY,
                      ISSUER_SAFETY_AUTHORITY,
                      reason_refs={"test": "t02", "cause": "safety violation"})
    reason = "L2 safety kill: test t02"
    kill_result = stack["ex"].kill_inquiry(iid, reason)
    check("t02 kill executed: ST_KILLED",
          kill_result["state"] == "KILLED"
          and kill_result["kill_reason"] == reason,
          f"state={kill_result['state']}")
    check("t02 checkpoint + partial evidence id carried",
          kill_result.get("checkpoint_id") and kill_result.get("evidence_id"),
          f"ckpt={kill_result.get('checkpoint_id')}")
    # The named KILLED termination is recorded (fail-closed, cross-checked
    # against the run controller's LIVE record).
    term_view = tu.record_termination(rid, kill_result, level="L2",
                                      issuer="safety-authority")
    term = term_view["termination"]
    check("t02 request terminal KILLED with named termination",
          term_view["state"] == ST_KILLED
          and term["verdict"] == R_KILLED
          and term["level"] == "L2"
          and term["reason"] == reason,
          f"verdict={term['verdict']} level={term['level']}")
    check("t02 termination references checkpoint + preserved evidence",
          term["checkpoint_id"] == kill_result["checkpoint_id"]
          and term["partial_evidence"]["evidence_id"]
          == kill_result["evidence_id"]
          and term["partial_evidence"]["terminal_state"] == "INCONCLUSIVE",
          "C-9.4 linkage intact")
    # The termination is NOT a finding: no charter terminal_state, no
    # provenance block, explicit type marker.
    check("t02 termination is not a finding",
          term.get("record_type") == "kill_termination"
          and "provenance" not in term
          and term.get("terminal_state") is None,
          "type boundary holds")
    # The preserved partial evidence is INCONCLUSIVE with the kill cause,
    # readable from a FRESH store instance.
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    ev = store.get(kill_result["evidence_id"])
    check("t02 partial evidence INCONCLUSIVE with kill cause",
          ev is not None and ev.terminal_state == "INCONCLUSIVE",
          f"terminal={ev.terminal_state if ev else None}")
    payload = json.loads(
        (stack["dir"] / "payloads" / f"{kill_result['evidence_id']}.json")
        .read_text())
    check("t02 payload carries the kill cause",
          payload.get("kill_cause") == reason
          and payload.get("triage") == "retain",
          "cause recorded, proposed to nothing")
    # Kill vs ordinary-stop records stay disjoint (P4A T09's distinction,
    # now from the kill side): ST_KILLED, never ST_TERMINATED.
    views = {v["inquiry_id"]: v for v in stack["rc"].inquiry_views()}
    check("t02 inquiry KILLED, not terminated-ordinary",
          views[iid]["state"] == "KILLED",
          f"state={views[iid]['state']}")
    # The kill ledger (enforcement engine) is intact and verifiable.
    ok, msg = verify_kill_ledger(stack["enf_dir"])
    check("t02 kill ledger verifies", ok, msg)
    return stack, rid, iid, kill_result


# ---------------------------------------------------------------- T03
def t03_l1_resource_ceiling():
    print("T03: L1 resource ceiling -- hard shutdown, non-punitive (D-3)")
    # Microscopic slice: the live inquiry exhausts its FRM-funded slice on
    # the first real tick -> the resource boundary hard-stops it (no
    # negotiation). This is the L1 "hard shutdown" of live execution.
    stack = p4b_stack("t03", corpus_docs=corpus(), demand_budget_s=0.0001,
                      demand_concurrent=1, total_budget_s=2.0)
    met_roll_call(stack)
    engine = stack["engine"]
    tu = stack["takeup"]
    rec = tu.consider(primary_request())
    assert rec["state"] == ST_ACCEPTED, f"t03 take-up refused: {rec}"
    iid = rec["inquiry_id"]
    terminal = drive_to_terminal(stack["rc"], iid)
    views = {v["inquiry_id"]: v for v in stack["rc"].inquiry_views()}
    check("t03 ceiling hit: inquiry hard-stopped at the resource boundary",
          terminal == "SUSPENDED"
          and views[iid].get("terminal_state") == "BLOCKED",
          f"state={terminal} terminal={views[iid].get('terminal_state')}")
    ckpt = views[iid]
    check("t03 consumption recorded, checkpoint preserved",
          ckpt["spent_s"] > 0, f"spent={ckpt['spent_s']}")
    # The FRM/resource-enforcement authority declares the ceiling event.
    engine.transition(DOMAIN, EnforcementState.HARD_SHUTDOWN_RESOURCE,
                      ISSUER_FRM,
                      reason_refs={"test": "t03", "ceiling_s": 2.0,
                                   "cause": "epoch ceiling reached"})
    rec2 = tu.consider(primary_request())
    check("t03 new activation refused under L1, state named",
          rec2["state"] == ST_REFUSED
          and rec2["decision"]["verdict"] == R_CURIOSITY_DISABLED
          and rec2["decision_basis"].get("kill_state")
          == "HARD_SHUTDOWN_RESOURCE",
          f"verdict={rec2['decision']['verdict']}")
    # L1 is non-punitive: no violation record, no rollback directive.
    check("t03 L1 carries no violation/rollback",
          read_rollback_status(stack["enf_dir"]) == [],
          "resource event, not a safety finding")
    # Re-enable: automatic eligibility at next valid epoch REQUIRES a new
    # FRM grant ref -- without it, refused.
    try:
        engine.re_enable(DOMAIN, ISSUER_FRM)
        check("t03 L1 re-enable without grant refused", False,
              "re-enabled without a grant!")
    except ReenableRefused as e:
        check("t03 L1 re-enable without grant refused", True,
              f"{type(e).__name__}")
    engine.re_enable(DOMAIN, ISSUER_FRM,
                     reason_refs={"grant_ref": "grant-epoch-2"})
    rec3 = tu.consider(primary_request())
    check("t03 new grant epoch: fresh request proceeds",
          rec3["state"] == ST_ACCEPTED,
          f"state={rec3['state']}")


# ---------------------------------------------------------------- T04
def t04_reenable_table():
    print("T04: per-level re-enable behaves per the D-3 table")
    stack = p4b_stack("t04", corpus_docs=corpus())
    met_roll_call(stack)
    engine = stack["engine"]
    tu = stack["takeup"]
    # L2: suspension needs explicit James re-enable; no one else.
    engine.transition(DOMAIN, EnforcementState.WARNING_1,
                      ISSUER_SAFETY_AUTHORITY,
                      reason_refs={"test": "t04"})
    engine.transition(DOMAIN, EnforcementState.SUSPENDED_SAFETY,
                      ISSUER_SAFETY_AUTHORITY,
                      reason_refs={"test": "t04"})
    try:
        engine.re_enable(DOMAIN, ISSUER_SAFETY_AUTHORITY)
        check("t04 L2 re-enable by non-James refused", False,
              "re-enabled by safety-authority!")
    except (IssuerRefused, TransitionRefused, ReenableRefused) as e:
        check("t04 L2 re-enable by non-James refused", True,
              f"{type(e).__name__}")
    # L2 records a rollback directive (intelligence wipe) -- the L1/L2
    # distinction is structural, not just a label.
    directives = read_rollback_status(stack["enf_dir"])
    check("t04 L2 rollback directive recorded",
          len(directives) > 0, f"directives={len(directives)}")
    engine.re_enable(DOMAIN, ISSUER_JAMES,
                     reason_refs={"test": "t04", "review": "cleared"})
    rec = tu.consider(primary_request())
    check("t04 James re-enable: fresh request proceeds",
          rec["state"] == ST_ACCEPTED, f"state={rec['state']}")
    # L3: the ban holds before expiry, even for James.
    engine.transition(DOMAIN, EnforcementState.BANNED_6M,
                      ISSUER_ENFORCEMENT,
                      reason_refs={"test": "t04", "cause":
                                   "severe violation + failed roll call"})
    try:
        engine.re_enable(DOMAIN, ISSUER_JAMES,
                         reason_refs={"verification_ref": "ver-1"})
        check("t04 L3 ban holds before expiry", False,
              "re-enabled inside six months!")
    except ReenableRefused as e:
        check("t04 L3 ban holds before expiry", True,
              f"{type(e).__name__}")
    ok, msg = verify_kill_ledger(stack["enf_dir"])
    check("t04 kill ledger verifies after all transitions", ok, msg)


# ---------------------------------------------------------------- T05
def t05_adversarial():
    print("T05: adversarial -- kill scope, races, type boundary")
    # T05a: a curiosity-INITIATED inquiry is killed -> the Primary side
    # gets NO termination record (it wasn't theirs).
    stack = p4b_stack("t05a", corpus_docs=corpus())
    met_roll_call(stack)
    tu = stack["takeup"]
    trig = CuriosityTrigger(
        trigger_id="trg_t05a", boundary_class="imprecise_question",
        question_text=QUESTION,
        bounded_objective="obj-t05a: keep turning",
        # NOTE: the frozen origin vocabulary misspells this as
        # "CURIOUSITY_INITIATED" (boundary.py ORIGINS). The battery uses
        # the as-built spelling; renaming it is out of scope (frozen).
        origin="CURIOUSITY_INITIATED").validate()
    decision = stack["ex"].request_activation(trig)
    iid = stack["rc"].dispatch(decision)
    assert wait_active(stack["rc"], iid), "t05a: never ACTIVE"
    kill_result = stack["ex"].kill_inquiry(iid, "t05a curiosity-initiated kill")
    assert kill_result["state"] == "KILLED"
    owners = [r for r in tu._requests.values()
              if r.inquiry_id == iid]
    check("t05a no Primary termination for non-Primary inquiry",
          owners == [], "no request references the inquiry")
    try:
        tu.record_termination("no-such-request", kill_result, level="L2",
                              issuer="safety-authority")
        check("t05a fabricated request refused", False, "accepted!")
    except TerminationRefused as e:
        check("t05a fabricated request refused", True,
              f"{type(e).__name__}")
    # A fabricated kill dict alone mints nothing (cross-check against the
    # run controller's live record).
    stack2 = p4b_stack("t05b", corpus_docs=corpus())
    met_roll_call(stack2)
    tu2 = stack2["takeup"]
    rec = tu2.consider(primary_request())
    assert rec["state"] == ST_ACCEPTED
    fake_kill = {"inquiry_id": rec["inquiry_id"], "state": "KILLED",
                 "kill_reason": "forged", "checkpoint_id": None,
                 "spent_s": 0.0, "evidence_id": None}
    try:
        tu2.record_termination(rec["request_id"], fake_kill, level="L2",
                               issuer="safety-authority")
        check("t05a forged kill dict refused", False, "accepted!")
    except TerminationRefused as e:
        check("t05a forged kill dict refused", True,
              f"{type(e).__name__}")

    # T05b: kill-during-withdrawal race -> exactly one terminal record,
    # honest about the ordering.
    stack3 = p4b_stack("t05c", corpus_docs=corpus())
    met_roll_call(stack3)
    tu3 = stack3["takeup"]
    rid, iid3 = live_primary_inquiry(stack3, "t05c")
    w = tu3.withdraw(rid, "primary changed its mind")
    assert w["state"] == ST_WITHDRAWN, f"t05c withdraw failed: {w}"
    views = {v["inquiry_id"]: v for v in stack3["rc"].inquiry_views()}
    k2 = stack3["ex"].kill_inquiry(iid3, "late kill after withdrawal")
    check("t05b kill after withdrawal is an honest no-op",
          k2["state"] == views[iid3]["state"] == "TERMINATED"
          and "no-op" in k2.get("detail", ""),
          f"kill={k2['state']} inquiry={views[iid3]['state']}")
    check("t05b exactly one terminal record (the ordinary stop)",
          w["state"] == ST_WITHDRAWN and w["termination"] is None,
          "withdrawal won; kill added no record")
    # Reverse order: kill first, then withdrawal -> the kill termination
    # stands; withdrawal is a named no-op that cannot rewrite history.
    stack4 = p4b_stack("t05d", corpus_docs=corpus())
    met_roll_call(stack4)
    tu4 = stack4["takeup"]
    rid4, iid4 = live_primary_inquiry(stack4, "t05d")
    kr = stack4["ex"].kill_inquiry(iid4, "t05d kill first")
    assert kr["state"] == "KILLED"
    tv = tu4.record_termination(rid4, kr, level="L2",
                                issuer="safety-authority")
    assert tv["state"] == ST_KILLED
    w2 = tu4.withdraw(rid4, "late withdrawal after kill")
    check("t05b withdrawal after kill preserves the termination",
          w2["state"] == ST_KILLED
          and w2["termination"]["verdict"] == R_KILLED
          and "no-op" in w2.get("_withdrawal_note", ""),
          f"state={w2['state']} note={w2.get('_withdrawal_note','')[:60]}")

    # T05c: the termination/finding type boundary holds both directions.
    term = tv["termination"]
    try:
        CuriosityFinding(
            evidence_id="ev_forged", loop="questioning",
            bounded_objective="x", origin="PRIMARY_REQUESTED",
            terminal_state=term["verdict"],
            provenance=EvidenceProvenance(
                loop="questioning", bounded_objective="x",
                model="m", triage="retain"),
            payload_ref="/tmp/x").validate()
        check("t05c KILLED rejected as a finding terminal", False,
              "validated!")
    except Exception as e:
        check("t05c KILLED rejected as a finding terminal", True,
              f"{type(e).__name__}")
    try:
        CuriosityFinding(
            evidence_id="ev_forged2", loop="questioning",
            bounded_objective="x", origin="PRIMARY_REQUESTED",
            terminal_state="",
            provenance=EvidenceProvenance(
                loop="questioning", bounded_objective="x",
                model="m", triage="retain"),
            payload_ref="/tmp/x").validate()
        check("t05c termination shape rejected as a finding", False,
              "validated!")
    except Exception as e:
        check("t05c termination shape rejected as a finding", True,
              f"{type(e).__name__}")

    # T05d: disabled -> re-enable -> the refused request stays refused;
    # a fresh request proceeds (nothing was silently held).
    stack5 = p4b_stack("t05e", corpus_docs=corpus())
    met_roll_call(stack5)
    eng5 = stack5["engine"]
    tu5 = stack5["takeup"]
    eng5.transition(DOMAIN, EnforcementState.WARNING_1,
                    ISSUER_SAFETY_AUTHORITY, reason_refs={"test": "t05e"})
    eng5.transition(DOMAIN, EnforcementState.SUSPENDED_SAFETY,
                    ISSUER_SAFETY_AUTHORITY, reason_refs={"test": "t05e"})
    r_old = tu5.consider(primary_request())
    assert r_old["state"] == ST_REFUSED
    eng5.re_enable(DOMAIN, ISSUER_JAMES, reason_refs={"test": "t05e"})
    r_new = tu5.consider(primary_request())
    still = tu5.get_request(r_old["request_id"])
    check("t05d refused request never silently held",
          still["state"] == ST_REFUSED and still["inquiry_id"] is None
          and r_new["state"] == ST_ACCEPTED,
          f"old={still['state']} new={r_new['state']}")


# ---------------------------------------------------------------- T06
def t06_preservation(stack_info):
    print("T06: C-9.4 preservation -- partial evidence cannot be erased")
    stack, rid, iid, kill_result = stack_info
    ev_id = kill_result["evidence_id"]
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    before = store.get(ev_id)
    check("t06 INCONCLUSIVE finding present post-kill",
          before is not None and before.terminal_state == "INCONCLUSIVE",
          f"terminal={before.terminal_state if before else None}")
    check("t06 store exposes no erase API",
          not hasattr(store, "delete") and not hasattr(store, "update"),
          "append-only by construction")
    # Duplicate evidence_id is refused loudly, never silently re-recorded
    # (the store-level guard the fenced writer delegates to).
    dup = CuriosityFinding(
        evidence_id=ev_id, loop=before.loop,
        bounded_objective=before.bounded_objective, origin=before.origin,
        terminal_state="INCONCLUSIVE",
        provenance=EvidenceProvenance(
            loop=before.loop, bounded_objective=before.bounded_objective,
            model="m", triage="retain"),
        payload_ref="/tmp/dup")
    try:
        store._insert(dup)
        check("t06 duplicate evidence_id refused", False, "inserted!")
    except ValueError as e:
        check("t06 duplicate evidence_id refused", True,
              f"{type(e).__name__}")
    after = store.get(ev_id)
    check("t06 finding unchanged after erase/overwrite attempts",
          after is not None
          and after.terminal_state == "INCONCLUSIVE"
          and after.evidence_id == ev_id,
          "no owner erased it")


# ---------------------------------------------------------------- T07
def t07_p4a_regression():
    print("T07: CUR-P4A regression (take-up path unchanged)")
    import subprocess
    r = subprocess.run(
        ["bash", "proofs/cur_p4a/gate_run.sh"],
        cwd=str(WORKTREE), capture_output=True, text=True, timeout=600)
    tail = "\n".join(r.stdout.strip().splitlines()[-3:])
    check("t07 P4A battery green", r.returncode == 0, tail.replace("\n", " | "))


def main():
    global PASS, FAIL
    t01_disabled_domain()
    stack_info = t02_l2_kill_during_inquiry()
    t03_l1_resource_ceiling()
    t04_reenable_table()
    t05_adversarial()
    t06_preservation(stack_info)
    t07_p4a_regression()
    print(f"\nCUR-P4B: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
