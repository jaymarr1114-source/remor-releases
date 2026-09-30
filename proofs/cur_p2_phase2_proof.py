#!/usr/bin/env python3
"""CUR-P2 Phase 2 adversarial proof battery.

Proves, for real (no mocks, no staged success), the Phase 2 mission
claim: a genuine inquiry executes

  trigger -> Curiosity Executive -> Curiosity Run Controller
  -> Questioning loop -> reasoning substrate (cognition inlet)
  -> evidence -> verification -> terminal state
  -> fenced Evidence Store -> return

under a real FRM grant, with provenance, enforcement, budget,
roll-call, and relevance genuinely enforced.

Every test builds a REAL stack (real FRM evaluation layer, real GAM
roll-call, real enforcement engine, real GraphController, real
microcontroller substrate, real checkpoint/evidence/ledger/attribution
stores) inside its own run directory under proofs/cur_p2/runs/.
"Fresh process" tests construct brand-new objects over the same
database files. Results are recorded to results.json; the process exits
0 only if every check passes.

Run:  python3 proofs/cur_p2_phase2_proof.py
"""

import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

WORKTREE = Path("/home/hatch/workspace/worktrees/curp2-mission")
sys.path.insert(0, str(WORKTREE / "pylib"))

from swarm_engine.curiosity.frm.policy import FrmPolicy
from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
from swarm_engine.curiosity.rollcall.scheduler import (
    RollCallScheduler, RollCallPolicy)
from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
from swarm_engine.curiosity.rollcall.gam import (
    GovernanceAttestationMonitor)
from swarm_engine.curiosity.rollcall.responder import (
    HonestTestDouble, SilentTestDouble, WrongNonceTestDouble)
from swarm_engine.curiosity.substrate import (
    CuriositySubstrate, LOOP_QUESTIONING)
from swarm_engine.curiosity.run_controller.controller import (
    CuriosityRunController)
from swarm_engine.curiosity.executive.executive import (
    CuriosityExecutive, ActivationRefused)
from swarm_engine.curiosity.executive.boundary import new_trigger
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.core.executive.terminal_routing import TerminalLedger
from swarm_engine.core.executive.checkpoint import CheckpointError
from swarm_engine.curiosity.attribution.chain import ChainLedger
from swarm_engine.governance.curiosity_enforcement._engine import (
    EnforcementEngine)
from swarm_engine.governance.curiosity_enforcement.states import (
    EnforcementState)
from swarm_engine.core.microcontroller.substrate import (
    MicrocontrollerSubstrate, Refusal, CognitionResult)

RUNS = Path(__file__).resolve().parent / "cur_p2" / "runs"
RUNS.mkdir(parents=True, exist_ok=True)

CORPUS_FILES = [
    "runtime/curiosity/executive/executive.py",
    "runtime/curiosity/run_controller/controller.py",
    "runtime/curiosity/loops/questioning/loop.py",
    "runtime/curiosity/substrate.py",
    "runtime/curiosity/cognition.py",
    "runtime/core/microcontroller/substrate.py",
]
CORPUS = [str(WORKTREE / f) and Path(str(WORKTREE / f)).read_text()
          for f in CORPUS_FILES]

# The three canonical proof questions (terminals verified deterministic):
Q_RESOLVED = "How does the run controller enforce the budget slice?"
Q_INSUFFICIENT = "What detected signals reveal lunar botany?"
Q_BOUNDARY = "What color are REMOR dreams?"

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append({"name": name, "status": "PASS" if cond else "FAIL",
                    "detail": detail})
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""))
    return cond


class CountingFRM(FinancialResourceManager):
    """Real FRM evaluation layer that counts round evaluations, so the
    proof can show the FRM was (or was not) consulted."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rounds = 0

    def evaluate_round(self, *a, **k):
        self.rounds += 1
        return super().evaluate_round(*a, **k)


def new_stack(name, *, demand_budget_s=60.0, demand_concurrent=2,
              total_budget_s=600.0, primary_minimum_budget_s=60.0,
              frm=None, roll_call_double=None):
    """Build a full real stack under proofs/cur_p2/runs/<name>/."""
    rundir = RUNS / name
    rundir.mkdir(parents=True, exist_ok=True)
    for sub in ("payloads",):
        (rundir / sub).mkdir(parents=True, exist_ok=True)
    policy = FrmPolicy(
        total_budget_s=total_budget_s, total_max_concurrent=8,
        primary_minimum_budget_s=primary_minimum_budget_s,
        primary_minimum_concurrent=1, epoch_s=300.0)
    if frm is None:
        frm = FinancialResourceManager(policy)
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
        corpus_docs=CORPUS)
    ex = CuriosityExecutive(
        frm=frm, enforcement_state_dir=enf_dir, gam=gam,
        run_controller=rc, demand_budget_s=demand_budget_s,
        demand_concurrent=demand_concurrent)
    stack = {"dir": rundir, "frm": frm, "gam": gam, "enf_dir": enf_dir,
             "sub": sub, "rc": rc, "ex": ex, "sched": sched}
    if roll_call_double is not None:
        att = gam.conduct_roll_call("curiosity", roll_call_double)
        stack["attestation"] = att
    return stack


def met_roll_call(stack):
    att = stack["gam"].conduct_roll_call("curiosity", HonestTestDouble())
    assert att["classification"] == "MET", att
    return att


def make_trigger(question, origin="PRIMARY_REQUESTED",
                 boundary_class="imprecise_question",
                 objective="cur-p2 proof"):
    return new_trigger(boundary_class=boundary_class,
                       question_text=question,
                       bounded_objective=objective, origin=origin)


def drive_until_stopped(rc, inquiry_id, max_ticks=600):
    for _ in range(max_ticks):
        inq = rc._inquiries[inquiry_id]
        if inq.state in ("TERMINATED", "SUSPENDED", "KILLED"):
            return inq
        rc.tick()
    raise RuntimeError(f"inquiry {inquiry_id} did not stop")


# ---------------------------------------------------------------------------
# T01: successful inquiry under a real FRM grant, end to end
# ---------------------------------------------------------------------------

def t01_end_to_end():
    print("T01: end-to-end inquiry under a real FRM grant")
    s = new_stack("t01_end_to_end")
    met_roll_call(s)
    ex, rc = s["ex"], s["rc"]
    trig = make_trigger(Q_RESOLVED)
    dec = ex.request_activation(trig)
    check("t01 activation approved", dec.approved is True)
    check("t01 real FRM grant",
          dec.grant is not None and dec.grant.budget_s == 60.0
          and dec.grant.max_concurrent == 2,
          f"budget_s={dec.grant.budget_s} max_concurrent="
          f"{dec.grant.max_concurrent} epoch={dec.epoch_id}")
    check("t01 grant enforcement RUNNING",
          dec.enforcement_state == "RUNNING")
    check("t01 roll-call MET on the decision", dec.roll_call == "MET")
    out = ex.activate(dec)
    check("t01 loop entered", out.entered is True, out.detail)
    res = out.result
    check("t01 terminal QUESTION_RESOLVED",
          res["terminal_state"] == "QUESTION_RESOLVED",
          f"score={res.get('score')} spent={res.get('spent_s')}s")
    check("t01 precise question composed",
          bool(res.get("precise_question")),
          res.get("precise_question", "")[:80])
    check("t01 inquiry id shared end to end",
          res["inquiry_id"].startswith("inq_"))
    iid, eid = res["inquiry_id"], res["evidence_id"]

    # Fenced evidence store: the finding is really there.
    store = CuriosityEvidenceStore(str(s["dir"] / "ev.db"))
    finding = store.get(eid)
    check("t01 finding persisted in the fenced store",
          finding is not None and finding.evidence_id == eid)
    check("t01 finding fields",
          finding.loop == "questioning"
          and finding.terminal_state == "QUESTION_RESOLVED"
          and finding.origin == "PRIMARY_REQUESTED",
          f"loop={finding.loop} terminal={finding.terminal_state}")
    prov = finding.provenance
    check("t01 provenance complete",
          prov.loop == "questioning" and prov.model
          and prov.bounded_objective == "cur-p2 proof" and prov.triage,
          f"model={prov.model} triage={prov.triage}")
    payload_path = Path(finding.payload_ref)
    check("t01 payload file exists", payload_path.exists(),
          str(payload_path))
    payload = json.loads(payload_path.read_text())
    check("t01 payload carries the precise question + passes",
          payload.get("precise_question") == res["precise_question"]
          and len(payload.get("passes", [])) >= 1
          and payload["inquiry_id"] == iid)
    cognize_marks = [
        v for p in payload["passes"]
        for v in p.get("node_summaries", {}).values()
        if "[cognize:" in v]
    check("t01 reasoning travelled the cognition inlet",
          len(cognize_marks) >= 3,
          f"{len(cognize_marks)} node summaries carry [cognize:<mc>]")

    # Terminal ledger routing (reused TerminalLedger, not a duplicate).
    ledger = TerminalLedger(str(s["dir"] / "term.db"))
    routes = ledger.routes(loop="questioning",
                           terminal_state="QUESTION_RESOLVED")
    check("t01 terminal routed through the shared ledger",
          len(routes) == 1
          and routes[0]["evidence_refs"].get("evidence_id") == eid,
          f"routes={len(routes)} consumer={routes[0]['consumer']}"
          if routes else "no routes")

    # Attribution chain: work -> result -> admission.
    chain = ChainLedger(str(s["dir"] / "attr.db"))
    wres = chain.result_for_work("work_" + iid)
    check("t01 attribution result recorded",
          wres is not None and wres.outcome == "success"
          and wres.evidence_ref == eid,
          f"outcome={wres.outcome if wres else None}")
    adm = chain.admission_for_result("res_" + iid)
    check("t01 attribution admission retained (not accepted)",
          adm is not None and adm.verdict == "retained",
          f"verdict={adm.verdict if adm else None}")

    # The executive's own outcome view is aggregate-only.
    view_blob = json.dumps(out.view)
    check("t01 outcome view carries no microcontroller ids",
          "mc-" not in view_blob, f"view keys={list(out.view)}")


# ---------------------------------------------------------------------------
# T02: provenance-stamped finding survives fresh-process reopen
# ---------------------------------------------------------------------------

def t02_fresh_process_provenance():
    print("T02: finding survives fresh-process reopen")
    s = new_stack("t02_fresh_provenance")
    met_roll_call(s)
    dec = s["ex"].request_activation(make_trigger(Q_RESOLVED))
    res = s["ex"].activate(dec).result
    eid = res["evidence_id"]

    # Fresh process: brand-new objects, same database files.
    s2 = new_stack("t02_fresh_provenance")
    store2 = CuriosityEvidenceStore(str(s2["dir"] / "ev.db"))
    finding = store2.get(eid)
    check("t02 finding reopens in a fresh process", finding is not None)
    check("t02 reopened fields intact",
          finding.terminal_state == "QUESTION_RESOLVED"
          and finding.loop == "questioning"
          and finding.provenance.model
          and finding.provenance.triage == "propose_investigation",
          f"triage={finding.provenance.triage}")
    payload = json.loads(Path(finding.payload_ref).read_text())
    check("t02 reopened payload intact",
          payload["precise_question"] == res["precise_question"]
          and payload["terminal_state"] == "QUESTION_RESOLVED")
    ledger2 = TerminalLedger(str(s2["dir"] / "term.db"))
    routes = ledger2.routes(terminal_state="QUESTION_RESOLVED")
    check("t02 ledger route reopens fresh",
          any(r["evidence_refs"].get("evidence_id") == eid
              for r in routes))


# ---------------------------------------------------------------------------
# T03: no-budget inquiry never starts
# ---------------------------------------------------------------------------

def t03_no_budget():
    print("T03: no-budget inquiry never starts")
    s = new_stack("t03_no_budget", demand_budget_s=0.0)
    met_roll_call(s)
    try:
        s["ex"].request_activation(make_trigger(Q_RESOLVED))
        check("t03 activation refused", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t03 activation refused with NO_BUDGET",
              str(exc).startswith("NO_BUDGET"), str(exc)[:100])
    check("t03 nothing dispatched", s["rc"].inquiry_views() == [])
    check("t03 loop never registered on the substrate",
          s["rc"].loop_view("questioning").get("registered") is False)


# ---------------------------------------------------------------------------
# T04: enforcement kill states prevent activation; the FRM is untouched
# ---------------------------------------------------------------------------

def t04_enforcement():
    print("T04: enforcement kill states vs WARNING_1 passthrough")
    cases = [
        ("t04_hard_shutdown", EnforcementState.HARD_SHUTDOWN_RESOURCE,
         "frm", None),
        ("t04_suspended", EnforcementState.SUSPENDED_SAFETY,
         "safety-authority", EnforcementState.WARNING_1),
        ("t04_banned", EnforcementState.BANNED_6M,
         "enforcement-mechanism", None),
    ]
    for name, target, issuer, via in cases:
        frm = CountingFRM(FrmPolicy(
            total_budget_s=600.0, total_max_concurrent=8,
            primary_minimum_budget_s=60.0,
            primary_minimum_concurrent=1, epoch_s=300.0))
        s = new_stack(name, frm=frm)
        met_roll_call(s)
        eng = EnforcementEngine(s["enf_dir"])
        if via is not None:
            eng.transition("curiosity", via, issuer)
        eng.transition("curiosity", target, issuer)
        try:
            s["ex"].request_activation(make_trigger(Q_RESOLVED))
            check(f"{name} refused", False, "no refusal raised")
        except ActivationRefused as exc:
            check(f"{name} refused with KILL_STATE",
                  str(exc).startswith("KILL_STATE"), str(exc)[:90])
        check(f"{name} FRM never consulted", frm.rounds == 0,
              f"evaluate_round calls={frm.rounds}")
        check(f"{name} nothing dispatched",
              s["rc"].inquiry_views() == [])

    # WARNING_1 passes through: the FRM restricts the grant (D-3).
    # demand_concurrent=4 -> int(4*0.25)=1 slot: the inquiry runs under
    # the cap.
    frm = CountingFRM(FrmPolicy(
        total_budget_s=600.0, total_max_concurrent=8,
        primary_minimum_budget_s=60.0,
        primary_minimum_concurrent=1, epoch_s=300.0))
    s = new_stack("t04_warning1", frm=frm, demand_concurrent=4)
    met_roll_call(s)
    eng = EnforcementEngine(s["enf_dir"])
    eng.transition("curiosity", EnforcementState.WARNING_1,
                   "safety-authority")
    dec = s["ex"].request_activation(make_trigger(Q_RESOLVED))
    check("t04_warning1 approved under WARNING_1", dec.approved is True)
    check("t04_warning1 grant restricted to 25% of demand",
          0 < dec.grant.budget_s <= 15.0 + 1e-9
          and dec.grant.max_concurrent == 1,
          f"budget_s={dec.grant.budget_s} slots={dec.grant.max_concurrent}")
    check("t04_warning1 FRM consulted exactly once", frm.rounds == 1)
    res = s["ex"].activate(dec).result
    check("t04_warning1 inquiry still converges under the cap",
          res["terminal_state"] == "QUESTION_RESOLVED",
          f"slice={dec.grant.budget_s:.2f}s spent={res['spent_s']}s")

    # WARNING_1 with demand_concurrent=2 -> the frozen FRM truncates
    # int(2*0.25)=0 slots. The executive refuses loudly (NO_SLOT) rather
    # than deadlocking in admission or inflating the restriction.
    frm2 = CountingFRM(FrmPolicy(
        total_budget_s=600.0, total_max_concurrent=8,
        primary_minimum_budget_s=60.0,
        primary_minimum_concurrent=1, epoch_s=300.0))
    s2 = new_stack("t04_warning1_noslot", frm=frm2, demand_concurrent=2)
    met_roll_call(s2)
    eng2 = EnforcementEngine(s2["enf_dir"])
    eng2.transition("curiosity", EnforcementState.WARNING_1,
                    "safety-authority")
    try:
        s2["ex"].request_activation(make_trigger(Q_RESOLVED))
        check("t04_warning1_noslot refused", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t04_warning1_noslot refused with NO_SLOT",
              str(exc).startswith("NO_SLOT"), str(exc)[:110])
    check("t04_warning1_noslot nothing dispatched",
          s2["rc"].inquiry_views() == [])


# ---------------------------------------------------------------------------
# T05: missing or non-MET roll-call prevents activation
# ---------------------------------------------------------------------------

def t05_roll_call():
    print("T05: missing or non-MET roll-call prevents activation")
    # Missing attestation.
    s = new_stack("t05_missing")
    try:
        s["ex"].request_activation(make_trigger(Q_RESOLVED))
        check("t05_missing refused", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t05_missing refused (ATTESTATION_INVALID)",
              str(exc).startswith("ATTESTATION_INVALID"), str(exc)[:100])

    # MISSED attestation.
    s = new_stack("t05_missed")
    att = s["gam"].conduct_roll_call("curiosity", SilentTestDouble())
    check("t05_missed attestation classified MISSED",
          att["classification"] == "MISSED", att["classification"])
    try:
        s["ex"].request_activation(make_trigger(Q_RESOLVED))
        check("t05_missed refused", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t05_missed refused (ATTESTATION_INVALID)",
              str(exc).startswith("ATTESTATION_INVALID"), str(exc)[:100])

    # INVALID attestation (wrong nonce).
    s = new_stack("t05_invalid")
    att = s["gam"].conduct_roll_call("curiosity", WrongNonceTestDouble())
    check("t05_invalid attestation classified INVALID",
          att["classification"] == "INVALID", att["classification"])
    try:
        s["ex"].request_activation(make_trigger(Q_RESOLVED))
        check("t05_invalid refused", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t05_invalid refused (ATTESTATION_INVALID)",
              str(exc).startswith("ATTESTATION_INVALID"), str(exc)[:100])

    # Adversarial contrast: MET admits.
    s = new_stack("t05_met")
    met_roll_call(s)
    dec = s["ex"].request_activation(make_trigger(Q_RESOLVED))
    check("t05_met admitted under MET", dec.approved is True
          and dec.roll_call == "MET")


# ---------------------------------------------------------------------------
# T06: poor-fit request refused; relevance never treated as acceptance
# ---------------------------------------------------------------------------

def t06_fit_and_relevance():
    print("T06: fit refusal; relevance advisory only (D-4)")
    s = new_stack("t06_fit")
    met_roll_call(s)
    # Not question-shaped: no interrogative marker.
    try:
        s["ex"].request_activation(
            make_trigger("Run the budget report now"))
        check("t06 non-question refused", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t06 non-question refused with FIT",
              str(exc).startswith("FIT"), str(exc)[:100])
    # Unbounded objective.
    try:
        s["ex"].request_activation(new_trigger(
            boundary_class="imprecise_question",
            question_text=Q_RESOLVED, bounded_objective="x" * 281,
            origin="PRIMARY_REQUESTED"))
        check("t06 unbounded refused", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t06 unbounded objective refused with FIT",
              str(exc).startswith("FIT"), str(exc)[:100])

    # Relevance is advisory: the persisted payload records the score and
    # the D-4 note; triage is never "accepted".
    dec = s["ex"].request_activation(make_trigger(Q_RESOLVED))
    res = s["ex"].activate(dec).result
    payload = json.loads(
        Path(s["dir"] / "payloads" / (res["evidence_id"] + ".json"))
        .read_text())
    rel = payload.get("relevance", {})
    check("t06 relevance recorded as advisory score",
          rel.get("score") == 1.0 and "0.25" in rel.get("d4_note", ""),
          f"score={rel.get('score')}")
    check("t06 triage never claims acceptance",
          payload.get("triage") == "propose_investigation"
          and "Primary Acceptance" in payload.get("triage_note", ""),
          f"triage={payload.get('triage')}")


# ---------------------------------------------------------------------------
# T07: future-loop boundary classes are named absent, never routed
# ---------------------------------------------------------------------------

def t07_absent_loop():
    print("T07: future-loop boundary named absent")
    s = new_stack("t07_absent")
    met_roll_call(s)
    try:
        s["ex"].request_activation(make_trigger(
            "What hypothesis explains the anomaly?",
            boundary_class="hypothesis_candidate"))
        check("t07 refused", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t07 refused with LOOP_ABSENT",
              str(exc).startswith("LOOP_ABSENT"), str(exc)[:120])
        check("t07 absence named (scientific_inquiry)",
              "scientific_inquiry" in str(exc))
    check("t07 nothing dispatched", s["rc"].inquiry_views() == [])


# ---------------------------------------------------------------------------
# T08: forced budget breach -> exact consumption, checkpoint, suspend,
#      RESOURCE_BOUNDARY returned, BLOCKED finding externalized
# ---------------------------------------------------------------------------

def t08_resource_boundary():
    print("T08: forced budget breach")
    # The FRM funds a microscopic grant: the first real tick's elapsed
    # time exceeds the inquiry's slice by orders of magnitude, so the
    # breach path is taken deterministically (not by racing the clock).
    s = new_stack("t08_boundary", total_budget_s=2e-5,
                  primary_minimum_budget_s=0.0)
    met_roll_call(s)
    dec = s["ex"].request_activation(make_trigger(Q_BOUNDARY))
    check("t08 microscopic grant issued",
          0 < dec.grant.budget_s <= 2e-5 + 1e-12,
          f"budget_s={dec.grant.budget_s} slice="
          f"{dec.grant.budget_s / dec.grant.max_concurrent}")
    res = s["rc"].run_inquiry(dec)
    check("t08 terminal BLOCKED", res["terminal_state"] == "BLOCKED")
    rb = res["resource_boundary"]
    check("t08 exact consumption recorded",
          rb["spent_s"] > 0 and rb["spent_s"] == res["spent_s"]
          and rb["spent_s"] >= rb["budget_s"],
          f"spent={rb['spent_s']} budget={rb['budget_s']} cause={rb['cause']}")
    iid = res["inquiry_id"]
    inq = s["rc"]._inquiries[iid]
    check("t08 inquiry SUSPENDED", inq.state == "SUSPENDED")
    check("t08 checkpoint written",
          bool(res.get("checkpoint_id")), res.get("checkpoint_id"))
    index = json.loads((s["dir"] / "ckpt.db.index.json").read_text())
    check("t08 checkpoint index locates the inquiry",
          index.get(iid, {}).get("checkpoint_id") == res["checkpoint_id"])

    # Fresh process: the BLOCKED finding is really in the fenced store.
    s2 = new_stack("t08_boundary")
    store2 = CuriosityEvidenceStore(str(s2["dir"] / "ev.db"))
    finding = store2.get(res["evidence_id"])
    check("t08 BLOCKED finding reopens fresh",
          finding is not None and finding.terminal_state == "BLOCKED"
          and finding.provenance.triage == "boundary")
    payload = json.loads(Path(finding.payload_ref).read_text())
    check("t08 BLOCKED payload carries boundary + partial work",
          payload["resource_boundary"]["spent_s"] == rb["spent_s"]
          and payload["checkpoint_id"] == res["checkpoint_id"]
          and isinstance(payload.get("partial_refinement"), list))
    ledger2 = TerminalLedger(str(s2["dir"] / "term.db"))
    routes = ledger2.routes(terminal_state="BLOCKED")
    check("t08 BLOCKED routed through the shared ledger",
          any(r["evidence_refs"].get("evidence_id") == res["evidence_id"]
              for r in routes))
    chain2 = ChainLedger(str(s2["dir"] / "attr.db"))
    wres = chain2.result_for_work("work_" + iid)
    check("t08 attribution records the failed attempt",
          wres is not None and wres.outcome == "failed"
          and "BLOCKED" in wres.detail, wres.detail if wres else None)


# ---------------------------------------------------------------------------
# T09: kill propagates executive -> run controller -> loop ->
#      microcontroller; lineage persists; fresh process resumes the same
#      inquiry and it converges
# ---------------------------------------------------------------------------

def t09_kill_resume():
    print("T09: kill then fresh-process resume")
    s = new_stack("t09_kill_resume")
    met_roll_call(s)
    ex, rc = s["ex"], s["rc"]
    dec = ex.request_activation(make_trigger(Q_RESOLVED))
    iid = rc.dispatch(dec)
    for _ in range(3):
        rc.tick()
    inq = rc._inquiries[iid]
    check("t09 inquiry ACTIVE before kill", inq.state == "ACTIVE")
    spent_at_kill = inq.spent_s
    kill_res = ex.kill_inquiry(iid, "proof kill")
    check("t09 kill executed via the executive",
          kill_res["state"] == "KILLED", kill_res.get("kill_reason"))
    check("t09 kill checkpointed", bool(kill_res.get("checkpoint_id")))
    check("t09 kill reason preserved",
          rc._inquiries[iid].kill_reason == "proof kill")
    # Killed inquiries do not advance: spend is frozen.
    for _ in range(3):
        rc.tick()
    check("t09 killed inquiry does not advance",
          rc._inquiries[iid].spent_s == spent_at_kill
          and rc._inquiries[iid].state == "KILLED")
    index = json.loads((s["dir"] / "ckpt.db.index.json").read_text())
    check("t09 kill checkpoint indexed",
          iid in index and index[iid]["label"] == "SUSPENDED_KILL")

    # Fresh process: new objects, same database files.
    s2 = new_stack("t09_kill_resume")
    rc2 = s2["rc"]
    resumed = rc2.resume_inquiry(iid)
    check("t09 resumed in a fresh process",
          resumed["state"] == "ACTIVE"
          and resumed["resumed_from"] not in ("pause",),
          f"resumed_from={resumed['resumed_from']}")
    new_inq = rc2._inquiries[iid]
    events = [e["event"] for e in new_inq.lineage]
    check("t09 lineage persists across the process boundary",
          "killed" in events and "resumed_from_checkpoint" in events,
          f"lineage_events={len(events)}")
    check("t09 same inquiry id after resume",
          new_inq.inquiry_id == iid)
    check("t09 spent_s preserved across resume",
          new_inq.spent_s == spent_at_kill)
    done = drive_until_stopped(rc2, iid)
    check("t09 resumed inquiry converges",
          done.state == "TERMINATED"
          and done.result["terminal_state"] == "QUESTION_RESOLVED",
          f"terminal={done.result['terminal_state']}")

    # Tamper-evidence: corrupt the checkpoint row in a fresh stack; the
    # resume must refuse BEFORE marking anything verified.
    s3 = new_stack("t09_tamper")
    met_roll_call(s3)
    ex3, rc3 = s3["ex"], s3["rc"]
    dec3 = ex3.request_activation(make_trigger(Q_RESOLVED))
    iid3 = rc3.dispatch(dec3)
    rc3.tick()
    rc3.kill_inquiry(iid3, "tamper proof")
    con = sqlite3.connect(str(s3["dir"] / "ckpt.db"))
    con.execute("UPDATE transition_checkpoints "
                "SET from_state_json='{\"tampered\": true}'")
    con.commit()
    con.close()
    s4 = new_stack("t09_tamper")  # fresh process, same files
    try:
        s4["rc"].resume_inquiry(iid3)
        check("t09 tampered checkpoint refused", False,
              "resume succeeded on a corrupted row")
    except CheckpointError as exc:
        check("t09 tampered checkpoint refused",
              "integrity hash mismatch" in str(exc), str(exc)[:90])
    ckpt_id3 = json.loads(
        (s3["dir"] / "ckpt.db.index.json").read_text())[iid3]["checkpoint_id"]
    check("t09 tampered checkpoint never marked verified",
          s4["rc"]._checkpoints.load_by_checkpoint_id(
              ckpt_id3)["status"] == "saved")

    # Attribution: two attempts, one lineage.
    chain2 = ChainLedger(str(s2["dir"] / "attr.db"))
    w1 = chain2.get_work("work_" + iid)
    r1 = chain2.result_for_work("work_" + iid)
    w2 = chain2.get_work("work_" + iid + "#a2")
    r2 = chain2.result_for_work("work_" + iid + "#a2")
    check("t09 first attempt recorded as failed",
          w1 is not None and r1 is not None and r1.outcome == "failed"
          and "killed" in r1.detail, r1.detail if r1 else None)
    check("t09 second attempt linked to the first",
          w2 is not None and w2.parent_work_ref == "work_" + iid
          and w2.attempt_no == 2,
          f"parent={w2.parent_work_ref if w2 else None}")
    check("t09 second attempt succeeded with evidence",
          r2 is not None and r2.outcome == "success"
          and r2.evidence_ref == done.result["evidence_id"])
    # The resumed finding is a fresh, real convergence (not replayed).
    store2 = CuriosityEvidenceStore(str(s2["dir"] / "ev.db"))
    finding = store2.get(done.result["evidence_id"])
    payload = json.loads(Path(finding.payload_ref).read_text())
    check("t09 resumed run re-executed the loop",
          len(payload.get("passes", [])) >= 1
          and payload["inquiry_id"] == iid)


# ---------------------------------------------------------------------------
# T10: pause freezes spend; resume continues in process
# ---------------------------------------------------------------------------

def t10_pause_resume():
    print("T10: pause / resume in process")
    s = new_stack("t10_pause")
    met_roll_call(s)
    rc = s["rc"]
    dec = s["ex"].request_activation(make_trigger(Q_RESOLVED))
    iid = rc.dispatch(dec)
    rc.tick()
    rc.tick()
    spent = rc._inquiries[iid].spent_s
    check("t10 spend accumulated while active", spent > 0, f"{spent}")
    paused = rc.pause_inquiry(iid)
    check("t10 paused", paused["state"] == "PAUSED")
    for _ in range(3):
        rc.tick()
    check("t10 spend frozen while paused",
          rc._inquiries[iid].spent_s == spent)
    resumed = rc.resume_inquiry(iid)
    check("t10 resumed from pause",
          resumed["state"] == "ACTIVE"
          and resumed["resumed_from"] == "pause")
    done = drive_until_stopped(rc, iid)
    check("t10 converges after resume",
          done.result["terminal_state"] == "QUESTION_RESOLVED")


# ---------------------------------------------------------------------------
# T11: priority admission -- PRIMARY_REQUESTED outranks CURIOSITY_INITIATED
# ---------------------------------------------------------------------------

def t11_priority():
    print("T11: priority admission under one concurrency slot")
    s = new_stack("t11_priority", demand_concurrent=1)
    met_roll_call(s)
    ex, rc = s["ex"], s["rc"]
    d_lo = ex.request_activation(make_trigger(
        Q_BOUNDARY, origin="CURIOUSITY_INITIATED"))
    d_hi = ex.request_activation(make_trigger(Q_RESOLVED))
    check("t11 priorities declared",
          d_hi.priority == 0 and d_lo.priority == 1,
          f"hi={d_hi.priority} lo={d_lo.priority}")
    check("t11 mid-epoch grant shared by identity",
          d_hi.grant is d_lo.grant,
          "same grant object -> per-grant slot accounting")
    # Occupy the single slot, then dispatch the low-priority inquiry
    # BEFORE the high-priority one: when the slot frees, admission must
    # still prefer the high-priority inquiry despite dispatch order.
    first_id = rc.dispatch(
        ex.request_activation(make_trigger(Q_RESOLVED)))
    check("t11 slot occupied by the first inquiry",
          rc._inquiries[first_id].state == "ACTIVE")
    lo_id = rc.dispatch(d_lo)
    hi_id = rc.dispatch(d_hi)
    check("t11 contenders both pending while the slot is held",
          rc._inquiries[lo_id].state == "PENDING"
          and rc._inquiries[hi_id].state == "PENDING")
    rc.kill_inquiry(first_id, "proof frees the slot")
    rc.tick()
    check("t11 high-priority admitted first despite dispatch order",
          rc._inquiries[hi_id].state == "ACTIVE"
          and rc._inquiries[lo_id].state == "PENDING")
    # Drive to the end, observing the exact interleaving: step the
    # active inquiries first, snapshot, then admit. The high-priority
    # inquiry must reach TERMINATED while the low-priority one is still
    # PENDING (admission only happens in the admit phase that follows).
    saw_hi_done_while_lo_pending = False
    for _ in range(600):
        for iid in (hi_id, lo_id):
            inq = rc._inquiries[iid]
            if inq.state == "ACTIVE":
                rc._step_inquiry(inq)
        states = {i: rc._inquiries[i].state for i in (hi_id, lo_id)}
        if (states[hi_id] == "TERMINATED"
                and states[lo_id] == "PENDING"):
            saw_hi_done_while_lo_pending = True
        if all(v == "TERMINATED" for v in states.values()):
            break
        rc._admit_pending()
    check("t11 hi terminated while lo still pending",
          saw_hi_done_while_lo_pending)
    check("t11 both inquiries converged",
          rc._inquiries[hi_id].result["terminal_state"]
          == "QUESTION_RESOLVED"
          and rc._inquiries[lo_id].result["terminal_state"]
          == "BOUNDARY_ESTABLISHED")


# ---------------------------------------------------------------------------
# T12: substrate isolation + executive aggregate-only state
# ---------------------------------------------------------------------------

def t12_isolation():
    print("T12: substrate isolation and aggregate-only executive state")
    s = new_stack("t12_isolation")
    cur_sub = s["sub"]
    # The real flow registers the loop at activation; the test mirrors
    # that before spawning directly against the substrate.
    cur_sub.register_loop("questioning", budget_s=100.0)
    # A Primary-side substrate instance with a Primary loop.
    primary_sub = MicrocontrollerSubstrate()
    primary_sub.register_loop("run", budget_s=100.0)

    cur_spawn = cur_sub.spawn("questioning", purpose="iso",
                              budget_s=5.0)
    check("t12 curiosity spawn ok", cur_spawn.ok)
    cur_id = cur_spawn.mc.mc_id
    refusal = primary_sub.retire(cur_id)
    check("t12 primary cannot retire curiosity MC",
          isinstance(refusal, Refusal)
          and refusal.reason == "unknown_microcontroller",
          getattr(refusal, "reason", None))

    pri_spawn = primary_sub.spawn("run", purpose="iso-p", budget_s=5.0)
    check("t12 primary spawn ok", pri_spawn.ok)
    refusal2 = cur_sub.retire(pri_spawn.mc.mc_id)
    check("t12 curiosity cannot retire primary MC",
          isinstance(refusal2, Refusal)
          and refusal2.reason == "unknown_microcontroller",
          getattr(refusal2, "reason", None))

    # Each side refuses the other's loop vocabulary at spawn: the loop
    # was never registered there (spawn returns a refusal, not an
    # exception).
    r_cur = cur_sub.spawn("run", purpose="x", budget_s=1.0)
    check("t12 curiosity refuses Primary loop names",
          not r_cur.ok and r_cur.refusal.reason == "loop_unregistered",
          r_cur.refusal.reason)
    r_pri = primary_sub.spawn("questioning", purpose="x", budget_s=1.0)
    check("t12 primary refuses curiosity loop names",
          not r_pri.ok and r_pri.refusal.reason == "loop_unregistered",
          r_pri.refusal.reason)

    # No executive object-graph path to microcontroller machinery:
    # depth-1 scan of the executive's own attributes.
    ex = s["ex"]
    bad = []
    for key, val in ex.__dict__.items():
        tname = type(val).__name__
        if ("Substrate" in tname or "Microcontroller" in tname
                or "GraphController" in tname):
            bad.append((key, tname))
    check("t12 executive holds no machinery handle", not bad,
          f"{bad if bad else 'held_references=' + str(ex.held_references)}")
    check("t12 held_references names no substrate",
          not any("substrat" in k.lower() or "substrat" in v.lower()
                  for k, v in ex.held_references.items()),
          str(ex.held_references))

    # Executive state is aggregates only.
    met_roll_call(s)
    dec = ex.request_activation(make_trigger(Q_RESOLVED))
    ex.activate(dec)
    view = ex.executive_view()
    blob = json.dumps(view)
    check("t12 executive view has no microcontroller ids",
          "mc-" not in blob)
    check("t12 executive view has no purposes",
          '"purpose"' not in blob)
    check("t12 executive view carries loop aggregates",
          view["loops"]["questioning"].get("loop") == "questioning"
          and len(view["inquiries"]) == 1)


# ---------------------------------------------------------------------------
# T13: the three terminal classes, each for real
# ---------------------------------------------------------------------------

def t13_terminals():
    print("T13: three terminal classes")
    cases = [
        ("t13_resolved", Q_RESOLVED, "QUESTION_RESOLVED"),
        ("t13_insufficient", Q_INSUFFICIENT, "INSUFFICIENT_EVIDENCE"),
        ("t13_boundary", Q_BOUNDARY, "BOUNDARY_ESTABLISHED"),
    ]
    for name, question, terminal in cases:
        s = new_stack(name)
        met_roll_call(s)
        dec = s["ex"].request_activation(make_trigger(question))
        res = s["ex"].activate(dec).result
        check(f"{name} terminal {terminal}",
              res["terminal_state"] == terminal,
              f"score={res.get('score')} spent={res.get('spent_s')}s")
        payload = json.loads(
            Path(s["dir"] / "payloads" / (res["evidence_id"] + ".json"))
            .read_text())
        check(f"{name} payload precise question present",
              bool(payload.get("precise_question")))


# ---------------------------------------------------------------------------
# T16: cognition fail-closed contrast -- a dead provider stops the
# inquiry loudly; nothing is fabricated past the inlet.
# ---------------------------------------------------------------------------

class _DeadProvider:
    def request_cognition(self, *, mc_id, prompt, context):
        return CognitionResult(
            ok=False, error="simulated provider outage")


def t16_cognition_fail_closed():
    print("T16: cognition fail-closed contrast")
    s = new_stack("t16_cognition_dead")
    met_roll_call(s)
    s["sub"].set_cognition_provider(_DeadProvider())
    dec = s["ex"].request_activation(make_trigger(Q_RESOLVED))
    res = s["ex"].activate(dec).result
    check("t16 dead provider suspends instead of resolving",
          res["terminal_state"] == "BLOCKED",
          f"terminal={res['terminal_state']}")
    check("t16 cause names the cognition failure",
          "cognition_failed" in res["resource_boundary"]["cause"],
          res["resource_boundary"]["cause"][:100])
    payload = json.loads(
        Path(s["dir"] / "payloads" / (res["evidence_id"] + ".json"))
        .read_text())
    partial = payload.get("partial_refinement", [])
    summaries = [sm for p in partial
                 for sm in p.get("node_summaries", {}).values()]
    check("t16 no reasoning fabricated past the dead inlet",
          not any("[cognize:" in sm for sm in summaries),
          f"partial_passes={len(partial)}")
    check("t16 failure externalized as a BLOCKED finding",
          payload.get("terminal_state") == "BLOCKED"
          and "cognition_failed" in payload["resource_boundary"]["cause"])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    # Hermetic runs: the battery owns proofs/cur_p2/runs/. Wipe it at
    # startup so re-runs never collide with stale SQLite rows (ledger
    # routes, attribution keys, checkpoint seqs). Fresh-process tests
    # create their second stack mid-test under the same name, which is
    # unaffected by this start-of-run wipe.
    if RUNS.exists():
        shutil.rmtree(RUNS)
    RUNS.mkdir(parents=True, exist_ok=True)
    start = time.time()
    t01_end_to_end()
    t02_fresh_process_provenance()
    t03_no_budget()
    t04_enforcement()
    t05_roll_call()
    t06_fit_and_relevance()
    t07_absent_loop()
    t08_resource_boundary()
    t09_kill_resume()
    t10_pause_resume()
    t11_priority()
    t12_isolation()
    t13_terminals()
    t16_cognition_fail_closed()
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = [r for r in RESULTS if r["status"] != "PASS"]
    summary = {
        "passed": passed,
        "failed": len(failed),
        "total": len(RESULTS),
        "elapsed_s": round(time.time() - start, 2),
        "results": RESULTS,
    }
    (RUNS / "results.json").write_text(json.dumps(summary, indent=2))
    print(f"\n{passed}/{len(RESULTS)} checks passed "
          f"in {summary['elapsed_s']}s")
    if failed:
        print("FAILURES:")
        for r in failed:
            print(f"  - {r['name']}: {r['detail']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
