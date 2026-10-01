"""
swarm_engine/core/autonomy.py

Long-running operation, resource budgets, and safe self-modification.

Three concerns that only appear once SWarm runs unattended.

BUDGETS make refusal possible. Without a resource ceiling an autonomous engine
has no way to decline work, and "can this plan work?" quietly becomes the only
question it ever asks. A budget lets it ask "can I afford this?" and stop
before exhausting the machine rather than after.

OBJECTIVES survive restarts. An objective is checkpointed after every step, so
an engine killed mid-run resumes where it stopped instead of repeating
completed work or losing it. Progress that only exists in memory is progress
that a power cut deletes.

SELF-MODIFICATION is transactional and reversible. Changes are staged, applied
under a snapshot, verified, and committed only if verification passes;
otherwise the snapshot is restored. The engine's own core files are protected
by default, because a system that can rewrite its own recovery path can put
itself somewhere it cannot recover from.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# RESOURCE BUDGETS
# ---------------------------------------------------------------------------

class BudgetExceeded(Exception):
    pass


@dataclass
class ResourceBudget:
    """A spend limit across a run. Every field is optional; None means no cap."""
    wall_clock_s: Optional[float] = None
    primitive_calls: Optional[int] = None
    synthesis_attempts: Optional[int] = None
    acquisitions: Optional[int] = None
    max_task_depth: Optional[int] = 8
    memory_mb: Optional[int] = None
    disk_mb: Optional[int] = None
    concurrency: Optional[int] = None
    sandbox_runs: Optional[int] = None

    started_at: float = field(default_factory=time.time)
    spent_calls: int = 0
    spent_synthesis: int = 0
    spent_acquisitions: int = 0
    spent_sandbox_runs: int = 0
    peak_memory_mb: float = 0.0
    active_concurrency: int = 0

    def sample_memory(self) -> float:
        """Record current RSS. Best-effort: on platforms without resource
        accounting this returns 0 rather than pretending to a number, because
        a fabricated memory figure is worse than an absent one."""
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # ru_maxrss is KB on Linux, bytes on macOS.
            mb = usage / 1024 if usage > 10_000_000 else usage / 1024
            self.peak_memory_mb = max(self.peak_memory_mb, mb)
            return self.peak_memory_mb
        except Exception:
            return 0.0

    def elapsed_s(self) -> float:
        return time.time() - self.started_at

    def remaining_s(self) -> Optional[float]:
        if self.wall_clock_s is None:
            return None
        return max(0.0, self.wall_clock_s - self.elapsed_s())

    def spend(self, calls: int = 0, synthesis: int = 0, acquisitions: int = 0,
              sandbox_runs: int = 0) -> None:
        self.spent_calls += calls
        self.spent_synthesis += synthesis
        self.spent_acquisitions += acquisitions
        self.spent_sandbox_runs += sandbox_runs
        self.check()

    def check(self) -> None:
        if self.wall_clock_s is not None and self.elapsed_s() > self.wall_clock_s:
            raise BudgetExceeded(
                f"wall clock budget exhausted ({self.elapsed_s():.1f}s > {self.wall_clock_s}s)")
        if self.primitive_calls is not None and self.spent_calls > self.primitive_calls:
            raise BudgetExceeded(
                f"primitive call budget exhausted ({self.spent_calls} > {self.primitive_calls})")
        if self.synthesis_attempts is not None and self.spent_synthesis > self.synthesis_attempts:
            raise BudgetExceeded(
                f"synthesis budget exhausted ({self.spent_synthesis} > {self.synthesis_attempts})")
        if self.acquisitions is not None and self.spent_acquisitions > self.acquisitions:
            raise BudgetExceeded(
                f"acquisition budget exhausted ({self.spent_acquisitions} > {self.acquisitions})")
        if self.sandbox_runs is not None and self.spent_sandbox_runs > self.sandbox_runs:
            raise BudgetExceeded(
                f"sandbox budget exhausted ({self.spent_sandbox_runs} > {self.sandbox_runs})")
        if self.memory_mb is not None:
            self.sample_memory()
            if self.peak_memory_mb > self.memory_mb:
                raise BudgetExceeded(
                    f"memory budget exhausted ({self.peak_memory_mb:.0f}MB > {self.memory_mb}MB)")

    def affordable(self, estimated_calls: int = 1) -> Tuple[bool, str]:
        """Ask before spending. This is what lets the planner choose the best
        *feasible* plan rather than the first valid one."""
        if self.primitive_calls is not None:
            if self.spent_calls + estimated_calls > self.primitive_calls:
                return False, "would exceed the primitive call budget"
        remaining = self.remaining_s()
        if remaining is not None and remaining <= 0:
            return False, "no wall clock budget remains"
        return True, "within budget"

    def as_dict(self) -> Dict[str, Any]:
        return {"elapsed_s": round(self.elapsed_s(), 2),
                "remaining_s": (None if self.remaining_s() is None
                                else round(self.remaining_s(), 2)),
                "spent_calls": self.spent_calls,
                "spent_synthesis": self.spent_synthesis,
                "spent_acquisitions": self.spent_acquisitions,
                "spent_sandbox_runs": self.spent_sandbox_runs,
                "peak_memory_mb": round(self.peak_memory_mb, 1)}


# ---------------------------------------------------------------------------
# LONG-RUNNING OBJECTIVES
# ---------------------------------------------------------------------------

class ObjectiveState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass
class Objective:
    objective_id: str
    goal: str
    payload: Dict[str, Any] = field(default_factory=dict)
    state: ObjectiveState = ObjectiveState.PENDING
    steps_done: List[str] = field(default_factory=list)
    last_error: str = ""
    attempts: int = 0
    result: Any = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # M+28.96: last RecoveryEngine diagnosis for this objective's most recent
    # failure, as Diagnosis.as_dict() (kind/action/detail/retryable/confidence).
    # Empty until a real failure has been diagnosed. This is provenance, not a
    # second state machine -- ObjectiveState above remains the only state.
    diagnosis: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"objective_id": self.objective_id, "goal": self.goal,
                "state": self.state.value, "steps_done": self.steps_done,
                "attempts": self.attempts, "last_error": self.last_error[:300],
                "result": self.result, "diagnosis": self.diagnosis}


class ObjectiveStore:
    """Checkpointed objectives that survive process death."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS objectives (
                    objective_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    payload TEXT,
                    state TEXT NOT NULL,
                    steps_done TEXT,
                    last_error TEXT,
                    attempts INTEGER DEFAULT 0,
                    result TEXT,
                    created_at REAL,
                    updated_at REAL
                )""")
            # M+28.96: additive column for recovery-diagnosis provenance.
            # Guarded so this remains safe against an already-migrated db
            # (fresh-process / cross-run schema reuse in earlier phases).
            existing_cols = {row[1] for row in
                             conn.execute("PRAGMA table_info(objectives)").fetchall()}
            if "diagnosis" not in existing_cols:
                conn.execute("ALTER TABLE objectives ADD COLUMN diagnosis TEXT")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, objective: Objective) -> Objective:
        objective.updated_at = time.time()
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO objectives
                (objective_id, goal, payload, state, steps_done, last_error,
                 attempts, result, created_at, updated_at, diagnosis)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
                objective.objective_id, objective.goal,
                json.dumps(objective.payload), objective.state.value,
                json.dumps(objective.steps_done), objective.last_error,
                objective.attempts, json.dumps(objective.result, default=str),
                objective.created_at, objective.updated_at,
                json.dumps(objective.diagnosis, default=str)))
        return objective

    def get(self, objective_id: str) -> Optional[Objective]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM objectives WHERE objective_id=?",
                               (objective_id,)).fetchone()
        return self._row(row) if row else None

    STALE_AFTER_S = 24 * 3600

    def _row(self, row) -> Objective:
        """Rebuild an objective, tolerating corrupted persisted state.

        A row whose JSON columns are damaged must not take down the whole
        queue: one unreadable objective would otherwise make every other
        objective unrecoverable, turning localized corruption into total loss.
        A damaged objective is surfaced as FAILED with the corruption recorded,
        so it is visible rather than silently dropped.
        """
        def _json(column, fallback):
            try:
                return json.loads(row[column] or fallback)
            except (json.JSONDecodeError, TypeError):
                return json.loads(fallback)

        try:
            state = ObjectiveState(row["state"])
        except (ValueError, KeyError):
            state = ObjectiveState.FAILED

        corrupted = []
        for column in ("payload", "steps_done", "result"):
            raw = row[column]
            if raw:
                try:
                    json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    corrupted.append(column)

        # diagnosis is additive (M+28.96) and absent on rows written before
        # that column existed; treat missing/unparseable the same as "no
        # diagnosis recorded yet" rather than corruption, since older valid
        # rows must not be quarantined for lacking a field they predate.
        try:
            diagnosis = json.loads(row["diagnosis"]) if row["diagnosis"] else {}
        except (json.JSONDecodeError, TypeError):
            diagnosis = {}

        objective = Objective(
            objective_id=row["objective_id"], goal=row["goal"],
            payload=_json("payload", "{}"),
            state=state,
            steps_done=_json("steps_done", "[]"),
            last_error=row["last_error"] or "", attempts=row["attempts"] or 0,
            result=_json("result", "null"),
            created_at=row["created_at"] or 0.0, updated_at=row["updated_at"] or 0.0,
            diagnosis=diagnosis)
        if corrupted:
            objective.state = ObjectiveState.FAILED
            objective.last_error = (f"persisted state corrupted in {corrupted}; "
                                    f"quarantined rather than replayed")
        return objective

    def stale(self, now: Optional[float] = None) -> List[Objective]:
        """Objectives that have sat unfinished long enough to be suspect."""
        now = now if now is not None else time.time()
        return [o for o in self.unfinished()
                if now - o.updated_at > self.STALE_AFTER_S]

    def unfinished(self) -> List[Objective]:
        """Objectives a previous run left in flight.

        RUNNING is included deliberately: a process that died mid-step leaves
        its objective marked RUNNING forever, and treating that as "someone
        else is handling it" is how work silently disappears across restarts.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM objectives WHERE state IN (?,?,?) ORDER BY created_at",
                (ObjectiveState.PENDING.value, ObjectiveState.RUNNING.value,
                 ObjectiveState.PAUSED.value)).fetchall()
        # Filter on the reconstructed state, not the stored column. A row whose
        # state column says PENDING but whose payload is corrupt is
        # reclassified FAILED during rebuild, and replaying it would feed
        # garbage into the engine on every cycle.
        return [o for o in (self._row(r) for r in rows)
                if o.state in (ObjectiveState.PENDING, ObjectiveState.RUNNING,
                               ObjectiveState.PAUSED)]


# ---------------------------------------------------------------------------
# SAFE SELF-MODIFICATION
# ---------------------------------------------------------------------------

class ModificationRefused(Exception):
    pass


@dataclass
class ModificationResult:
    committed: bool
    path: str
    reason: str = ""
    verification: Dict[str, Any] = field(default_factory=dict)
    rolled_back: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"committed": self.committed, "path": self.path,
                "reason": self.reason, "rolled_back": self.rolled_back,
                "verification": self.verification}


class SelfModificationGuard:
    """Transactional edits to the engine's own source.

    Protected paths are refused outright rather than merely warned about. The
    governor, the primitive core, and this guard itself are on that list: a
    change that can disable the mechanism which validates changes removes the
    only thing standing between a bad edit and an unrecoverable engine.

    Every permitted edit is snapshotted, applied, verified by a caller-supplied
    check, and committed only on success. Verification failure restores the
    snapshot, so the worst case is no change rather than a broken engine.
    """

    PROTECTED = (
        "primitives/core.py",        # the substrate everything else stands on
        "governance/",               # permission and provenance machinery
        "core/autonomy.py",          # this guard
        "verification/",             # the checks that catch bad changes
    )

    def __init__(self, root: str, snapshot_dir: Optional[str] = None,
                 allow_protected: bool = False):
        self.root = os.path.abspath(root)
        self.snapshot_dir = snapshot_dir or tempfile.mkdtemp(prefix="swarm_snapshots_")
        self.allow_protected = allow_protected
        self.history: List[Dict[str, Any]] = []

    def is_protected(self, path: str) -> bool:
        normalised = os.path.abspath(path).replace(os.sep, "/")
        return any(marker.rstrip("/") in normalised for marker in self.PROTECTED)

    def _guard_path(self, path: str) -> str:
        absolute = os.path.abspath(path)
        if not absolute.startswith(self.root):
            raise ModificationRefused(
                f"{path!r} lies outside the managed root {self.root!r}")
        if self.is_protected(absolute) and not self.allow_protected:
            raise ModificationRefused(
                f"{path!r} is protected; modifying it could disable the "
                f"machinery that validates modifications")
        return absolute

    def snapshot(self, path: str) -> str:
        absolute = os.path.abspath(path)
        digest = hashlib.sha256(absolute.encode()).hexdigest()[:12]
        destination = os.path.join(self.snapshot_dir,
                                   f"{digest}_{int(time.time()*1000)}.bak")
        os.makedirs(self.snapshot_dir, exist_ok=True)
        shutil.copy2(absolute, destination)
        return destination

    def _invalidate_bytecode_cache(self, absolute_path: str) -> None:
        """Remove any compiled bytecode for a file this guard just wrote.

        Writing new source to disk is not enough by itself. CPython decides
        whether a cached .pyc is still valid by comparing the source file's
        mtime (and size) against what the .pyc recorded at compile time. Two
        writes to the same path close enough together — a modify followed by
        a same-transaction restore, which is exactly apply()'s failure path —
        can land on filesystems whose mtime resolution is coarser than the
        gap between the writes, so the second write is invisible to the
        staleness check and a subprocess started right after can silently
        execute bytecode compiled from the version that was supposed to have
        been reverted. This surfaced as a real, reproducible failure during
        development: an intermediate, broken version of a file got cached,
        and a subprocess spawned moments after the file was corrected loaded
        the stale, broken bytecode instead. A write from this guard now always
        clears the corresponding cache entry, so the next import is forced to
        recompile from whatever is actually on disk.
        """
        try:
            import importlib.util
            cached = importlib.util.cache_from_source(absolute_path)
            if cached and os.path.exists(cached):
                os.remove(cached)
        except Exception:
            # Best-effort: a cache miss just costs one recompilation, and a
            # failure here must never be allowed to mask the real modification
            # outcome the caller is waiting on.
            pass

    def apply(self, path: str, new_source: str,
              verify: Optional[Callable[[], Tuple[bool, Dict[str, Any]]]] = None
              ) -> ModificationResult:
        absolute = self._guard_path(path)
        if not os.path.isfile(absolute):
            raise ModificationRefused(f"{path!r} does not exist")

        # Refuse source that does not parse before it ever reaches disk.
        try:
            compile(new_source, absolute, "exec")
        except SyntaxError as exc:
            return ModificationResult(False, path,
                                      reason=f"replacement does not compile: {exc}")

        backup = self.snapshot(absolute)
        with open(absolute, "w") as fh:
            fh.write(new_source)
        self._invalidate_bytecode_cache(absolute)

        verification: Dict[str, Any] = {}
        if verify is not None:
            try:
                passed, verification = verify()
            except Exception as exc:
                passed, verification = False, {"error": f"{type(exc).__name__}: {exc}"}
            if not passed:
                shutil.copy2(backup, absolute)
                self._invalidate_bytecode_cache(absolute)
                self.history.append({"path": path, "committed": False,
                                     "restored_from": backup})
                return ModificationResult(False, path,
                                          reason="verification failed; change reverted",
                                          verification=verification, rolled_back=True)

        self.history.append({"path": path, "committed": True, "backup": backup})
        return ModificationResult(True, path, reason="committed",
                                  verification=verification)

    def rollback_last(self) -> ModificationResult:
        for entry in reversed(self.history):
            if entry.get("committed") and entry.get("backup"):
                target = os.path.abspath(entry["path"])
                shutil.copy2(entry["backup"], target)
                self._invalidate_bytecode_cache(target)
                entry["committed"] = False
                return ModificationResult(False, entry["path"],
                                          reason="rolled back", rolled_back=True)
        return ModificationResult(False, "", reason="nothing to roll back")
