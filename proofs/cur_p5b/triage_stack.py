"""Shared real-stack builder for the CUR-P5B battery.

Every test builds a REAL stack: real FRM evaluation layer, real GAM
roll-call, real enforcement read path, real GraphController, real
curiosity microcontroller substrate, real checkpoint/evidence/ledger/
attribution stores -- inside its own run directory. No mocks, no staged
success. Modeled on the CUR-P3A-INT int_stack (proven pattern).
"""

import sys
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent.parent
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
from swarm_engine.curiosity.executive.boundary import new_trigger

RUNS = Path(__file__).resolve().parent / "runs"


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
    """Build a full real stack under proofs/cur_p5b/runs/<name>/."""
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


def q_trigger(text, objective="prove the triage pass",
              origin="PRIMARY_REQUESTED"):
    return new_trigger(boundary_class="imprecise_question",
                       question_text=text,
                       bounded_objective=objective,
                       origin=origin)


# Corpus that substantively answers the budget-slice question (drives
# QUESTION_RESOLVED through the real questioning chain).
Q_RESOLVED_TEXT = "How does the run controller enforce the budget slice?"
Q_RESOLVED_CORPUS_FILES = [
    "runtime/curiosity/run_controller/controller.py",
    "runtime/curiosity/executive/executive.py",
    "runtime/curiosity/loops/questioning/loop.py",
    "runtime/curiosity/substrate.py",
]


def q_resolved_corpus():
    return [Path(str(WORKTREE / f)).read_text()
            for f in Q_RESOLVED_CORPUS_FILES]


# Corpus with no coverage of the question's observable (drives
# INSUFFICIENT_EVIDENCE): the question points at evidence, the corpus
# carries no substantive passage for it.
Q_UNCOVERED_TEXT = ("What measurable latency effect does the L3 cache "
                    "prefetcher have on cold-start p99?")
Q_UNCOVERED_CORPUS = [
    "The office kitchen stocks oat milk and espresso beans.",
    "Rain is forecast for the northern counties on Thursday.",
    "The quarterly planning session moved to the annex building.",
]

# A question naming no observable at all, over an unrelated corpus
# (drives BOUNDARY_ESTABLISHED: no observable path).
Q_UNOBSERVABLE_TEXT = "What is the true nature of the ineffable?"
Q_UNOBSERVABLE_CORPUS = list(Q_UNCOVERED_CORPUS)
