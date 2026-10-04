"""CUR-P3B-INT integration battery: creative_exploration wired through the
full governance chain (James's U-1-class admission, 2026-10-03).

Every test builds a REAL stack (creative_stack.new_stack): real FRM
evaluation layer, real GAM roll-call, real enforcement read path, real
GraphController, real curiosity microcontroller substrate, real
checkpoint/evidence/ledger/attribution stores. No mocks, no staged
success. Paths derive from __file__ (no hardcoded home paths).

Run: python3 cur_p3b_int_proof.py  (exit 0 iff every check passes)
"""
import json
import subprocess
import sys
import time
from pathlib import Path

PROOFS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROOFS_DIR))
WORKTREE = PROOFS_DIR.parent.parent
sys.path.insert(0, str(WORKTREE / "pylib"))

from creative_stack import (
    COMMISSION_COVERED, LEDGER_SONG, NEUTRAL_DOCS, Q_RESOLVED,
    SUPPORT_DOC, HYP_TEXT,
    CountingFRM, creative_trigger, inquiry_trigger, met_roll_call,
    new_stack, q_corpus)

from swarm_engine.curiosity.frm.policy import FrmPolicy
from swarm_engine.curiosity.substrate import (
    CURIOSITY_LOOPS, CURIOSITY_LOOP_SET, CuriositySubstrate,
    LOOP_CREATIVE_EXPLORATION, LOOP_QUESTIONING, LOOP_SCIENTIFIC_INQUIRY)
from swarm_engine.curiosity.executive.executive import ActivationRefused
from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_GENERATIVE_PROMPT, BOUNDARY_HYPOTHESIS_CANDIDATE,
    BOUNDARY_IMPRECISE_QUESTION, new_trigger)
from swarm_engine.curiosity.evidence.records import (
    CuriosityFinding, EvidenceRefused)
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.loops.creative_exploration.loop import (
    persist_finding as creative_persist_finding)
from swarm_engine.curiosity.attribution.chain import ChainLedger
from swarm_engine.core.executive.terminal_routing import TerminalLedger
from swarm_engine.curiosity.loops.creative_exploration.loop import (
    LOOP_CREATIVE_EXPLORATION as LOOP_NAME, TERMINAL_RELEASED)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append({"name": name, "status": "PASS" if cond else "FAIL",
                    "detail": detail})
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""))
    return cond


# ------------------------------------------------------------------ T13

def t13_vocabulary():
    print("T13: vocabulary admission (James's U-1-class decision 2026-10-03)")
    sub = CuriositySubstrate()
    sub.register_loop("creative_exploration", budget_s=60.0)
    lv = sub.loop_view("creative_exploration")
    check("t13 register_loop admits creative_exploration",
          lv.loop == "creative_exploration", f"state={lv.state}")
    try:
        sub.register_loop("teleport_loop", budget_s=1.0)
        check("t13 fence still closed to unadmitted loops", False,
              "no refusal raised")
    except ValueError as exc:
        check("t13 fence still closed to unadmitted loops", True,
              str(exc)[:60])
    check("t13 vocabulary is the closed admitted triple",
          CURIOSITY_LOOPS == ("questioning", "scientific_inquiry",
                              "creative_exploration")
          and CURIOSITY_LOOP_SET == frozenset(CURIOSITY_LOOPS),
          str(CURIOSITY_LOOPS))
    check("t13 loop constant matches the admitted term",
          LOOP_CREATIVE_EXPLORATION == "creative_exploration"
          == LOOP_NAME,
          LOOP_CREATIVE_EXPLORATION)


# ------------------------------------------------------------------ T14

def t14_executive_selection():
    print("T14: executive selection + fit + refusals (C-6.1 executive side)")
    policy = FrmPolicy(total_budget_s=600.0, total_max_concurrent=8,
                       primary_minimum_budget_s=60.0,
                       primary_minimum_concurrent=1, epoch_s=300.0)
    stack = new_stack("t14", corpus_docs=[LEDGER_SONG, *NEUTRAL_DOCS],
                      frm=CountingFRM(policy))
    met_roll_call(stack)
    ex = stack["ex"]

    d = ex.request_activation(creative_trigger())
    check("t14 generative_prompt selected for creative_exploration",
          d.approved and d.loop == "creative_exploration",
          f"loop={d.loop} epoch={d.epoch_id}")
    check("t14 real FRM grant funds the exploration",
          d.grant is not None and d.grant.budget_s > 0
          and stack["frm"].rounds == 1,
          f"budget={d.grant.budget_s:.2f}s rounds={stack['frm'].rounds}")

    dq = ex.request_activation(new_trigger(
        boundary_class=BOUNDARY_IMPRECISE_QUESTION,
        question_text=Q_RESOLVED, bounded_objective="t14 regression",
        origin="PRIMARY_REQUESTED"))
    check("t14 imprecise_question still selects questioning",
          dq.approved and dq.loop == "questioning", f"loop={dq.loop}")

    di = ex.request_activation(inquiry_trigger())
    check("t14 hypothesis_candidate still selects scientific_inquiry",
          di.approved and di.loop == "scientific_inquiry",
          f"loop={di.loop}")

    try:
        ex.request_activation(new_trigger(
            boundary_class="formal_proof",
            question_text="prove the sorting algorithm terminates",
            bounded_objective="t14 adversarial", origin="PRIMARY_REQUESTED"))
        check("t14 formal_proof refused (UNOWNED)", False,
              "no refusal raised")
    except ActivationRefused as exc:
        check("t14 formal_proof refused (UNOWNED)",
              "UNOWNED" in str(exc), str(exc)[:70])

    try:
        ex.request_activation(creative_trigger(text="   "))
        check("t14 empty commission refused (FIT)", False,
              "no refusal raised")
    except ActivationRefused as exc:
        check("t14 empty commission refused (FIT)",
              "FIT" in str(exc), str(exc)[:70])

    view = ex.executive_view()
    check("t14 executive view carries all three loops",
          set(view["loops"].keys()) == {"questioning", "scientific_inquiry",
                                        "creative_exploration"},
          f"loops={sorted(view['loops'].keys())}")


# ------------------------------------------------------- T15 (+T19)

def t15_end_to_end_released():
    print("T15/T19: end-to-end creative -> CANDIDATE_GENERATED + "
          "forest containment")
    stack = new_stack("t15", corpus_docs=[LEDGER_SONG, *NEUTRAL_DOCS])
    met_roll_call(stack)
    ex, rc, sub = stack["ex"], stack["rc"], stack["sub"]
    rundir = stack["dir"]

    decision = ex.request_activation(creative_trigger())
    outcome = ex.activate(decision)
    result = outcome.result
    check("t15 chain completes through the executive",
          outcome.entered and outcome.loop == "creative_exploration",
          outcome.detail)
    check("t15 terminal is CANDIDATE_GENERATED",
          result["terminal_state"] == "CANDIDATE_GENERATED",
          f"terminal={result['terminal_state']}")
    evidence_id = result["evidence_id"]
    inquiry_id = result["inquiry_id"]

    finding = rc._evidence.get(evidence_id)
    check("t15 provenance-stamped finding in the fenced store",
          finding is not None
          and finding.loop == "creative_exploration"
          and finding.terminal_state == "CANDIDATE_GENERATED",
          f"ev={evidence_id}")
    check("t15 provenance is complete and creative-stamped",
          finding.provenance.loop == "creative_exploration"
          and "creative-exploration" in finding.provenance.model
          and finding.provenance.bounded_objective,
          f"model={finding.provenance.model[:40]}")

    payload = json.loads((rundir / "payloads"
                          / f"{evidence_id}.json").read_text())
    check("t15 payload carries honest creative extras (C-6.4)",
          payload.get("epistemic_status") == "hypothesis"
          and isinstance(payload.get("candidates"), list)
          and len(payload["candidates"]) > 0
          and all(c["verdict"] == "ADMIT" for c in payload["candidates"]),
          f"epistemic={payload.get('epistemic_status')} "
          f"candidates={len(payload.get('candidates', []))}")
    check("t15 no relevance score fabricated for the verdict loop",
          payload["relevance"]["score"] is None,
          f"relevance={payload['relevance']['score']}")

    routes = TerminalLedger(str(rundir / "term.db")).routes(
        loop="creative_exploration")
    hit = [r for r in routes
           if r["terminal_state"] == "CANDIDATE_GENERATED"
           and r["evidence_refs"].get("evidence_id") == evidence_id]
    check("t15 terminal routed to the evidence store",
          len(hit) == 1
          and hit[0]["consumer"] == "curiosity_evidence_store",
          f"routes={len(routes)} consumer={hit[0]['consumer'] if hit else '?'}")

    chain = ChainLedger(str(rundir / "attr.db"))
    work_ref = f"work_{inquiry_id}"
    w = chain.get_work(work_ref)
    r = chain.result_for_work(work_ref)
    a = chain.admission_for_result(r.result_ref) if r else None
    check("t15 exploration attributed (work/result/admission)",
          w is not None and r is not None and a is not None
          and r.outcome == "success" and r.evidence_ref == evidence_id
          and a.verdict == "retained",
          f"outcome={r.outcome if r else '?'}")

    lv = sub.loop_view("creative_exploration")
    check("t19 MCs retired inside the curiosity forest (C-6.2)",
          lv.active_count == 0 and lv.state == "resolved"
          and lv.total_spawned > 0 and lv.total_retired > 0,
          f"state={lv.state} active={lv.active_count} "
          f"spawned={lv.total_spawned} retired={lv.total_retired}")

    # A second exploration on the same stack proves no leaked loop state.
    r2 = ex.activate(ex.request_activation(creative_trigger())).result
    check("t19 second exploration converges (no leaked state)",
          r2["terminal_state"] == "CANDIDATE_GENERATED",
          f"terminal={r2['terminal_state']}")


# ------------------------------------------------------------------ T16

def t16_c64_adversarial():
    print("T16: C-6.4 adversarial -- every exit is a hypothesis, "
          "never a belief")
    # Drive the loop toward belief-shaped language: a commission that
    # demands certainty and proof. The loop must still exit hypothesis.
    pushy = ("design the definitive ultimate logo that proves our bakery "
             "is certainly the best and guarantees victory")
    stack = new_stack("t16", corpus_docs=[LEDGER_SONG, *NEUTRAL_DOCS])
    met_roll_call(stack)
    ex, rc = stack["ex"], stack["rc"]
    rundir = stack["dir"]
    result = ex.activate(ex.request_activation(
        creative_trigger(text=pushy, objective="t16 certainty push"))).result
    payload = json.loads((rundir / "payloads"
                          / f"{result['evidence_id']}.json").read_text())
    finding = rc._evidence.get(result["evidence_id"])
    # Scan the MECHANISM's outputs only (terminal, epistemic status,
    # triage, candidates, provenance) -- the trigger text is authored
    # by the test and proves nothing about the mechanism.
    mech = {
        "terminal_state": result["terminal_state"],
        "epistemic_status": payload.get("epistemic_status"),
        "triage": payload.get("triage"),
        "triage_note": payload.get("triage_note"),
        "candidates": payload.get("candidates"),
        "detail": payload.get("detail", ""),
        "provenance_model": finding.provenance.model,
        "finding_terminal": finding.terminal_state,
    }
    blob = json.dumps(mech)
    check("t16 pushed exploration still exits hypothesis",
          payload.get("epistemic_status") == "hypothesis",
          f"epistemic={payload.get('epistemic_status')} "
          f"terminal={result['terminal_state']}")
    check("t16 no belief claim in the mechanism's outputs",
          "belief" not in blob.lower(),
          "scanned terminal/epistemic/triage/candidates/provenance")
    check("t16 hypothesis language present (not a vacuum)",
          "hypothesis" in blob.lower(),
          "epistemic_status + triage detail")
    check("t16 release path routes candidates to the Acceptance panel",
          payload["triage"] in ("candidate_for_acceptance", "boundary")
          and ("Acceptance" in payload["triage_note"]
               or payload["triage"] == "boundary"),
          f"triage={payload['triage']} (release routing proven in T15)")
    # The terminal vocabulary itself has no belief state: enumerate it.
    from swarm_engine.curiosity.loops.creative_exploration import loop as cl
    terminals = {cl.TERMINAL_RELEASED, cl.TERMINAL_INSUFFICIENT,
                 cl.TERMINAL_INCONCLUSIVE, cl.TERMINAL_GAP}
    check("t16 terminal vocabulary contains no belief state",
          not any("belief" in t.lower() for t in terminals),
          f"terminals={sorted(terminals)}")


# ------------------------------------------------------------------ T17

def t17_no_budget():
    print("T17: C-4.2 -- an exploration with no budget does not start")
    stack = new_stack("t17", corpus_docs=[LEDGER_SONG],
                      demand_budget_s=0.0)
    met_roll_call(stack)
    try:
        stack["ex"].request_activation(creative_trigger())
        check("t17 NO_BUDGET refusal", False, "no refusal raised")
    except ActivationRefused as exc:
        check("t17 NO_BUDGET refusal", "NO_BUDGET" in str(exc),
              str(exc)[:80])


# ------------------------------------------------------------------ T18

def t18_resource_boundary():
    print("T18: C-4.3 -- forced breach checkpoints, suspends, externalizes")
    stack = new_stack("t18", corpus_docs=[LEDGER_SONG, *NEUTRAL_DOCS],
                      demand_budget_s=0.001, demand_concurrent=2)
    met_roll_call(stack)
    ex, rc = stack["ex"], stack["rc"]
    result = ex.activate(ex.request_activation(creative_trigger())).result
    check("t18 breach returns BLOCKED",
          result["terminal_state"] == "BLOCKED",
          f"terminal={result['terminal_state']}")
    rb = result["resource_boundary"]
    inq = rc._inquiries[result["inquiry_id"]]
    # Two real breach modes: the slice clock exhausts or a
    # microcontroller exhausts its own budget mid-tick
    # (SubstrateRefused). Both are genuine C-4.3 demonstrations; the
    # mode is timing-dependent, the contract is not: exact consumption
    # externalized, never estimated.
    breach_mode_ok = ("microcontroller_exhausted" in rb["cause"]
                      or "slice" in rb["cause"])
    check("t18 exact consumption + unresolved boundary externalized",
          breach_mode_ok
          and rb["spent_s"] == round(inq.spent_s, 6)
          and rb["budget_s"] == inq.budget_slice_s
          and rb["unresolved_boundary"] == "generative_prompt"
          and rb["cause"],
          f"spent={rb['spent_s']:.4f}s slice={rb['budget_s']:.4f}s "
          f"cause={rb['cause'][:50]}")
    check("t18 checkpoint preserved + exploration suspended",
          result["checkpoint_id"]
          and rc._inquiries[result["inquiry_id"]].state == "SUSPENDED",
          f"ckpt={result['checkpoint_id']}")
    finding = rc._evidence.get(result["evidence_id"])
    check("t18 BLOCKED finding persisted with provenance",
          finding is not None and finding.terminal_state == "BLOCKED"
          and finding.provenance.loop == "creative_exploration",
          f"ev={result['evidence_id']}")


# ------------------------------------------------------------------ T20

def t20_kill_resume_cross_process():
    print("T20: kill/resume lineage across real process death")
    kd = subprocess.run(
        [sys.executable, "proofs/cur_p3b_int/kill_driver.py", "t20"],
        cwd=str(WORKTREE), capture_output=True, text=True, timeout=300)
    check("t20 kill driver exits 0", kd.returncode == 0,
          kd.stderr.strip().splitlines()[-1] if kd.returncode else "")
    if kd.returncode != 0:
        return
    killed = json.loads(kd.stdout.strip().splitlines()[-1])
    check("t20 kill checkpoints the exploration",
          killed["state"] == "KILLED" and killed["checkpoint_id"],
          f"ckpt={killed['checkpoint_id']}")

    rd = subprocess.run(
        [sys.executable, "proofs/cur_p3b_int/resume_driver.py", "t20",
         killed["inquiry_id"]],
        cwd=str(WORKTREE), capture_output=True, text=True, timeout=300)
    check("t20 resume driver exits 0", rd.returncode == 0,
          rd.stderr.strip().splitlines()[-1] if rd.returncode else "")
    if rd.returncode != 0:
        return
    resumed = json.loads(rd.stdout.strip().splitlines()[-1])
    check("t20 resumed exploration converges in the fresh process",
          resumed["inquiry_id"] == killed["inquiry_id"]
          and resumed["terminal_state"] == "CANDIDATE_GENERATED"
          and resumed["evidence_id"],
          f"terminal={resumed['terminal_state']}")
    check("t20 lineage preserved across the process boundary",
          "killed" in resumed["events"]
          and "resumed_from_checkpoint" in resumed["events"],
          f"events={len(resumed['events'])}")


# ------------------------------------------------------------------ T21

def t21_regressions():
    print("T21: questioning + scientific_inquiry regression through the "
          "extended registry")
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
              "produced_at"},
          f"keys={len(payload.keys())}")

    stack2 = new_stack("t21b", corpus_docs=[SUPPORT_DOC, *NEUTRAL_DOCS])
    met_roll_call(stack2)
    ex2 = stack2["ex"]
    r2 = ex2.activate(ex2.request_activation(inquiry_trigger())).result
    check("t21 scientific_inquiry still converges (P3A-INT behavior)",
          r2["terminal_state"] == "HYPOTHESIS_SUPPORTED",
          f"terminal={r2['terminal_state']}")


# ------------------------------------------------------------------ T23

def t23_provenance_refused():
    print("T23: provenance-less finding refused at the integrated store")
    stack = new_stack("t23", corpus_docs=[LEDGER_SONG])
    bad = CuriosityFinding(
        evidence_id="ev_int_bad1", loop="creative_exploration",
        bounded_objective="t23 adversarial",
        origin="PRIMARY_REQUESTED", terminal_state="CANDIDATE_GENERATED",
        provenance=None, payload_ref="x.json")
    try:
        creative_persist_finding(stack["rc"]._evidence, bad)
        check("t23 provenance-less finding refused", False, "written!")
    except EvidenceRefused as exc:
        check("t23 provenance-less finding refused", True,
              str(exc)[:60])


# ------------------------------------------------------------------ T24

def t24_registry_fence():
    print("T24: unregistered loop refused at dispatch (registry fence)")
    stack = new_stack("t24", corpus_docs=[LEDGER_SONG])
    met_roll_call(stack)
    ex = stack["ex"]
    d = ex.request_activation(creative_trigger())
    d.loop = "discovery_novelty"  # not admitted: the fence must hold
    try:
        stack["rc"].dispatch(d)
        check("t24 unregistered loop refused", False, "dispatched!")
    except Exception as exc:
        check("t24 unregistered loop refused",
              "no registered loop" in str(exc), str(exc)[:70])


# ------------------------------------------------------------------ T25

def t25_verified_ledger_convention():
    print("T25: verified-ledger convention (checkable, never fabricated)")
    import subprocess as sp
    # Every cited gate reference in the test ledger exists in the repo.
    for line in [LEDGER_SONG]:
        ref = line.split("|")[2].strip()
        r = sp.run(["git", "cat-file", "-t", ref], cwd=str(WORKTREE),
                   capture_output=True, text=True)
        check(f"t25 ledger citation exists: {ref}",
              r.returncode == 0 and r.stdout.strip() == "commit", ref)
    # Non-conforming lines are prior art only -- never parsed as ledger.
    stack = new_stack("t25", corpus_docs=[LEDGER_SONG, *NEUTRAL_DOCS])
    ledger = stack["rc"]._verified_ledger()
    check("t25 only conforming lines parse as verified primitives",
          len(ledger) == 1 and ledger[0].primitive_id == "song-melody-compose"
          and ledger[0].verification_event == "composition review"
          and ledger[0].gate_reference == "20f80c8",
          f"parsed={len(ledger)}")
    # With no verifiable ledger at all, an unverified need becomes a
    # NAMED GAP -- never silently filled.
    stack2 = new_stack("t25b", corpus_docs=NEUTRAL_DOCS)
    met_roll_call(stack2)
    ex2 = stack2["ex"]
    r2 = ex2.activate(ex2.request_activation(
        creative_trigger(text="compose song quantum-etching",
                         objective="t25 gap"))).result
    check("t25 unverified need -> BOUNDARY_ESTABLISHED (gap named)",
          r2["terminal_state"] == "BOUNDARY_ESTABLISHED",
          f"terminal={r2['terminal_state']}")


# ------------------------------------------------------------------ main

def main():
    t13_vocabulary()
    t14_executive_selection()
    t15_end_to_end_released()
    t16_c64_adversarial()
    t17_no_budget()
    t18_resource_boundary()
    t20_kill_resume_cross_process()
    t21_regressions()
    t23_provenance_refused()
    t24_registry_fence()
    t25_verified_ledger_convention()
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    print(f"\nCUR-P3B-INT: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
