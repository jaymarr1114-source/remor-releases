"""CUR-P6A drill harness: Level 1 forced ceiling breach (non-cooperative hard shutdown).

The drill:
  1. builds a full REAL stack (FRM, roll-call/GAM, enforcement engine,
     substrate, run controller, executive) in an isolated rundir;
  2. seeds accumulated knowledge (a completed inquiry -> terminal finding);
  3. captures ground truth across every store;
  4. starts the drill inquiry, ticks it to mid-execution;
  5. (done by the DRILL DRIVER, not this module) the enforcement transition
     is declared by a caller OUTSIDE the curiosity domain -- the domain-
     separation guard
     (swarm_engine.governance.curiosity_enforcement._guard) refuses
     enforcement mutation from any swarm_engine.curiosity.* frame, so the
     FRM/governance-plane action cannot originate inside curiosity; the
     battery's force step lives in the proof script itself;
  6. hard-stops the live inquiry through the real executive kill switch;
  7. verifies preservation field-by-field from a FRESH process.

The forcing event is the enforcement transition itself: the inquiry gets no
say, no RESOURCE_BOUNDARY drain -- that is what makes it non-cooperative.
The kill machinery is driven, never reimplemented.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

DOMAIN = "curiosity"


def _swarm_imports():
    from swarm_engine.curiosity.executive.executive import CuriosityExecutive
    from swarm_engine.curiosity.executive.boundary import new_trigger
    from swarm_engine.curiosity.frm.policy import FrmPolicy
    from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
    from swarm_engine.curiosity.rollcall.scheduler import (
        RollCallPolicy, RollCallScheduler)
    from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
    from swarm_engine.curiosity.rollcall.gam import GovernanceAttestationMonitor
    from swarm_engine.curiosity.substrate import CuriositySubstrate
    from swarm_engine.curiosity.run_controller.controller import (
        CuriosityRunController)
    from swarm_engine.governance.curiosity_enforcement._engine import (
        EnforcementEngine)
    return (CuriosityExecutive, new_trigger, FrmPolicy,
            FinancialResourceManager, RollCallPolicy, RollCallScheduler,
            AttestationLedger, GovernanceAttestationMonitor,
            CuriositySubstrate, CuriosityRunController, EnforcementEngine)


def build_drill_stack(runs_root: Path, name: str, corpus_docs: List[str],
                      demand_budget_s: float = 60.0,
                      demand_concurrent: int = 2,
                      total_budget_s: float = 600.0) -> Dict[str, Any]:
    """Build the full real stack under runs_root/<name>/. Clean base: this
    mission does not depend on any unlanded branch."""
    (CuriosityExecutive, new_trigger, FrmPolicy,
     FinancialResourceManager, RollCallPolicy, RollCallScheduler,
     AttestationLedger, GovernanceAttestationMonitor,
     CuriositySubstrate, CuriosityRunController,
     EnforcementEngine) = _swarm_imports()
    rundir = runs_root / name
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "payloads").mkdir(parents=True, exist_ok=True)
    from swarm_engine.curiosity.executive.boundary import new_trigger
    from swarm_engine.curiosity.frm.policy import FrmPolicy
    from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
    from swarm_engine.curiosity.rollcall.scheduler import (
        RollCallPolicy, RollCallScheduler)
    from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
    from swarm_engine.curiosity.rollcall.gam import GovernanceAttestationMonitor
    from swarm_engine.curiosity.substrate import CuriositySubstrate
    from swarm_engine.curiosity.run_controller.controller import (
        CuriosityRunController)
    from swarm_engine.governance.curiosity_enforcement._engine import (
        EnforcementEngine)

    rundir = runs_root / name
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "payloads").mkdir(parents=True, exist_ok=True)
    policy = FrmPolicy(
        total_budget_s=total_budget_s, total_max_concurrent=8,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        epoch_s=300.0)

    class CountingFRM(FinancialResourceManager):
        """Real FRM evaluation layer that counts rounds, so the proof shows
        the FRM was genuinely consulted (proven pattern from CUR-P3A-INT)."""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.rounds = 0

        def evaluate_round(self, *a, **k):
            self.rounds += 1
            return super().evaluate_round(*a, **k)

    frm = CountingFRM(policy)
    sched = RollCallScheduler(RollCallPolicy())
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
    engine = EnforcementEngine(enf_dir)
    return {"dir": rundir, "frm": frm, "gam": gam, "enf_dir": enf_dir,
            "sub": sub, "rc": rc, "ex": ex, "engine": engine,
            "new_trigger": new_trigger}


def met_roll_call(stack: Dict[str, Any]):
    """Conduct a MET roll-call attestation (activation gate requirement)."""
    from swarm_engine.curiosity.rollcall.responder import HonestTestDouble
    att = stack["gam"].conduct_roll_call("curiosity", HonestTestDouble())
    assert att["classification"] == "MET", att
    return att


def questioning_trigger(new_trigger, text: str,
                        origin: str = "CURIOUSITY_INITIATED"):
    """Build a question-shaped trigger for the questioning loop.

    NOTE: the frozen origin vocabulary carries the misspelling
    "CURIOUSITY_INITIATED" (observed, not repaired -- renaming breaks landed
    callers). We use the frozen spelling as-is.
    """
    return new_trigger(
        boundary_class="imprecise_question",
        question_text=text,
        bounded_objective="drill objective: answer the posed question",
        origin=origin)


def drive_to_terminal(rc, inquiry_id: str, max_ticks: int = 200) -> str:
    for _ in range(max_ticks):
        views = {v["inquiry_id"]: v for v in rc.inquiry_views()}
        st = views[inquiry_id]["state"]
        if st in ("TERMINATED", "SUSPENDED", "KILLED"):
            return st
        rc.tick()
    return "TICK_LIMIT"


def wait_active(rc, inquiry_id: str, max_ticks: int = 10) -> bool:
    for _ in range(max_ticks):
        rc.tick()
        views = {v["inquiry_id"]: v for v in rc.inquiry_views()}
        if views[inquiry_id]["state"] == "ACTIVE":
            return True
    return False


# ---------------------------------------------------------------------------
# Ground truth capture (content-based, pre-breach)
# ---------------------------------------------------------------------------

def _read_table(db_path: str, table: str) -> List[Dict[str, Any]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def capture_ground_truth(stack: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot every store's full content. Called BEFORE the breach."""
    rundir: Path = stack["dir"]
    rc = stack["rc"]
    gt: Dict[str, Any] = {}
    # (a) consumption records: FRM rounds genuinely evaluated
    gt["frm_rounds"] = stack["frm"].rounds
    # (b) grants: the activation decisions' grants are captured by the caller
    #     per inquiry (see note_grant); the checkpoint store also persists them.
    # (c) provenance + (d) completed evidence: the fenced evidence store
    from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
    store = CuriosityEvidenceStore(str(rundir / "ev.db"))
    gt["evidence"] = {f.evidence_id: f.as_dict() for f in store.all()}
    # (e) checkpoints: every row + the handoff index
    gt["checkpoints"] = _read_table(str(rundir / "ckpt.db"),
                                    "transition_checkpoints")
    index_path = rundir / "ckpt.db.index"
    # the controller keeps its index next to the db; capture whatever exists
    gt["checkpoint_index"] = _read_index(rundir)
    # (f) admission records: attribution chain tables (real names:
    # work_units / results / admissions / capability_acquisitions)
    gt["attr_work_units"] = _read_table(str(rundir / "attr.db"),
                                       "work_units")
    gt["attr_results"] = _read_table(str(rundir / "attr.db"), "results")
    gt["attr_admissions"] = _read_table(str(rundir / "attr.db"),
                                       "admissions")
    gt["attr_capability_acquisitions"] = _read_table(
        str(rundir / "attr.db"), "capability_acquisitions")
    # enforcement history + kill ledger
    gt["enforcement_history"] = _enforcement_history(stack)
    gt["kill_ledger"] = _read_kill_ledger(stack)
    # terminal ledger
    gt["term_routes"] = _read_table(str(rundir / "term.db"), "terminal_routes") \
        if (rundir / "term.db").exists() else []
    return gt


def _read_index(rundir: Path) -> Dict[str, Any]:
    for cand in rundir.glob("*.index"):
        try:
            return json.loads(cand.read_text())
        except (ValueError, OSError):
            pass
    # the controller writes its index beside the checkpoint db; find it
    for cand in (rundir / "ckpt.db.index",):
        if cand.exists():
            try:
                return json.loads(cand.read_text())
            except (ValueError, OSError):
                pass
    return {}


def _enforcement_history(stack: Dict[str, Any]) -> List[Dict[str, Any]]:
    # persistence is current-record-only (no history table); capture the
    # current record via to_dict() (the record's real serializer).
    rec = stack["engine"].current(DOMAIN)
    d = rec.to_dict() if hasattr(rec, "to_dict") else {}
    return [d] if d else []


def _read_kill_ledger(stack: Dict[str, Any]) -> List[Dict[str, Any]]:
    from swarm_engine.governance.curiosity_enforcement.read_api import (
        read_kill_ledger)
    try:
        rows = read_kill_ledger(stack["enf_dir"])
        out = []
        for r in rows:
            out.append(r.as_dict() if hasattr(r, "as_dict") else dict(r))
        return out
    except Exception:
        return []


