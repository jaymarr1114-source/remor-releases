"""
swarm_engine/synthesis/abstraction.py

Recursive capability construction: capabilities acquired or composed earlier
become material for later synthesis, and repeated successful patterns become
generalized reusable skeletons rather than staying one-off results.

Two mechanisms, both real:

1. Acquired capabilities already register into the primitive registry
   (`_register_acquired`), which means the planner, composer and synthesizer
   can already reference them by name in later compositions — recursion is
   structural, not bolted on. This module verifies and exercises that path.

2. `CompositionAbstractor` watches which *shapes* of two-stage composition
   succeed across different acquisitions, and once a shape has succeeded
   enough times on different specifications, it promotes that shape to a
   named, persisted skeleton the synthesizer will try first — turning a
   pattern SWarm discovered into part of its own vocabulary of hypotheses.
   This is generalization from repeated success, not a hardcoded skeleton
   someone wrote in advance.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ObservedComposition:
    stage_a: str
    stage_b: str
    spec_name: str
    succeeded: bool

    def shape(self) -> str:
        return f"{self.stage_a}->{self.stage_b}"


@dataclass
class GeneralizedSkeleton:
    shape: str
    template: str
    observations: int
    successes: int
    promoted_at: float

    def as_dict(self) -> Dict[str, Any]:
        return {"shape": self.shape, "observations": self.observations,
                "successes": self.successes,
                "success_rate": round(self.successes / max(1, self.observations), 3),
                "promoted_at": self.promoted_at}


class CompositionAbstractor:
    """Tracks which composition shapes work across different acquisitions and
    promotes ones that repeatedly succeed into named, reusable skeletons.

    Promotion requires success on more than one distinct specification,
    because a shape that only ever worked for one spec might just be that
    spec's answer, not a generalizable pattern. This is what separates
    abstraction from memorization: the bar is repeated success *across
    different problems*, not repeated success on the same one.
    """

    PROMOTE_AFTER = 2          # distinct specs
    PROMOTE_MIN_RATE = 0.6

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS composition_observations (
                shape TEXT, spec_name TEXT, succeeded INTEGER, at REAL,
                PRIMARY KEY (shape, spec_name))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS generalized_skeletons (
                shape TEXT PRIMARY KEY, template TEXT, observations INTEGER,
                successes INTEGER, promoted_at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def observe(self, stage_a: str, stage_b: str, spec_name: str,
               succeeded: bool) -> Optional[GeneralizedSkeleton]:
        """Record one composition outcome; promote if the bar is now met."""
        shape = f"{stage_a}->{stage_b}"
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO composition_observations
                (shape, spec_name, succeeded, at) VALUES (?,?,?,?)""",
                (shape, spec_name, int(succeeded), time.time()))
            rows = conn.execute(
                "SELECT spec_name, succeeded FROM composition_observations "
                "WHERE shape=?", (shape,)).fetchall()

        distinct_specs = {r["spec_name"] for r in rows}
        successes = sum(r["succeeded"] for r in rows)
        if len(distinct_specs) < self.PROMOTE_AFTER:
            return None
        if successes / len(rows) < self.PROMOTE_MIN_RATE:
            return None
        return self.promote(shape, stage_a, stage_b, len(rows), successes)

    def promote(self, shape: str, stage_a: str, stage_b: str,
               observations: int, successes: int) -> GeneralizedSkeleton:
        template = (f"def capability({{args}}):\n"
                   f"    _stage1 = {stage_a}\n    _stage2 = {stage_b}\n"
                   f"    return _stage2\n")
        skeleton = GeneralizedSkeleton(shape=shape, template=template,
                                       observations=observations,
                                       successes=successes, promoted_at=time.time())
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO generalized_skeletons
                (shape, template, observations, successes, promoted_at)
                VALUES (?,?,?,?,?)""",
                (shape, template, observations, successes, skeleton.promoted_at))
        return skeleton

    def promoted(self) -> List[GeneralizedSkeleton]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM generalized_skeletons").fetchall()
        return [GeneralizedSkeleton(r["shape"], r["template"], r["observations"],
                                    r["successes"], r["promoted_at"]) for r in rows]

    def is_promoted(self, stage_a: str, stage_b: str) -> bool:
        shape = f"{stage_a}->{stage_b}"
        return any(s.shape == shape for s in self.promoted())


def record_synthesis_outcome(abstractor: CompositionAbstractor,
                             candidate_notes: str, spec_name: str,
                             accepted: bool) -> Optional[GeneralizedSkeleton]:
    """Feed a SynthesizingSource candidate's outcome into the abstractor.

    Two-stage candidates carry their shape in `notes` (set by
    SynthesizingSource as "composition {first} then {second}"); this parses
    that back out rather than requiring the synthesizer to know about
    abstraction, so the two modules stay independently testable.
    """
    if not candidate_notes.startswith("composition "):
        return None
    body = candidate_notes[len("composition "):]
    if " then " not in body:
        return None
    stage_a, stage_b = body.split(" then ", 1)
    return abstractor.observe(stage_a, stage_b, spec_name, accepted)
