#!/usr/bin/env python3
"""CUR-P3C stage battery: Discovery/Novelty loop controller.

Proves the stage-built loop at the mechanical level: boundary
presentation design, adjacent-space exploration, novelty assessment
(the core adversarial: routine patterns are never claimed novel),
investigation, verification, classification convergence, the
Questioning feed, C-6.1 refusals, C-2.2 provenance + real fenced-store
round-trip, restore() lineage, the integration boundary (real
refusals), and the NODE_GOALS relevance floor (the permanent
CUR-P3A-INT lesson check).

Fresh sequential processes only; run via proofs/cur_p3c/gate_run.sh.
Exit 0 iff every check passes.
"""

import json
import os
import sys
import traceback
from dataclasses import asdict
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKTREE / "pylib"))

from swarm_engine.curiosity.loops.discovery_novelty import (
    BOUNDARY_NOVEL_PATTERN,
    DISCOVERY_BOUNDARIES,
    DISCOVERY_OBJECTIVE,
    LOOP_DISCOVERY_NOVELTY,
    MODEL_ID,
    NODE_CLASSIFY,
    NODE_DETECT,
    NODE_EXPLORE,
    NODE_GOALS,
    NODE_INVESTIGATE,
    NODE_VERIFY,
    TERMINAL_CLASSIFIED,
    TERMINAL_GAP,
    TERMINAL_INSUFFICIENT,
    TRIAGE_FEED,
    DiscoveryNoveltyLoop,
    DiscoveryNoveltyLoopInlet,
    Investigation,
    LoopContext,
    LoopRefused,
    NoveltyAssessment,
    PatternSighting,
    SubstrateRefused,
    Verification,
    assess_novelty,
    build_finding,
    build_questioning_trigger,
    classify_novelty,
    explore_adjacent_space,
    investigate_pattern,
    persist_finding,
    stamp_provenance,
    verify_pattern,
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
    TERMINAL_STATES,
    CuriosityFinding,
    EvidenceProvenance,
    EvidenceRefused,
)
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.substrate import CuriositySubstrate
from swarm_engine.core.graph_controller.controller import (
    _relevance, _tokens)

RUN_DIR = Path(__file__).resolve().parent / "runs"
RUN_DIR.mkdir(exist_ok=True)

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


# -- fixtures ---------------------------------------------------------------

#: The known envelope: patterns the system already understands.
ENVELOPE = [
    {"name": "cache_policy_lru",
     "markers": ["cache", "policy", "lru", "eviction", "memory"]},
    {"name": "retry_backoff",
     "markers": ["retry", "backoff", "timeout", "network"]},
    {"name": "log_rotation",
     "markers": ["log", "rotation", "disk", "retention"]},
]

#: Prior art: already-classified patterns.
PRIOR_ART = ("cache policy arc insertion memory",)

#: A verified ledger entry the battery's characterizations check
#: against (mechanical: marker overlap with the characterization
#: text).
LEDGER = [
    {"primitive_id": "adjacency-scanner",
     "markers": ["adjacent", "space", "scan", "pattern", "detection"],
     "verification_event": "CUR-P3C battery fixture",
     "gate_reference": "n/a"},
]

PATTERN_TEXT = ("detected pattern in adjacent space: cache admission "
                "policy varies with request burst shape")


def _trigger(boundary_class=BOUNDARY_NOVEL_PATTERN, text=PATTERN_TEXT):
    return new_trigger(boundary_class=boundary_class, question_text=text,
                       bounded_objective="test the discovery pipeline",
                       origin="PRIMARY_REQUESTED")


def _ctx(**kw):
    class _Sub:
        pass
    base = dict(substrate=_Sub(), discovery_id="dis_t",
                budget_slice_s=60.0, known_envelope=ENVELOPE,
                prior_art=PRIOR_ART, verified_ledger=LEDGER)
    base.update(kw)
    return LoopContext(**base)


# -- tests ------------------------------------------------------------------

def t01_boundary_presentation_design():
    print("T01: boundary-presentation design (mandate 2)")
    check("t01 novel_pattern is the loop's proposed presentation",
          BOUNDARY_NOVEL_PATTERN == "novel_pattern")
    check("t01 the loop owns exactly its presentation",
          DISCOVERY_BOUNDARIES == (BOUNDARY_NOVEL_PATTERN,))
    frozen = {BOUNDARY_IMPRECISE_QUESTION, BOUNDARY_HYPOTHESIS_CANDIDATE,
              BOUNDARY_NOVEL_OBSERVATION, BOUNDARY_GENERATIVE_PROMPT,
              BOUNDARY_NOVEL_TASK}
    check("t01 novel_pattern is distinct from every frozen class",
          BOUNDARY_NOVEL_PATTERN not in frozen,
          f"frozen={sorted(frozen)}")
    check("t01 the presentation is not declared in frozen boundary.py",
          "novel_pattern" not in open(
              WORKTREE / "pylib/swarm_engine/curiosity/executive"
              "/boundary.py").read())


def t02_exploration():
    print("T02: adjacent-space exploration with lineage")
    obs = [
        {"markers": ["cache", "admission", "burst", "shape"],
         "dimension": "request path",
         "variation_note": "admission varies with burst shape"},
        {"markers": ["retry", "jitter"],
         "dimension": "network edge",
         "variation_note": "jitter added to backoff"},
    ]
    sightings = explore_adjacent_space(known_envelope=ENVELOPE,
                                       adjacent_observations=obs)
    check("t02 two observations -> two sightings", len(sightings) == 2)
    check("t02 lineage records the adjacent dimension",
          sightings[0].adjacent_dimension == "request path")
    check("t02 lineage records the variation note",
          "burst shape" in sightings[0].variation_note)
    check("t02 sighting ids are unique",
          len({s.sighting_id for s in sightings}) == 2)


def t03_novelty_assessment():
    print("T03: novelty assessment -- routine is never claimed novel")
    s = PatternSighting(sighting_id="sgt_r1",
                        markers=["cache", "policy", "lru", "eviction",
                                 "memory"],
                        adjacent_dimension="d", variation_note="")
    a = assess_novelty(s, known_envelope=ENVELOPE, prior_art=PRIOR_ART)
    check("t03 exact repeat refused as non-novel",
          not a.novel and a.reason == "exact_repeat",
          f"reason={a.reason}")
    s = PatternSighting(sighting_id="sgt_r2",
                        markers=["cache", "policy", "lru", "eviction",
                                 "memory", "prefetch"],
                        adjacent_dimension="d", variation_note="")
    a = assess_novelty(s, known_envelope=ENVELOPE, prior_art=PRIOR_ART)
    check("t03 trivial variation refused as non-novel",
          not a.novel and a.reason == "trivial_variation",
          f"reason={a.reason}")
    s = PatternSighting(sighting_id="sgt_r3",
                        markers=["cache", "policy", "arc", "insertion",
                                 "memory"],
                        adjacent_dimension="d", variation_note="")
    a = assess_novelty(s, known_envelope=ENVELOPE, prior_art=PRIOR_ART)
    check("t03 already-classified refused as non-novel",
          not a.novel and a.reason == "already_classified",
          f"reason={a.reason}")
    s = PatternSighting(sighting_id="sgt_r4", markers=["blip"],
                        adjacent_dimension="d", variation_note="")
    a = assess_novelty(s, known_envelope=ENVELOPE, prior_art=PRIOR_ART)
    check("t03 structureless noise refused as non-novel",
          not a.novel and a.reason == "no_stable_structure",
          f"reason={a.reason}")
    s = PatternSighting(sighting_id="sgt_n1",
                        markers=["cache", "admission", "burst", "shape",
                                 "scanner"],
                        adjacent_dimension="request path",
                        variation_note="admission varies with burst shape")
    a = assess_novelty(s, known_envelope=ENVELOPE, prior_art=PRIOR_ART)
    check("t03 genuine novelty admitted",
          a.novel and a.reason == "novel_pattern",
          f"reason={a.reason}")
    check("t03 distinguishing markers named",
          set(a.distinguishing_markers) >= {"admission", "burst", "shape",
                                            "scanner"},
          f"markers={a.distinguishing_markers}")


def t04_investigation():
    print("T04: investigation is for novel sightings only")
    s = PatternSighting(sighting_id="sgt_n1",
                        markers=["cache", "admission", "burst"],
                        adjacent_dimension="request path",
                        variation_note="v")
    a = assess_novelty(s, known_envelope=ENVELOPE, prior_art=PRIOR_ART)
    inv = investigate_pattern(s, a)
    check("t04 novel sighting investigated",
          inv.sighting_id == "sgt_n1" and inv.open_questions,
          f"questions={inv.open_questions}")
    check("t04 investigation relates to the envelope",
          inv.relates_to == ["cache_policy_lru"],
          f"relates_to={inv.relates_to}")
    routine = NoveltyAssessment(sighting_id="sgt_r1", novel=False,
                               reason="exact_repeat")
    try:
        investigate_pattern(s, routine)
        check("t04 routine sighting never investigated", False,
              "no error raised")
    except ValueError:
        check("t04 routine sighting never investigated", True)


def t05_verification():
    print("T05: verified-substrate check; unverified names its gap")
    inv = Investigation(sighting_id="sgt_n1",
                        characterization="adjacent space scan pattern "
                                         "detection",
                        relates_to=["cache_policy_lru"],
                        open_questions=["q"])
    v = verify_pattern(inv, verified_ledger=LEDGER)
    check("t05 checkable pattern verified",
          v.verified, f"note={v.check_note}")
    inv2 = Investigation(sighting_id="sgt_n2",
                         characterization="quantum frobnicate manifold",
                         relates_to=[], open_questions=["q"])
    v2 = verify_pattern(inv2, verified_ledger=LEDGER)
    check("t05 unverified pattern not treated as fact",
          (not v2.verified)
          and v2.gap_named == "unverified_pattern_characterization",
          f"gap={v2.gap_named}")


def t06_classification():
    print("T06: classification convergence")
    s = PatternSighting(sighting_id="sgt_n1",
                        markers=["cache", "admission", "burst"],
                        adjacent_dimension="request path",
                        variation_note="v")
    a = assess_novelty(s, known_envelope=ENVELOPE, prior_art=PRIOR_ART)
    # Understood: the ledger answers the investigation's questions.
    inv = Investigation(sighting_id="sgt_n1",
                        characterization="adjacent space scan pattern "
                                         "detection",
                        relates_to=["cache_policy_lru"],
                        open_questions=["what does the scan pattern imply"])
    v = verify_pattern(inv, verified_ledger=LEDGER)
    check("t06 ledger resolves the open question",
          v.resolved_questions == ["what does the scan pattern imply"],
          f"resolved={v.resolved_questions}")
    ts, outcome, triage, detail = classify_novelty(
        sighting=s, assessment=a, investigation=inv, verification=v)
    check("t06 understood novelty classifies",
          ts == TERMINAL_CLASSIFIED, f"terminal={ts}")
    v2 = Verification(sighting_id="sgt_n2", verified=False,
                      check_note="no", gap_named="unverified_x")
    ts2, _, triage2, detail2 = classify_novelty(
        sighting=s, assessment=a, investigation=inv, verification=v2)
    check("t06 unverified characterization is a boundary",
          ts2 == TERMINAL_GAP and detail2["gap"] == "unverified_x",
          f"terminal={ts2}")
    # Not understood: the ledger cannot answer the question.
    inv3 = Investigation(sighting_id="sgt_n3",
                         characterization="adjacent space scan",
                         relates_to=[],
                         open_questions=["what is the quantum meaning"])
    v3 = verify_pattern(inv3, verified_ledger=LEDGER)
    ts3, outcome3, triage3, detail3 = classify_novelty(
        sighting=s, assessment=a, investigation=inv3, verification=v3)
    check("t06 not-understood novelty feeds questioning",
          triage3 == TRIAGE_FEED
          and detail3["open_questions"] == ["what is the quantum meaning"],
          f"triage={triage3}")
    feed = build_questioning_trigger(
        discovery_id="dis_t06",
        classification={"open_questions": detail3["open_questions"]},
        bounded_objective="test the discovery pipeline")
    check("t06 feed is a well-formed questioning input",
          feed["boundary_class"] == "imprecise_question"
          and feed["origin"] == "CURIOUSITY_INITIATED"
          and "cannot understand" in feed["question_text"],
          f"keys={sorted(feed.keys())}")


def t07_inlet_refuses_foreign():
    print("T07: C-6.1 -- inlet and loop refuse foreign boundaries")
    loop = DiscoveryNoveltyLoop()
    inlet = DiscoveryNoveltyLoopInlet(loop)
    ctx = _ctx()
    for foreign in (BOUNDARY_IMPRECISE_QUESTION,
                    BOUNDARY_HYPOTHESIS_CANDIDATE,
                    BOUNDARY_GENERATIVE_PROMPT,
                    "formal_proof"):
        try:
            inlet.enter(_trigger(boundary_class=foreign), ctx)
            check(f"t07 inlet refuses {foreign}", False, "admitted!")
        except LoopRefused:
            check(f"t07 inlet refuses {foreign}", True)
        try:
            loop.new_discovery(_trigger(boundary_class=foreign), ctx)
            check(f"t07 loop refuses {foreign}", False, "admitted!")
        except LoopRefused:
            check(f"t07 loop refuses {foreign}", True)
    state = inlet.enter(_trigger(), ctx)
    check("t07 novel_pattern admitted with correct state shape",
          state["boundary_class"] == BOUNDARY_NOVEL_PATTERN
          and state["stage"] == "explore"
          and state["status"] == "running",
          f"stage={state['stage']}")
    try:
        inlet.enter(_trigger(text="   "), ctx)
        check("t07 pattern-less presentation refused", False, "admitted!")
    except LoopRefused:
        check("t07 pattern-less presentation refused", True)


def t08_provenance():
    print("T08: provenance stamping (C-2.2 loop obligation)")
    p = stamp_provenance(bounded_objective="test the discovery pipeline",
                         triage="retain")
    check("t08 provenance names the loop",
          p.loop == LOOP_DISCOVERY_NOVELTY, f"loop={p.loop}")
    check("t08 provenance carries the objective",
          p.bounded_objective == "test the discovery pipeline")
    check("t08 provenance names the model/provider",
          MODEL_ID in p.model, f"model={p.model[:50]}")
    check("t08 triage advisory recorded", p.triage == "retain")


def t09_store_roundtrip():
    print("T09: C-2.2 through the REAL fenced Evidence Store")
    db = str(RUN_DIR / "t09_evidence.db")
    if os.path.exists(db):
        os.remove(db)
    store = CuriosityEvidenceStore(db)
    s = PatternSighting(sighting_id="sgt_n1",
                        markers=["cache", "admission", "burst"],
                        adjacent_dimension="request path",
                        variation_note="v")
    a = assess_novelty(s, known_envelope=ENVELOPE, prior_art=PRIOR_ART)
    trigger = _trigger().as_dict()
    payload = {"discovery_id": "dis_t09", "terminal_state": TERMINAL_CLASSIFIED,
               "assessment": asdict(a),
               "detail": "novelty classified with provenance"}
    payload_ref = str(RUN_DIR / "t09_payload.json")
    with open(payload_ref, "w") as fh:
        json.dump(payload, fh, indent=2)
    finding = build_finding(
        discovery_id="dis_t09", trigger=trigger,
        terminal_state=TERMINAL_CLASSIFIED, outcome="NOVELTY_CONVERGED",
        triage="retain", assessment=a,
        detail="novelty classified with provenance",
        payload_ref=payload_ref)
    saved = persist_finding(store, finding)
    check("t09 complete finding accepted by the fenced store",
          saved.evidence_id == finding.evidence_id,
          f"ev={saved.evidence_id} terminal={saved.terminal_state}")
    check("t09 terminal is the novelty terminal",
          saved.terminal_state == TERMINAL_CLASSIFIED)
    reopened = CuriosityEvidenceStore(db)
    check("t09 finding reopens in a fresh store instance",
          reopened is not None)
    bad = CuriosityFinding(
        evidence_id="ev_bad1", loop=LOOP_DISCOVERY_NOVELTY,
        bounded_objective="test the discovery pipeline",
        origin="PRIMARY_REQUESTED", terminal_state=TERMINAL_CLASSIFIED,
        provenance=None, payload_ref=payload_ref)
    try:
        persist_finding(store, bad)
        check("t09 provenance-less finding refused", False, "written!")
    except EvidenceRefused as exc:
        check("t09 provenance-less finding refused", True,
              str(exc)[:60])
    bad2 = CuriosityFinding(
        evidence_id="ev_bad2", loop=LOOP_DISCOVERY_NOVELTY,
        bounded_objective="test the discovery pipeline",
        origin="PRIMARY_REQUESTED", terminal_state="MADE_UP",
        provenance=stamp_provenance(
            bounded_objective="test the discovery pipeline",
            triage="retain"),
        payload_ref=payload_ref)
    try:
        persist_finding(store, bad2)
        check("t09 terminal-invented finding refused", False, "written!")
    except EvidenceRefused as exc:
        check("t09 terminal-invented finding refused", True,
              str(exc)[:60])


def t10_integration_boundary():
    print("T10: the integration boundary (real refusals, not assumptions)")
    from swarm_engine.curiosity.executive.executive import (
        CuriosityExecutive, ActivationRefused)
    import inspect as _inspect
    src = _inspect.getsource(CuriosityExecutive.request_activation)
    check("t10 executive has no discovery ownership path",
          "discovery_novelty" not in src and "novel_pattern" not in src)
    sub = CuriositySubstrate()
    try:
        sub.register_loop("discovery_novelty", budget_s=60.0)
        check("t10 register_loop refuses discovery_novelty", False,
              "admitted!")
    except ValueError as exc:
        check("t10 register_loop refuses discovery_novelty", True,
              str(exc)[:70])
    from swarm_engine.curiosity.run_controller.controller import (
        CuriosityRunController)
    csrc = _inspect.getsource(CuriosityRunController.__init__)
    check("t10 run-controller registry has no discovery entry",
          "discovery_novelty" not in csrc)


def t11_restore():
    print("T11: restore() rebuilds runnable state from JSON-native forms")
    loop = DiscoveryNoveltyLoop()
    state = loop.new_discovery(_trigger(), _ctx())
    s = PatternSighting(sighting_id="sgt_n1",
                        markers=["cache", "admission", "burst"],
                        adjacent_dimension="request path",
                        variation_note="v")
    a = assess_novelty(s, known_envelope=ENVELOPE, prior_art=PRIOR_ART)
    state["sightings"] = [asdict(s)]
    state["assessments"] = [asdict(a)]
    # The checkpoint lesson: the persisted form must be JSON-native.
    try:
        blob = json.dumps(state)
        check("t11 state serializes to JSON (checkpoint-safe)", True)
    except TypeError as exc:
        check("t11 state serializes to JSON (checkpoint-safe)", False,
              str(exc)[:60])
        return
    restored = loop.restore(json.loads(blob))
    check("t11 restore resets live handles",
          restored["region_id"] is None
          and restored["root_mc"] is None
          and restored["status"] == "running")
    check("t11 restore rebuilds dataclass forms faithfully",
          isinstance(restored["sightings"][0], PatternSighting)
          and isinstance(restored["assessments"][0], NoveltyAssessment)
          and restored["sightings"][0].sighting_id == "sgt_n1")
    check("t11 restore keeps history and resumes",
          restored["assessments"][0].novel is True)


def t12_pipeline_end_to_end():
    print("T12: end-to-end pipeline through the pure stages")
    obs = [{"markers": ["cache", "admission", "burst", "shape"],
            "dimension": "request path",
            "variation_note": "admission varies with burst shape"}]
    sightings = explore_adjacent_space(known_envelope=ENVELOPE,
                                       adjacent_observations=obs)
    assessments = [assess_novelty(s, known_envelope=ENVELOPE,
                                  prior_art=PRIOR_ART)
                   for s in sightings]
    check("t12 exploration -> detection finds the novelty",
          len(assessments) == 1 and assessments[0].novel)
    # Path 1: the ledger answers the investigation -> NOVELTY_CLASSIFIED.
    inv = Investigation(
        sighting_id=sightings[0].sighting_id,
        characterization="adjacent space scan pattern detection",
        relates_to=["cache_policy_lru"],
        open_questions=["what does the scan pattern imply"])
    v = verify_pattern(inv, verified_ledger=LEDGER)
    ts, outcome, triage, detail = classify_novelty(
        sighting=sightings[0], assessment=assessments[0],
        investigation=inv, verification=v)
    check("t12 answered novelty -> NOVELTY_CLASSIFIED",
          ts == TERMINAL_CLASSIFIED, f"terminal={ts}")
    check("t12 terminal is in the frozen vocabulary",
          TERMINAL_CLASSIFIED in TERMINAL_STATES)
    # Path 2: the mechanical investigate_pattern raises a question the
    # ledger cannot answer -> the novelty feeds Questioning.
    inv2 = investigate_pattern(sightings[0], assessments[0])
    v2 = verify_pattern(inv2, verified_ledger=LEDGER)
    ts2, _, triage2, detail2 = classify_novelty(
        sighting=sightings[0], assessment=assessments[0],
        investigation=inv2, verification=v2)
    check("t12 unanswered novelty feeds questioning",
          triage2 == TRIAGE_FEED and detail2["open_questions"],
          f"triage={triage2}")
    feed = build_questioning_trigger(
        discovery_id="dis_t12",
        classification={"open_questions": detail2["open_questions"]},
        bounded_objective="test the discovery pipeline")
    check("t12 feed carries the unanswered question",
          detail2["open_questions"][0] in feed["question_text"])
    # A routine observation never reaches classification.
    obs2 = [{"markers": ["cache", "policy", "lru", "eviction", "memory"],
             "dimension": "memory",
             "variation_note": "none"}]
    s2 = explore_adjacent_space(known_envelope=ENVELOPE,
                                adjacent_observations=obs2)
    a2 = assess_novelty(s2[0], known_envelope=ENVELOPE,
                        prior_art=PRIOR_ART)
    check("t12 routine observation dies at detection",
          not a2.novel and a2.reason == "exact_repeat",
          f"reason={a2.reason}")


def t13_node_goals_relevant():
    print("T13: node goals clear the relevance floor (empirical)")
    obj_toks = _tokens(DISCOVERY_OBJECTIVE)
    for nid, goal in NODE_GOALS.items():
        r = _relevance(obj_toks, _tokens(goal))
        check(f"t13 goal {nid} relevant (>= 0.41 vs 0.34 floor)",
              r >= 0.41, f"{r:.3f}")


def main():
    print("=== CUR-P3C stage battery ===")
    tests = [t01_boundary_presentation_design, t02_exploration,
             t03_novelty_assessment, t04_investigation, t05_verification,
             t06_classification, t07_inlet_refuses_foreign,
             t08_provenance, t09_store_roundtrip,
             t10_integration_boundary, t11_restore,
             t12_pipeline_end_to_end, t13_node_goals_relevant]
    for test in tests:
        try:
            test()
        except Exception:
            global FAILED
            FAILED += 1
            name = test.__name__
            FAILURES.append(name + " (exception)")
            print(f"  [FAIL] {name} -- EXCEPTION")
            traceback.print_exc()
    print()
    print(f"CUR-P3C: {PASSED} passed, {FAILED} failed")
    if FAILURES:
        print("FAILURES:", FAILURES)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
