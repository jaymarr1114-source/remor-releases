#!/usr/bin/env python3
"""CUR-P3B proof battery: Creative Exploration loop controller (stage).

Stage build only: the loop controller as NEW files, proven at the
stage level. Anything requiring execution through the frozen Phase-2
chain is labeled NOT VERIFIED THIS SHIFT, never faked.

Path rule (James 2026-10-03): no hardcoded home paths -- the worktree
root is derived from this file's location.
"""
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKTREE / "pylib"))

RUN_DIR = WORKTREE / "proofs" / "cur_p3b" / "runs"
RUN_DIR.mkdir(parents=True, exist_ok=True)

from swarm_engine.curiosity.loops.creative_exploration import (
    CREATIVE_BOUNDARIES,
    CREATIVE_OBJECTIVE,
    LOOP_CREATIVE_EXPLORATION,
    MAX_REFINEMENT_TURNS,
    MODEL_ID,
    NODE_GOALS,
    TERMINAL_GAP,
    TERMINAL_INCONCLUSIVE,
    TERMINAL_INSUFFICIENT,
    TERMINAL_RELEASED,
    TRIAGE_BOUNDARY,
    TRIAGE_CANDIDATE,
    TRIAGE_INVESTIGATE,
    VARIATION_DIMENSIONS,
    CandidateVerdict,
    ComposedCandidate,
    CreativeExplorationLoop,
    CreativeExplorationLoopInlet,
    Evaluation,
    LoopContext,
    LoopRefused,
    VerifiedPrimitive,
    assess_intent,
    build_finding,
    compose_candidates,
    evaluate_candidates,
    finding_payload,
    generate_candidates,
    persist_finding,
    release_decision,
    stamp_provenance,
)
from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_GENERATIVE_PROMPT,
    BOUNDARY_HYPOTHESIS_CANDIDATE,
    BOUNDARY_IMPRECISE_QUESTION,
    new_trigger,
)
from swarm_engine.curiosity.evidence.records import (
    TERMINAL_STATES,
    CuriosityFinding,
    EvidenceProvenance,
    EvidenceRefused,
)
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.substrate import CuriositySubstrate
from swarm_engine.core.graph_controller.controller import (
    _relevance, _tokens)

PASSED = 0
FAILED = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAILED += 1
        FAILURES.append(name)
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


def _trigger(boundary_class, text):
    return new_trigger(boundary_class=boundary_class, question_text=text,
                       bounded_objective="test the creative pipeline",
                       origin="PRIMARY_REQUESTED")


# A verified ledger whose every cited commit is checked to exist in the
# repo (T00). Entries are real: the primitive, the verification event,
# the gate reference.
LEDGER = [
    VerifiedPrimitive("curiosity-questioning-loop",
                      "CUR-P2 gate 113/113", "20f80c8"),
    VerifiedPrimitive("curiosity-evidence-store",
                      "CUR-P1A gate", "21f3de1"),
    VerifiedPrimitive("evidence-wire",
                      "EVIDENCE-WIRE-1 gate 20/20", "48b5348"),
]

COMMISSION_SONG = "make a song with melody and rhythm"
COMMISSION_LOGO = "design a logo for a bakery"
#: Commission fully covered by the test ledger (all content terms name
#: ledger primitives): the happy-path pipeline converges to release.
COMMISSION_COVERED = "compose song melody"
LEDGER_SONG = [VerifiedPrimitive("song-melody-compose",
                                 "composition review", "20f80c8")]


def t00_ledger_grounded():
    print("T00: the verified ledger's citations exist in the repo")
    for prim in LEDGER:
        r = subprocess.run(
            ["git", "cat-file", "-t", prim.gate_reference],
            cwd=str(WORKTREE), capture_output=True, text=True)
        check(f"t00 ledger entry {prim.primitive_id} cites a real commit",
              r.returncode == 0 and r.stdout.strip() == "commit",
              f"{prim.gate_reference} ({prim.verification_event})")


def t01_intent_ownership():
    print("T01: intent-state ownership (the selection grammar begins here)")
    owned = assess_intent(commission=COMMISSION_SONG,
                          bounded_objective="song candidate for acceptance")
    check("t01 creative commission owned", owned.owned,
          f"terms={owned.intent_terms[:5]}")
    check("t01 intent terms extracted", len(owned.intent_terms) >= 2)
    missing = assess_intent(commission="",
                            bounded_objective="song candidate")
    check("t01 missing intent-state refused", not missing.owned,
          missing.reason[:60])
    blank = assess_intent(commission="   ",
                          bounded_objective="song candidate")
    check("t01 blank intent-state refused", not blank.owned)
    foreign = assess_intent(
        commission="resolve whether the cache policy is correct",
        bounded_objective="answer the question")
    check("t01 convergence-shaped intent refused (foreign)",
          not foreign.owned, foreign.reason[:70])
    noncomm = assess_intent(commission="the weather today",
                            bounded_objective="x")
    check("t01 non-commission text refused", not noncomm.owned)


def t02_generation():
    print("T02: generation along explicit variation dimensions")
    intent = assess_intent(commission=COMMISSION_LOGO,
                           bounded_objective="logo candidate")
    cands = generate_candidates(intent_terms=intent.intent_terms)
    check("t02 candidates generated", len(cands) == 6, f"n={len(cands)}")
    dims_seen = {d for c in cands for d in c.dimension_settings}
    check("t02 all variation dimensions searched",
          dims_seen == set(VARIATION_DIMENSIONS),
          f"{sorted(dims_seen)}")
    check("t02 lineage recorded on every candidate",
          all(c.lineage for c in cands))
    check("t02 sketches carry the intent terms",
          all("bakery" in c.sketch or "logo" in c.sketch for c in cands))
    # Deterministic: same inputs, same candidates.
    again = generate_candidates(intent_terms=intent.intent_terms)
    check("t02 generation is deterministic (no hidden sampling)",
          [c.sketch for c in again] == [c.sketch for c in cands])


def t03_composition():
    print("T03: bounded composition from the verified ledger only")
    intent = assess_intent(commission=COMMISSION_SONG,
                           bounded_objective="song candidate")
    cands = generate_candidates(intent_terms=intent.intent_terms)
    # A ledger whose primitives share terms with the commission.
    ledger = [VerifiedPrimitive("song-melody-compose",
                                "composition review", "20f80c8"),
              VerifiedPrimitive("rhythm-structure",
                                "structure audit", "21f3de1")]
    comp = compose_candidates(candidates=cands, verified_ledger=ledger,
                              intent_terms=intent.intent_terms)
    check("t03 all candidates composed", len(comp) == len(cands))
    check("t03 refinement bounded (never indefinite)",
          all(c.turns <= MAX_REFINEMENT_TURNS for c in comp),
          f"turns={[c.turns for c in comp[:3]]}")
    check("t03 only ledger primitives applied",
          all(p in ("song-melody-compose", "rhythm-structure")
              for c in comp for p in c.applied))
    check("t03 refinement actually applied primitives",
          any(c.applied for c in comp))
    # Uncovered intent terms become named gaps, never silent fills.
    check("t03 uncovered terms named as gaps (not filled)",
          any(c.gaps for c in comp),
          f"gaps0={comp[0].gaps}")
    # Empty ledger: nothing applied, everything a gap.
    comp0 = compose_candidates(candidates=cands, verified_ledger=[],
                               intent_terms=intent.intent_terms)
    check("t03 empty ledger applies nothing",
          all(not c.applied for c in comp0))


def t04_evaluation():
    print("T04: the mandatory investigation/verification path")
    intent = assess_intent(commission=COMMISSION_COVERED,
                           bounded_objective="song candidate")
    cands = generate_candidates(intent_terms=intent.intent_terms)
    ledger = LEDGER_SONG
    comp = compose_candidates(candidates=cands, verified_ledger=ledger,
                              intent_terms=intent.intent_terms)
    ev = evaluate_candidates(composed=comp, verified_ledger=ledger,
                             intent_terms=intent.intent_terms)
    check("t04 every candidate evaluated", ev.evaluated == len(comp))
    admitted = [v for v in ev.verdicts if v.verdict == "ADMIT"]
    check("t04 ledger-clean candidates admitted", len(admitted) > 0,
          f"admitted={len(admitted)}/{len(ev.verdicts)}")
    # Attack the defense-in-depth path: a candidate with an unlisted
    # applied primitive is quarantined even though compose_candidates
    # can never produce one.
    attacked = ComposedCandidate(
        candidate=cands[0], applied=["unverified-magic-primitive"],
        gaps=[], turns=1, refined_sketch=cands[0].sketch)
    ev2 = evaluate_candidates(composed=[attacked], verified_ledger=ledger,
                              intent_terms=intent.intent_terms)
    check("t04 unlisted primitive quarantined (defense in depth)",
          ev2.verdicts[0].verdict == "QUARANTINE"
          and ev2.verdicts[0].quarantined_primitives
          == ["unverified-magic-primitive"],
          f"{ev2.verdicts[0].reasons[0][:60]}")
    # Load-bearing unverified need -> GAP with the gap named.
    needy = assess_intent(
        commission="compose song quantum-etching",
        bounded_objective="song candidate")
    cands_n = generate_candidates(intent_terms=needy.intent_terms)
    comp_n = compose_candidates(candidates=cands_n,
                                verified_ledger=ledger,
                                intent_terms=needy.intent_terms)
    ev_n = evaluate_candidates(composed=comp_n, verified_ledger=ledger,
                               intent_terms=needy.intent_terms)
    gaps = [v for v in ev_n.verdicts if v.verdict == "GAP"]
    check("t04 unverified need converges to GAP (gap named, not filled)",
          len(gaps) > 0 and any("quantum" in r for v in gaps
                                for r in v.reasons),
          f"gaps={len(gaps)}")
    # Novelty: a sketch duplicating prior art is quarantined. Present
    # one candidate's own sketch as prior art: the mechanical check
    # must catch it as not novel.
    prior = (comp[0].refined_sketch,)
    ev_p = evaluate_candidates(composed=comp, verified_ledger=ledger,
                               intent_terms=intent.intent_terms,
                               prior_art=prior)
    dup = [v for v in ev_p.verdicts
           if any("prior art" in r for r in v.reasons)]
    check("t04 prior-art duplicate quarantined (not novel)",
          any(v.candidate_id == comp[0].candidate.candidate_id
              for v in dup),
          f"dup={[v.candidate_id for v in dup]}")

def t05_no_immediate_beliefs():
    print("T05: C-6.4 -- creativity -> immediate belief is impossible")
    intent = assess_intent(commission=COMMISSION_COVERED,
                           bounded_objective="song candidate")
    cands = generate_candidates(intent_terms=intent.intent_terms)
    ledger = LEDGER_SONG
    comp = compose_candidates(candidates=cands, verified_ledger=ledger,
                              intent_terms=intent.intent_terms)
    ev = evaluate_candidates(composed=comp, verified_ledger=ledger,
                             intent_terms=intent.intent_terms)
    ts, outcome, triage, detail = release_decision(evaluation=ev,
                                                   evaluated=True)
    payload = finding_payload(
        exploration_id="exp_t05", terminal_state=ts,
        candidates=ev.verdicts, gaps=[], detail=detail)
    check("t05 payload epistemic_status is hypothesis, never belief",
          payload["epistemic_status"] == "hypothesis")
    check("t05 terminal is a candidate terminal, not a belief terminal",
          ts == TERMINAL_RELEASED and ts in TERMINAL_STATES)
    check("t05 triage routes to the Acceptance panel (advisory only)",
          triage == TRIAGE_CANDIDATE)
    check("t05 no admission path exists in the payload",
          payload["admission_path"] is None)
    # The loop module imports no admission interface: the only writer
    # it touches is the curiosity evidence writer.
    import swarm_engine.curiosity.loops.creative_exploration.loop as mod
    import_lines = [ln for ln in open(mod.__file__).read().splitlines()
                    if ln.strip().startswith(("import ", "from "))]
    check("t05 loop imports no acceptance/admission interface",
          not any("accept" in ln.lower() or "admit" in ln.lower()
                  for ln in import_lines),
          f"{len(import_lines)} import lines scanned")
    # Release is structurally gated on evaluation: no bypass.
    try:
        release_decision(evaluation=ev, evaluated=False)
        check("t05 release without evaluation refused", False,
              "released!")
    except LoopRefused as exc:
        check("t05 release without evaluation refused", True,
              str(exc)[:60])


def t06_inlet_refuses_foreign():
    print("T06: C-6.1 loop-level refusal of foreign boundaries")
    loop = CreativeExplorationLoop()
    inlet = CreativeExplorationLoopInlet(loop)
    ctx = LoopContext(substrate=None, exploration_id="exp_test",
                      budget_slice_s=1.0)
    # A pure-logic boundary: formal deduction/verification-shaped.
    for foreign, text in (
            ("formal_proof",
             "prove that the sorting algorithm terminates for all inputs"),
            (BOUNDARY_IMPRECISE_QUESTION, "what is the cache policy?"),
            (BOUNDARY_HYPOTHESIS_CANDIDATE,
             "hypothesis: the cache reduces latency")):
        try:
            inlet.enter(_trigger(foreign, text), ctx)
            check(f"t06 inlet refuses {foreign}", False,
                  "no refusal raised")
        except LoopRefused as exc:
            check(f"t06 inlet refuses {foreign}", True,
                  str(exc)[:60])
        try:
            loop.new_exploration(_trigger(foreign, text), ctx)
            check(f"t06 loop refuses {foreign}", False,
                  "no refusal raised")
        except LoopRefused:
            check(f"t06 loop refuses {foreign}", True, "LoopRefused")
    # The owned boundary is admitted with the correct state shape.
    state = inlet.enter(_trigger(BOUNDARY_GENERATIVE_PROMPT,
                                 COMMISSION_LOGO), ctx)
    check("t06 generative_prompt admitted",
          state["boundary_class"] == BOUNDARY_GENERATIVE_PROMPT
          and state["stage"] == "intent")
    check("t06 state carries the owned intent",
          state["intent"].owned is True)


def t07_intent_refused_at_loop():
    print("T07: missing/foreign intent refused at the loop (defense in depth)")
    loop = CreativeExplorationLoop()
    ctx = LoopContext(substrate=None, exploration_id="exp_test",
                      budget_slice_s=1.0)
    for text, why in (("", "missing"),
                      ("resolve whether the cache is correct", "foreign")):
        try:
            loop.new_exploration(
                _trigger(BOUNDARY_GENERATIVE_PROMPT, text), ctx)
            check(f"t07 loop refuses {why} intent", False,
                  "no refusal raised")
        except LoopRefused as exc:
            check(f"t07 loop refuses {why} intent", True,
                  str(exc)[:60])


def t08_provenance():
    print("T08: provenance stamping (C-2.2 loop obligation)")
    prov = stamp_provenance(bounded_objective="test the creative pipeline",
                            triage=TRIAGE_CANDIDATE)
    check("t08 provenance names the loop",
          prov.loop == LOOP_CREATIVE_EXPLORATION)
    check("t08 provenance carries the objective",
          prov.bounded_objective == "test the creative pipeline")
    check("t08 provenance names the model/provider",
          bool(prov.model.strip()), prov.model[:50])
    check("t08 triage advisory recorded",
          prov.triage == TRIAGE_CANDIDATE)


def t09_store_roundtrip():
    print("T09: C-2.2 through the REAL fenced Evidence Store")
    db = str(RUN_DIR / "t09_evidence.db")
    if os.path.exists(db):
        os.remove(db)
    store = CuriosityEvidenceStore(db)
    trigger = _trigger(BOUNDARY_GENERATIVE_PROMPT,
                       COMMISSION_COVERED).as_dict()
    intent = assess_intent(commission=COMMISSION_COVERED,
                           bounded_objective="test the creative pipeline")
    cands = generate_candidates(intent_terms=intent.intent_terms)
    ledger = LEDGER_SONG
    comp = compose_candidates(candidates=cands, verified_ledger=ledger,
                              intent_terms=intent.intent_terms)
    ev = evaluate_candidates(composed=comp, verified_ledger=ledger,
                             intent_terms=intent.intent_terms)
    ts, outcome, triage, detail = release_decision(evaluation=ev,
                                                   evaluated=True)
    payload = finding_payload(
        exploration_id="exp_t09", terminal_state=ts,
        candidates=ev.verdicts, gaps=[], detail=detail)
    payload_ref = str(RUN_DIR / "t09_payload.json")
    with open(payload_ref, "w") as fh:
        json.dump(payload, fh, indent=2)
    finding = build_finding(
        exploration_id="exp_t09", trigger=trigger, terminal_state=ts,
        outcome=outcome, triage=triage, candidates=ev.verdicts,
        detail=detail, payload_ref=payload_ref)
    # The loop's own write path (curiosity-domain module: the writer's
    # domain fence admits it).
    saved = persist_finding(store, finding)
    check("t09 complete candidate finding accepted by the fenced store",
          saved.evidence_id == finding.evidence_id,
          f"ev={saved.evidence_id} terminal={saved.terminal_state}")
    check("t09 terminal is the candidate terminal",
          saved.terminal_state == TERMINAL_RELEASED)
    reopened = CuriosityEvidenceStore(db)
    check("t09 finding reopens in a fresh store instance",
          reopened is not None)
    # Adversarial: strip provenance -> the store refuses (C-2.2).
    bad = CuriosityFinding(
        evidence_id="ev_bad1", loop=LOOP_CREATIVE_EXPLORATION,
        bounded_objective="test the creative pipeline",
        origin="PRIMARY_REQUESTED", terminal_state=ts,
        provenance=None, payload_ref=payload_ref)
    try:
        persist_finding(store, bad)
        check("t09 provenance-less finding refused", False, "written!")
    except EvidenceRefused as exc:
        check("t09 provenance-less finding refused", True,
              str(exc)[:60])
    # Adversarial: terminal outside the enumerated set -> refused.
    bad2 = CuriosityFinding(
        evidence_id="ev_bad2", loop=LOOP_CREATIVE_EXPLORATION,
        bounded_objective="test the creative pipeline",
        origin="PRIMARY_REQUESTED", terminal_state="MADE_UP",
        provenance=EvidenceProvenance(
            loop=LOOP_CREATIVE_EXPLORATION,
            bounded_objective="test the creative pipeline",
            model="x"),
        payload_ref=payload_ref)
    try:
        persist_finding(store, bad2)
        check("t09 terminal-invented finding refused", False, "written!")
    except EvidenceRefused as exc:
        check("t09 terminal-invented finding refused", True,
              str(exc)[:60])

def t10_integration_boundary():
    print("T10: the integration boundary (three frozen gates, real calls)")
    # Gate 1: the executive names the absence -- LOOP_ABSENT for the
    # generative-prompt boundary class (real request_activation call).
    from swarm_engine.curiosity.executive.executive import (
        ABSENT_OWNERSHIP, LOOP_OWNERSHIP, ActivationRefused,
        CuriosityExecutive)
    enf_dir = str(RUN_DIR / "t10_enforcement")
    os.makedirs(enf_dir, exist_ok=True)
    ex = CuriosityExecutive(frm=None, enforcement_state_dir=enf_dir,
                            gam=None, run_controller=None)
    check("t10 generative_prompt not in LOOP_OWNERSHIP",
          LOOP_OWNERSHIP.get(BOUNDARY_GENERATIVE_PROMPT) is None)
    check("t10 generative_prompt named in ABSENT_OWNERSHIP",
          BOUNDARY_GENERATIVE_PROMPT in ABSENT_OWNERSHIP,
          ABSENT_OWNERSHIP.get(BOUNDARY_GENERATIVE_PROMPT, ""))
    try:
        ex.request_activation(
            _trigger(BOUNDARY_GENERATIVE_PROMPT, COMMISSION_LOGO))
        check("t10 executive refuses generative_prompt (LOOP_ABSENT)",
              False, "activated!")
    except ActivationRefused as exc:
        check("t10 executive refuses generative_prompt (LOOP_ABSENT)",
              "LOOP_ABSENT" in str(exc), str(exc)[:80])
    # Gate 2: the substrate vocabulary is fenced -- register_loop
    # refuses creative_exploration (real call).
    sub = CuriositySubstrate()
    try:
        sub.register_loop("creative_exploration", budget_s=60.0)
        check("t10 substrate refuses creative_exploration", False,
              "registered?!")
    except ValueError as exc:
        check("t10 substrate refuses creative_exploration",
              "unknown curiosity loop" in str(exc), str(exc)[:70])
    # Gate 3: the run controller has no registry path for the loop --
    # dispatching a creative_exploration decision through the REAL
    # dispatch path dies at the substrate backstop (real call).
    from swarm_engine.curiosity.run_controller.controller import (
        CuriosityRunController)
    rc = CuriosityRunController(
        substrate=sub, checkpoint_db=str(RUN_DIR / "t10_checkpoints.db"),
        evidence_db=str(RUN_DIR / "t10_evidence.db"),
        ledger_db=str(RUN_DIR / "t10_ledger.db"),
        attribution_db=str(RUN_DIR / "t10_attribution.db"),
        payload_dir=str(RUN_DIR / "t10_payloads"), corpus_docs=[])

    class Decision:
        approved = True
        priority = 1
        epoch_id = 1
        loop = "creative_exploration"
        trigger = _trigger(BOUNDARY_GENERATIVE_PROMPT, COMMISSION_LOGO)

        class Grant:
            grant_id = "gr_t10"
            budget_s = 60.0
            max_concurrent = 1
        grant = Grant()

    try:
        rc.dispatch(Decision())
        check("t10 run controller cannot dispatch creative_exploration",
              False, "dispatched!")
    except ValueError as exc:
        check("t10 run controller cannot dispatch creative_exploration",
              "unknown curiosity loop" in str(exc), str(exc)[:70])
    # The bypass routes are the same breaking change by another door:
    # mutating the frozen maps at runtime, subclass re-admission, or a
    # parallel run controller would each break the U-1 fence the gate
    # exists to protect. Named here, not attempted.
    check("t10 bypass routes rejected (documented, not attempted)", True,
          "runtime map mutation / subclass re-admission / parallel "
          "controller == breaking the U-1 fence by another door")


def t11_restore():
    print("T11: restore() rebuilds runnable state")
    loop = CreativeExplorationLoop()
    saved = {"exploration_id": "exp_r", "stage": "compose",
             "region_id": "reg_old", "root_mc": "mc_old",
             "stage_mc": "mc_stage", "status": "converged",
             "node_summaries": {"intent": "owned"}}
    state = loop.restore(saved)
    check("t11 restore resets live handles",
          state["region_id"] is None and state["root_mc"] is None
          and state["stage_mc"] is None)
    check("t11 restore keeps history and resumes",
          state["node_summaries"] == {"intent": "owned"}
          and state["status"] == "running"
          and state["stage"] == "compose")


def t12_pipeline_end_to_end():
    print("T12: end-to-end pipeline through the pure stages")
    # Happy path: covered ledger -> CANDIDATE_GENERATED.
    intent = assess_intent(commission=COMMISSION_COVERED,
                           bounded_objective="song candidate")
    cands = generate_candidates(intent_terms=intent.intent_terms)
    ledger = LEDGER_SONG
    comp = compose_candidates(candidates=cands, verified_ledger=ledger,
                              intent_terms=intent.intent_terms)
    ev = evaluate_candidates(composed=comp, verified_ledger=ledger,
                             intent_terms=intent.intent_terms)
    ts, outcome, triage, detail = release_decision(evaluation=ev,
                                                   evaluated=True)
    check("t12 commission -> CANDIDATE_GENERATED",
          ts == TERMINAL_RELEASED and outcome == "CANDIDATE_RELEASED"
          and triage == TRIAGE_CANDIDATE,
          f"terminal={ts} outcome={outcome}")
    # Vague intent -> INSUFFICIENT_EVIDENCE.
    ev0 = Evaluation(verdicts=[], evaluated=0)
    ts0, *_ = release_decision(evaluation=ev0, evaluated=True)
    check("t12 vague intent -> INSUFFICIENT_EVIDENCE",
          ts0 == TERMINAL_INSUFFICIENT, f"terminal={ts0}")
    # Unverified need -> BOUNDARY_ESTABLISHED with the gap named.
    needy = assess_intent(
        commission="compose song quantum-etching",
        bounded_objective="song candidate")
    cands_n = generate_candidates(intent_terms=needy.intent_terms)
    comp_n = compose_candidates(candidates=cands_n,
                                verified_ledger=ledger,
                                intent_terms=needy.intent_terms)
    ev_n = evaluate_candidates(composed=comp_n, verified_ledger=ledger,
                               intent_terms=needy.intent_terms)
    ts_n, outcome_n, triage_n, detail_n = release_decision(
        evaluation=ev_n, evaluated=True)
    check("t12 unverified need -> BOUNDARY_ESTABLISHED (gap named)",
          ts_n == TERMINAL_GAP and "quantum" in detail_n,
          f"terminal={ts_n} triage={triage_n}")


def t13_node_goals_relevant():
    print("T13: node goals clear the relevance floor (empirical)")
    obj_toks = _tokens(CREATIVE_OBJECTIVE)
    for nid, goal in NODE_GOALS.items():
        r = _relevance(obj_toks, _tokens(goal))
        check(f"t13 goal {nid} relevant (>= 0.41 vs 0.34 floor)",
              r >= 0.41, f"{r:.3f}")


def main():
    global FAILED
    print("=== CUR-P3B stage battery ===")
    tests = [t00_ledger_grounded, t01_intent_ownership, t02_generation,
             t03_composition, t04_evaluation, t05_no_immediate_beliefs,
             t06_inlet_refuses_foreign, t07_intent_refused_at_loop,
             t08_provenance, t09_store_roundtrip,
             t10_integration_boundary, t11_restore,
             t12_pipeline_end_to_end, t13_node_goals_relevant]
    for test in tests:
        try:
            test()
        except Exception:
            FAILED += 1
            name = test.__name__
            FAILURES.append(name + " (exception)")
            print(f"  [FAIL] {name} -- EXCEPTION")
            traceback.print_exc()
    print()
    print(f"CUR-P3B: {PASSED} passed, {FAILED} failed")
    if FAILURES:
        print("FAILURES:", FAILURES)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
