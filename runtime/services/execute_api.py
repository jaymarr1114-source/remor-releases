"""
swarm_engine/services/execute_api.py

Contract 5 — Execute.

The GUI "Execute" tab: "Python runs in a real subprocess on this machine
(30s timeout). It runs as a versioned artifact in the artifact sandbox...
Each run is saved as a new artifact revision."

This module is a thin contract over the verified ArtifactStore substrate:
  * execute_code(code, name=None) saves the code as a versioned artifact,
    then runs the latest revision in the REAL subprocess sandbox via
    ArtifactStore.run, and reports what the run actually did.

Revision rule (ArtifactStore.save semantics, documented here because the
GUI depends on it):
  * name=None, or a name matching no existing artifact -> brand-new
    artifact (revision 1).
  * name matching an existing artifact -> a NEW REVISION of that artifact;
    history is preserved, the artifact_id is stable.

generated_files is a REAL directory listing: the sandbox dir is snapshotted
before the run and again after it, and the difference is reported. Nothing
is predicted. The runner's own temp script (artifact_<id>_r<rev>_*.py) is
always removed by ArtifactStore.run before the after-snapshot, so it can
never appear in the list.

Language: python ONLY. Any other language is refused with the typed
unavailability (EXECUTE_LANGUAGE_UNSUPPORTED) — never faked.

Sandbox bound (kept, stated honestly, hardened S13): the sandbox is a
same-host subprocess with OS-level rlimit confinement applied in the
child via preexec_fn -- RLIMIT_CPU, RLIMIT_AS, RLIMIT_FSIZE,
RLIMIT_NPROC (see swarm_engine/services/sandbox.py; limits are
environment-tunable via REMOR_SANDBOX_*). With REMOR_SANDBOX_PRIVDROP=1
and the server running as root, the child additionally setgid/setuid's
to an unprivileged account (``nobody``) after the rlimits are applied;
the drop is opt-in because it needs a traversable ancestor chain for
the sandbox dir. Every execute result carries a "sandbox" disclosure
naming the active mode, the limits, and the residual. What is NOT here:
no container, no mount-namespace, no network/syscall filtering; without
the opt-in there is no UID isolation (same-user + rlimits only).

Plugin / external-AI-bot execution (Contract 3's idea) is REAL: a
disk-persisted plugin registry (swarm_engine.plugin) with hash-pinned
manifests, a versioned bot protocol, and sandboxed execution reusing the
services sandbox (rlimits, optional privdrop). execute_plugin() resolves,
loads (re-verifying hashes), runs, and returns the result with provenance;
every failure is a typed honest refusal. The plugin registry lives in a
stable sibling directory of the sandbox dir unless the caller names one.

HTTP surface: routes_for_execute(artifact_store, sandbox_dir,
plugin_registry_dir=None) returns {(method, path): handler} with
handler(body_dict) -> JSON-serializable dict. http_adapter.py is NOT
edited; the coordinator's HTTP layer merges these.
"""
from __future__ import annotations

import datetime
import os
import time
import uuid
from typing import Any, Dict, List, Optional

from swarm_engine.services.contract_types import contract_unavailable, dispatch
from swarm_engine.plugin.errors import (
    PLUGIN_INVALID_TASK,
    plugin_error as _plugin_error,
)
from swarm_engine.plugin.service import (
    PluginService,
    default_registry_dir,
)

_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 600.0


def _snapshot_files(root: str) -> set:
    """Relative paths of all regular files under root (follows nothing
    outside root: os.walk stays within the tree)."""
    found = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                if os.path.isfile(full) and not os.path.islink(full):
                    found.add(os.path.relpath(full, root))
            except OSError:
                continue
    return found


def _default_name() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%d-%H%M%S")
    return f"execute-{stamp}-{uuid.uuid4().hex[:8]}"


class ExecuteService:
    """Contract 5: versioned execution over an ArtifactStore."""

    def __init__(self, artifact_store, sandbox_dir: str,
                 plugin_registry_dir: Optional[str] = None) -> None:
        self._store = artifact_store
        self._sandbox_dir = sandbox_dir
        self._plugins = PluginService(
            plugin_registry_dir or default_registry_dir(sandbox_dir))

    # -- main contract -------------------------------------------------
    def execute_code(
        self,
        code: str,
        name: Optional[str] = None,
        language: str = "python",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> Dict[str, Any]:
        language = (language or "").strip().lower()
        if language != "python":
            return contract_unavailable(
                "EXECUTE_LANGUAGE_UNSUPPORTED",
                f"execute_code was asked for language {language!r}; "
                "only python executes in the artifact sandbox",
                missing_substrate="language runtime registry "
                "(only the python interpreter exists)",
            )
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "error": "empty code"}
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            return {"ok": False, "error": "timeout must be a positive number"}
        if not (timeout > 0) or timeout > _MAX_TIMEOUT:
            return {
                "ok": False,
                "error": f"timeout must be in (0, {_MAX_TIMEOUT:g}] seconds",
            }

        artifact_name = (name or "").strip() or _default_name()
        saved = self._store.save(artifact_name, "python", code)
        if not saved.get("ok"):
            return saved
        artifact_id = saved["artifact_id"]
        revision = saved["revision"]

        before = _snapshot_files(self._sandbox_dir)
        run = self._store.run(artifact_id, revision, timeout=timeout)
        after = _snapshot_files(self._sandbox_dir)
        generated = sorted(after - before)

        if not run.get("ok"):
            # The revision is still saved: GUI spec says every run is saved
            # as a revision, even when the run itself fails to launch.
            result = {
                "ok": False,
                "artifact_id": artifact_id,
                "revision": revision,
                "language": "python",
                "error": run.get("error", "run failed"),
                "generated_files": generated,
                "sandbox": run.get("sandbox"),
            }
            return result
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "revision": revision,
            "language": "python",
            "returncode": run.get("exit_code"),
            "timed_out": run.get("timed_out", False),
            "stdout": run.get("stdout", ""),
            "stderr": run.get("stderr", ""),
            "generated_files": generated,
            "sandbox": run.get("sandbox"),
        }

    # -- plugin / external-bot execution (REAL) --------------------------
    @property
    def plugin_service(self) -> PluginService:
        """The real plugin service (registry + loader + sandbox)."""
        return self._plugins

    def execute_plugin(self, plugin_name: str = "",
                       task: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a registered plugin against the user-stated task.

        The bot protocol: the user states the outcome (task), the plugin
        does the work sandboxed, it reports back with provenance. Unknown
        plugins, bad manifests, and sandbox breaches are typed honest
        refusals — never simulated success.
        """
        return self._plugins.execute(plugin_name or "", task)


def routes_for_execute(artifact_store, sandbox_dir: str,
                       plugin_registry_dir: Optional[str] = None):
    """Route table for Contract 5. Handlers take body_dict -> JSON dict.

    plugin_registry_dir is optional: when omitted the registry lives in
    a stable sibling directory of sandbox_dir (default_registry_dir).
    """
    svc = ExecuteService(artifact_store, sandbox_dir, plugin_registry_dir)

    def _post_execute(body: Dict[str, Any]) -> Dict[str, Any]:
        return svc.execute_code(
            body.get("code", ""),
            name=body.get("name"),
            language=body.get("language", "python"),
            timeout=body.get("timeout", _DEFAULT_TIMEOUT),
        )

    def _post_plugin(body: Dict[str, Any]) -> Dict[str, Any]:
        task = body.get("task")
        if task is None:
            task = {}
        if not isinstance(task, dict):
            return _plugin_error(
                PLUGIN_INVALID_TASK,
                "task must be a JSON object (the user-stated outcome)",
                {"task_type": type(task).__name__})
        return svc.execute_plugin(body.get("plugin") or body.get("name") or "",
                                  task)

    return {
        ("POST", "/api/execute"): _post_execute,
        ("POST", "/api/execute/plugin"): _post_plugin,
    }


__all__ = [
    "ExecuteService",
    "routes_for_execute",
    "dispatch",
    "contract_unavailable",
]
