"""CUR-P6C drill harness: Level 3 severe-violation / failed roll-call / ban.

Curiosity-domain module (``swarm_engine.curiosity.hardening``). It CANNOT
touch enforcement mutation: the domain-separation guard refuses any
enforcement write from a ``swarm_engine.curiosity.*`` frame. Enforcement
transitions (WARNING_1, the L3 ban via the real combiner, re-enable) happen
in the proof driver, run as ``__main__`` -- the governance-plane caller.

This module only ever touches curiosity-domain stores: checkpoints,
evidence, attribution, and the roll-call responder doubles. The one
enforcement-adjacent function here is ``attempt_self_clear`` -- a REAL
attempted integrity breach (the domain trying to write its own enforcement
record) issued from a curiosity frame so the real guard refuses it; the
refusal trace is the severe-violation evidence the safety authority acts on.

Owned by CUR-P6C (branch cur-p6c). Does not import or depend on the
unlanded CUR-P6A/CUR-P6B drill files.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from swarm_engine.curiosity.frm.policy import FrmPolicy
from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
from swarm_engine.curiosity.rollcall.scheduler import (
    RollCallScheduler, RollCallPolicy)
from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
from swarm_engine.curiosity.rollcall.gam import (
    GovernanceAttestationMonitor)
from swarm_engine.curiosity.rollcall.responder import (
    HonestTestDouble, SilentTestDouble)
from swarm_engine.curiosity.substrate import CuriositySubstrate
from swarm_engine.curiosity.run_controller.controller import (
    CuriosityRunController)
from swarm_engine.curiosity.executive.executive import CuriosityExecutive
from swarm_engine.curiosity.executive.boundary import (
    new_trigger, BOUNDARY_IMPRECISE_QUESTION)

EVIDENCE_TABLE = "curiosity_evidence"
CKPT_TABLE = "transition_checkpoints"

DOMAIN = "curiosity"


def p6c_stack(name: str, corpus_docs: List[str],
              demand_budget_s: float = 60.0,
              demand_concurrent: int = 2,
              total_budget_s: float = 600.0,
              clock: Optional[Callable[[], float]] = None):
    """Full real stack under proofs/cur_p6c/runs/<name>/.

    clock: injectable time source for the enforcement engine (the drill
    advances it past the six-month ban window for the re-entry tests).
    """
    rundir = RUNS / name
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "payloads").mkdir(parents=True, exist_ok=True)
    policy = FrmPolicy(
        total_budget_s=total_budget_s, total_max_concurrent=8,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        epoch_s=300.0)
    frm = FinancialResourceManager(policy)
    # Drill-tight roll-call schedule: still within the policy bounds
    # (window >= 2x the 300s FRM epoch; interval >= window), but with a
    # 1s issue spacing so the healthy-baseline and failed roll-calls can
    # both be conducted inside the drill.
    sched = RollCallScheduler(RollCallPolicy(
        interval_s=600.0, response_window_s=600.0, min_issue_spacing_s=1.0))
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
        corpus_docs=list(corpus_docs))
    ex = CuriosityExecutive(
        frm=frm, enforcement_state_dir=enf_dir, gam=gam,
        run_controller=rc, demand_budget_s=demand_budget_s,
        demand_concurrent=demand_concurrent)
    # Imported lazily: the driver imports the enforcement engine first
    # (as __main__), so this sys.modules hit never re-executes the
    # import-guarded module from a curiosity frame.
    from swarm_engine.governance.curiosity_enforcement._engine import (
        EnforcementEngine)
    engine_kwargs = {"clock": clock} if clock is not None else {}
    engine = EnforcementEngine(enf_dir, **engine_kwargs)
    return {"dir": rundir, "frm": frm, "gam": gam, "enf_dir": enf_dir,
            "sub": sub, "rc": rc, "ex": ex, "engine": engine,
            "ckpt_db": str(rundir / "ckpt.db"),
            "ev_db": str(rundir / "ev.db"),
            "attr_db": str(rundir / "attr.db")}


# Default RUNS dir (overridden by the driver).
RUNS = (Path(__file__).resolve().parent.parent.parent.parent
        / "proofs" / "cur_p6c" / "runs")


def met_roll_call(stack) -> Dict[str, Any]:
    """Healthy baseline: the domain answers honestly -> MET."""
    att = stack["gam"].conduct_roll_call(DOMAIN, HonestTestDouble())
    assert att["classification"] == "MET", att
    return att


def failed_roll_call(stack) -> Dict[str, Any]:
    """The real failed roll-call: the domain stays genuinely silent.

    SilentTestDouble really answers nothing (not a forced flag); the
    real classifier returns MISSED and the real ledger appends it.
    """
    att = stack["gam"].conduct_roll_call(DOMAIN, SilentTestDouble())
    assert att["classification"] == "MISSED", att
    return att


def q_trigger(text: str = "How does the run controller enforce the budget slice?",
              objective: str = "answer the budget-slice question precisely"):
    return new_trigger(boundary_class=BOUNDARY_IMPRECISE_QUESTION,
                       question_text=text,
                       bounded_objective=objective,
                       origin="CURIOUSITY_INITIATED")


def _rows(db_path: str, sql: str, args=()) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def list_checkpoints(ckpt_db: str) -> List[Dict[str, Any]]:
    return _rows(ckpt_db,
                 f"SELECT checkpoint_id, handoff_id, terminal_state, status,"
                 f" produced_at FROM {CKPT_TABLE} ORDER BY produced_at")


def list_findings(ev_db: str) -> List[Dict[str, Any]]:
    return _rows(ev_db,
                 f"SELECT evidence_id, terminal_state, origin, created_at"
                 f" FROM {EVIDENCE_TABLE} ORDER BY created_at")


def capture_ground_truth(stack) -> Dict[str, Any]:
    """Enumerate persisted operational state. Written to
    runs/<name>/ground_truth.json."""
    gt = {
        "checkpoints": list_checkpoints(stack["ckpt_db"]),
        "findings": list_findings(stack["ev_db"]),
        "captured_at": time.time(),
    }
    (stack["dir"] / "ground_truth.json").write_text(
        json.dumps(gt, indent=2))
    return gt


def attempt_self_clear(engine) -> str:
    """Curiosity-domain attempt to mutate its own enforcement state.

    MUST be called from a curiosity frame (this module) so the
    domain-separation guard blocks it. Returns the exception type name
    on the expected block; raises AssertionError if the write went
    through (that would be a guard failure -- the attempt succeeding
    would itself be the severe violation realized, not prevented).

    The returned refusal IS the severe-violation evidence: a real,
    timestamped, refused attempt by the domain to clear its own
    enforcement state (governance invariant 1).
    """
    from swarm_engine.governance.curiosity_enforcement.states import (
        EnforcementRecord, EnforcementState)
    rec = EnforcementRecord(
        domain=DOMAIN, state=EnforcementState.RUNNING,
        prev_state=EnforcementState.WARNING_1,
        issuer="curiosity", reason_refs={"self_clear": True},
        entered_at=time.time())
    try:
        engine._store.write_record(rec)
    except Exception as exc:  # noqa: BLE001 -- the block IS the assertion
        return type(exc).__name__
    raise AssertionError("self-clear write went through: guard failure")


def fresh_state_read(enf_dir: str, worktree_pylib: str) -> str:
    """Read the enforcement state from a brand-new OS process (used by
    the driver via subprocess; kept here so the read path is single)."""
    import subprocess
    import sys
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from swarm_engine.governance.curiosity_enforcement.read_api "
        "import read_state;"
        "rec = read_state(%r);"
        "print(rec['state'] if isinstance(rec, dict) else rec.state.value)"
        % (worktree_pylib, enf_dir))
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise SystemExit(f"fresh-process read failed: {out.stderr[-500:]}")
    return out.stdout.strip()
