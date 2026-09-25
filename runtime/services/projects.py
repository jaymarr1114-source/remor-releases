"""
swarm_engine/services/projects.py

ProjectService: persistence, listing, transition, and monitored work-loop
execution for REMOR projects — the API surface the GUI calls.

Built on real machinery only:
  * ProjectIngestor / ProjectStore (swarm_engine.project.ingestion)
  * ProjectLifecycle / ProjectProgress (swarm_engine.project.lifecycle)
  * ProjectWorkLoop (swarm_engine.project.loop)
  * RunControl / set_current / RunStopped (swarm_engine.services.run_control,
    the frozen preemption contract)

Honest design notes (read before extending):
  * A freshly created project has NO project_state row. ProjectLifecycle
    treats a missing row as INGESTED, and history() therefore begins at the
    first real transition. We do not insert a fake "created" log row.
  * run_loop() requires the project to already be in EXECUTING. It will not
    auto-advance ingested->analyzing->planning->executing, because those
    stages did not run; the caller must transition explicitly (the GUI can
    chain transition() calls, each of which is a real persisted log entry).
  * run_loop() requires an engine bound at construction. engine=None is a
    legal service configuration (create/list/get/transition all work), but
    run_loop() then returns an honest error instead of pretending.
  * Loop runs on a dedicated daemon thread with a RunControl installed via
    set_current(). The per-round checkpoint() inside ProjectWorkLoop.run()
    is what makes stop/pause cooperative; without it, request_stop() would
    only take effect at whatever checkpoints exist.
  * Lifecycle settle mapping on job finish (all transitions are legal per
    the lifecycle table; a refused transition is recorded, never forced):
      completed + report.success  -> EXECUTING -> VERIFYING
      completed + not success     -> EXECUTING -> BLOCKED
      stopped (RunStopped)        -> EXECUTING -> BLOCKED (reason: operator)
      failed (worker exception)   -> EXECUTING -> BLOCKED (reason: error)
    VERIFYING -> COMPLETE is deliberately NOT taken automatically: the
    loop's own test pass is recorded as the verification evidence, but
    final acceptance stays an explicit operator transition.
  * Progress persisted from the report is gap-derived: outstanding =
    remaining gap kinds/paths, satisfied = [] (the loop does not track
    requirement-level satisfaction — claiming otherwise would be invented).
    cycles = report rounds. fraction_complete is therefore 1.0 on a clean
    success and 0.0 while gaps remain; that is the honest reading of this
    representation, not a project-health metric.
  * delete_project is declared but ABSENT (returns an honest refusal):
    ingested project roots, store rows, and lifecycle history have no
    governed deletion path; a partial delete would orphan history.
  * Monitoring: loop_status() reports rounds_done live (counted from real
    test-run invocations, baseline excluded; approximate while running) and
    the authoritative report.rounds once finished.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from swarm_engine.project.ingestion import ProjectIngestor, ProjectStore
from swarm_engine.project.lifecycle import (
    IllegalProjectTransition,
    ProjectLifecycle,
    ProjectProgress,
    ProjectState,
    _TRANSITIONS,
)
from swarm_engine.project.loop import ProjectWorkLoop
from swarm_engine.services.run_control import (
    RunControl,
    RunStopped,
    set_current,
)

_TERMINAL = ("completed", "failed", "stopped")


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


class ProjectService:
    """GUI-facing project API over the real project machinery."""

    def __init__(self, db_path: str, projects_root: str, engine=None):
        self._db_path = db_path
        self._projects_root = os.path.abspath(projects_root)
        os.makedirs(self._projects_root, exist_ok=True)
        self._engine = engine
        self._store = ProjectStore(db_path)
        self._lifecycle = ProjectLifecycle(db_path)
        self._ingestor = ProjectIngestor(self._projects_root)
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._running_projects: set = set()

    # ------------------------------------------------------------------
    # create / list / get / transition
    # ------------------------------------------------------------------
    def create(self, source: Dict[str, Any]) -> Dict[str, Any]:
        """Ingest a project.

        source is one of:
          {"kind": "zip",  "path": <zipfile>, "project_id": <optional>}
          {"kind": "dir",  "path": <directory>, "project_id": <optional>}
          {"kind": "blank", "project_id": <id>}
        "dir" ingestion indexes the directory IN PLACE (it is not copied).
        """
        source = source or {}
        kind = source.get("kind")
        try:
            if kind == "zip":
                path = source.get("path") or ""
                if not os.path.isfile(path):
                    return {"ok": False,
                            "error": f"zip not found: {path!r}"}
                model = self._ingestor.ingest_zip(
                    path, source.get("project_id") or _new_id())
            elif kind == "dir":
                path = source.get("path") or ""
                if not os.path.isdir(path):
                    return {"ok": False,
                            "error": f"directory not found: {path!r}"}
                model = self._ingestor.ingest_directory(
                    path, source.get("project_id") or _new_id())
            elif kind == "blank":
                project_id = source.get("project_id") or _new_id()
                root = os.path.join(self._projects_root, project_id)
                os.makedirs(root, exist_ok=True)
                with open(os.path.join(root, "README.md"), "w",
                          encoding="utf-8") as fh:
                    fh.write(f"# {project_id}\n\nBlank REMOR project.\n")
                with open(os.path.join(root, "main.py"), "w",
                          encoding="utf-8") as fh:
                    fh.write('"""Blank project placeholder."""\n\n'
                             'def main():\n'
                             '    raise NotImplementedError('
                             '"blank project: no behavior yet")\n')
                model = self._ingestor.index(root, project_id)
            else:
                return {"ok": False,
                        "error": f"unknown source kind: {kind!r} "
                                 f"(expected 'zip', 'dir', or 'blank')"}
            self._store.save(model)
        except Exception as exc:  # ingest raises ValueError on bad archives
            return {"ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "project_id": model.project_id,
                "root": model.root, "file_count": len(model.files),
                "requirement_count": len(model.requirements)}

    def list_projects(self) -> List[Dict[str, Any]]:
        """One row per persisted project, merging store + lifecycle + progress."""
        rows = []
        for project_id in self._store.list():
            try:
                state = self._lifecycle.state_of(project_id).value
                corrupt = None
            except ValueError as exc:
                # Fail closed: the persisted state is not a legal ProjectState.
                # Report it as corrupt rather than silently dropping the row
                # or inventing a state.
                state = "corrupt"
                corrupt = f"persisted state is not a legal ProjectState: {exc}"
            progress = self._lifecycle.load_progress(project_id)
            if progress is not None:
                fraction = progress.as_dict()["fraction_complete"]
                cycles = progress.cycles
                updated_at = progress.updated_at
            else:
                # No progress ever recorded: nothing is complete.
                fraction, cycles, updated_at = 0.0, 0, None
            row = {"project_id": project_id, "state": state,
                   "fraction_complete": fraction, "cycles": cycles,
                   "updated_at": updated_at}
            if corrupt:
                row["error"] = corrupt
            rows.append(row)
        return rows

    def get_project(self, project_id: str) -> Dict[str, Any]:
        model = self._store.get(project_id)
        if model is None:
            return {"ok": False, "error": f"not found: {project_id}"}
        try:
            state = self._lifecycle.state_of(project_id)
        except ValueError as exc:
            return {"ok": False,
                    "error": f"corrupt persisted state for {project_id}: {exc}"}
        progress = self._lifecycle.load_progress(project_id)
        return {"ok": True, "project_id": project_id, "model": model,
                "state": state.value,
                "history": self._lifecycle.history(project_id),
                "progress": progress.as_dict() if progress else None,
                "legal_next": [s.value for s in _TRANSITIONS[state]]}

    def transition(self, project_id: str, to_state: str,
                   reason: str = "") -> Dict[str, Any]:
        if self._store.get(project_id) is None:
            return {"ok": False, "error": f"not found: {project_id}"}
        try:
            target = ProjectState(to_state)
        except ValueError:
            return {"ok": False,
                    "error": f"unknown state {to_state!r} (legal states: "
                             f"{[s.value for s in ProjectState]})"}
        try:
            self._lifecycle.transition(project_id, target, reason=reason)
        except IllegalProjectTransition as exc:
            # The exception message names the legal options; surface it.
            return {"ok": False, "error": str(exc)}
        except ValueError as exc:
            return {"ok": False,
                    "error": f"corrupt persisted state for {project_id}: {exc}"}
        return {"ok": True, "project_id": project_id, "state": target.value}

    def delete_project(self, project_id: str) -> Dict[str, Any]:
        # ABSENT by design: there is no governed deletion path for an
        # ingested project root + store row + lifecycle history. Deleting
        # the files while leaving history (or vice versa) would orphan
        # state; refusing is the honest behavior until such a path exists.
        return {"ok": False,
                "error": "ABSENT: project deletion has no governed path "
                         "(ingested roots, store rows, and lifecycle history "
                         "would be orphaned); refusing rather than deleting "
                         "partially"}

    # ------------------------------------------------------------------
    # run_loop (background) + monitoring
    # ------------------------------------------------------------------
    def run_loop(self, project_id: str,
                 max_rounds: int = 4) -> Dict[str, Any]:
        """Start a ProjectWorkLoop on a background thread.

        Requires the project to exist, an engine bound at construction, and
        the lifecycle already in EXECUTING (advance with transition() first).
        """
        model = self._store.get(project_id)
        if model is None:
            return {"ok": False, "error": f"not found: {project_id}"}
        if self._engine is None:
            return {"ok": False,
                    "error": "no engine bound to this ProjectService; "
                             "run_loop requires an engine"}
        try:
            state = self._lifecycle.state_of(project_id)
        except ValueError as exc:
            return {"ok": False,
                    "error": f"corrupt persisted state for {project_id}: {exc}"}
        if state is not ProjectState.EXECUTING:
            return {"ok": False,
                    "error": f"run_loop requires state 'executing' "
                             f"(currently '{state.value}'); advance with "
                             f"transition() first"}
        with self._lock:
            if project_id in self._running_projects:
                return {"ok": False,
                        "error": f"a loop is already running for {project_id}"}
            job_id = uuid.uuid4().hex
            control = RunControl()
            job: Dict[str, Any] = {
                "job_id": job_id, "project_id": project_id,
                "project_root": model["root"], "max_rounds": max_rounds,
                "status": "starting", "control": control, "thread": None,
                "rounds_done": 0, "report": None, "error": None,
                "started_at": time.time(), "ended_at": None,
                "settle_note": "",
            }
            thread = threading.Thread(target=self._run_worker, args=(job_id,),
                                      name=f"remor-project-loop-{job_id[:8]}",
                                      daemon=True)
            job["thread"] = thread
            self._jobs[job_id] = job
            self._running_projects.add(project_id)
            thread.start()
        return {"ok": True, "job_id": job_id, "project_id": project_id}

    def _run_worker(self, job_id: str) -> None:
        job = self._jobs[job_id]
        control: RunControl = job["control"]
        set_current(control)  # frozen contract: install on the worker thread
        try:
            with self._lock:
                job["status"] = "running"
            loop = ProjectWorkLoop(self._engine, job["project_root"],
                                   job["max_rounds"])
            # Live round counting, composed without touching the loop:
            # run_tests() is invoked once for the baseline plus once per
            # round, so completed rounds ~= invocations - 1 while running.
            orig_run_tests = loop.run_tests
            calls = {"n": 0}

            def _counting():
                result = orig_run_tests()
                calls["n"] += 1
                with self._lock:
                    job["rounds_done"] = max(0, calls["n"] - 1)
                return result

            loop.run_tests = _counting  # type: ignore[method-assign]
            try:
                report = loop.run()
            except RunStopped as exc:
                self._finish_job(job_id, "stopped", error=str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - worker must not die silent
                self._finish_job(
                    job_id, "failed",
                    error=f"{type(exc).__name__}: {exc}")
                return
            self._finish_job(job_id, "completed", report=report.as_dict())
        finally:
            set_current(None)
            with self._lock:
                self._running_projects.discard(job["project_id"])

    def _finish_job(self, job_id: str, status: str,
                    report: Optional[Dict[str, Any]] = None,
                    error: Optional[str] = None) -> None:
        job = self._jobs[job_id]
        with self._lock:
            job["status"] = status
            job["ended_at"] = time.time()
            if report is not None:
                job["report"] = report
                job["rounds_done"] = int(report.get("rounds", 0) or 0)
            if error is not None:
                job["error"] = error
        self._settle_lifecycle(job, status)

    def _settle_lifecycle(self, job: Dict[str, Any], status: str) -> None:
        """Persist report progress and move the lifecycle legally.

        Mapping (documented in the module docstring):
          completed + success -> EXECUTING -> VERIFYING
          completed + failure -> EXECUTING -> BLOCKED
          stopped             -> EXECUTING -> BLOCKED (operator stop)
          failed (exception)  -> EXECUTING -> BLOCKED (error)
        A refused transition is recorded on the job, never forced.
        """
        project_id = job["project_id"]
        report = job.get("report") or {}
        rounds = int(job.get("rounds_done") or 0)
        gaps = report.get("gaps") or []
        progress = ProjectProgress(
            project_id=project_id,
            satisfied_requirements=[],
            outstanding_requirements=[
                f"{g.get('kind')}:{g.get('path')}" for g in gaps
                if isinstance(g, dict)],
            attempted_capabilities=[],
            cycles=rounds,
        )
        try:
            self._lifecycle.save_progress(progress)
        except Exception as exc:  # noqa: BLE001 - record, don't crash settle
            job["settle_note"] += f" progress persist failed: {exc};"
        reason_base = f"run_loop {status}: {rounds} rounds"
        try:
            if status == "completed" and report.get("success"):
                self._lifecycle.transition(
                    project_id, ProjectState.VERIFYING,
                    reason=f"{reason_base}, loop tests passed "
                           f"({len(report.get('test_runs', []))} test runs)")
            elif status == "completed":
                self._lifecycle.transition(
                    project_id, ProjectState.BLOCKED,
                    reason=f"{reason_base}, loop tests still failing")
            elif status == "stopped":
                self._lifecycle.transition(
                    project_id, ProjectState.BLOCKED,
                    reason=f"{reason_base}, stopped by operator")
            else:  # failed
                self._lifecycle.transition(
                    project_id, ProjectState.BLOCKED,
                    reason=f"{reason_base}, worker error: {job.get('error')}")
        except IllegalProjectTransition as exc:
            job["settle_note"] += f" transition refused: {exc};"
        except Exception as exc:  # noqa: BLE001 - record, don't crash settle
            job["settle_note"] += f" settle error: {exc};"

    def loop_status(self, job_id: str) -> Dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job: {job_id}"}
        with self._lock:
            return {"ok": True, "job_id": job_id,
                    "project_id": job["project_id"],
                    "status": job["status"],
                    "rounds_done": job["rounds_done"],
                    "report": job["report"],  # None until finished
                    "error": job["error"],
                    "settle_note": job["settle_note"] or None}

    def stop_loop(self, job_id: str) -> Dict[str, Any]:
        """Request cooperative stop. Takes effect at the next loop checkpoint
        (per-round inside ProjectWorkLoop.run()); never kills the thread."""
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": f"unknown job: {job_id}"}
        with self._lock:
            status = job["status"]
        if status in _TERMINAL:
            return {"ok": True, "job_id": job_id, "status": status,
                    "note": "already finished; stop had no effect"}
        job["control"].request_stop()
        return {"ok": True, "job_id": job_id, "status": status,
                "note": "stop requested; takes effect at next checkpoint"}
