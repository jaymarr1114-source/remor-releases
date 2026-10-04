"""Shared real-stack builder for the CUR-P3B-INT integration battery.

Every test builds a REAL stack: real FRM evaluation layer, real GAM
roll-call, real enforcement read path, real GraphController, real
curiosity microcontroller substrate, real checkpoint/evidence/ledger/
attribution stores -- inside its own run directory. No mocks, no staged
success. Paths derive from __file__ (no hardcoded home paths).

The creative loop composes ONLY from the presented verified ledger.
Corpus documents use the documented convention: a line of the form
"primitive_id | verification_event | gate_reference" presents one
verified primitive (the battery verifies every cited gate reference
exists); other lines are prior art for the novelty check only.
"""

import sys
from pathlib import Path

PROOFS_DIR = Path(__file__).resolve().parent
# proofs/cur_p3b_int -> proofs -> worktree root (two levels up).
WORKTREE = PROOFS_DIR.parent.parent
sys.path.insert(0, str(WORKTREE / "pylib"))

from swarm_engine.curiosity.frm.policy import FrmPolicy
from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
from swarm_engine.curiosity.rollcall.scheduler import (
    RollCallScheduler, RollCallPolicy)
from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
from swarm_engine.curiosity.rollcall.gam import GovernanceAttestationMonitor
from swarm_engine.curiosity.rollcall.responder import HonestTestDouble
from swarm_engine.curiosity.substrate import CuriositySubstrate
from swarm_engine.curiosity.run_controller.controller import (
    CuriosityRunController)
from swarm_engine.curiosity.executive.executive import CuriosityExecutive
from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_GENERATIVE_PROMPT, new_trigger)

RUNS = PROOFS_DIR / "runs_int"

# Commission fully covered by the test ledger (all content terms name
# ledger primitives): the happy-path pipeline converges to release.
COMMISSION_COVERED = "compose song melody"
LEDGER_SONG = "song-melody-compose | composition review | 20f80c8"
NEUTRAL_DOCS = [
    "The office kitchen stocks oat milk and espresso beans.",
    "Rain is forecast for the northern counties on Thursday.",
]

# The CUR-P2 canonical regression question (questioning must still
# converge through the extended registry exactly as in Phase 2).
Q_RESOLVED = "How does the run controller enforce the budget slice?"
Q_CORPUS_FILES = [
    "runtime/curiosity/executive/executive.py",
    "runtime/curiosity/run_controller/controller.py",
    "runtime/curiosity/loops/questioning/loop.py",
    "runtime/curiosity/substrate.py",
    "runtime/curiosity/cognition.py",
    "runtime/core/microcontroller/substrate.py",
]

# Hypothesis text for the scientific_inquiry regression (from CUR-P3A-INT).
HYP_TEXT = ("Cache latency drops when the index warms: warm indexes show "
            "measurably faster reads in production. Falsified if warm "
            "indexes show no latency change under load.")
SUPPORT_DOC = ("Production logs confirmed warm cache indexes measurably "
               "reduced read latency during peak traffic.")


class CountingFRM(FinancialResourceManager):
    """Real FRM evaluation layer that counts round evaluations, so the
    proof can show the FRM was genuinely consulted."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rounds = 0

    def evaluate_round(self, *a, **k):
        self.rounds += 1
        return super().evaluate_round(*a, **k)


def new_stack(name, *, corpus_docs, demand_budget_s=60.0,
              demand_concurrent=2, total_budget_s=600.0,
              primary_minimum_budget_s=60.0, frm=None):
    """Build a full real stack under proofs/cur_p3b_int/runs_int/<name>/."""
    rundir = RUNS / name
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "payloads").mkdir(parents=True, exist_ok=True)
    policy = FrmPolicy(
        total_budget_s=total_budget_s, total_max_concurrent=8,
        primary_minimum_budget_s=primary_minimum_budget_s,
        primary_minimum_concurrent=1, epoch_s=300.0)
    if frm is None:
        frm = FinancialResourceManager(policy)
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
    return {"dir": rundir, "frm": frm, "gam": gam, "enf_dir": enf_dir,
            "sub": sub, "rc": rc, "ex": ex, "sched": sched}


def met_roll_call(stack):
    att = stack["gam"].conduct_roll_call("curiosity", HonestTestDouble())
    assert att["classification"] == "MET", att
    return att


def creative_trigger(text=COMMISSION_COVERED,
                     objective="release a song candidate"):
    return new_trigger(boundary_class=BOUNDARY_GENERATIVE_PROMPT,
                       question_text=text,
                       bounded_objective=objective,
                       origin="PRIMARY_REQUESTED")


def q_corpus():
    return [Path(str(WORKTREE / f)).read_text() for f in Q_CORPUS_FILES]


def inquiry_trigger(text=HYP_TEXT, boundary_class="hypothesis_candidate",
                    objective="prove the integrated inquiry chain"):
    return new_trigger(boundary_class=boundary_class,
                       question_text=text,
                       bounded_objective=objective,
                       origin="PRIMARY_REQUESTED")
