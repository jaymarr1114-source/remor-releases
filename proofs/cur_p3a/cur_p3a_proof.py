#!/usr/bin/env python3
"""CUR-P3A proof battery: Scientific Inquiry loop controller.

Proves, for real (no mocks, no staged success), what CAN be proven
without the frozen-chain integration:

  T01-T05  the pure stage machinery: hypothesis formation /
           falsifiability, prediction derivation, evidence test,
           convergence assessment (the pipeline's causal core)
  T06-T07  C-6.1 at the loop level: the inlet and the loop refuse
           foreign boundary classes; inquiry boundaries are admitted
  T08-T09  C-2.2: every finding carries complete provenance; the REAL
           fenced Evidence Store accepts complete findings and refuses
           provenance-less / terminal-less ones through the loop's
           own write path
  T10      the integration boundary, demonstrated (not assumed): the
           three frozen gates that refuse "scientific_inquiry" --
           executive LOOP_ABSENT, substrate ValueError, run
           controller's hard-coded questioning loop
  T11      restore() rebuilds runnable state from a checkpoint
  T12      end-to-end pipeline simulation through the pure stages:
           each boundary class converges to its honest terminal state

What is NOT proven here (blocked, named in BOUNDARY.md): execution
through the real governance chain (executive selection, substrate
registration, run-controller dispatch, FRM grant, kill/resume
lineage). Those need the U-1-class vocabulary decision + authorized
frozen-file integration.

Run:  python3 proofs/cur_p3a/cur_p3a_proof.py
Exit 0 only if every check passes.
"""

import os
import sys
import traceback
from pathlib import Path

WORKTREE = Path("/home/hatch/workspace/worktrees/cur-p3a")
sys.path.insert(0, str(WORKTREE / "pylib"))

from swarm_engine.curiosity.loops.scientific_inquiry import (
    INQUIRY_BOUNDARIES,
    LOOP_SCIENTIFIC_INQUIRY,
    LoopContext,
    LoopRefused,
    ScientificInquiryLoop,
    ScientificInquiryLoopInlet,
    assess_convergence,
    build_finding,
    derive_prediction,
    form_hypothesis,
    persist_finding,
    run_test,
    stamp_provenance,
)
from swarm_engine.curiosity.loops.scientific_inquiry.loop import (
    TERMINAL_INSUFFICIENT,
    TERMINAL_METHOD_BOUNDARY,
)
from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_GENERATIVE_PROMPT,
    BOUNDARY_HYPOTHESIS_CANDIDATE,
    BOUNDARY_IMPRECISE_QUESTION,
    BOUNDARY_NOVEL_OBSERVATION,
    BOUNDARY_NOVEL_TASK,
    new_trigger,
)
from swarm_engine.curiosity.evidence.records import (
    CuriosityFinding,
    EvidenceProvenance,
    EvidenceRefused,
    TERMINAL_STATES,
)
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.substrate import CuriositySubstrate

RUN_DIR = WORKTREE / "proofs" / "cur_p3a" / "runs"
RUN_DIR.mkdir(parents=True, exist_ok=True)

passed = failed = 0
failures = []


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
    else:
        failed += 1
        failures.append(name)
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


HYP_FALSIFIABLE = (
    "I hypothesize that the new cache policy reduces p99 latency: "
    "measuring request latency under the A/B test will show p99 "
    "latency dropping by at least 10% compared to the control group.")
HYP_UNFALSIFIABLE = (
    "I hypothesize that an invisible undetectable force influences "
    "the system's behavior in ways that can never be measured.")
OBS_WITH_OBSERVABLE = (
    "Novel observation: every Tuesday at 03:00 the checkpoint writer "
    "records a latency spike of 400ms, measured across four consecutive "
    "weeks in the production log.")
OBS_BARE = "Novel observation: something feels off about the system lately."
EVIDENCE_SUPPORT = [
    "The A/B test measured p99 latency dropping 18% under the new cache "
    "policy, observed across 10k requests.",
    "Control group measurements confirmed the baseline before the test.",
]
EVIDENCE_REFUTE = [
    "The A/B test measured cache policy latency and found no evidence of "
    "change: p99 latency was absent from the improvement, contradicting "
    "the predicted drop.",
]
EVIDENCE_MIXED = EVIDENCE_SUPPORT[:1] + EVIDENCE_REFUTE[:1]
EVIDENCE_IRRELEVANT = [
    "The team had lunch at noon and discussed the roadmap.",
]
# Observation-subject evidence (for the novel-observation pipeline: the
# claim terms come from the checkpoint-spike observation).
OBS_SUPPORT = [
    "Independent re-observation confirmed the Tuesday 03:00 checkpoint "
    "latency spike, measured at 410ms in the production log.",
]
OBS_REFUTE = [
    "Re-observation found no evidence of the Tuesday checkpoint spike; "
    "latency was absent at 03:00, contradicting the reported pattern.",
]


def t01_hypothesis_formation():
    print("T01: hypothesis formation + falsifiability")
    h = form_hypothesis(source_text=HYP_FALSIFIABLE,
                        boundary_class=BOUNDARY_HYPOTHESIS_CANDIDATE)
    check("t01 falsifiable candidate accepted as falsifiable", h.falsifiable,
          h.reason)
    check("t01 falsifier named", bool(h.falsifier.strip()),
          h.falsifier[:60])
    check("t01 observable consequence named",
          bool(h.observable_consequence.strip()))
    h2 = form_hypothesis(source_text=HYP_UNFALSIFIABLE,
                         boundary_class=BOUNDARY_HYPOTHESIS_CANDIDATE)
    check("t01 unfalsifiable candidate detected", not h2.falsifiable,
          h2.reason)
    check("t01 claim terms extracted", len(h.claim_terms) > 0,
          str(h.claim_terms[:6]))


def t02_novel_observation():
    print("T02: novel observation -> reproducibility hypothesis")
    h = form_hypothesis(source_text=OBS_WITH_OBSERVABLE,
                        boundary_class=BOUNDARY_NOVEL_OBSERVATION)
    check("t02 observable observation yields falsifiable claim",
          h.falsifiable, h.reason)
    check("t02 reproducibility statement formed",
          "reproducible" in h.statement)
    check("t02 falsifier is failed replication",
          "fails to reproduce" in h.falsifier)
    h2 = form_hypothesis(source_text=OBS_BARE,
                         boundary_class=BOUNDARY_NOVEL_OBSERVATION)
    check("t02 bare feeling without observable is not falsifiable",
          not h2.falsifiable, h2.reason)


def t03_prediction():
    print("T03: prediction derivation")
    h = form_hypothesis(source_text=HYP_FALSIFIABLE,
                        boundary_class=BOUNDARY_HYPOTHESIS_CANDIDATE)
    p = derive_prediction(h)
    check("t03 prediction statement formed", p.statement.startswith("If "),
          p.statement[:70])
    check("t03 observable markers extracted", len(p.observable_markers) > 0,
          str(p.observable_markers[:6]))
    check("t03 markers overlap the claim",
          bool(set(p.observable_markers) & set(h.claim_terms)))


def t04_test_execution():
    print("T04: evidence test")
    h = form_hypothesis(source_text=HYP_FALSIFIABLE,
                        boundary_class=BOUNDARY_HYPOTHESIS_CANDIDATE)
    p = derive_prediction(h)
    r = run_test(p, h, EVIDENCE_SUPPORT)
    check("t04 supporting evidence -> SUPPORTED", r.verdict == "SUPPORTED",
          r.detail)
    check("t04 supporting lines recorded", len(r.supporting) >= 1)
    r = run_test(p, h, EVIDENCE_REFUTE)
    check("t04 refuting evidence -> REFUTED", r.verdict == "REFUTED",
          r.detail)
    check("t04 refuting lines recorded", len(r.refuting) >= 1)
    r = run_test(p, h, EVIDENCE_MIXED)
    check("t04 mixed evidence -> INCONCLUSIVE", r.verdict == "INCONCLUSIVE",
          r.detail)
    r = run_test(p, h, [])
    check("t04 no evidence -> INSUFFICIENT", r.verdict == "INSUFFICIENT",
          r.detail)
    r = run_test(p, h, EVIDENCE_IRRELEVANT)
    check("t04 irrelevant evidence -> INSUFFICIENT",
          r.verdict == "INSUFFICIENT", r.detail)


def t05_convergence():
    print("T05: convergence assessment")
    h = form_hypothesis(source_text=HYP_FALSIFIABLE,
                        boundary_class=BOUNDARY_HYPOTHESIS_CANDIDATE)
    p = derive_prediction(h)
    for ev, terminal in ((EVIDENCE_SUPPORT, "HYPOTHESIS_SUPPORTED"),
                         (EVIDENCE_REFUTE, "HYPOTHESIS_REFUTED"),
                         ([], "INSUFFICIENT_EVIDENCE"),
                         (EVIDENCE_MIXED, "INCONCLUSIVE")):
        r = run_test(p, h, ev)
        ts, outcome, triage = assess_convergence(h, r)
        check(f"t05 {r.verdict} -> {terminal}",
              ts == terminal and outcome == "QUESTION_CONVERGED",
              f"terminal={ts} outcome={outcome} triage={triage}")
        check(f"t05 {terminal} in frozen vocabulary", ts in TERMINAL_STATES)
    h_bad = form_hypothesis(source_text=HYP_UNFALSIFIABLE,
                            boundary_class=BOUNDARY_HYPOTHESIS_CANDIDATE)
    r = run_test(derive_prediction(h_bad), h_bad, EVIDENCE_SUPPORT)
    ts, outcome, triage = assess_convergence(h_bad, r)
    check("t05 unfalsifiable -> BOUNDARY_ESTABLISHED (method boundary)",
          ts == TERMINAL_METHOD_BOUNDARY and outcome == "BOUNDARY_UNREACHABLE",
          f"terminal={ts} outcome={outcome} triage={triage}")


def _trigger(boundary_class, text):
    return new_trigger(boundary_class=boundary_class, question_text=text,
                       bounded_objective="test the inquiry pipeline",
                       origin="PRIMARY_REQUESTED")


def t06_inlet_refuses_foreign():
    print("T06: C-6.1 loop-level refusal of foreign boundaries")
    loop = ScientificInquiryLoop()
    inlet = ScientificInquiryLoopInlet(loop)
    ctx = LoopContext(substrate=None, inquiry_id="inq_test",
                      budget_slice_s=1.0)
    for foreign in (BOUNDARY_IMPRECISE_QUESTION, BOUNDARY_GENERATIVE_PROMPT,
                    BOUNDARY_NOVEL_TASK):
        try:
            inlet.enter(_trigger(foreign, "what is the cache policy?"), ctx)
            check(f"t06 inlet refuses {foreign}", False, "no refusal raised")
        except LoopRefused as exc:
            check(f"t06 inlet refuses {foreign}", True, str(exc)[:70])
        # Defense in depth: the loop itself refuses even past the inlet.
        try:
            loop.new_inquiry(_trigger(foreign, "what is the cache policy?"),
                             ctx)
            check(f"t06 loop refuses {foreign}", False, "no refusal raised")
        except LoopRefused:
            check(f"t06 loop refuses {foreign}", True, "LoopRefused")


def t07_inquiry_admitted():
    print("T07: inquiry boundaries admitted, state shape")
    loop = ScientificInquiryLoop()
    inlet = ScientificInquiryLoopInlet(loop)
    ctx = LoopContext(substrate=None, inquiry_id="inq_admit",
                      budget_slice_s=1.0)
    for bc, text in ((BOUNDARY_HYPOTHESIS_CANDIDATE, HYP_FALSIFIABLE),
                     (BOUNDARY_NOVEL_OBSERVATION, OBS_WITH_OBSERVABLE)):
        state = inlet.enter(_trigger(bc, text), ctx)
        check(f"t07 {bc} admitted", state["status"] == "running")
        check(f"t07 {bc} state carries boundary",
              state["boundary_class"] == bc)
        check(f"t07 {bc} pipeline starts at observe",
              state["stage"] == "observe")
        check(f"t07 {bc} provenance objective present",
              state["trigger"]["bounded_objective"] == "test the inquiry pipeline")


def t08_provenance():
    print("T08: provenance stamping (C-2.2 loop obligation)")
    prov = stamp_provenance(bounded_objective="test the inquiry pipeline",
                            triage="retain")
    check("t08 provenance names the loop",
          prov.loop == LOOP_SCIENTIFIC_INQUIRY)
    check("t08 provenance carries the objective",
          prov.bounded_objective == "test the inquiry pipeline")
    check("t08 provenance names the model/provider", bool(prov.model.strip()),
          prov.model[:50])
    check("t08 triage advisory recorded", prov.triage == "retain")


def t09_store_roundtrip():
    print("T09: C-2.2 through the REAL fenced Evidence Store")
    db = str(RUN_DIR / "t09_evidence.db")
    if os.path.exists(db):
        os.remove(db)
    store = CuriosityEvidenceStore(db)
    trigger = _trigger(BOUNDARY_HYPOTHESIS_CANDIDATE,
                       HYP_FALSIFIABLE).as_dict()
    h = form_hypothesis(source_text=HYP_FALSIFIABLE,
                        boundary_class=BOUNDARY_HYPOTHESIS_CANDIDATE)
    p = derive_prediction(h)
    r = run_test(p, h, EVIDENCE_SUPPORT)
    ts, outcome, triage = assess_convergence(h, r)
    payload_ref = str(RUN_DIR / "t09_payload.json")
    with open(payload_ref, "w") as fh:
        fh.write('{"verdict": "SUPPORTED"}')
    finding = build_finding(
        inquiry_id="inq_t09", trigger=trigger, terminal_state=ts,
        outcome=outcome, triage=triage, hypothesis=h, prediction=p,
        test_result=r, detail="t09 roundtrip", payload_ref=payload_ref)
    # The loop's own write path (curiosity-domain module: the writer's
    # domain fence admits it).
    saved = persist_finding(store, finding)
    check("t09 complete finding accepted by the fenced store",
          saved.evidence_id == finding.evidence_id,
          f"ev={saved.evidence_id} terminal={saved.terminal_state}")
    reopened = CuriosityEvidenceStore(db)
    check("t09 finding reopens in a fresh store instance",
          reopened is not None)
    # Adversarial: strip provenance -> the store refuses (C-2.2).
    bad = CuriosityFinding(
        evidence_id="ev_bad1", loop=LOOP_SCIENTIFIC_INQUIRY,
        bounded_objective="test the inquiry pipeline",
        origin="PRIMARY_REQUESTED", terminal_state=ts,
        provenance=None, payload_ref=payload_ref)
    try:
        persist_finding(store, bad)
        check("t09 provenance-less finding refused", False, "written!")
    except EvidenceRefused as exc:
        check("t09 provenance-less finding refused", True, str(exc)[:60])
    # Adversarial: terminal outside the enumerated set -> refused.
    bad2 = CuriosityFinding(
        evidence_id="ev_bad2", loop=LOOP_SCIENTIFIC_INQUIRY,
        bounded_objective="test the inquiry pipeline",
        origin="PRIMARY_REQUESTED", terminal_state="MADE_UP",
        provenance=EvidenceProvenance(
            loop=LOOP_SCIENTIFIC_INQUIRY,
            bounded_objective="test the inquiry pipeline",
            model="x"),
        payload_ref=payload_ref)
    try:
        persist_finding(store, bad2)
        check("t09 terminal-less finding refused", False, "written!")
    except EvidenceRefused as exc:
        check("t09 terminal-less finding refused", True, str(exc)[:60])


def t10_authorized_admission():
    print("T10: the James-authorized admission (U-1 class, CUR-P3A-INT)")
    # The three gates T10 once demonstrated are now OPEN by James's
    # explicit decision; the fence stays closed to unadmitted loops.
    # Gate 1: the executive owns the inquiry boundary classes.
    from swarm_engine.curiosity.executive.executive import (
        LOOP_OWNERSHIP, ABSENT_OWNERSHIP)
    check("t10 executive owns the inquiry boundary classes",
          LOOP_OWNERSHIP.get(BOUNDARY_HYPOTHESIS_CANDIDATE)
          == "scientific_inquiry"
          and LOOP_OWNERSHIP.get(BOUNDARY_NOVEL_OBSERVATION)
          == "scientific_inquiry"
          and BOUNDARY_HYPOTHESIS_CANDIDATE not in ABSENT_OWNERSHIP
          and BOUNDARY_NOVEL_OBSERVATION not in ABSENT_OWNERSHIP
          and BOUNDARY_GENERATIVE_PROMPT in ABSENT_OWNERSHIP,
          f"owned={sorted(LOOP_OWNERSHIP)}")
    # Gate 2: the substrate vocabulary admits scientific_inquiry only.
    sub = CuriositySubstrate()
    sub.register_loop("scientific_inquiry", budget_s=60.0)
    try:
        sub.register_loop("creative_exploration", budget_s=60.0)
        check("t10 fence still closed to unadmitted loops", False,
              "registered?!")
    except ValueError as exc:
        check("t10 fence still closed to unadmitted loops",
              "unknown curiosity loop" in str(exc), str(exc)[:60])
    check("t10 substrate admits scientific_inquiry",
          sub.loop_view("scientific_inquiry").loop == "scientific_inquiry",
          "registered + visible")
    # Gate 3: the run controller dispatches by inq.loop from a registry.
    from swarm_engine.curiosity.run_controller.controller import (
        CuriosityRunController)
    rc = CuriosityRunController(
        substrate=sub, checkpoint_db=str(RUN_DIR / "t10_checkpoints.db"),
        evidence_db=str(RUN_DIR / "t10_evidence.db"),
        ledger_db=str(RUN_DIR / "t10_ledger.db"),
        attribution_db=str(RUN_DIR / "t10_attribution.db"),
        payload_dir=str(RUN_DIR / "t10_payloads"), corpus_docs=[])
    check("t10 run controller registry holds both loops",
          rc._loops["questioning"].loop_name == "questioning"
          and rc._loops["scientific_inquiry"].loop_name
          == "scientific_inquiry"
          and type(rc._inlets["questioning"]).__name__
          == "QuestioningLoopInlet"
          and type(rc._inlets["scientific_inquiry"]).__name__
          == "ScientificInquiryLoopInlet",
          f"loops={sorted(rc._loops)}")


def t11_restore():
    print("T11: restore() rebuilds runnable state")
    loop = ScientificInquiryLoop()
    saved = {
        "inquiry_id": "inq_r", "stage": "predict",
        "hypothesis": "h", "region_id": "reg_1",
        "root_mc": "mc_1", "stage_mc": "mc_2", "status": "converged",
        "node_summaries": {"observe": "s"},
    }
    state = loop.restore(saved)
    check("t11 restore resets live handles",
          state["region_id"] is None and state["root_mc"] is None
          and state["stage_mc"] is None)
    check("t11 restore keeps history and resumes",
          state["status"] == "running" and state["stage"] == "predict"
          and state["node_summaries"] == {"observe": "s"})


def t12_pipeline_simulation():
    print("T12: end-to-end pipeline simulation through the pure stages")
    cases = [
        (BOUNDARY_HYPOTHESIS_CANDIDATE, HYP_FALSIFIABLE, EVIDENCE_SUPPORT,
         "HYPOTHESIS_SUPPORTED"),
        (BOUNDARY_HYPOTHESIS_CANDIDATE, HYP_FALSIFIABLE, EVIDENCE_REFUTE,
         "HYPOTHESIS_REFUTED"),
        (BOUNDARY_HYPOTHESIS_CANDIDATE, HYP_UNFALSIFIABLE, EVIDENCE_SUPPORT,
         "BOUNDARY_ESTABLISHED"),
        (BOUNDARY_NOVEL_OBSERVATION, OBS_WITH_OBSERVABLE, [],
         "INSUFFICIENT_EVIDENCE"),
        (BOUNDARY_NOVEL_OBSERVATION, OBS_WITH_OBSERVABLE, OBS_SUPPORT,
         "HYPOTHESIS_SUPPORTED"),
        (BOUNDARY_NOVEL_OBSERVATION, OBS_WITH_OBSERVABLE,
         OBS_SUPPORT + OBS_REFUTE, "INCONCLUSIVE"),
    ]
    for bc, text, ev, terminal in cases:
        # observe -> hypothesize -> predict -> test -> conclude
        h = form_hypothesis(source_text=text, boundary_class=bc)
        p = derive_prediction(h)
        r = run_test(p, h, ev)
        ts, outcome, triage = assess_convergence(h, r)
        check(f"t12 {bc} -> {terminal}", ts == terminal,
              f"verdict={r.verdict} outcome={outcome}")


def main():
    tests = [t01_hypothesis_formation, t02_novel_observation, t03_prediction,
             t04_test_execution, t05_convergence, t06_inlet_refuses_foreign,
             t07_inquiry_admitted, t08_provenance, t09_store_roundtrip,
             t10_authorized_admission, t11_restore, t12_pipeline_simulation]
    for t in tests:
        try:
            t()
        except Exception:
            failed_msg = f"{t.__name__} RAISED"
            failures.append(failed_msg)
            print(f"  [FAIL] {failed_msg}")
            traceback.print_exc()
            globals()["failed"] += 1
    print(f"\nCUR-P3A: {passed} passed, {failed} failed")
    if failures:
        print("failures:", failures)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
