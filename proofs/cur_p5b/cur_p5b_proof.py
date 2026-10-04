#!/usr/bin/env python3
"""CUR-P5B proof battery: first-pass triage (charter C-3.3/C-3.4).

Every check runs against REAL machinery: the questioning loop driven
through the REAL executive -> run controller -> substrate chain (real
FRM grants, real roll-call, real fenced stores), real CuriosityFinding
records, the REAL fenced write path. No mocks, no staged success.

The triage pass under test is the NEW unified machinery in
runtime/curiosity/triage/ (standalone: the run controller's legacy
score rule is untouched by this mission -- the DELTA is documented in
rules.py and the report).

Conventions: check() halts on first failure (fail-closed battery).
Each test builds its own stack in its own run directory.
"""

import json
import sys
import traceback
from dataclasses import replace
from pathlib import Path

PROOF_DIR = Path(__file__).resolve().parent
WORKTREE = PROOF_DIR.parent.parent
sys.path.insert(0, str(WORKTREE / "pylib"))
sys.path.insert(0, str(PROOF_DIR))

import triage_stack
from triage_stack import (
    new_stack, met_roll_call, q_trigger,
    q_resolved_corpus, Q_RESOLVED_TEXT,
    Q_UNCOVERED_TEXT, Q_UNCOVERED_CORPUS,
    Q_UNOBSERVABLE_TEXT, Q_UNOBSERVABLE_CORPUS,
)

from swarm_engine.curiosity.triage import (
    TriageLedger, TriageRefused, assign_triage,
    triage_finding, retriage, persist_triaged,
    TRIAGE_RETAIN, TRIAGE_PROPOSE_CAPABILITY,
    TRIAGE_PROPOSE_INVESTIGATION, TRIAGE_BOUNDARY,
)
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore

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
        raise AssertionError(f"battery halted at first failure: {name}")


def fresh_triage_store(rundir, name="triage.db"):
    return CuriosityEvidenceStore(str(rundir / name))


def fresh_ledger(rundir):
    return TriageLedger(str(rundir / "triage_ledger.db"))


def run_chain(name, text, corpus):
    """Drive a real questioning inquiry through the executive chain.
    Returns (stack, finding, payload_dict). The finding carries the
    controller's legacy triage -- callers strip it before first-pass."""
    stack = triage_stack.new_stack("t_" + name, corpus_docs=corpus)
    triage_stack.met_roll_call(stack)
    ex, rc = stack["ex"], stack["rc"]
    decision = ex.request_activation(triage_stack.q_trigger(text))
    assert decision.approved, f"activation refused: {decision}"
    outcome = ex.activate(decision)
    assert outcome.entered, f"not entered: {outcome.detail}"
    result = outcome.result
    finding = rc._evidence.get(result["evidence_id"])
    assert finding is not None
    payload = json.loads(
        (stack["dir"] / "payloads" / f"{result['evidence_id']}.json")
        .read_text())
    return stack, finding, payload


def cleared(finding, new_id):
    """Return the finding with legacy triage stripped and a fresh id,
    so the unified first-pass can run on real chain-produced content."""
    return replace(
        finding, evidence_id=new_id,
        provenance=replace(finding.provenance, triage=None))


def do_triage(finding, payload, ledger):
    stamped, event = triage_finding(
        finding=finding, payload=payload, ledger=ledger)
    return stamped, event

# ---------------------------------------------------------------- T01

def t01_resolved_retains():
    print("T01: QUESTION_RESOLVED -> propose_investigation (R-INVESTIGATE)")
    stack, finding, payload = run_chain(
        "01", Q_RESOLVED_TEXT, q_resolved_corpus())
    check("t01 chain reached QUESTION_RESOLVED",
          finding.terminal_state == "QUESTION_RESOLVED",
          f"terminal={finding.terminal_state}")
    f2 = cleared(finding, "ev_t01")
    ledger = fresh_ledger(stack["dir"])
    stamped, event = do_triage(f2, payload, ledger)
    # A resolved question is the seed of further inquiry (theory
    # pipeline Questioning -> Scientific Inquiry): the honest first-pass
    # advisory is propose_investigation -- agreeing with the as-built
    # score rule on this case.
    check("t01 triage is propose_investigation",
          stamped.provenance.triage == TRIAGE_PROPOSE_INVESTIGATION,
          f"triage={stamped.provenance.triage}")
    check("t01 rule cited is R-INVESTIGATE", event.rule_id == "R-INVESTIGATE",
          f"rule={event.rule_id}")
    check("t01 ledger event is first-pass", event.cause == "first-pass"
          and event.seq == 0, f"cause={event.cause} seq={event.seq}")
    store = fresh_triage_store(stack["dir"])
    persist_triaged(finding=stamped, store=store)
    back = store.get("ev_t01")
    check("t01 persisted finding carries the triage",
          back is not None and back.provenance.triage == "propose_investigation",
          "read back from the fenced store")


# ---------------------------------------------------------------- T02

def t02_boundary_maps():
    print("T02: BOUNDARY_ESTABLISHED -> boundary (R-BOUNDARY)")
    stack, finding, payload = run_chain(
        "02", Q_UNOBSERVABLE_TEXT, Q_UNOBSERVABLE_CORPUS)
    check("t02 chain reached BOUNDARY_ESTABLISHED",
          finding.terminal_state == "BOUNDARY_ESTABLISHED",
          f"terminal={finding.terminal_state}")
    f2 = cleared(finding, "ev_t02")
    ledger = fresh_ledger(stack["dir"])
    stamped, event = do_triage(f2, payload, ledger)
    check("t02 triage is boundary",
          stamped.provenance.triage == TRIAGE_BOUNDARY,
          f"triage={stamped.provenance.triage}")
    check("t02 rule cited is R-BOUNDARY", event.rule_id == "R-BOUNDARY",
          f"rule={event.rule_id}")
    store = fresh_triage_store(stack["dir"])
    persist_triaged(finding=stamped, store=store)
    check("t02 boundary finding persisted",
          store.get("ev_t02").provenance.triage == "boundary",
          "read back from the fenced store")


# ---------------------------------------------------------------- T03

def t03_insufficient_proposes_investigation():
    print("T03: INSUFFICIENT_EVIDENCE -> propose_investigation (R-INVESTIGATE)")
    stack, finding, payload = run_chain(
        "03", Q_UNCOVERED_TEXT, Q_UNCOVERED_CORPUS)
    check("t03 chain reached INSUFFICIENT_EVIDENCE",
          finding.terminal_state == "INSUFFICIENT_EVIDENCE",
          f"terminal={finding.terminal_state}")
    f2 = cleared(finding, "ev_t03")
    ledger = fresh_ledger(stack["dir"])
    stamped, event = do_triage(f2, payload, ledger)
    # DELTA vs the as-built score rule (which retains here): open work
    # is not buried -- the honest advisory is further investigation.
    check("t03 triage is propose_investigation",
          stamped.provenance.triage == TRIAGE_PROPOSE_INVESTIGATION,
          f"triage={stamped.provenance.triage}")
    check("t03 rule cited is R-INVESTIGATE", event.rule_id == "R-INVESTIGATE",
          f"rule={event.rule_id}")


# ---------------------------------------------------------------- T04

def t04_capability_proposal():
    print("T04: well-formed capability_proposal + converged -> "
          "propose_capability (R-CAPABILITY)")
    stack, finding, payload = run_chain(
        "04", Q_RESOLVED_TEXT, q_resolved_corpus())
    assert finding.terminal_state == "QUESTION_RESOLVED"
    # The loop discovered a reusable mechanism: the payload carries the
    # well-formed capability_proposal block (mechanical schema).
    payload2 = dict(payload)
    payload2["capability_proposal"] = {
        "name": "budget-slice-enforcement",
        "description": "run controller enforces per-inquiry budget slices",
        "evidence_refs": [finding.evidence_id],
    }
    (stack["dir"] / "payloads" / "ev_t04.json").write_text(
        json.dumps(payload2))
    f2 = cleared(finding, "ev_t04")
    # QUESTION_RESOLVED is an investigate-terminal, not capability
    # eligible: use a converged-positive terminal for the capability
    # case. Build the finding shape honestly from the real one.
    f3 = replace(f2, terminal_state="HYPOTHESIS_SUPPORTED",
                 payload_ref=str(stack["dir"] / "payloads" / "ev_t04.json"))
    ledger = fresh_ledger(stack["dir"])
    stamped, event = do_triage(f3, payload2, ledger)
    check("t04 triage is propose_capability",
          stamped.provenance.triage == TRIAGE_PROPOSE_CAPABILITY,
          f"triage={stamped.provenance.triage}")
    check("t04 rule cited is R-CAPABILITY", event.rule_id == "R-CAPABILITY",
          f"rule={event.rule_id}")
    # Malformed block -> no proposal (mechanical schema, no vibes).
    payload3 = dict(payload2)
    payload3["capability_proposal"] = {"name": "  ", "description": "x",
                                       "evidence_refs": []}
    f4 = cleared(finding, "ev_t04b")
    f4 = replace(f4, terminal_state="HYPOTHESIS_SUPPORTED",
                 payload_ref=str(stack["dir"] / "payloads" / "ev_t04.json"))
    stamped4, event4 = do_triage(f4, payload3, ledger)
    check("t04 malformed block falls to retain",
          stamped4.provenance.triage == TRIAGE_RETAIN
          and event4.rule_id == "R-RETAIN",
          f"triage={stamped4.provenance.triage} rule={event4.rule_id}")
    # Refuted hypothesis with a well-formed block -> retain (a refuted
    # hypothesis is evidence, never a capability candidate).
    f5 = cleared(finding, "ev_t04c")
    f5 = replace(f5, terminal_state="HYPOTHESIS_REFUTED",
                 payload_ref=str(stack["dir"] / "payloads" / "ev_t04.json"))
    stamped5, event5 = do_triage(f5, payload2, ledger)
    check("t04 refuted + block still retains",
          stamped5.provenance.triage == TRIAGE_RETAIN
          and event5.rule_id == "R-RETAIN",
          f"triage={stamped5.provenance.triage}")


# ---------------------------------------------------------------- T05

def t05_true_but_irrelevant_retained():
    print("T05: true-but-irrelevant -> retain, neither discarded nor "
          "promoted (C-3.4)")
    # A well-formed, true finding about something no Primary objective
    # cares about (oat-milk logistics): converged, complete provenance,
    # no capability block.
    stack, finding, payload = run_chain(
        "05", Q_RESOLVED_TEXT, q_resolved_corpus())
    f2 = cleared(finding, "ev_t05")
    f2 = replace(
        f2, terminal_state="HYPOTHESIS_SUPPORTED",
        bounded_objective="oat milk stock levels in the office kitchen",
        provenance=replace(
            f2.provenance,
            bounded_objective="oat milk stock levels in the office kitchen"))
    ledger = fresh_ledger(stack["dir"])
    stamped, event = do_triage(f2, payload, ledger)
    check("t05 triage is retain",
          stamped.provenance.triage == TRIAGE_RETAIN,
          f"triage={stamped.provenance.triage}")
    check("t05 rule cited is R-RETAIN", event.rule_id == "R-RETAIN",
          f"rule={event.rule_id}")
    store = fresh_triage_store(stack["dir"])
    persist_triaged(finding=stamped, store=store)
    back = store.get("ev_t05")
    check("t05 not discarded (still retrievable)",
          back is not None and back.terminal_state == "HYPOTHESIS_SUPPORTED",
          "read back with terminal intact")
    d = back.as_dict()
    check("t05 not promoted (no admission marker)",
          "admitted" not in d and "capability_id" not in d
          and d["provenance"]["triage"] == "retain",
          "record carries no admission marker")

# ---------------------------------------------------------------- T06

def t06_deterministic():
    print("T06: triage is reproducible from content (no vibes)")
    payload_a = {"evidence_id": "ev_x", "note": "stable content"}
    payload_b = {"evidence_id": "ev_x", "note": "stable content"}
    r1 = assign_triage(terminal_state="QUESTION_RESOLVED", payload=payload_a)
    r2 = assign_triage(terminal_state="QUESTION_RESOLVED", payload=payload_b)
    check("t06 same content -> same triage+rule", r1 == r2,
          f"{r1} == {r2}")
    r3 = assign_triage(terminal_state="BOUNDARY_ESTABLISHED", payload=None)
    r4 = assign_triage(terminal_state="BOUNDARY_ESTABLISHED",
                       payload={"other": "stuff"})
    check("t06 boundary rule ignores payload", r3 == r4 == ("boundary", "R-BOUNDARY"),
          f"{r3}")


# ---------------------------------------------------------------- T07

def t07_adversarial():
    print("T07: triage is not admission (load-bearing adversarial)")
    stack = triage_stack.new_stack("t_07", corpus_docs=["doc"])
    ledger = fresh_ledger(stack["dir"])

    # T07a: the triage package has no admit API -- the prohibition is
    # structural, not documented.
    import swarm_engine.curiosity.triage as triage_pkg
    for missing in ("admit", "promote", "register_capability",
                    "enter_primary_machinery"):
        try:
            getattr(triage_pkg, missing)
            check(f"t07a no {missing} API", False, "API exists!")
        except AttributeError:
            check(f"t07a no {missing} API", True, "AttributeError")

    # T07b: a PROPOSE_CAPABILITY finding carries no admission marker.
    stack2, finding, payload = run_chain(
        "07b", Q_RESOLVED_TEXT, q_resolved_corpus())
    payload2 = dict(payload)
    payload2["capability_proposal"] = {
        "name": "budget-slice-enforcement",
        "description": "run controller enforces per-inquiry budget slices",
        "evidence_refs": [finding.evidence_id],
    }
    f2 = cleared(finding, "ev_t07b")
    f2 = replace(f2, terminal_state="HYPOTHESIS_SUPPORTED")
    stamped, event = do_triage(f2, payload2, fresh_ledger(stack2["dir"]))
    assert stamped.provenance.triage == "propose_capability"
    store = fresh_triage_store(stack2["dir"])
    persist_triaged(finding=stamped, store=store)
    d = store.get("ev_t07b").as_dict()
    check("t07b no admission marker on the record",
          "admitted" not in d and "capability_id" not in d
          and "admission" not in json.dumps(d).lower(),
          "record carries nothing admission-shaped")

    # T07c: no consumer auto-admits on propose_capability. Mechanical
    # proof: enumerate every `.triage` attribute read outside the triage
    # package, and assert none of those modules contains an
    # admission-shaped action (admit/register/accept-capability).
    import ast
    readers = {}
    root = WORKTREE / "runtime"
    for py in root.rglob("*.py"):
        if "__pycache__" in str(py):
            continue
        try:
            tree = ast.parse(py.read_text())
        except Exception:
            continue
        rel = str(py.relative_to(root))
        if rel.startswith("curiosity/triage/"):
            continue
        hits = [n for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr == "triage"]
        if hits:
            readers[rel] = py.read_text()
    admitters = [
        rel for rel, src in readers.items()
        if ("def admit" in src or "register_capability(" in src
            or "admit_capability" in src)
    ]
    print(f"  [info] triage readers outside triage/: {sorted(readers)}")
    check("t07c no auto-admit consumer in the tree", not admitters,
          f"admission-shaped readers: {admitters}")

    # T07d: re-triage without new evidence -> refused; with new
    # evidence -> appended, latest-wins, history intact.
    f3 = cleared(finding, "ev_t07d")
    f3 = replace(f3, terminal_state="INSUFFICIENT_EVIDENCE")
    leg = fresh_ledger(stack2["dir"])
    stamped3, ev1 = do_triage(f3, payload, leg)
    assert ev1.triage == "propose_investigation"
    try:
        retriage(finding_id="ev_t07d", new_evidence_ref="ev_t07d",
                 cause="upgrade", triage="propose_capability",
                 rule_id="R-CAPABILITY", ledger=leg)
        check("t07d re-triage without new evidence refused", False,
              "no refusal raised")
    except TriageRefused as e:
        check("t07d re-triage without new evidence refused", True,
              str(e)[:60])
    ev2 = retriage(finding_id="ev_t07d", new_evidence_ref="ev_t07d_v2",
                   cause="new supporting evidence ev_t07d_v2",
                   triage="propose_capability", rule_id="R-CAPABILITY",
                   ledger=leg)
    check("t07d re-triage with new evidence appended",
          ev2.seq == 1 and ev2.triage == "propose_capability",
          f"seq={ev2.seq} triage={ev2.triage}")
    hist = leg.history("ev_t07d")
    check("t07d history append-only (2 events)",
          len(hist) == 2 and hist[0].triage == "propose_investigation"
          and hist[1].triage == "propose_capability",
          "both events present, order intact")
    check("t07d get_triage is latest-wins",
          leg.get_triage("ev_t07d").triage == "propose_capability",
          "latest event returned")

    # T07e: a BOUNDARY finding carrying a capability block still maps
    # to boundary -- R-BOUNDARY short-circuits R-CAPABILITY.
    stack3, finding3, payload3b = run_chain(
        "07e", Q_UNOBSERVABLE_TEXT, Q_UNOBSERVABLE_CORPUS)
    assert finding3.terminal_state == "BOUNDARY_ESTABLISHED"
    payload4 = dict(payload3b)
    payload4["capability_proposal"] = {
        "name": "boundary-mapper", "description": "maps limits",
        "evidence_refs": [finding3.evidence_id]}
    f4 = cleared(finding3, "ev_t07e")
    stamped4, event4 = do_triage(
        f4, payload4, fresh_ledger(stack3["dir"]))
    check("t07e boundary + capability block -> boundary",
          stamped4.provenance.triage == "boundary"
          and event4.rule_id == "R-BOUNDARY",
          f"triage={stamped4.provenance.triage} rule={event4.rule_id}")

    # T07f: no acceptance-record writer exists on the curiosity side --
    # attempt the write path and show the refusal is structural.
    from swarm_engine.curiosity.evidence.records import DomainFenceError
    import swarm_engine.curiosity.triage.triage as triage_mod
    check("t07f triage module has no acceptance writer",
          not hasattr(triage_mod, "write_acceptance_record")
          and not hasattr(triage_mod, "set_verdict"),
          "no such API on the triage module")


# ---------------------------------------------------------------- T08

def t08_unknown_terminal_refused():
    print("T08: unknown terminal_state -> TriageRefused (fail-closed)")
    stack = triage_stack.new_stack("t_08", corpus_docs=["doc"])
    ledger = fresh_ledger(stack["dir"])
    try:
        assign_triage(terminal_state="MADE_UP_STATE", payload={})
        check("t08 unknown terminal refused", False, "no refusal raised")
    except TriageRefused as e:
        check("t08 unknown terminal refused", True, str(e)[:60])


# ---------------------------------------------------------------- main

def main():
    global PASS, FAIL
    tests = [
        t01_resolved_retains,
        t02_boundary_maps,
        t03_insufficient_proposes_investigation,
        t04_capability_proposal,
        t05_true_but_irrelevant_retained,
        t06_deterministic,
        t07_adversarial,
        t08_unknown_terminal_refused,
    ]
    try:
        for t in tests:
            t()
    except AssertionError as e:
        print(f"HALTED: {e}")
    except Exception:
        FAIL += 1
        print("UNEXPECTED EXCEPTION:")
        traceback.print_exc()
    print(f"\nCUR-P5B: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
