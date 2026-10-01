"""Sandboxed plugin execution.

Reuses the proven OS-level sandbox from services (swarm_engine.services.
sandbox.SandboxContext: rlimits via preexec_fn, optional privdrop) instead
of building a second one. Plugin-specific confinement added on top:

  * every run gets a FRESH per-run directory (a copy of the verified
    package); the plugin's cwd is that directory and nothing else;
  * the entry hash was verified by the loader immediately before this
    copy was made;
  * post-run, generated files are diffed (before/after snapshot);
  * caller-supplied canary paths detect absolute-path escapes: a plugin
    that writes outside its run dir cannot do so silently — the run is
    refused with PLUGIN_POLICY_VIOLATION and the evidence is reported.

Honest residual (same as the execute path): no container, no
mount-namespace, no network/syscall filtering. In same-user mode an
absolute-path write by the child is possible at the OS level — which is
exactly why the canary detection exists and why every result discloses
the sandbox posture. UID isolation applies only with the opt-in
privdrop (see services/sandbox.py).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from swarm_engine.plugin.errors import (
    PLUGIN_EXECUTION_FAILED,
    PLUGIN_POLICY_VIOLATION,
    PLUGIN_TIMEOUT,
    PluginError,
)
from swarm_engine.plugin.loader import LoadedPlugin
from swarm_engine.plugin.protocol import (
    parse_result_envelope,
    task_envelope,
)
from swarm_engine.services.sandbox import SandboxContext

_DEFAULT_TIMEOUT_S = 30.0
_MAX_TIMEOUT_S = 600.0


def _snapshot_files(root: str) -> set:
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


def _copy_tree_nolinks(src: str, dst: str) -> None:
    """Copy regular files only; refuse symlinks (defense in depth — the
    package was already validated, but the run copy is re-checked)."""
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        for dirname in dirnames:
            full = os.path.join(dirpath, dirname)
            if os.path.islink(full):
                raise PluginError(
                    PLUGIN_POLICY_VIOLATION,
                    "run copy refused: symlink in package tree",
                    {"path": os.path.relpath(full, src)})
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full) or not os.path.isfile(full):
                raise PluginError(
                    PLUGIN_POLICY_VIOLATION,
                    "run copy refused: non-regular file in package tree",
                    {"path": os.path.relpath(full, src)})
            shutil.copy2(full, os.path.join(target_dir, filename))


@contextmanager
def _patched_env(overrides: Optional[Dict[str, str]]):
    """Temporarily patch os.environ (restored afterwards). Lets one run
    carry its own REMOR_SANDBOX_* knobs without touching the parent."""
    if not overrides:
        yield
        return
    saved = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def run_plugin(loaded: LoadedPlugin,
               task: Dict[str, Any],
               registry_root: str,
               timeout_s: float = _DEFAULT_TIMEOUT_S,
               canary_paths: Optional[List[str]] = None,
               limits_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Execute one verified plugin run. Raises PluginError on every
    honest refusal; returns the success result dict otherwise."""
    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError):
        raise PluginError(PLUGIN_EXECUTION_FAILED,
                          "timeout must be a positive number",
                          {"timeout": timeout_s})
    if not (timeout_s > 0) or timeout_s > _MAX_TIMEOUT_S:
        raise PluginError(PLUGIN_EXECUTION_FAILED,
                          f"timeout must be in (0, {_MAX_TIMEOUT_S:g}]",
                          {"timeout": timeout_s})

    runs_root = os.path.join(os.path.abspath(registry_root), "runs")
    os.makedirs(runs_root, exist_ok=True)
    run_id = uuid.uuid4().hex
    run_dir = os.path.join(runs_root, run_id)
    os.makedirs(run_dir)

    try:
        _copy_tree_nolinks(loaded.package_dir, run_dir)
        entry_rel = os.path.relpath(loaded.entry_path, loaded.package_dir)
        run_entry = os.path.join(run_dir, entry_rel)

        before = _snapshot_files(run_dir)
        stdin_bytes = task_envelope(task, timeout_s)

        with _patched_env(limits_env):
            ctx = SandboxContext(run_dir)
            proc = subprocess.Popen(
                [sys.executable, run_entry],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=run_dir,
                preexec_fn=ctx.make_preexec(),
            )
            try:
                stdout_b, stderr_b = proc.communicate(
                    input=stdin_bytes, timeout=timeout_s)
                returncode = proc.returncode
                timed_out = False
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout_b, stderr_b = proc.communicate()
                raise PluginError(
                    PLUGIN_TIMEOUT,
                    f"plugin {loaded.ref} exceeded its {timeout_s:g}s "
                    f"time budget and was killed",
                    {"plugin": loaded.ref, "timeout_s": timeout_s,
                     "stdout_head": stdout_b.decode(
                         "utf-8", "replace")[:500]})

        after = _snapshot_files(run_dir)
        generated = sorted(after - before)
        stdout_text = stdout_b.decode("utf-8", "replace")
        stderr_text = stderr_b.decode("utf-8", "replace")

        # Canary check: absolute-path escapes cannot succeed silently.
        escaped: List[str] = []
        for canary in canary_paths or []:
            if os.path.lexists(canary):
                escaped.append(canary)
        if escaped:
            raise PluginError(
                PLUGIN_POLICY_VIOLATION,
                f"plugin {loaded.ref} wrote outside its run directory; "
                f"the run is refused",
                {"plugin": loaded.ref, "escaped_paths": escaped,
                 "sandbox": ctx.describe()})

        if returncode != 0:
            raise PluginError(
                PLUGIN_EXECUTION_FAILED,
                f"plugin {loaded.ref} exited with code {returncode}",
                {"plugin": loaded.ref, "returncode": returncode,
                 "stderr_head": stderr_text[:500],
                 "sandbox": ctx.describe()})

        envelope = parse_result_envelope(stdout_text)

        result: Dict[str, Any] = {
            "ok": True,
            "plugin": loaded.ref,
            "version": loaded.manifest["version"],
            "result": envelope.get("result"),
            "report": str(envelope.get("report", "")),
            "plugin_ok": envelope["ok"],
            "generated_files": generated,
            "provenance": {
                "kind": loaded.kind,
                "name": loaded.name,
                "version": loaded.manifest["version"],
                "protocol": loaded.manifest["protocol"],
                "manifest_sha256": loaded.manifest["sha256"],
                "entry_hash_verified_at_load": True,
            },
            "sandbox": ctx.describe(),
        }
        if not envelope["ok"]:
            # The plugin ran fine but reports its own failure: honest
            # pass-through, never upgraded to success.
            result["ok"] = False
            result["error"] = {
                "code": "PLUGIN_REPORTED_FAILURE",
                "reason": str(envelope.get("error", "plugin reported "
                                                   "failure")),
            }
        return result
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


__all__ = ["run_plugin"]
