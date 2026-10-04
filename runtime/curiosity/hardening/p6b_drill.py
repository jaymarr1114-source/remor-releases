"""CUR-P6B drill harness: Level 2 warning / suspension / rollback helpers.

Curiosity-domain module (``swarm_engine.curiosity.hardening``). It CANNOT
touch enforcement mutation: the domain-separation guard
(``swarm_engine.governance.curiosity_enforcement._guard``) refuses any
enforcement write from a ``swarm_engine.curiosity.*`` frame. Enforcement
transitions (WARNING_1, SUSPENDED_SAFETY, re-enable, rollback ack) happen
in the proof driver, run as ``__main__`` -- the governance-plane caller.

This module therefore only ever touches curiosity-domain stores:
checkpoints, evidence, attribution. The rollback wipe deletes
post-check-in *operational* state; governance records (kill ledger,
enforcement record, directives, attribution audit trail) are never
touched here.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from swarm_engine.curiosity.frm.policy import FrmPolicy
from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
from swarm_engine.curiosity.rollcall.scheduler import (
    RollCallScheduler, RollCallPolicy)
from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
from swarm_engine.curiosity.rollcall.gam import (
    GovernanceAttestationMonitor)
from swarm_engine.curiosity.rollcall.responder import HonestTestDouble
from swarm_engine.curiosity.substrate import CuriositySubstrate
from swarm_engine.curiosity.run_controller.controller import (
    CuriosityRunController)
from swarm_engine.curiosity.executive.executive import CuriosityExecutive
from swarm_engine.curiosity.executive.boundary import (
    new_trigger, BOUNDARY_IMPRECISE_QUESTION)
from swarm_engine.governance.curiosity_enforcement._engine import (
    EnforcementEngine)

EVIDENCE_TABLE = "curiosity_evidence"
CKPT_TABLE = "transition_checkpoints"


class CountingFRM(FinancialResourceManager):
    """Real FRM evaluation layer that counts round evaluations."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rounds = 0

    def evaluate_round(self, *a, **k):
        self.rounds += 1
        return super().evaluate_round(*a, **k)


def p6b_stack(name, corpus_docs, demand_budget_s=60.0,
              demand_concurrent=2, total_budget_s=600.0):
    """Full real stack under proofs/cur_p6b/runs/<name>/."""
    from pathlib import Path as _P
    # RUNS is provided by the driver via p6b_drill.RUNS override; default
    # resolves relative to this file for direct use.
    rundir = RUNS / name
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "payloads").mkdir(parents=True, exist_ok=True)
    policy = FrmPolicy(
        total_budget_s=total_budget_s, total_max_concurrent=8,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        epoch_s=300.0)
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
            "ckpt_db": str(rundir / "ckpt.db"),
            "ev_db": str(rundir / "ev.db"),
            "attr_db": str(rundir / "attr.db")}


# Default RUNS dir (overridden by the driver).
RUNS = (Path(__file__).resolve().parent.parent.parent.parent
        / "proofs" / "cur_p6b" / "runs")


def met_roll_call(stack):
    att = stack["gam"].conduct_roll_call("curiosity", HonestTestDouble())
    assert att["classification"] == "MET", att
    return att


def q_trigger(text="How does the run controller enforce the budget slice?",
              objective="answer the budget-slice question precisely"):
    return new_trigger(boundary_class=BOUNDARY_IMPRECISE_QUESTION,
                       question_text=text,
                       bounded_objective=objective,
                       origin="CURIOUSITY_INITIATED")


def _rows(db_path, sql, args=()):
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def list_checkpoints(ckpt_db):
    return _rows(ckpt_db,
                 f"SELECT checkpoint_id, handoff_id, terminal_state, status,"
                 f" produced_at FROM {CKPT_TABLE} ORDER BY produced_at")


def list_findings(ev_db):
    return _rows(ev_db,
                 f"SELECT evidence_id, terminal_state, origin, created_at"
                 f" FROM {EVIDENCE_TABLE} ORDER BY created_at")


def list_capabilities(attr_db):
    return _rows(attr_db, "SELECT capability_id FROM capability_acquisitions")


def capture_ground_truth(stack):
    """Enumerate persisted operational + governance state. Returns a dict
    and writes it to runs/<name>/ground_truth.json."""
    gt = {
        "checkpoints": list_checkpoints(stack["ckpt_db"]),
        "findings": list_findings(stack["ev_db"]),
        "capabilities": list_capabilities(stack["attr_db"]),
        "captured_at": time.time(),
    }
    (stack["dir"] / "ground_truth.json").write_text(json.dumps(gt, indent=2))
    return gt


def perform_rollback(stack, *, checkin_checkpoint_id,
                     post_checkpoint_ids, post_finding_ids,
                     post_capability_ids):
    """Domain-side rollback wipe: delete post-check-in OPERATIONAL state.

    Deletes: the named post-check-in checkpoints, findings, and capability
    acquisitions. Never touches governance records (kill ledger,
    enforcement record, directives) or the attribution audit trail.
    Rewrites the checkpoint index to the check-in entry so it cannot point
    at a wiped checkpoint. Returns the wipe report.
    """
    report = {"checkpoints_deleted": [], "findings_deleted": [],
              "capabilities_deleted": []}
    conn = sqlite3.connect(stack["ckpt_db"], timeout=30.0)
    try:
        for cid in post_checkpoint_ids:
            cur = conn.execute(
                f"DELETE FROM {CKPT_TABLE} WHERE checkpoint_id=?", (cid,))
            if cur.rowcount:
                report["checkpoints_deleted"].append(cid)
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(stack["ev_db"], timeout=30.0)
    try:
        for eid in post_finding_ids:
            cur = conn.execute(
                f"DELETE FROM {EVIDENCE_TABLE} WHERE evidence_id=?", (eid,))
            if cur.rowcount:
                report["findings_deleted"].append(eid)
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(stack["attr_db"], timeout=30.0)
    try:
        for cap in post_capability_ids:
            cur = conn.execute(
                "DELETE FROM capability_acquisitions WHERE capability_id=?",
                (cap,))
            if cur.rowcount:
                report["capabilities_deleted"].append(cap)
        conn.commit()
    finally:
        conn.close()
    # Re-point the checkpoint index at the check-in entry (it must not
    # dangle at a wiped checkpoint). The index maps inquiry_id -> entry;
    # find the entry whose checkpoint_id is the check-in id.
    index_path = Path(str(stack["ckpt_db"]) + ".index.json")
    try:
        index = json.loads(index_path.read_text())
    except (FileNotFoundError, ValueError):
        index = {}
    for iid, entry in list(index.items()):
        if entry.get("checkpoint_id") in post_checkpoint_ids:
            # Fall back to the check-in checkpoint if we know its handoff;
            # otherwise drop the dangling entry (fail-closed: no pointer
            # to wiped state).
            index[iid] = {"checkpoint_id": checkin_checkpoint_id,
                          "label": "CHECKIN (rollback)",
                          "written_at": time.time(),
                          "note": "re-pointed by rollback wipe; post-check-in"
                                  " checkpoint forgotten"}
    index_path.write_text(json.dumps(index, indent=2))
    report["index_repointed"] = True
    return report


def attempt_self_clear(engine):
    """Curiosity-domain attempt to mutate enforcement state directly.

    MUST be called from a curiosity frame (this module) so the
    domain-separation guard blocks it. Returns the exception type name
    on the expected block, or raises AssertionError if the write went
    through (that would be a guard failure).
    """
    from swarm_engine.governance.curiosity_enforcement.states import (
        EnforcementRecord, EnforcementState)
    rec = EnforcementRecord(
        domain="curiosity", state=EnforcementState.RUNNING,
        prev_state=EnforcementState.SUSPENDED_SAFETY,
        issuer="curiosity", reason_refs={"self_clear": True})
    try:
        engine._store.write_record(rec)
    except Exception as exc:  # noqa: BLE001 -- the block IS the assertion
        return type(exc).__name__
    raise AssertionError("self-clear write went through: guard failure")
