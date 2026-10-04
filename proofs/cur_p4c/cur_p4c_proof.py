#!/usr/bin/env python3
"""CUR-P4C proof battery: C-9 evidence-return leg.

T01  end-to-end return leg: Primary-requested inquiry -> terminal
     finding -> present_for_acceptance -> REAL RelevanceGate.decide
     -> ADMITTED, decision retrievable, linkage recorded.
T02  RETAINED: curiosity-initiated finding, valid, below threshold.
T03  REJECTED: malformed + refuted findings through the real gate.
T04  verdict consequences: append-only decisions, latest-wins reads,
     no set_verdict API, no curiosity-side decision writer.
T05  adversarial: forged objective link refused; KILLED termination
     refused as not-a-finding; unknown evidence refused.
T06  no boundary-closing capability on the curiosity side; the gate's
     objective untouched by the return leg; max outcome is a verdict.
T07  regression: P4B battery (which itself runs P4A's) green.
"""
import json
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]  # worktree root, no hardcode
RUNS = Path(__file__).resolve().parent / "runs"
sys.path.insert(0, str(WORKTREE / "pylib"))
sys.path.insert(0, str(WORKTREE / "proofs" / "cur_p3a"))  # int_stack helpers
sys.path.insert(0, str(WORKTREE / "proofs" / "cur_p4a"))  # p4a stack helpers

from cur_p4a_proof import (  # noqa: E402
    p4a_stack, corpus, primary_request, takeup_for, drive_to_terminal,
    met_roll_call,
)
from swarm_engine.curiosity.activation import (  # noqa: E402
    present_for_acceptance, ReturnRefused, ST_ACCEPTED,
)
from swarm_engine.curiosity.activation import evidence_return as return_mod
from swarm_engine.curiosity.evidence.store import (  # noqa: E402
    CuriosityEvidenceStore,
)
from swarm_engine.core.executive.relevance import (  # noqa: E402
    RelevanceGate, Finding, FindingProvenance, OperationalObjective,
)

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


def gate_for(rundir, objective_id="obj-keep-turning",
             statement="Keep the Primary loop turning", threshold=0.25):
    return RelevanceGate(
        objective=OperationalObjective(objective_id=objective_id,
                                       statement=statement),
        store_path=str(rundir / "rel.db"),
        threshold=threshold,
    )


def run_primary_request(stack, tu):
    """Full real take-up -> terminal finding. Returns
    (request_id, inquiry_id, evidence_id)."""
    rec = tu.consider(primary_request())
    assert rec["state"] == ST_ACCEPTED, rec
    iid = rec["inquiry_id"]
    terminal = drive_to_terminal(stack["rc"], iid)
    assert terminal == "TERMINATED", terminal
    inq = stack["rc"]._inquiries[iid]
    return rec["request_id"], iid, inq.evidence_id


# ---------------------------------------------------------------- T01
def t01_end_to_end_return():
    print("T01: end-to-end return leg -> ADMITTED via provenance link")
    stack = p4a_stack("p4c_t01", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    req_id, iid, ev_id = run_primary_request(stack, tu)
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    gate = gate_for(stack["dir"])
    link = present_for_acceptance(
        ev_id, request_id=req_id, takeup=tu,
        evidence_store=store, gate=gate)
    check("t01 verdict ADMITTED via provenance link",
          link["verdict"] == "admitted" and link["criterion"]
          == "provenance-link",
          f"verdict={link['verdict']} criterion={link['criterion']}")
    check("t01 linkage recorded",
          link["request_id"] == req_id and link["inquiry_id"] == iid
          and link["requested_by_primary"] is True
          and link["evidence_id"] == ev_id,
          f"link={link['request_id'][:8]}.. {link['inquiry_id'][:8]}..")
    dec = gate.get_decision(ev_id)
    check("t01 decision retrievable via get_decision",
          dec is not None and dec.verdict == "admitted"
          and dec.objective_id == "obj-keep-turning",
          f"verdict={dec.verdict if dec else None}")
    rows = gate.findings_by_admission("admitted")
    check("t01 persisted row flagged requested_by_primary",
          any(r["finding_id"] == ev_id for r in rows),
          f"admitted_rows={len(rows)}")
    with sqlite3.connect(str(stack["dir"] / "rel.db")) as conn:
        flag = conn.execute(
            "SELECT requested_by_primary FROM findings WHERE finding_id=?",
            (ev_id,)).fetchone()[0]
    check("t01 requested_by_primary=1 in the findings table", flag == 1,
          f"flag={flag}")


# ---------------------------------------------------------------- T02
def t02_retained():
    print("T02: RETAINED -- curiosity-initiated, below threshold")
    stack = p4a_stack("p4c_t02", corpus_docs=corpus())
    met_roll_call(stack)
    # Curiosity-initiated: straight through the executive, no take-up.
    from swarm_engine.curiosity.executive.executive import (
        CuriosityTrigger,
    )
    trigger = CuriosityTrigger(
        trigger_id=f"cur-{uuid.uuid4().hex[:12]}",
        boundary_class="imprecise_question",
        question_text="How does the run controller enforce the budget slice?",
        bounded_objective="obj-elsewhere: an unrelated objective",
        origin="CURIOUSITY_INITIATED",
    ).validate()
    decision = stack["ex"].request_activation(trigger)
    iid = stack["rc"].dispatch(decision)
    terminal = drive_to_terminal(stack["rc"], iid)
    assert terminal == "TERMINATED", terminal
    inq = stack["rc"]._inquiries[iid]
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    # Threshold 0.99: no content overlap can honestly admit; the
    # threshold itself is recorded on the decision (auditable).
    gate = gate_for(stack["dir"], threshold=0.99)
    link = present_for_acceptance(
        inq.evidence_id, request_id=None, takeup=takeup_for(stack),
        evidence_store=store, gate=gate)
    check("t02 verdict RETAINED below threshold",
          link["verdict"] == "retained" and link["criterion"]
          == "below-threshold",
          f"verdict={link['verdict']} score={link['score']:.3f}")
    check("t02 requested_by_primary False (distinct path)",
          link["requested_by_primary"] is False
          and link["request_id"] is None,
          "curiosity-initiated handled distinctly")
    dec = gate.get_decision(inq.evidence_id)
    check("t02 recorded threshold is the auditable 0.99",
          dec is not None and dec.threshold == 0.99,
          f"threshold={dec.threshold if dec else None}")


# ---------------------------------------------------------------- T03
def t03_rejected():
    print("T03: REJECTED -- malformed + refuted through the real gate")
    stack = p4a_stack("p4c_t03", corpus_docs=corpus())
    gate = gate_for(stack["dir"])
    bad = Finding(
        finding_id="ev_malformed", content="whatever",
        provenance=FindingProvenance(
            source_loop="", bounded_objective_id="",
            requested_by_primary=False),
        terminal_state="QUESTION_RESOLVED")
    dec = gate.decide(bad)
    check("t03 malformed -> REJECTED/malformed",
          dec.verdict == "rejected" and dec.criterion == "malformed",
          f"verdict={dec.verdict} criterion={dec.criterion}")
    refuted = Finding(
        finding_id="ev_refuted", content="the cache policy does not help",
        provenance=FindingProvenance(
            source_loop="scientific_inquiry",
            bounded_objective_id="obj-keep-turning",
            requested_by_primary=True),
        terminal_state="HYPOTHESIS_REFUTED")
    dec2 = gate.decide(refuted)
    check("t03 refuted -> REJECTED/refuted (kept as knowledge)",
          dec2.verdict == "rejected" and dec2.criterion == "refuted",
          f"verdict={dec2.verdict} criterion={dec2.criterion}")


# ---------------------------------------------------------------- T04
def t04_verdict_consequences():
    print("T04: verdict consequences -- append-only, latest-wins, no writer")
    stack = p4a_stack("p4c_t04", corpus_docs=corpus())
    gate = gate_for(stack["dir"])
    f = Finding(
        finding_id="ev_append", content="Keep the Primary loop turning",
        provenance=FindingProvenance(
            source_loop="questioning",
            bounded_objective_id="obj-keep-turning",
            requested_by_primary=False),
        terminal_state="QUESTION_RESOLVED")
    n0 = gate.decision_count()
    gate.decide(f)
    gate.decide(f)
    check("t04 decisions append-only (two decides -> two rows)",
          gate.decision_count() == n0 + 2,
          f"count={gate.decision_count()}")
    dec = gate.get_decision("ev_append")
    check("t04 get_decision returns the latest row",
          dec is not None and dec.verdict == "admitted",
          f"verdict={dec.verdict if dec else None}")
    check("t04 no set_verdict API on the gate",
          not hasattr(gate, "set_verdict"),
          "verdicts are decided, never set")
    try:
        gate.set_verdict("ev_append", "admitted")  # noqa: B018
        check("t04 set_verdict refused", False, "no AttributeError")
    except AttributeError as exc:
        check("t04 set_verdict refused", True, str(exc)[:50])
    # Structural: the curiosity tree contains no writer for the
    # relevance decisions table -- the gate's _record is the only one.
    out = subprocess.run(
        ["grep", "-rn", "INSERT INTO decisions", "runtime/curiosity/"],
        cwd=str(WORKTREE), capture_output=True, text=True)
    check("t04 no curiosity-side decisions writer in the tree",
          out.stdout.strip() == "",
          "only the gate writes decisions")


# ---------------------------------------------------------------- T05
def t05_adversarial():
    print("T05: adversarial -- forgery, not-a-finding, unknown evidence")
    stack = p4a_stack("p4c_t05", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    req_id, iid, ev_id = run_primary_request(stack, tu)
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    gate = gate_for(stack["dir"])
    # Forged objective link: present the real finding against a
    # DIFFERENT request (different objective).
    other = tu.consider(primary_request(**{
        "operational_objective_ref": {
            "objective_id": "obj-other",
            "statement": "Do something entirely different",
        }}))
    assert other["state"] == ST_ACCEPTED, other
    try:
        present_for_acceptance(
            ev_id, request_id=other["request_id"], takeup=tu,
            evidence_store=store, gate=gate)
        check("t05 forged objective link refused", False, "no refusal")
    except ReturnRefused as exc:
        check("t05 forged objective link refused", True, str(exc)[:70])
    # KILLED termination occupying a store row is not a finding: the
    # row's blob carries no terminal_state/provenance block, so the
    # real store read fails -> named refusal, gate never consulted.
    with sqlite3.connect(str(stack["dir"] / "ev.db")) as conn:
        conn.execute(
            "INSERT INTO curiosity_evidence (evidence_id, data, "
            "terminal_state, origin, created_at) VALUES (?,?,?,?,?)",
            ("ev_termin", json.dumps({"kind": "KILLED",
                                      "reason": "test termination"}),
             "KILLED", "PRIMARY_REQUESTED", time.time()))
    try:
        present_for_acceptance(
            "ev_termin", request_id=req_id, takeup=tu,
            evidence_store=store, gate=gate)
        check("t05 termination refused as not-a-finding", False,
              "no refusal")
    except ReturnRefused as exc:
        check("t05 termination refused as not-a-finding", True,
              str(exc)[:60])
    # Unknown evidence id.
    try:
        present_for_acceptance(
            "ev_nope", request_id=req_id, takeup=tu,
            evidence_store=store, gate=gate)
        check("t05 unknown evidence refused", False, "no refusal")
    except ReturnRefused as exc:
        check("t05 unknown evidence refused", True, str(exc)[:50])
    # The gate saw nothing of the refused presentations.
    check("t05 refused presentations never reached the gate",
          gate.get_decision("ev_nope") is None
          and gate.decision_count() == 0,
          f"decisions={gate.decision_count()}")


# ---------------------------------------------------------------- T06
def t06_no_boundary_closing():
    print("T06: no boundary-closing capability on the curiosity side")
    for name in ("close_boundary", "mark_closed", "close_primary_boundary",
                 "write_acceptance_record", "set_verdict"):
        check(f"t06 no {name} on the return module",
              not hasattr(return_mod, name), "absent")
    src = (WORKTREE / "runtime" / "curiosity" / "activation"
           / "evidence_return.py").read_text()
    check("t06 return leg never touches set_objective",
          "gate.set_objective" not in src
          and ".set_objective(" not in src,
          "present-only surface")
    check("t06 return leg never writes the relevance store",
          "sqlite3" not in src and "INSERT" not in src,
          "no store access")
    # The gate's objective is untouched by a presentation.
    stack = p4a_stack("p4c_t06", corpus_docs=corpus())
    met_roll_call(stack)
    tu = takeup_for(stack)
    req_id, iid, ev_id = run_primary_request(stack, tu)
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    gate = gate_for(stack["dir"])
    before = (gate.objective.objective_id, gate.threshold)
    link = present_for_acceptance(
        ev_id, request_id=req_id, takeup=tu,
        evidence_store=store, gate=gate)
    check("t06 gate objective + threshold unchanged by presentation",
          (gate.objective.objective_id, gate.threshold) == before,
          f"objective={before[0]} threshold={before[1]}")
    check("t06 max outcome is a verdict, never a closed boundary",
          link["verdict"] in ("admitted", "retained", "rejected")
          and "closed" not in json.dumps(link).lower(),
          f"verdict={link['verdict']}")


# ---------------------------------------------------------------- T07
def t07_regression():
    print("T07: regression -- P4B battery (runs P4A's) green")
    proc = subprocess.run(
        ["bash", "proofs/cur_p4b/gate_run.sh"],
        cwd=str(WORKTREE), capture_output=True, text=True, timeout=600)
    tail = "\n".join(proc.stdout.strip().splitlines()[-3:])
    check("t07 P4B battery green (incl. P4A regression)",
          proc.returncode == 0 and "ALL BATTERIES GREEN" in proc.stdout,
          tail.replace("\n", " | ")[:120])


def main():
    RUNS.mkdir(parents=True, exist_ok=True)
    t01_end_to_end_return()
    t02_retained()
    t03_rejected()
    t04_verdict_consequences()
    t05_adversarial()
    t06_no_boundary_closing()
    t07_regression()
    print(f"\nCUR-P4C: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
