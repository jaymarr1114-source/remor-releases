#!/usr/bin/env python3
"""Shared fixture builders for the CREATIVITY-INTEGRATE-1 end-to-end proof.

Pure builders, no shared state: each proof script calls these to stand up
its own isolated fixture world (scratch tmp dirs, real stores via public
APIs). The pattern mirrors the proven EXEC-1 / RUNCTRL-1 fixtures — the
executive constructs its OWN stores from store_dir (T9); fixtures seed
them through a second handle on the same sqlite files.
"""
import os
import sys
import tempfile
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WT_ROOT = os.environ.get("WT_ROOT", os.path.abspath(
    os.path.join(SCRIPT_DIR, "..", "..")))
sys.path.insert(0, WT_ROOT)
sys.path.insert(0, os.path.join(WT_ROOT, "pylib"))  # swarm_engine import root

from swarm_engine.primitives.core import (  # noqa: E402
    NUM,
    Effect,
    PrimitiveRegistry,
)
from runtime.services.evidence import EvidenceStore  # noqa: E402
from runtime.governance.provenance import ProvenanceStore  # noqa: E402
from runtime.acquisition.gaps import GapRegistry  # noqa: E402
from runtime.core.executive.detector import ScanReport  # noqa: E402
from runtime.creativity.executive import (  # noqa: E402
    CreativityExecutiveController,
    SearchBounds,
)
from runtime.creativity.intent import register_intent  # noqa: E402
from runtime.creativity.ledger import LedgerEntry  # noqa: E402
from runtime.creativity.budget import (  # noqa: E402
    COMPUTE,
    MONETARY,
    BudgetEnvelope,
    CostBound,
    CreativityBudget,
)
from runtime.creativity.run_controller import (  # noqa: E402
    CreativityRunController,
)
from runtime.creativity.release import AdmissionRegistry  # noqa: E402

OPS = {
    "add": lambda a, b: a + b,
    "mul": lambda a, b: a * b,
    "sub": lambda a, b: a - b,
    "div": lambda a, b: a / b if b else 0.0,
    "mod": lambda a, b: a % b if b else 0.0,
    "powi": lambda a, b: a ** min(int(b), 4),
    "maxi": lambda a, b: max(a, b),
    "mini": lambda a, b: min(a, b),
    "avg": lambda a, b: (a + b) / 2.0,
    "dif2": lambda a, b: abs(a - b) * 2.0,
}

CLEAR_INTENT_OUTCOME = "compose a short melody"  # 'melody' -> CLEAR, no suspension


def make_registry(names):
    reg = PrimitiveRegistry()
    for name in names:
        fn = OPS[name]
        reg.define(name, "arithmetic", {"a": NUM, "b": NUM}, NUM,
                   effects=(Effect.PURE,))(fn)
    return reg


def empty_scan():
    return ScanReport(scanned_at=time.time(), presentations=[],
                      source_absent=[], observed={}, notes=[])


def make_world(pids, compute_bound, work_id, refinement_bound=2,
               bounds=None, kill_event=None, prefix="integ1_"):
    """One isolated fixture world. Returns a dict of handles.

    tmp: scratch dir; ex: executive (own stores); budget; ctl: run
    controller sharing kill_event with the executive; ev: second
    EvidenceStore handle on the executive's ev.db (fixture seeding).
    """
    tmp = tempfile.mkdtemp(prefix=prefix)
    reg = make_registry(pids)
    kill = kill_event if kill_event is not None else threading.Event()
    ex = CreativityExecutiveController(
        store_dir=tmp,
        primitives=reg,
        boundary_scan=empty_scan,
        stop_event=kill,
        search_bounds=bounds or SearchBounds(max_composition_size=2,
                                            max_candidates=10),
        repo_root=WT_ROOT,
        admission_db_path=os.path.join(tmp, "adm.db"))
    ev = EvidenceStore(os.path.join(tmp, "ev.db"))
    for pid in pids:
        e = ev.add_entry(
            "observation",
            f"gate crossing: primitive '{pid}' admitted (integrate1 fixture)",
            source="gate:INTEGRATE1")
        ev.verify_entry(e["id"], "gate:INTEGRATE1")
        ex.ledger.add_entry(
            LedgerEntry(pid, "evidence", e["id"], "gate:INTEGRATE1@integrate1"))
    envelope = BudgetEnvelope({
        COMPUTE: CostBound(dimension=COMPUTE, unit="s", bound=compute_bound,
                           measurement="integrate1 battery wall-clock",
                           provenance="integrate1 battery fixture"),
        MONETARY: CostBound(dimension=MONETARY, unit="USD", bound=None,
                            measurement="unpriced",
                            provenance="integrate1 battery fixture"),
    })
    budget = CreativityBudget(envelope,
                              spend_db_path=os.path.join(tmp, "spend.db"))
    ctl = CreativityRunController(
        executive=ex, budget=budget, refinement_bound=refinement_bound,
        work_id=work_id, kill_event=kill)
    return {"tmp": tmp, "ex": ex, "budget": budget, "ctl": ctl,
            "kill": kill, "ev": ev, "pids": tuple(pids)}


def make_intent(outcome=CLEAR_INTENT_OUTCOME, principal="commissioner"):
    return register_intent(principal, outcome)


def gap_registry_for(tmp):
    return GapRegistry(None, db_path=os.path.join(tmp, "gaps.db"))


def admission_registry_for(tmp):
    return AdmissionRegistry(os.path.join(tmp, "adm.db"))
