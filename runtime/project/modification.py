"""
swarm_engine/project/modification.py

Safe autonomous modification of project files.

This is deliberately the same shape as SelfModificationGuard, not a new
design: snapshot, apply, verify, commit-or-rollback. That pattern was already
proven under real failure injection (crash mid-verification, syntax errors,
rollback-of-rollback). Re-deriving a different mechanism for project files
would mean re-earning trust a working design already has.

What's different: the protected-path concept becomes "don't touch files
outside the project root" rather than "don't touch the engine's own core" —
a project has no equivalent of primitives/core.py to protect by default, but
the caller can still declare paths off-limits (a vendored dependency
directory, a lockfile) the same way.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


class ProjectModificationRefused(Exception):
    pass


@dataclass
class ProjectChangeResult:
    committed: bool
    path: str
    action: str                 # create | modify | delete
    reason: str = ""
    rolled_back: bool = False

    def as_dict(self) -> Dict[str, any]:
        return {"committed": self.committed, "path": self.path,
                "action": self.action, "reason": self.reason,
                "rolled_back": self.rolled_back}


class ProjectModificationGuard:
    """Transactional create/modify/delete within a project root.

    Caller authorization: the guard can be bound to an AgentDirectory
    (bind_authorization). The REPAIR-APPLICATION path -- apply_repair()
    -- then requires an authenticated caller holding
    'agent:repair_apply'. This is the governed choke point for the file
    modification that precedes repair submission/admission: an
    unauthenticated or unauthorized caller cannot stage a repair.

    The generic create()/modify()/delete() primitives are NOT
    caller-authorized. They are library functions used by
    engine-internal code paths (growth engine, project loop). Do not
    mistake them for a governed boundary: only apply_repair() is the
    authorization choke point for agent-driven repair application.
    """

    def __init__(self, root: str, protected: Optional[List[str]] = None,
                 snapshot_dir: Optional[str] = None,
                 agents: Optional[Any] = None):
        self.root = os.path.abspath(root)
        self.protected = list(protected or [])
        self.snapshot_dir = snapshot_dir or tempfile.mkdtemp(prefix="swarm_project_snap_")
        self.history: List[Dict] = []
        self.agents = agents  # AgentDirectory or None (unbound = no authz)

    def bind_authorization(self, agents: Any) -> "ProjectModificationGuard":
        """Bind an AgentDirectory; enables the authorized apply_repair()
        path. Returns self for chaining."""
        self.agents = agents
        return self

    def _guard_path(self, path: str) -> str:
        absolute = os.path.normpath(os.path.join(self.root, path)
                                    if not os.path.isabs(path) else path)
        if not absolute.startswith(self.root):
            raise ProjectModificationRefused(
                f"{path!r} lies outside the project root {self.root!r}")
        for marker in self.protected:
            if marker.rstrip("/") in absolute.replace(os.sep, "/"):
                raise ProjectModificationRefused(f"{path!r} is protected")
        return absolute

    def _snapshot(self, absolute: str) -> Optional[str]:
        if not os.path.isfile(absolute):
            return None
        digest = hashlib.sha256(absolute.encode()).hexdigest()[:12]
        destination = os.path.join(self.snapshot_dir,
                                   f"{digest}_{int(time.time()*1000)}.bak")
        os.makedirs(self.snapshot_dir, exist_ok=True)
        shutil.copy2(absolute, destination)
        return destination

    def create(self, path: str, content: str,
              verify: Optional[Callable[[], Tuple[bool, Dict]]] = None
              ) -> ProjectChangeResult:
        absolute = self._guard_path(path)
        if os.path.exists(absolute):
            return ProjectChangeResult(False, path, "create",
                                       "file already exists; use modify")
        return self._write(absolute, path, content, "create", backup=None,
                           verify=verify)

    def modify(self, path: str, content: str,
              verify: Optional[Callable[[], Tuple[bool, Dict]]] = None
              ) -> ProjectChangeResult:
        absolute = self._guard_path(path)
        if not os.path.isfile(absolute):
            raise ProjectModificationRefused(f"{path!r} does not exist; use create")
        backup = self._snapshot(absolute)
        return self._write(absolute, path, content, "modify", backup=backup,
                           verify=verify)

    def delete(self, path: str,
              verify: Optional[Callable[[], Tuple[bool, Dict]]] = None
              ) -> ProjectChangeResult:
        absolute = self._guard_path(path)
        if not os.path.isfile(absolute):
            raise ProjectModificationRefused(f"{path!r} does not exist")
        backup = self._snapshot(absolute)
        os.remove(absolute)

        verification: Dict = {}
        if verify is not None:
            try:
                passed, verification = verify()
            except Exception as exc:
                passed, verification = False, {"error": str(exc)}
            if not passed:
                shutil.copy2(backup, absolute)
                self.history.append({"path": path, "committed": False,
                                     "restored_from": backup})
                return ProjectChangeResult(False, path, "delete",
                                          "verification failed; deletion reverted",
                                          rolled_back=True)
        self.history.append({"path": path, "committed": True, "backup": backup,
                             "action": "delete"})
        return ProjectChangeResult(True, path, "delete", "committed")

    def _write(self, absolute: str, path: str, content: str, action: str,
              backup: Optional[str],
              verify: Optional[Callable[[], Tuple[bool, Dict]]]) -> ProjectChangeResult:
        os.makedirs(os.path.dirname(absolute) or ".", exist_ok=True)
        with open(absolute, "w") as fh:
            fh.write(content)

        verification: Dict = {}
        if verify is not None:
            try:
                passed, verification = verify()
            except Exception as exc:
                passed, verification = False, {"error": f"{type(exc).__name__}: {exc}"}
            if not passed:
                if backup:
                    shutil.copy2(backup, absolute)
                elif os.path.exists(absolute):
                    os.remove(absolute)
                self.history.append({"path": path, "committed": False,
                                     "restored_from": backup})
                return ProjectChangeResult(False, path, action,
                                          f"verification failed; reverted: {verification}",
                                          rolled_back=True)

        self.history.append({"path": path, "committed": True, "backup": backup,
                             "action": action})
        return ProjectChangeResult(True, path, action, "committed")

    def rollback_last(self) -> Optional[ProjectChangeResult]:
        for entry in reversed(self.history):
            if entry.get("committed"):
                absolute = os.path.join(self.root, entry["path"])
                if entry.get("backup"):
                    shutil.copy2(entry["backup"], absolute)
                elif entry.get("action") == "create" and os.path.exists(absolute):
                    os.remove(absolute)
                entry["committed"] = False
                return ProjectChangeResult(False, entry["path"],
                                          entry.get("action", "unknown"),
                                          "rolled back", rolled_back=True)
        return None

    # -- authorized repair application ----------------------------------
    def apply_repair(self, path: str, content: str,
                     caller: Any,
                     verify: Optional[Callable[[], Tuple[bool, Dict]]] = None,
                     repair_id: Optional[str] = None,
                     ) -> ProjectChangeResult:
        """Apply a repair to a project file through the governed choke
        point. The caller must be token-authenticated and hold
        'agent:repair_apply'; the guard must be bound to an
        AgentDirectory (bind_authorization). The authenticated caller id
        is recorded in history as applied_by.

        This authorizes the REAL file modification that precedes repair
        submission (verify_repair) and admission (admit_repair): without
        it, admit_repair() would bless bytes no authorized caller ever
        staged. Unauthenticated, forged, unauthorized, or destroyed
        callers are refused before any byte is written. repair_id is
        optional audit metadata (the repair record id is minted at
        submission time, after the bytes exist).
        """
        if self.agents is None:
            raise ProjectModificationRefused(
                "apply_repair requires a bound AgentDirectory: "
                "bind_authorization() first")
        from swarm_engine.governance.caller_authorization import (
            require_authorized)
        from swarm_engine.governance.oracle_binding import (
            DECISION_REPAIR_APPLY)
        authed = require_authorized(
            self.agents.reg, self.agents, caller, DECISION_REPAIR_APPLY,
            "apply_repair", target=repair_id or path)
        result = self.modify(path, content, verify=verify)
        if self.history and self.history[-1].get("path") == path:
            self.history[-1]["applied_by"] = authed
            if repair_id:
                self.history[-1]["repair_id"] = repair_id
        return result
