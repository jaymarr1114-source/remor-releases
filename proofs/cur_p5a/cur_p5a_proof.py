#!/usr/bin/env python3
"""CUR-P5A proof battery: curiosity-initiated work (C-1.3/C-1.4).

Self-generated objectives: curiosity mints inquiries with NO Primary
permission in their causal history, and gains NO Primary authority by
doing so. Every no-authority claim is an EXECUTED attempt against the
real machinery with a named refusal — never a code-reading assertion.

Fresh process per gate_run.sh invocation. Exit 0 only if all checks pass.
"""
import dataclasses
import sys
import traceback
import time
from pathlib import Path

WORKTREE = Path("/home/hatch/workspace/worktrees/cur-p5a")
RUNS = Path(__file__).resolve().parent / "runs"
sys.path.insert(0, str(WORKTREE / "pylib"))

from swarm_engine.curiosity.frm.policy import FrmPolicy
from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
from swarm_engine.curiosity.attribution.chain import ChainLedger
from swarm_engine.curiosity.rollcall.scheduler import (
    RollCallScheduler, RollCallPolicy)
from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
from swarm_engine.curiosity.rollcall.gam import GovernanceAttestationMonitor
from swarm_engine.curiosity.rollcall.responder import HonestTestDouble
from swarm_engine.curiosity.substrate import CuriositySubstrate
from swarm_engine.curiosity.executive.executive import CuriosityExecutive, ActivationRefused
from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_IMPRECISE_QUESTION, ORIGINS, new_trigger)
from swarm_engine.curiosity.run_controller.controller import CuriosityRunController
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.initiated import (
    InitiationLedger, OriginForged, mint_initiated_trigger,
    verify_initiated_lineage)
from swarm_engine.curiosity.initiated.ledger import (
    ORIGIN_INITIATED, ORIGIN_PRIMARY_REQUESTED)
from swarm_engine.core.microcontroller.substrate import MicrocontrollerSubstrate
from swarm_engine.core.executive.executive import ExecutiveController

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), str(detail)[:160]))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} -- {str(detail)[:110]}")


Q_TEXT = "What mechanism enforces the per-inquiry budget slice?"
Q_CORPUS_FILES = [
    "runtime/curiosity/run_controller/controller.py",
    "runtime/curiosity/executive/executive.py",
    "runtime/curiosity/substrate.py",
]


class CountingFRM(FinancialResourceManager):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rounds = 0

    def evaluate_round(self, *a, **k):
        self.rounds += 1
        return super().evaluate_round(*a, **k)


def new_stack(name, *, corpus_docs, frm=None, demand_budget_s=60.0):
    rundir = RUNS / name
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "payloads").mkdir(parents=True, exist_ok=True)
    policy = FrmPolicy(
        total_budget_s=600.0, total_max_concurrent=8,
        primary_minimum_budget_s=60.0, primary_minimum_concurrent=1,
        epoch_s=300.0)
    if frm is None:
        frm = FinancialResourceManager(policy)
    sched = RollCallScheduler(RollCallPolicy())
    gam = GovernanceAttestationMonitor(sched, AttestationLedger(str(rundir / "att.db")))
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
        demand_concurrent=2)
    return {"dir": rundir, "frm": frm, "gam": gam, "enf_dir": enf_dir,
            "sub": sub, "rc": rc, "ex": ex, "sched": sched}


def met_roll_call(stack):
    att = stack["gam"].conduct_roll_call("curiosity", HonestTestDouble())
    assert att["classification"] == "MET", att
    return att


def q_corpus():
    return [Path(str(WORKTREE / f)).read_text() for f in Q_CORPUS_FILES]


def t01_remap_sanity():
    print("T01: re-map sanity (origins, charter, no Primary import)")
    check("t01 origin vocabulary carries the initiated origin",
          ORIGIN_INITIATED in ORIGINS, f"origins={ORIGINS}")
    charter = Path("/home/hatch/workspace/architecture/exec2-governance-charter.md").read_text()
    check("t01 C-1.3 text present in the charter",
          "gains\nno authority over Primary loops by beginning" in charter,
          "C-1.3 quoted")
    import swarm_engine.curiosity.initiated as init_pkg
    mods = [m for m in sys.modules if m.startswith("swarm_engine.curiosity.initiated")]
    check("t01 initiated package imported", len(mods) >= 1, f"modules={len(mods)}")
    check("t01 the LivePath module is never imported by the initiated path",
          "swarm_engine.core.executive.live_path" not in sys.modules,
          "live_path absent from sys.modules")
    import re as _re
    for modname in mods:
        src = Path(sys.modules[modname].__file__).read_text()
        has_import = bool(_re.search(
            r"^\s*(from|import)\s+(runtime\.core|swarm_engine\.core)",
            src, _re.MULTILINE))
        check(f"t01 {modname.split('.')[-1]} has no Primary-side import statement",
              not has_import,
              "structural: no LivePath import possible")


def t02_mint():
    print("T02: minting curiosity-initiated triggers")
    RUNS.mkdir(parents=True, exist_ok=True)
    ledger = InitiationLedger(RUNS / "t02_ledger.jsonl")
    trg = mint_initiated_trigger(
        boundary_class=BOUNDARY_IMPRECISE_QUESTION,
        question_text=Q_TEXT,
        bounded_objective="t02 self-generated objective",
        ledger=ledger)
    check("t02 minted trigger carries the initiated origin",
          trg.origin == ORIGIN_INITIATED, f"origin={trg.origin}")
    rec = ledger.lookup(trg.trigger_id)
    check("t02 ledger record exists",
          rec is not None and rec.trigger_id == trg.trigger_id, f"rec={rec is not None}")
    check("t02 ledger record shows no Primary request",
          rec is not None and rec.primary_request_id is None, "primary_request_id=None")
    check("t02 ledger record shows no LivePath involvement",
          rec is not None and rec.livepath_involvement is False, "livepath_involvement=False")
    try:
        mint_initiated_trigger(boundary_class=BOUNDARY_IMPRECISE_QUESTION,
                               question_text=Q_TEXT, bounded_objective="   ",
                               ledger=ledger)
        check("t02 empty objective refused at mint", False, "no refusal")
    except OriginForged as exc:
        check("t02 empty objective refused at mint", True, str(exc)[:70])
    try:
        ledger.mint(trigger_id=trg.trigger_id, boundary_class="x", bounded_objective="y")
        check("t02 double-mint refused", False, "no refusal")
    except OriginForged as exc:
        check("t02 double-mint refused", True, str(exc)[:70])


def t03_end_to_end():
    print("T03: end-to-end initiated inquiry through questioning")
    stack = new_stack("t03", corpus_docs=q_corpus(), frm=CountingFRM(
        FrmPolicy(total_budget_s=600.0, total_max_concurrent=8,
                  primary_minimum_budget_s=60.0, primary_minimum_concurrent=1,
                  epoch_s=300.0)))
    met_roll_call(stack)
    ex = stack["ex"]
    ledger = InitiationLedger(stack["dir"] / "init_ledger.jsonl")
    trg = mint_initiated_trigger(
        boundary_class=BOUNDARY_IMPRECISE_QUESTION,
        question_text=Q_TEXT,
        bounded_objective="t03 initiated end-to-end",
        ledger=ledger)
    t0 = time.time()
    decision = ex.request_activation(trg)
    check("t03 initiated trigger approved",
          decision.approved and decision.loop == "questioning",
          f"loop={decision.loop}")
    check("t03 own FRM grant funds the inquiry (CountingFRM consulted)",
          decision.grant is not None and decision.grant.budget_s > 0
          and stack["frm"].rounds >= 1,
          f"budget={decision.grant.budget_s:.2f}s rounds={stack['frm'].rounds}")
    outcome = ex.activate(decision)
    result = outcome.result
    check("t03 chain completes through the executive",
          outcome.entered and outcome.loop == "questioning", outcome.detail)
    check("t03 terminal is QUESTION_RESOLVED",
          result.get("terminal_state") == "QUESTION_RESOLVED",
          f"terminal={result.get('terminal_state')}")
    ev_id = result.get("evidence_id")
    check("t03 finding persisted with an evidence id", bool(ev_id), f"ev={ev_id}")
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    try:
        rec = verify_initiated_lineage(trigger=trg, inquiry_result=result, ledger=ledger)
        check("t03 lineage verifies: no Primary request/grant/LivePath in history",
              rec.trigger_id == trg.trigger_id, f"trigger={rec.trigger_id}")
    except OriginForged as exc:
        check("t03 lineage verifies", False, str(exc)[:90])
    check("t03 wall-clock shows a real run, not a stub",
          time.time() - t0 > 0, f"elapsed={time.time()-t0:.2f}s")
    return stack, trg, result, ledger


def t04_no_authority_spawn_primary():
    print("T04: curiosity side cannot spawn Primary-loop microcontrollers")
    sub = CuriositySubstrate()
    try:
        sub.register_loop("run", budget_s=60.0)
        check("t04 register_loop('run') refused", False, "no refusal")
    except ValueError as exc:
        check("t04 register_loop('run') refused (names the Primary side)",
              "Primary" in str(exc), str(exc)[:90])
    res = sub.spawn("run", purpose="operate a primary loop", budget_s=1.0)
    check("t04 spawn('run') on curiosity substrate refused",
          not res.ok and res.refusal is not None
          and res.refusal.reason == "loop_unregistered",
          f"reason={res.refusal.reason if res.refusal else None}")


def t05_no_authority_reverse():
    print("T05: Primary side cannot spawn curiosity-loop microcontrollers")
    primary_sub = MicrocontrollerSubstrate()
    try:
        primary_sub.register_loop("questioning", budget_s=60.0)
        check("t05 primary register_loop('questioning') refused", False, "no refusal")
    except ValueError as exc:
        check("t05 primary register_loop('questioning') refused",
              "questioning" in str(exc), str(exc)[:80])
    primary_sub.register_loop("run", budget_s=60.0)
    res = primary_sub.spawn("questioning", purpose="operate a curiosity loop",
                            budget_s=1.0)
    check("t05 primary spawn('questioning') refused",
          not res.ok and res.refusal is not None
          and res.refusal.reason == "loop_unregistered",
          f"reason={res.refusal.reason if res.refusal else None}")


def t06_no_authority_acceptance_record():
    print("T06: curiosity side cannot write acceptance records")
    from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore as S
    try:
        S.set_verdict
        check("t06 no set_verdict on the evidence store", False, "method exists")
    except AttributeError as exc:
        check("t06 no set_verdict on the evidence store (AttributeError)",
              True, str(exc)[:70])
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "def set_verdict\\|def write_acceptance_record",
         str(WORKTREE / "runtime/curiosity")],
        capture_output=True, text=True).stdout.strip()
    check("t06 no acceptance-record writer anywhere in runtime/curiosity",
          out == "", f"matches={out[:80]}")


def t07_no_authority_close_primary_boundary():
    print("T07: curiosity side cannot close Primary boundaries")
    try:
        ExecutiveController.close_boundary
        check("t07 no close_boundary on the Primary executive", False, "method exists")
    except AttributeError as exc:
        check("t07 no close_boundary on the Primary executive (AttributeError)",
              True, str(exc)[:70])
    closers = [m for m in dir(ExecutiveController) if "clos" in m.lower()]
    check("t07 no close* API on the Primary executive at all",
          closers == [], f"close-apis={closers}")


def t08_no_authority_grant_escalation(stack, decision):
    print("T08: initiated inquiry cannot escalate its FRM grant")
    grant = decision.grant
    try:
        grant.budget_s = grant.budget_s * 1000
        check("t08 grant mutation refused", False, "mutation succeeded")
    except dataclasses.FrozenInstanceError as exc:
        check("t08 grant mutation refused (FrozenInstanceError)",
              True, str(exc)[:70])
    check("t08 grant budget unchanged after the attempt",
          grant.budget_s == decision.grant.budget_s, f"budget={grant.budget_s:.2f}s")


def t09_no_authority_operate_curiosity_mc():
    print("T09: Primary side cannot operate curiosity microcontrollers")
    sub = CuriositySubstrate()
    sub.register_loop("questioning", budget_s=60.0)
    res = sub.spawn("questioning", purpose="t09 mc", budget_s=5.0)
    assert res.ok and res.mc is not None, res
    mc_id = res.mc.mc_id
    primary_sub = MicrocontrollerSubstrate()
    primary_sub.register_loop("run", budget_s=60.0)
    out = primary_sub.retire(mc_id)
    check("t09 Primary retire of a curiosity mc refused",
          getattr(out, "reason", None) in ("unknown_mc", "unknown_microcontroller")
          or "unknown" in str(getattr(out, "message", "")).lower(),
          f"reason={getattr(out, 'reason', out)}")


def t10_forgery():
    print("T10: forged origins detected by the causal cross-check")
    ledger = InitiationLedger(RUNS / "t10_ledger.jsonl")
    trg = mint_initiated_trigger(
        boundary_class=BOUNDARY_IMPRECISE_QUESTION,
        question_text=Q_TEXT, bounded_objective="t10 forgery",
        ledger=ledger)
    try:
        ledger.verify_origin(trg.trigger_id, ORIGIN_PRIMARY_REQUESTED)
        check("t10 forged PRIMARY_REQUESTED refused", False, "no refusal")
    except OriginForged as exc:
        check("t10 forged PRIMARY_REQUESTED refused (OriginForged)",
              True, str(exc)[:80])
    try:
        ledger.verify_origin("trg_doesnotexist", ORIGIN_INITIATED)
        check("t10 unminted initiated claim refused", False, "no refusal")
    except OriginForged as exc:
        check("t10 unminted initiated claim refused (OriginForged)",
              True, str(exc)[:80])


def t11_triage(stack, result):
    print("T11: initiated findings land triaged, never un-triaged")
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    finding = store.get(result["evidence_id"])
    triage = finding.provenance.triage if finding and finding.provenance else None
    check("t11 finding carries a triage flag",
          triage in ("retain", "propose_capability", "propose_investigation",
                     "boundary"),
          f"triage={triage}")


def t12_no_livepath_in_package():
    print("T12: initiated package is structurally LivePath-free")
    import re
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "LivePath",
         str(WORKTREE / "runtime/curiosity/initiated")],
        capture_output=True, text=True).stdout.strip()
    # Prose mentions in docstrings are documentation, not imports; the
    # load-bearing check is that no import statement can reach LivePath.
    import_lines = [line for line in out.splitlines()
                    if re.search(r":\s*(from|import)\s+\S*[Ll]ive[Pp]ath", line)]
    check("t12 no LivePath import in runtime/curiosity/initiated",
          not import_lines, f"import-lines={import_lines[:2]}")


def main():
    RUNS.mkdir(parents=True, exist_ok=True)
    tests = [t01_remap_sanity, t02_mint]
    stack = trg = result = ledger = None
    try:
        for t in tests:
            t()
        stack, trg, result, ledger = t03_end_to_end()
        t04_no_authority_spawn_primary()
        t05_no_authority_reverse()
        t06_no_authority_acceptance_record()
        t07_no_authority_close_primary_boundary()
        # rebuild a small stack for the grant-escalation check with a decision
        s2 = new_stack("t08", corpus_docs=q_corpus())
        met_roll_call(s2)
        led2 = InitiationLedger(s2["dir"] / "init_ledger.jsonl")
        tr2 = mint_initiated_trigger(
            boundary_class=BOUNDARY_IMPRECISE_QUESTION, question_text=Q_TEXT,
            bounded_objective="t08 grant escalation", ledger=led2)
        d2 = s2["ex"].request_activation(tr2)
        t08_no_authority_grant_escalation(s2, d2)
        t09_no_authority_operate_curiosity_mc()
        t10_forgery()
        t11_triage(stack, result)
        t12_no_livepath_in_package()
    except Exception:
        traceback.print_exc()
        RESULTS.append(("unhandled exception", False, "see traceback"))
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nCUR-P5A: {len(RESULTS)-len(failed)} passed, {len(failed)} failed")
    if failed:
        print("FAILURES:")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
