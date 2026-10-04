"""CUR-P6F drill harness: fresh-process persistence of the three stores.

Curiosity-domain module (runs in swarm_engine.curiosity.* frames). It builds
the real store stack and populates the curiosity-side stores through their
real write paths:

  * Evidence Store      (runtime/curiosity/evidence/)      -- SQLite
  * Checkpoint store    (runtime/core/executive/checkpoint.py) -- SQLite
  * FRM epoch ledger    (runtime/curiosity/frm/ledger.py)  -- SQLite

Enforcement mutation is NOT here: enforcement_state.json + kill_ledger.jsonl
(runtime/governance/curiosity_enforcement/) are written ONLY from the proof
driver running as __main__ (the governance-plane caller); the
domain-separation guard refuses curiosity-frame writes.

Every writer below is the store's real writer. The battery (proofs/cur_p6f/)
SIGKILLs real writer processes mid-write and verifies from brand-new OS
processes that no torn write is ever presented as valid.
"""

from __future__ import annotations

import os
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

from swarm_engine.curiosity.evidence.records import (
    CuriosityFinding, EvidenceProvenance,
)
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.evidence.writer import CuriosityWriter
from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
from swarm_engine.curiosity.frm.policy import (
    CostInput, CostKind, DomainDemand, FrmPolicy,
)
from swarm_engine.core.executive.checkpoint import TransitionCheckpointStore


# -- store paths (all under one state dir) --------------------------------

def store_paths(state_dir: str) -> Dict[str, str]:
    enf = os.path.join(state_dir, "enforcement")
    return {
        "enforcement_dir": enf,
        "enforcement_state": os.path.join(enf, "enforcement_state.json"),
        "kill_ledger": os.path.join(enf, "kill_ledger.jsonl"),
        "evidence_db": os.path.join(state_dir, "curiosity_evidence.db"),
        "checkpoint_db": os.path.join(state_dir, "transition_checkpoints.db"),
        "checkpoint_index": os.path.join(
            state_dir, "transition_checkpoints.db.index.json"),
        "frm_ledger_db": os.path.join(state_dir, "frm_epochs.db"),
    }


# -- stack -----------------------------------------------------------------

class P6FStack:
    """The real store stack. Enforcement engine is NOT constructed here --
    the proof driver (governance plane) owns it."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        self.paths = store_paths(state_dir)
        os.makedirs(self.paths["enforcement_dir"], exist_ok=True)
        self.evidence = CuriosityEvidenceStore(self.paths["evidence_db"])
        self.writer = CuriosityWriter(self.evidence)
        self.checkpoints = TransitionCheckpointStore(
            self.paths["checkpoint_db"])
        policy = FrmPolicy(total_budget_s=60.0, total_max_concurrent=2,
                          primary_minimum_budget_s=10.0,
                          primary_minimum_concurrent=1)
        self.frm = FinancialResourceManager(policy,
                                            ledger_path=self.paths["frm_ledger_db"])


def build_stack(state_dir: str) -> P6FStack:
    return P6FStack(state_dir)


# -- real population ---------------------------------------------------------

_TERMINAL_STATES = ("QUESTION_RESOLVED",)


def make_finding(tag: str) -> CuriosityFinding:
    return CuriosityFinding(
        evidence_id=f"ev_p6f_{tag}_{uuid.uuid4().hex[:8]}",
        loop="questioning",
        bounded_objective=f"p6f-persistence-probe-{tag}",
        origin="CURIOUSITY_INITIATED",
        terminal_state="QUESTION_RESOLVED",
        provenance=EvidenceProvenance(
            loop="questioning",
            bounded_objective=f"p6f-persistence-probe-{tag}",
            model="p6f-drill",
        ),
        payload_ref=f"payload/p6f/{tag}",
        created_at=time.time(),
    )


def make_handoff(tag: str, evidence_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        handoff_id=f"handoff_p6f_{tag}_{uuid.uuid4().hex[:8]}",
        from_loop="questioning",
        to_loop="questioning",
        terminal_state="QUESTION_RESOLVED",
        boundary_kind="p6f-probe",
        triggering_boundary_id=f"bnd_p6f_{tag}",
        chain_depth=1,
        evidence_refs={"finding": evidence_id},
        resource_delta={"spent_s": 0.01},
    )


# -- single-write primitives (for the SIGKILL writer) -------------------------

def submit_one(stack: P6FStack, tag: str) -> str:
    """One real evidence write through the fenced writer. Must be called
    from a swarm_engine.curiosity.* frame (the writer's domain fence)."""
    finding = make_finding(tag)
    stack.writer.submit(finding)
    return finding.evidence_id


def save_one_checkpoint(stack: P6FStack, tag: str) -> str:
    """One real checkpoint write."""
    finding_id = f"ev_p6f_{tag}"
    handoff = make_handoff(tag, finding_id)
    return stack.checkpoints.save(handoff, {"round": tag})


def run_one_frm_round(stack: P6FStack, i: int) -> None:
    """One real FRM contention round (appends to the epoch ledger)."""
    stack.frm.evaluate_round(
        enforcement_state="RUNNING",
        primary_demand=DomainDemand(domain="primary", budget_s=10.0,
                                    max_concurrent=1),
        curiosity_demand=DomainDemand(domain="curiosity", budget_s=20.0,
                                       max_concurrent=2),
        cost_inputs=(CostInput(kind=CostKind.UNPRICED, value=None,
                               provenance="p6f-drill"),),
    )


def populate_curiosity_side(stack: P6FStack, n: int = 5) -> Dict[str, Any]:
    """Write n findings + n checkpoints + FRM rounds/epoch-close through the
    real writers. Returns ground truth (ids and counts)."""
    finding_ids: List[str] = []
    checkpoint_ids: List[str] = []
    for i in range(n):
        tag = f"seed{i}"
        finding_ids.append(submit_one(stack, tag))
        checkpoint_ids.append(save_one_checkpoint(stack, tag))

    # Real FRM contention rounds, then a real epoch close.
    policy_rounds = 0
    for i in range(2):
        run_one_frm_round(stack, i)
        policy_rounds += 1
    stack.frm.advance_epoch()
    return {
        "finding_ids": finding_ids,
        "checkpoint_ids": checkpoint_ids,
        "frm_rounds": policy_rounds,
        "frm_epoch_closes": 1,
    }
