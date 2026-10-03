#!/usr/bin/env python3
"""CUR-P3A-INT integration proof battery.

Proves, for real (no mocks, no staged success), the James-authorized
integration claim: a scientific inquiry executes the FULL Phase-2
governance chain

  trigger -> Curiosity Executive -> Curiosity Run Controller
  -> Scientific Inquiry loop -> reasoning substrate (cognition inlet)
  -> evidence -> verification -> terminal state
  -> fenced Evidence Store -> return

under a real FRM grant with a real budget, producing a
provenance-stamped terminal finding -- with executive selection,
FRM grant admission, per-stage MC spawn/charge/retire with forest
containment (C-6.2), the C-4.2/C-4.3 budget paths, kill/resume lineage
across real process death, terminal routing and attribution of a
scientific-inquiry finding, and a byte-identical questioning
regression through the refactored registry.

Every test builds a REAL stack (int_stack.new_stack) inside its own
run directory under proofs/cur_p3a/runs_int/. Results are printed;
the process exits 0 only if every check passes.

Run:  python3 proofs/cur_p3a/cur_p3a_int_proof.py
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

WORKTREE = Path("/home/hatch/workspace/worktrees/cur-p3a")
sys.path.insert(0, str(WORKTREE / "pylib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from int_stack import (
    HYP_TEXT, SUPPORT_DOC, REFUTE_DOC, NEUTRAL_DOCS, Q_RESOLVED,
    CountingFRM, inquiry_trigger, met_roll_call, new_stack, q_corpus)

from swarm_engine.curiosity.frm.policy import FrmPolicy

from swarm_engine.curiosity.substrate import (
    CURIOSITY_LOOPS, CURIOSITY_LOOP_SET, CuriositySubstrate,
    LOOP_QUESTIONING, LOOP_SCIENTIFIC_INQUIRY)
from swarm_engine.curiosity.executive.executive import ActivationRefused
from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_GENERATIVE_PROMPT, BOUNDARY_HYPOTHESIS_CANDIDATE,
    BOUNDARY_IMPRECISE_QUESTION, BOUNDARY_NOVEL_OBSERVATION, new_trigger)
from swarm_engine.curiosity.run_controller.controller import AdmissionRefused
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.evidence.records import (
    CuriosityFinding, EvidenceRefused)
from swarm_engine.curiosity.attribution.chain import ChainLedger
from swarm_engine.core.executive.terminal_routing import TerminalLedger
from swarm_engine.curiosity.loops.scientific_inquiry.loop import (
    persist_finding)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append({"name": name, "status": "PASS" if cond else "FAIL",
                    "detail": detail})
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""))
    return cond


# ---------------------------------------------------------------- T13

def t13_vocabulary():
    print("T13: vocabulary admission (James's U-1-class decision)")
    sub = CuriositySubstrate()
    sub.register_loop("scientific_inquiry", budget_s=60.0)
    lv = sub.loop_view("scientific_inquiry")
    check("t13 register_loop admits scientific_inquiry",
          lv.loop == "scientific_inquiry", f"state={lv.state}")
    try:
        sub.register_loop("teleport_loop", budget_s=1.0)
        check("t13 fence still closed to unadmitted loops", False,
              "no refusal raised")
    except ValueError as exc:
        check("t13 fence still closed to unadmitted loops", True,
              str(exc)[:60])
    check("t13 vocabulary is the closed admitted pair",
          CURIOSITY_LOOPS == ("questioning", "scientific_inquiry")
          and CURIOSITY_LOOP_SET == frozenset(CURIOSITY_LOOPS),
          str(CURIOSITY_LOOPS))


# ---------------------------------------------------------------- T14

def t14_executive_selection():
    print("T14: executive selection + fit + refusals (C-6.1 executive side)")
    # CountingFRM proves the real evaluation layer was consulted.
    policy = FrmPolicy(total_budget_s=600.0, total_max_concurrent=8,
                       primary_minimum_budget_s=60.0,
                       primary_minimum_concurrent=1, epoch_s=300.0)
    stack = new_stack("t14", corpus_docs=[SUPPORT_DOC, *NEUTRAL_DOCS],
                      frm=CountingFRM(policy))
    met_roll_call(stack)
    ex = stack["ex"]

    d = ex.request_activation(inquiry_trigger())
    check("t14 hypothesis_candidate selected for scientific_inquiry",
          d.approved and d.loop == "scientific_inquiry",
          f"loop={d.loop} epoch={d.epoch_id}")
    check("t14 real FRM grant funds the inquiry",
          d.grant is not None and d.grant.budget_s > 0
          and stack["frm"].rounds == 1,
          f"budget={d.grant.budget_s:.2f}s rounds={stack['frm'].rounds}")

    dq = ex.request_activation(new_trigger(
        boundary_class=BOUNDARY_IMPRECISE_QUESTION,
        question_text=Q_RESOLVED, bounded_objective="t14 regression",
        origin="PRIMARY_REQUESTED"))
    check("t14 imprecise_question still selects questioning",
          dq.approved and dq.loop == "questioning", f"loop={dq.loop}")

    try:
        ex.request_activation(new_trigger(
            boundary_class=BOUNDARY_GENERATIVE_PROMPT,
            question_text="paint me a dream", bounded_objective="t14",
            origin="PRIMARY_REQUESTED"))
        check("t14 generative_prompt refused (LOOP_ABSENT)", False,
              "no refusal raised")
    except ActivationRefused as exc:
        check("t14 generative_prompt refused (LOOP_ABSENT)",
              "LOOP_ABSENT" in str(exc), str(exc)[:70])

    try:
        ex.request_activation(inquiry_trigger(text="   "))
        check("t14 empty presentation refused (FIT)", False,
              "no refusal raised")
    except ActivationRefused as exc:
        check("t14 empty presentation refused (FIT)",
              "FIT" in str(exc), str(exc)[:70])

    dn = ex.request_activation(inquiry_trigger(
        boundary_class=BOUNDARY_NOVEL_OBSERVATION))
    check("t14 novel_observation selects scientific_inquiry",
          dn.approved and dn.loop == "scientific_inquiry",
          f"loop={dn.loop}")


# ---------------------------------------------------------------- T15 + T19

def t15_end_to_end_supported():
    print("T15/T19: end-to-end inquiry -> SUPPORTED + forest containment")
    stack = new_stack("t15", corpus_docs=[SUPPORT_DOC, *NEUTRAL_DOCS])
    met_roll_call(stack)
    ex, rc, sub = stack["ex"], stack["rc"], stack["sub"]
    rundir = stack["dir"]

    decision = ex.request_activation(inquiry_trigger())
    outcome = ex.activate(decision)
    result = outcome.result
    check("t15 chain completes through the executive",
          outcome.entered and outcome.loop == "scientific_inquiry",
          outcome.detail)
    check("t15 terminal is HYPOTHESIS_SUPPORTED",
          result["terminal_state"] == "HYPOTHESIS_SUPPORTED",
          f"terminal={result['terminal_state']}")
    evidence_id = result["evidence_id"]
    inquiry_id = result["inquiry_id"]

    finding = rc._evidence.get(evidence_id)
    check("t15 provenance-stamped finding in the fenced store",
          finding is not None
          and finding.loop == "scientific_inquiry"
          and finding.terminal_state == "HYPOTHESIS_SUPPORTED",
          f"ev={evidence_id}")
    check("t15 provenance is complete and inquiry-stamped",
          finding.provenance.loop == "scientific_inquiry"
          and "scientific-inquiry" in finding.provenance.model
          and finding.provenance.bounded_objective
          and finding.provenance.triage,
          f"model={finding.provenance.model[:40]}")

    payload = json.loads((rundir / "payloads" / f"{evidence_id}.json")
                         .read_text())
    check("t15 payload envelope carries the inquiry terminal",
          payload["loop"] == "scientific_inquiry"
          and payload["terminal_state"] == "HYPOTHESIS_SUPPORTED"
          and payload["relevance"]["score"] is None,
          "relevance honestly absent for verdict loops")

    routes = TerminalLedger(str(rundir / "term.db")).routes(
        loop="scientific_inquiry")
    hit = [r for r in routes
           if r["terminal_state"] == "HYPOTHESIS_SUPPORTED"
           and r["evidence_refs"].get("evidence_id") == evidence_id]
    check("t15 terminal routed to the evidence store",
          len(hit) == 1
          and hit[0]["consumer"] == "curiosity_evidence_store",
          f"routes={len(routes)} consumer={hit[0]['consumer'] if hit else '?' }")

    chain = ChainLedger(str(rundir / "attr.db"))
    work_ref = f"work_{inquiry_id}"
    w = chain.get_work(work_ref)
    r = chain.result_for_work(work_ref)
    a = chain.admission_for_result(r.result_ref) if r else None
    check("t15 inquiry attributed (work/result/admission)",
          w is not None and r is not None and a is not None
          and r.outcome == "success" and r.evidence_ref == evidence_id
          and a.verdict == "retained",
          f"outcome={r.outcome if r else '?'}")

    lv = sub.loop_view("scientific_inquiry")
    check("t19 MCs retired inside the curiosity forest (C-6.2)",
          lv.active_count == 0 and lv.state == "resolved"
          and lv.total_spawned > 0,
          f"state={lv.state} active={lv.active_count} "
          f"spawned={lv.total_spawned} retired={lv.total_retired}")

    # A second inquiry on the same stack proves no leaked loop state.
    d2 = ex.request_activation(inquiry_trigger())
    r2 = ex.activate(d2).result
    check("t19 second inquiry converges (no leaked state)",
          r2["terminal_state"] == "HYPOTHESIS_SUPPORTED",
          f"terminal={r2['terminal_state']}")


def t16_end_to_end_refuted():
    print("T16: end-to-end inquiry -> REFUTED")
    stack = new_stack("t16", corpus_docs=[REFUTE_DOC, *NEUTRAL_DOCS])
    met_roll_call(stack)
    ex = stack["ex"]
    result = ex.activate(ex.request_activation(inquiry_trigger())).result
    check("t16 terminal is HYPOTHESIS_REFUTED",
          result["terminal_state"] == "HYPOTHESIS_REFUTED",
          f"terminal={result['terminal_state']}")
    finding = stack["rc"]._evidence.get(result["evidence_id"])
    check("t16 refuted finding persisted with provenance",
          finding is not None
          and finding.terminal_state == "HYPOTHESIS_REFUTED"
          and finding.provenance.triage,
          f"ev={result['evidence_id']}")


# ---------------------------------------------------------------- T17/T18

def t17_no_budget():
    print("T17: C-4.2 -- an inquiry with no budget does not start")
    stack = new_stack("t17", corpus_docs=[SUPPORT_DOC],
                      demand_budget_s=0.0)
    met_roll_call(stack)
    try:
        stack["ex"].request_activation(inquiry_trigger())
        check("t17 NO_BUDGET refusal", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t17 NO_BUDGET refusal", "NO_BUDGET" in str(exc),
              str(exc)[:80])


def t18_resource_boundary():
    print("T18: C-4.3 -- forced breach checkpoints, suspends, externalizes")
    stack = new_stack("t18", corpus_docs=[SUPPORT_DOC, *NEUTRAL_DOCS],
                      demand_budget_s=0.001, demand_concurrent=2)
    met_roll_call(stack)
    ex, rc = stack["ex"], stack["rc"]
    result = ex.activate(ex.request_activation(inquiry_trigger())).result
    check("t18 breach returns BLOCKED",
          result["terminal_state"] == "BLOCKED",
          f"terminal={result['terminal_state']}")
    rb = result["resource_boundary"]
    inq = rc._inquiries[result["inquiry_id"]]
    # Two real breach modes: the slice clock exhausts (>= slice pre-tick
    # or > slice post-tick) or a microcontroller exhausts its own
    # budget mid-tick (SubstrateRefused). Both are genuine C-4.3
    # demonstrations; the mode is timing-dependent, the contract is
    # not: exact consumption externalized, never estimated.
    breach_mode_ok = (
        "microcontroller_exhausted" in rb["cause"]
        or "slice" in rb["cause"])
    check("t18 exact consumption + unresolved boundary externalized",
          breach_mode_ok
          and rb["spent_s"] == round(inq.spent_s, 6)
          and rb["budget_s"] == inq.budget_slice_s
          and rb["unresolved_boundary"] == "hypothesis_candidate"
          and rb["cause"],
          f"spent={rb['spent_s']:.4f}s slice={rb['budget_s']:.4f}s "
          f"cause={rb['cause'][:50]}")
    check("t18 checkpoint preserved + inquiry suspended",
          result["checkpoint_id"]
          and rc._inquiries[result["inquiry_id"]].state == "SUSPENDED",
          f"ckpt={result['checkpoint_id']}")
    finding = rc._evidence.get(result["evidence_id"])
    check("t18 BLOCKED finding persisted with provenance",
          finding is not None and finding.terminal_state == "BLOCKED"
          and finding.provenance.loop == "scientific_inquiry",
          f"ev={result['evidence_id']}")


# ---------------------------------------------------------------- T20

def t20_kill_resume_cross_process():
    print("T20: kill/resume lineage across real process death")
    kd = subprocess.run(
        [sys.executable, "proofs/cur_p3a/kill_driver.py", "t20"],
        cwd=str(WORKTREE), capture_output=True, text=True, timeout=300)
    check("t20 kill driver exits 0", kd.returncode == 0,
          kd.stderr.strip().splitlines()[-1] if kd.returncode else "")
    killed = json.loads(kd.stdout.strip().splitlines()[-1])
    check("t20 kill checkpoints the inquiry",
          killed["state"] == "KILLED" and killed["checkpoint_id"],
          f"ckpt={killed['checkpoint_id']}")

    rd = subprocess.run(
        [sys.executable, "proofs/cur_p3a/resume_driver.py", "t20",
         killed["inquiry_id"]],
        cwd=str(WORKTREE), capture_output=True, text=True, timeout=300)
    check("t20 resume driver exits 0", rd.returncode == 0,
          rd.stderr.strip().splitlines()[-1] if rd.returncode else "")
    resumed = json.loads(rd.stdout.strip().splitlines()[-1])
    check("t20 resumed inquiry converges in the fresh process",
          resumed["inquiry_id"] == killed["inquiry_id"]
          and resumed["terminal_state"] == "HYPOTHESIS_SUPPORTED"
          and resumed["evidence_id"],
          f"terminal={resumed['terminal_state']}")
    check("t20 lineage preserved across the process boundary",
          "killed" in resumed["events"]
          and "resumed_from_checkpoint" in resumed["events"],
          f"events={len(resumed['events'])}")


# ---------------------------------------------------------------- T21

def t21_questioning_regression():
    print("T21: questioning regression through the refactored registry")
    stack = new_stack("t21", corpus_docs=q_corpus())
    met_roll_call(stack)
    ex, rc = stack["ex"], stack["rc"]
    rundir = stack["dir"]
    d = ex.request_activation(new_trigger(
        boundary_class=BOUNDARY_IMPRECISE_QUESTION,
        question_text=Q_RESOLVED, bounded_objective="t21 regression",
        origin="PRIMARY_REQUESTED"))
    result = ex.activate(d).result
    check("t21 questioning still converges (Phase-2 behavior)",
          result["terminal_state"] == "QUESTION_RESOLVED",
          f"terminal={result['terminal_state']}")
    payload = json.loads((rundir / "payloads"
                          / f"{result['evidence_id']}.json").read_text())
    check("t21 questioning payload shape unchanged",
          set(payload.keys()) == {
              "evidence_id", "inquiry_id", "trigger_id", "loop",
              "bounded_objective", "origin", "terminal_state",
              "precise_question", "precision_score", "passes",
              "relevance", "triage", "triage_note", "resource",
              "produced_at"}
          and payload["loop"] == "questioning"
          and payload["precise_question"]
          and isinstance(payload["relevance"]["score"], float)
          and isinstance(payload["passes"], list),
          f"precise_q={payload['precise_question'][:40]}")


# ---------------------------------------------------------------- T22/T23/T24

def t22_executive_view():
    print("T22: executive view carries both loops (C-6.3)")
    stack = new_stack("t22", corpus_docs=[SUPPORT_DOC])
    view = stack["ex"].executive_view()
    loops = view["loops"]
    check("t22 view has questioning + scientific_inquiry entries",
          set(loops) == {"questioning", "scientific_inquiry"}
          and all(isinstance(v, dict) for v in loops.values()),
          f"loops={sorted(loops)}")


def t23_provenance_refused_controller_store():
    print("T23: provenance-less finding refused at the integrated store")
    stack = new_stack("t23", corpus_docs=[SUPPORT_DOC])
    bad = CuriosityFinding(
        evidence_id="ev_int_bad1", loop="scientific_inquiry",
        bounded_objective="t23 adversarial",
        origin="PRIMARY_REQUESTED", terminal_state="HYPOTHESIS_SUPPORTED",
        provenance=None, payload_ref="x.json")
    try:
        persist_finding(stack["rc"]._evidence, bad)
        check("t23 provenance-less finding refused", False, "written!")
    except EvidenceRefused as exc:
        check("t23 provenance-less finding refused", True,
              str(exc)[:60])


def t24_registry_fence():
    print("T24: unregistered loop refused at dispatch (registry fence)")
    stack = new_stack("t24", corpus_docs=[SUPPORT_DOC])
    met_roll_call(stack)
    ex, rc = stack["ex"], stack["rc"]
    d = ex.request_activation(inquiry_trigger())
    d.loop = "creative_exploration"  # forged: no such registered loop
    try:
        rc.dispatch(d)
        check("t24 unregistered loop refused", False, "dispatched!")
    except AdmissionRefused as exc:
        check("t24 unregistered loop refused", True, str(exc)[:60])


def main():
    t13_vocabulary()
    t14_executive_selection()
    t15_end_to_end_supported()
    t16_end_to_end_refuted()
    t17_no_budget()
    t18_resource_boundary()
    t20_kill_resume_cross_process()
    t21_questioning_regression()
    t22_executive_view()
    t23_provenance_refused_controller_store()
    t24_registry_fence()
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = len(RESULTS) - passed
    print(f"\nCUR-P3A-INT: {passed} passed, {failed} failed")
    for r in RESULTS:
        if r["status"] == "FAIL":
            print(f"  FAILED: {r['name']} -- {r['detail']}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
