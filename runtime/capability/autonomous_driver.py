"""
swarm_engine/capability/autonomous_driver.py

SwarmAutonomousDriver: the unifying entry point Prompts 1-3 never built.

Every mechanism this file uses is already real and independently tested:
GeneralSynthesizer, PrimitivePromoter, IterativePrimitiveGrower,
GenericCapabilityGrowthEngine, QueuedKnowledgeSource,
CoordinatedGrowthOrchestrator. What did not exist until now is a single
entry point that decides, ON ITS OWN, which of these to invoke for a given
requirement, and that can PAUSE on a genuine information need and RESUME
later — rather than a human writing a new driver script per task, hand-
sequencing calls to each mechanism, which is what every earlier benchmark
this session actually was.

The loop:

  perceive (ingest the project)
    -> plan (decompose into per-file requirements + real dependencies,
       both read from the project's own structure, not invented)
    -> attempt growth in dependency order via the SAME
       CoordinatedGrowthOrchestrator built in Prompt 3, which itself tries
       composition first and falls through to external knowledge
    -> if a real information need surfaces, CHECKPOINT rather than block —
       SWarm's process genuinely cannot wait on real network I/O
       synchronously
    -> validate the resulting SYSTEM (Prompt 3), persist, report progress
       honestly: done, blocked (naming exactly what's pending), or failed

This file adds no new way to construct a capability. It adds the decision
of which existing way to use, and the checkpoint discipline to keep
pursuing an objective across a real, unavoidable pause.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.capability.coordinated_growth import (
    CoordinatedGrowthOrchestrator, FileRequirement, ProjectRequirement,
)
from swarm_engine.capability.external_knowledge import QueuedKnowledgeSource
from swarm_engine.capability.growth_engine import GenericCapabilityGrowthEngine
from swarm_engine.capability.requirement import CapabilityRequirement

_FILE_HEADING = re.compile(r"^([\w./-]+\.\w+)$")
_DEPENDS_PHRASE = re.compile(
    r"\bimports?\b\s+(?:the\s+)?([\w]+)(?:\s+module)?\b|"
    r"\buses?\b.{0,20}?\bthe\s+([\w]+)\s+module\b",
    re.IGNORECASE)
# A generic worked-example pattern: one or more `name=value` pairs, an
# arrow, then a result (optionally itself `name=value`). A "value" is
# either a scalar number or a bracketed list of numbers — generalized from
# scalar-only, found necessary directly when a real list-typed task
# ("values=[3,7,1,9] -> 3") silently fell through to the wrong acquisition
# route because the old pattern only matched `\w+\s*=\s*-?[\d.]+` and
# never recognized "[3,7,1,9]" as a value at all. Not tied to any one
# project's param names or to lists specifically — "n=8 -> 3",
# "width=3, height=4 -> perimeter=14", and "values=[3,7,1,9] -> 3" all
# match the same pattern.
_VALUE = r"(?:-?[\d.]+|\[[^\]]*\])"
_EXAMPLE_PATTERN = re.compile(
    rf"((?:\w+\s*=\s*{_VALUE}\s*,?\s*)+)->\s*(?:\w+\s*=\s*)?({_VALUE})")
_ARG_PATTERN = re.compile(rf"(\w+)\s*=\s*({_VALUE})")


def _parse_value(raw: str):
    """Turns an extracted value STRING into a real Python value — a float
    for a scalar, or a list of floats for a bracketed literal. Kept as one
    shared parser (not inlined at each call site) so args and results are
    parsed identically."""
    raw = raw.strip()
    if raw.startswith("["):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [float(x.strip()) for x in inner.split(",") if x.strip()]
    return float(raw)


@dataclass
class DriverState:
    objective: str
    project_root: str
    status: str = "in_progress"
    last_report: Optional[Dict[str, Any]] = None
    cycles: int = 0
    last_signature: Optional[str] = None
    last_fulfilled_count: int = 0
    consecutive_stagnant_cycles: int = 0
    escalation_attempted: bool = False
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"objective": self.objective, "project_root": self.project_root,
                "status": self.status, "last_report": self.last_report,
                "cycles": self.cycles, "last_signature": self.last_signature,
                "last_fulfilled_count": self.last_fulfilled_count,
                "consecutive_stagnant_cycles": self.consecutive_stagnant_cycles,
                "escalation_attempted": self.escalation_attempted,
                "updated_at": self.updated_at}


class DriverStateStore:
    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS driver_state (
                project_root TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL)""")

    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, state: DriverState) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO driver_state
                (project_root, data, updated_at) VALUES (?,?,?)""",
                (state.project_root, json.dumps(state.as_dict()), time.time()))

    def load(self, project_root: str) -> Optional[DriverState]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM driver_state WHERE project_root=?",
                (project_root,)).fetchone()
        if row is None:
            return None
        return DriverState(**json.loads(row["data"]))


class SwarmAutonomousDriver:
    def __init__(self, swarm_engine, project_root: str, db_path: str = "swarm_engine.db"):
        self.engine = swarm_engine
        self.project_root = project_root
        self.knowledge_source = QueuedKnowledgeSource(db_path=db_path)
        self.growth = GenericCapabilityGrowthEngine(
            swarm_engine=swarm_engine, project_root=project_root,
            knowledge_source=self.knowledge_source, provenance=swarm_engine.provenance)
        self.orchestrator = CoordinatedGrowthOrchestrator(self.growth, project_root)
        self.state_store = DriverStateStore(db_path=db_path)

    def _decompose(self) -> ProjectRequirement:
        model = self.engine.project_ingestor.ingest_directory(
            self.project_root, project_id="autonomous_pursuit")
        self.engine.projects.save(model)

        by_file: Dict[str, List[str]] = {}
        for r in model.requirements:
            heading = r.heading.strip()
            m = _FILE_HEADING.match(heading)
            key = m.group(1) if m else "_unassigned"
            by_file.setdefault(key, []).append(r.text)

        files: List[FileRequirement] = []
        for filename, texts in by_file.items():
            if filename == "_unassigned":
                continue
            description = " ".join(texts)
            depends_on = []
            for text in texts:
                for m in _DEPENDS_PHRASE.finditer(text):
                    module = m.group(1) or m.group(2)
                    if module:
                        dep_file = f"{module}.py"
                        if dep_file != filename:
                            depends_on.append(module)

            # Real worked-example extraction, generic across param names —
            # without this, nothing is ever is_example_driven() and the
            # composition/iteration routes this driver is supposed to be
            # able to choose between can never actually fire; every prior
            # benchmark constructed CapabilityRequirement.examples by hand.
            examples = []
            param_names = None
            for text in texts:
                for m in _EXAMPLE_PATTERN.finditer(text):
                    args_str, output_str = m.groups()
                    args = {k: _parse_value(v) for k, v in _ARG_PATTERN.findall(args_str)}
                    if not args:
                        continue
                    if param_names is None:
                        param_names = tuple(args.keys())
                    if tuple(args.keys()) == param_names:
                        examples.append((args, _parse_value(output_str)))

            key = filename.rsplit(".", 1)[0]
            files.append(FileRequirement(
                key=key,
                requirement=CapabilityRequirement(
                    description=description, target_domain="python",
                    examples=examples or None, param_names=param_names,
                    constraints={"target_path": filename,
                                "real_dependencies": depends_on}),
                depends_on=depends_on))

        return ProjectRequirement(objective="pursue REQUIREMENTS.md", files=files)

    def _compute_signature(self, report) -> str:
        """A fingerprint of 'what actually happened,' not just 'did it
        succeed' — includes each file's outcome, validation level, and the
        actual bytes of what was produced (hashed), plus the system
        validation's own verdict. Two cycles producing the IDENTICAL
        signature did not make ANY progress, however many times pursue()
        is called between them — this is what replaces an arbitrary retry
        count with an actual fact about whether anything changed."""
        import hashlib
        import os
        parts = []
        for key in sorted(report.results.keys()):
            r = report.results[key]
            content_hash = ""
            if r.artifact_path:
                full_path = os.path.join(self.project_root, r.artifact_path)
                if os.path.exists(full_path):
                    with open(full_path, "rb") as fh:
                        content_hash = hashlib.sha256(fh.read()).hexdigest()[:12]
            parts.append(f"{key}:{r.succeeded}:{r.validation_level}:"
                        f"{content_hash}:{r.route_attempted}")
        parts.append(f"sysval:{report.system_validation.get('valid') if report.system_validation else None}")
        parts.append(f"blocked:{sorted(report.blocked)}")
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    def _escalate(self, project: ProjectRequirement, report) -> bool:
        """Change strategy, not just retry: for every file that grew
        successfully on its own but whose content the SYSTEM validation
        flagged as wrong (the exact 2+-dependency deadlock shape — an
        individually-succeeding file reusing a cached answer that doesn't
        actually satisfy the project), invalidate that cached answer and
        re-formulate a strengthened query using the validation failure's
        own diagnostic text, so the next research() call asks for
        something different rather than replaying the same content.
        Returns whether any escalation action was actually taken."""
        if report.system_validation is None:
            return False
        failing_files = {c["file"] for c in report.system_validation.get("checks", [])
                         if not c.get("valid", True) and "file" in c}
        acted = False
        by_key = {f.key: f for f in project.files}
        for key in failing_files:
            f = by_key.get(key)
            result = report.results.get(key)
            if f is None or result is None or result.route_attempted != "external_knowledge":
                continue
            old_query = self.growth._formulate_query(f.requirement)
            if self.knowledge_source.invalidate(old_query):
                reasons = [c["reason"] for c in report.system_validation["checks"]
                          if c.get("file") == key and not c.get("valid", True)]
                f.requirement.description = (
                    f.requirement.description +
                    f" (previous attempt failed: {'; '.join(reasons)[:200]})")
                acted = True
        return acted

    def pursue(self) -> DriverState:
        project = self._decompose()
        report = self.orchestrator.grow(project)
        signature = self._compute_signature(report)

        state = self.state_store.load(self.project_root) or DriverState(
            objective=project.objective, project_root=self.project_root)
        state.cycles += 1
        state.last_report = report.as_dict()

        fulfilled_now = self.knowledge_source.fulfilled_count()
        got_new_information = fulfilled_now > state.last_fulfilled_count
        no_change = (state.last_signature is not None and
                    state.last_signature == signature and not got_new_information)

        if report.succeeded and report.fully_objective_verified:
            state.status = "done"
            state.consecutive_stagnant_cycles = 0
        elif report.succeeded and report.has_unverified_components:
            # Executes cleanly and nothing detected as wrong, but not
            # every component had a real oracle to check against — the
            # honest distinction Fix 2 exists to make. Never collapsed
            # into plain "done", which is what let an executable-but-wrong
            # program look identical to a genuinely verified one.
            state.status = "done_unverified"
            state.consecutive_stagnant_cycles = 0
        elif no_change:
            state.consecutive_stagnant_cycles += 1
            if not state.escalation_attempted:
                escalated = self._escalate(project, report)
                state.escalation_attempted = True
                state.status = "blocked" if escalated else "failed"
                if not escalated:
                    state.status = "failed"
            else:
                # Escalation was already tried once and stagnation
                # persisted anyway — bounded, honest termination rather
                # than an indefinite "in_progress".
                state.status = "failed"
        elif self.knowledge_source.pending():
            state.status = "blocked"
            state.consecutive_stagnant_cycles = 0
        elif report.blocked:
            state.status = "failed"
            state.consecutive_stagnant_cycles = 0
        else:
            state.status = "in_progress"
            state.consecutive_stagnant_cycles = 0

        state.last_signature = signature
        state.last_fulfilled_count = fulfilled_now
        self.state_store.save(state)
        return state

    def resume(self) -> DriverState:
        return self.pursue()

    def pending_information_needs(self) -> List[Dict[str, Any]]:
        return self.knowledge_source.pending()
