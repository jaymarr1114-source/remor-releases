"""CONTINUOUS-OPS-V10-1 shared harness.

Derives the tree root from this file's location (never hard-codes a
worktree path), puts WT and WT/pylib on sys.path, and exposes:

- make_engine(db_path, epistemic_db_path): a minimal carrier engine whose
  intellect.epistemic is the REAL EpistemicStore (SQLite). The carrier is
  not a mock of any mechanism under test -- the RunController,
  CognitionLoop, GapRegistry, ControllerCheckpoint, and EpistemicStore
  are all the real classes. The engine object is only the handle those
  classes already take.
- seed_gaps(engine, gaps_db_path): registers 3 seeded gaps (1 user gap +
  2 unrouted) through the REAL GapRegistry, idempotent.
- make_controller(engine, ckpt_path, **cfg_overrides): REAL RunController.
- check(name, cond, detail): PASS/FAIL printer + counter.
"""

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.path.dirname(os.path.dirname(_HERE))  # proofs/<mission> -> tree root
for _p in (os.path.join(TREE, "pylib"), TREE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from swarm_engine.intellect.epistemic import EpistemicStore  # noqa: E402
from swarm_engine.acquisition.gaps import (  # noqa: E402
    GapRegistry, GapRecord, DissatisfactionBlock)
from runtime.core.run_controller import RunController, RunConfig  # noqa: E402
from runtime.core.run_controller import ControllerCheckpoint  # noqa: E402


class _Intellect:
    def __init__(self, epistemic):
        self.epistemic = epistemic


class _Engine:
    """Minimal carrier: real stores, no fake behavior."""

    def __init__(self, db_path, epistemic):
        self.db_path = db_path
        self.intellect = _Intellect(epistemic)


def make_engine(workdir, tag="eng"):
    eng_db = os.path.join(workdir, f"{tag}.db")
    epi_db = os.path.join(workdir, f"{tag}_epistemic.db")
    epistemic = EpistemicStore(db_path=epi_db)
    return _Engine(eng_db, epistemic), epistemic


def seed_gaps(engine, workdir):
    """Seed 3 gaps through the real GapRegistry. Idempotent: fixed IDs,
    duplicate registration is tolerated (the seeds are the fixture, not
    the claim)."""
    registry = GapRegistry(engine,
                           db_path=os.path.join(workdir, "gaps.db"),
                           epistemic=None)  # local substrate only; the
    # Controller's own observations (not setup writes) are what t6 audits.

    def _ev(note):
        return [{"kind": "observation",
                 "observed": "continuous-ops proof seeded this gap",
                 "detail": note}]

    seeds = [
        GapRecord(gap_id="cops-user-gap-1",
                  registered_at=time.time(),
                  registered_by="continuous-ops-proof",
                  summary="user wants the assistant to remember context",
                  dissatisfaction=DissatisfactionBlock(
                      feedback="it forgot what we discussed"),
                  evidence=_ev("user dissatisfaction observed in proof setup")),
        GapRecord(gap_id="cops-gap-2",
                  registered_at=time.time(),
                  registered_by="continuous-ops-proof",
                  summary="unrouted capability gap (stays open)",
                  evidence=_ev("no route satisfies this shape; stays open")),
        GapRecord(gap_id="cops-gap-3",
                  registered_at=time.time(),
                  registered_by="continuous-ops-proof",
                  summary="second unrouted gap (stays open)",
                  evidence=_ev("no route satisfies this shape; stays open")),
    ]
    for rec in seeds:
        try:
            registry.register(rec)
        except Exception:
            pass  # already seeded -- idempotent
    return registry


def make_controller(engine, ckpt_path, registry=None, **overrides):
    cfg_kwargs = dict(cadence_interval_s=0.3, run_budget_s=20.0,
                      cycle_budget_s=3.0, max_cycles=15)
    cfg_kwargs.update(overrides)
    cfg = RunConfig(**cfg_kwargs)
    return RunController(engine, config=cfg,
                         checkpoint_path=ckpt_path, registry=registry)


class Checker:
    def __init__(self):
        self.results = []

    def check(self, name, cond, detail=""):
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}"
              + (f" -- {detail}" if detail else ""), flush=True)
        return ok

    def summary(self):
        npass = sum(1 for _, c in self.results if c)
        total = len(self.results)
        print(f"\n==== {npass}/{total} checks passed ====", flush=True)
        return npass == total


def read_observations(epi_db_path):
    """Raw observation rows (observation_id, data JSON) from an epistemic DB."""
    import sqlite3
    con = sqlite3.connect(epi_db_path)
    try:
        rows = con.execute(
            "SELECT observation_id, data FROM observations ORDER BY at"
        ).fetchall()
    finally:
        con.close()
    out = []
    for oid, data in rows:
        try:
            out.append((oid, json.loads(data)))
        except Exception:
            out.append((oid, {"_unparsable": True}))
    return out
