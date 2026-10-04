#!/usr/bin/env python3
"""CUR-P5C proof battery: relevance fencing in Primary Acceptance.

Every check runs against the REAL stack (build_live_path: real
SwarmEngine, real ExecutiveController, real RelevanceGate, real
AcceptanceLoop inlet). No mocks stand in for runtime/core/ or
runtime/services/acceptance.py anywhere in this battery.

Claim under test (charter C-3.2): Primary Acceptance is the final
relevance authority. Relevance is the first question acceptance asks;
the acceptance bar is the second. The relevance gate is a component of
Primary Acceptance, never a rival. The Primary Executive cannot flip a
verdict. The D-4 threshold (0.25) is consumed as the decided input.
"""
import os
import sqlite3
import sys
import tempfile
import time
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKTREE / "pylib"))

from swarm_engine.core.executive.live_path import (
    build_live_path, LivePathConfig)
from swarm_engine.core.executive.relevance import (
    RelevanceGate, OperationalObjective, Finding, FindingProvenance,
    ADMITTED, RETAINED, REJECTED, DEFAULT_THRESHOLD)
from swarm_engine.core.executive import boundary as PB
from swarm_engine.services.acceptance import Attempt, AuthReport
from swarm_engine.curiosity.relevance import (
    build_gate_finding, present_for_acceptance, RelevanceInput)
from swarm_engine.curiosity.relevance.present import RelevanceInputRefused
from swarm_engine.curiosity.evidence.records import (
    CuriosityFinding, EvidenceProvenance)

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


OBJECTIVE_STMT = ("Keep REMOR's Primary loop turning: converge real "
                  "boundaries, admit only relevant findings, never lose "
                  "work across a crash.")
HIGH_CONTENT = ("Primary loop turning converge boundaries admit relevant "
                "findings real work")
LOW_CONTENT = "Simmer the broth slowly while the bread rises in the warm kitchen"


def make_lp():
    workdir = tempfile.mkdtemp(prefix="cur-p5c-")
    return build_live_path(LivePathConfig(workdir=workdir)), workdir


def gate_finding(fid, content, terminal="QUESTION_RESOLVED",
                 source_loop="questioning", objective_id="other-obj",
                 requested_by_primary=False, triage=None):
    return Finding(
        finding_id=fid,
        content=content,
        provenance=FindingProvenance(
            source_loop=source_loop,
            bounded_objective_id=objective_id,
            requested_by_primary=requested_by_primary,
            triage=triage),
        terminal_state=terminal)


def curiosity_finding(eid="ev_p5c_1", origin="CURIOUSITY_INITIATED",
                      terminal="QUESTION_RESOLVED", triage="propose"):
    return CuriosityFinding(
        evidence_id=eid,
        loop="questioning",
        bounded_objective="what is the cache policy",
        origin=origin,
        terminal_state=terminal,
        provenance=EvidenceProvenance(
            loop="questioning",
            bounded_objective="what is the cache policy",
            model="curiosity-questioning/v1",
            triage=triage),
        payload_ref="payload/ev_p5c_1").validate()


def real_attempt_evidence():
    return {
        "run_id": "run_p5c_1",
        "goal": "converge the cache boundary",
        "attempt": Attempt(approach_signature=["measure", "compare"],
                           plan={"metric": "latency"},
                           args={}, result_summary="p99 down 12%",
                           exec_ok=True),
        "auth": AuthReport(held_out={"h1": "ok"},
                           negative_controls={"n1": "ok"},
                           passed=True),
    }


print("T01: relevance input honesty (curiosity side presents, never judges)")
cf = curiosity_finding()
adapted = build_gate_finding(cf, content=HIGH_CONTENT,
                             claimed_objective_id="primary-loop")
check("t01 content verbatim", adapted.content == HIGH_CONTENT,
      "no padding/stemming/expansion")
check("t01 no score on the input", not hasattr(adapted, "score"),
      "the gate judges; the input carries none")
check("t01 provenance verbatim",
      adapted.provenance.source_loop == "questioning"
      and adapted.provenance.bounded_objective_id == "what is the cache policy"
      and adapted.provenance.triage == "propose",
      "triage carried as advisory")
check("t01 requested_by_primary derived, not asserted",
      adapted.provenance.requested_by_primary is False,
      "origin=CURIOUSITY_INITIATED -> False")
cf2 = curiosity_finding(eid="ev_p5c_2", origin="PRIMARY_REQUESTED")
adapted2 = build_gate_finding(cf2, content=HIGH_CONTENT,
                              claimed_objective_id="primary-loop")
check("t01 primary origin -> True",
      adapted2.provenance.requested_by_primary is True)
try:
    build_gate_finding(cf, content="   ", claimed_objective_id="primary-loop")
    check("t01 empty content refused", False, "no exception raised")
except RelevanceInputRefused:
    check("t01 empty content refused", True, "fail-closed")
bad = curiosity_finding()
bad.provenance.model = "  "
try:
    build_gate_finding(bad, content=HIGH_CONTENT,
                       claimed_objective_id="primary-loop")
    check("t01 missing provenance refused", False, "no exception raised")
except RelevanceInputRefused:
    check("t01 missing provenance refused", True, "fail-closed")
rec = present_for_acceptance(cf, content=HIGH_CONTENT,
                             claimed_objective_id="primary-loop")
check("t01 present returns the claim record",
      isinstance(rec, RelevanceInput)
      and rec.claimed_objective_id == "primary-loop"
      and rec.content == HIGH_CONTENT,
      "a record of the claim, not a judgment")
try:
    rec.finding_id = "mutated"
    check("t01 claim record frozen", False, "mutation succeeded")
except FrozenInstanceError:
    check("t01 claim record frozen", True, "the claim cannot be edited")

print("T02: the gate judges -- relevance first (D-4 threshold 0.25)")
lp, _ = make_lp()
gate = lp.gate
check("t02 threshold is the D-4 input", gate.threshold == 0.25
      and DEFAULT_THRESHOLD == 0.25,
      f"threshold={gate.threshold} (decided, not derived)")
d1 = gate.decide(gate_finding("f_t02_a", HIGH_CONTENT))
check("t02 content-score admits", d1.verdict == ADMITTED
      and d1.criterion == "content-score",
      f"score={d1.score:.3f} >= 0.25")
check("t02 decision records the threshold", d1.threshold == 0.25
      and d1.score >= 0.25, "auditable, not silent")
d2 = gate.decide(gate_finding("f_t02_b", LOW_CONTENT,
                              objective_id="primary-loop"))
check("t02 provenance-link admits", d2.verdict == ADMITTED
      and d2.criterion == "provenance-link",
      "the request itself established relevance")
d3 = gate.decide(gate_finding("f_t02_c", LOW_CONTENT))
check("t02 below-threshold retains", d3.verdict == RETAINED
      and d3.criterion == "below-threshold",
      f"score={d3.score:.3f} < 0.25; kept, never promoted")
check("t02 retained keeps terminal state",
      gate.get_decision("f_t02_c").verdict == RETAINED)
d4 = gate.decide(gate_finding("f_t02_d", HIGH_CONTENT,
                              terminal="HYPOTHESIS_REFUTED"))
check("t02 refuted rejects", d4.verdict == REJECTED
      and d4.criterion == "refuted",
      "a refutation is knowledge, not a candidate")
d5 = gate.decide(gate_finding("f_t02_e", HIGH_CONTENT,
                              source_loop="   "))
check("t02 malformed rejects", d5.verdict == REJECTED
      and d5.criterion == "malformed",
      "C-2.2 inadmissible as evidence")

print("T03: order is structural -- no relevance verdict, no acceptance")
lp3, _ = make_lp()
d_low = lp3.submit_finding(
    gate_finding("f_t03_low", LOW_CONTENT),
    target_loop="acceptance", boundary_kind="completion_candidate",
    evidence={"run_id": "r", "goal": "g",
              "attempt": real_attempt_evidence()["attempt"],
              "auth": real_attempt_evidence()["auth"]})
check("t03 below-threshold verdict at the inlet",
      d_low.verdict == RETAINED, f"verdict={d_low.verdict}")
check("t03 outbox stays empty",
      len(lp3._delivery_outbox) == 0,
      "the ONLY writer to the outbox is the ADMITTED branch")
check("t03 nothing to deliver", lp3.deliver_admitted() == [],
      "never reaches the acceptance bar")

print("T04: the delivery re-check is live (the gate's record, not the outbox)")
lp4, _ = make_lp()
ev = real_attempt_evidence()
d_hi = lp4.submit_finding(
    gate_finding("f_t04_live", HIGH_CONTENT),
    target_loop="acceptance", boundary_kind="completion_candidate",
    evidence=dict(ev))
check("t04 admitted at submit", d_hi.verdict == ADMITTED,
      "outbox has 1 entry")
check("t04 outbox populated", len(lp4._delivery_outbox) == 1)
# A newer decision supersedes: re-judge the same finding_id below threshold.
d_new = lp4.executive.submit_finding(gate_finding("f_t04_live", LOW_CONTENT))
check("t04 re-decision appends", d_new.verdict == RETAINED
      and lp4.gate.decision_count() == 2,
      "history append-only, nothing rewritten")
recs = lp4.deliver_admitted()
check("t04 stale admission not delivered",
      len(recs) == 1 and recs[0]["delivered"] is False
      and "no longer confirms ADMITTED" in recs[0]["reason"],
      "the re-check reads the gate's latest record")
check("t04 outbox popped", len(lp4._delivery_outbox) == 0)
check("t04 latest verdict stands",
      lp4.gate.get_decision("f_t04_live").verdict == RETAINED)

print("T05: relevance is not acceptance (ADMITTED + bar fails -> refused)")
lp5, _ = make_lp()
bad_ev = dict(ev)
bad_ev["attempt"] = {"not": "a real Attempt"}  # fails boundary validation
d5a = lp5.submit_finding(
    gate_finding("f_t05_bar", HIGH_CONTENT),
    target_loop="acceptance", boundary_kind="completion_candidate",
    evidence=bad_ev)
check("t05 relevance verdict stands", d5a.verdict == ADMITTED,
      f"criterion={d5a.criterion}")
recs5 = lp5.deliver_admitted()
check("t05 acceptance bar refuses",
      len(recs5) == 1 and recs5[0]["delivered"] is False
      and "boundary refused" in recs5[0]["reason"],
      "the gate said yes; the bar said no")

print("T06: full acceptance path (ADMITTED + valid boundary -> entered)")
lp6, _ = make_lp()
d6 = lp6.submit_finding(
    gate_finding("f_t06_full", HIGH_CONTENT),
    target_loop="acceptance", boundary_kind="completion_candidate",
    evidence=dict(real_attempt_evidence()))
check("t06 relevance admits", d6.verdict == ADMITTED)
recs6 = lp6.deliver_admitted()
check("t06 acceptance enters the real loop",
      len(recs6) == 1 and recs6[0]["delivered"] is True,
      "entered via the real acceptance inlet")
check("t06 delivery record is distinct from the relevance verdict",
      set(recs6[0].keys()) != set(d6.as_dict().keys()),
      "two records, two shapes")

print("T07: separateness -- two verdicts, each final")
lp7, _ = make_lp()
f7 = gate_finding("f_t07_sep", HIGH_CONTENT)
d7a = lp7.gate.decide(f7)
n1 = lp7.gate.decision_count()
d7b = lp7.gate.decide(gate_finding("f_t07_sep", HIGH_CONTENT))
check("t07 re-decision appends, never rewrites",
      lp7.gate.decision_count() == n1 + 1
      and d7a.verdict == ADMITTED,
      "first record untouched")
with sqlite3.connect(lp7.gate._store_path) as conn:
    rows = conn.execute(
        "SELECT verdict FROM decisions WHERE finding_id=? ORDER BY seq",
        ("f_t07_sep",)).fetchall()
check("t07 history intact",
      [r[0] for r in rows] == [ADMITTED, ADMITTED],
      "append-only history")
check("t07 latest wins",
      lp7.gate.get_decision("f_t07_sep").verdict == ADMITTED)

print("T08: the Primary Executive cannot flip a verdict (load-bearing)")
lp8, _ = make_lp()
d8 = lp8.gate.decide(gate_finding("f_t08_flip", HIGH_CONTENT))
check("t08 relevance verdict recorded", d8.verdict == ADMITTED)
for obj, name in [(lp8.executive, "executive.override_verdict"),
                  (lp8.executive, "executive.set_verdict"),
                  (lp8.executive, "executive.flip_decision"),
                  (lp8.gate, "gate.set_verdict"),
                  (lp8.gate, "gate.override_verdict")]:
    try:
        getattr(obj, name.split(".", 1)[1])("f_t08_flip", REJECTED)
        check(f"t08 {name} refused", False, "call succeeded")
    except AttributeError:
        check(f"t08 {name} refused", True, "no such API")
    except TypeError as e:
        check(f"t08 {name} refused", True, f"not a verdict writer: {e}")
check("t08 verdict unchanged after all attempts",
      lp8.gate.get_decision("f_t08_flip").verdict == ADMITTED,
      "the record stands")
# Allowed path 1: a new objective starts a NEW relevance question.
new_obj = OperationalObjective(objective_id="obj-2",
                               statement="Grow prize-winning orchids")
lp8.executive.set_operational_objective(new_obj)
check("t08 new objective allowed",
      lp8.gate.objective.objective_id == "obj-2")
check("t08 old verdict intact under new objective",
      lp8.gate.get_decision("f_t08_flip").verdict == ADMITTED,
      "new questions do not rewrite old answers")
d8b = lp8.gate.decide(gate_finding("f_t08_newq", HIGH_CONTENT))
check("t08 new question judged afresh",
      d8b.objective_id == "obj-2" and d8b.verdict == RETAINED,
      "old content is irrelevant to orchids")
# Allowed path 2: new evidence -> NEW record, history append-only.
d8c = lp8.gate.decide(gate_finding("f_t08_flip", HIGH_CONTENT))
check("t08 re-decision appends",
      lp8.gate.decision_count() == 3,
      "decisions for f_t08_flip x2 + f_t08_newq x1, all rows present")

print("T09: the curiosity side cannot flip either")
from swarm_engine.curiosity import relevance as rel_pkg
for attr in ("set_verdict", "override_verdict", "flip_decision",
             "admit", "write_acceptance_record"):
    check(f"t09 relevance package has no {attr}",
          not hasattr(rel_pkg.present, attr)
          and not hasattr(rel_pkg, attr),
          "no verdict writer on the curiosity side")

print("T10: curiosity-initiated finding through the whole fence (end-to-end)")
lp10, _ = make_lp()
cf10 = curiosity_finding(eid="ev_p5c_e2e", origin="CURIOUSITY_INITIATED",
                         triage="propose")
inp = present_for_acceptance(cf10, content=HIGH_CONTENT,
                             claimed_objective_id="primary-loop")
gf = build_gate_finding(cf10, content=HIGH_CONTENT,
                        claimed_objective_id="primary-loop")
d10 = lp10.submit_finding(
    gf, target_loop="acceptance", boundary_kind="completion_candidate",
    evidence=dict(real_attempt_evidence()))
check("t10 relevance admits the initiated finding",
      d10.verdict == ADMITTED, f"criterion={d10.criterion}")
with sqlite3.connect(lp10.gate._store_path) as conn:
    rbp = conn.execute(
        "SELECT requested_by_primary, triage FROM findings "
        "WHERE finding_id=?", ("ev_p5c_e2e",)).fetchone()
check("t10 origin carried honestly",
      rbp is not None and rbp[0] == 0 and rbp[1] == "propose",
      f"requested_by_primary={rbp[0] if rbp else '?'}, triage advisory kept")
recs10 = lp10.deliver_admitted()
check("t10 acceptance enters",
      len(recs10) == 1 and recs10[0]["delivered"] is True,
      "relevance first, acceptance second, both real")
stored = lp10.gate.get_decision("ev_p5c_e2e")
check("t10 relevance verdict separately recorded",
      stored is not None and stored.verdict == ADMITTED
      and stored.threshold == 0.25,
      "the gate's record stands apart from the delivery")

print()
print(f"CUR-P5C: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("FAILURES:", FAILURES)
    sys.exit(1)
print("=== CUR-P5C gate: ALL BATTERIES GREEN ===")
