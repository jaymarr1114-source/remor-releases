"""PluginService — the orchestration behind POST /api/execute/plugin.

The user states the outcome (the task); the service resolves the plugin,
loads it with re-verified hashes, runs it sandboxed, and returns the
result with provenance. Every failure mode is a typed honest refusal —
never a simulated success.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from swarm_engine.plugin.errors import (
    PLUGIN_INVALID_TASK,
    PLUGIN_REPORTED_FAILURE,
    PluginError,
    plugin_error,
)
from swarm_engine.plugin.loader import PluginLoader
from swarm_engine.plugin.registry import PluginRegistry
from swarm_engine.plugin.sandbox import run_plugin

_DEFAULT_TIMEOUT_S = 30.0


class PluginService:
    """Real plugin / external-bot execution over a disk registry."""

    def __init__(self, registry_root: str) -> None:
        self.registry = PluginRegistry(registry_root)
        self.loader = PluginLoader(self.registry)

    # -- main contract -------------------------------------------------
    def execute(self, plugin_ref: str,
                task: Optional[Dict[str, Any]] = None,
                timeout_s: float = _DEFAULT_TIMEOUT_S,
                canary_paths: Optional[List[str]] = None,
                limits_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Execute a registered plugin against a user-stated task."""
        ref = (plugin_ref or "").strip()
        if task is None:
            task = {}
        if not isinstance(task, dict):
            return plugin_error(
                PLUGIN_INVALID_TASK,
                "task must be a JSON object (the user-stated outcome)",
                {"task_type": type(task).__name__})
        try:
            loaded = self.loader.load_ref(ref)
        except PluginError as exc:
            return exc.as_result()
        try:
            return run_plugin(
                loaded, task,
                registry_root=self.registry.root,
                timeout_s=timeout_s,
                canary_paths=canary_paths,
                limits_env=limits_env)
        except PluginError as exc:
            return exc.as_result()

    # -- registry passthrough (governance surface) ----------------------
    def register(self, package_dir: str,
                 overwrite: bool = False) -> Dict[str, Any]:
        try:
            return self.registry.register(package_dir,
                                           overwrite=overwrite)
        except PluginError as exc:
            return exc.as_result()

    def list(self, kind: Optional[str] = None) -> Dict[str, Any]:
        return self.registry.list(kind=kind)

    def remove(self, kind: str, name: str) -> Dict[str, Any]:
        try:
            return self.registry.remove(kind, name)
        except PluginError as exc:
            return exc.as_result()


def default_registry_dir(sandbox_dir: str) -> str:
    """Registry home when the caller does not name one: a stable
    sibling of the sandbox dir (never inside the sandbox itself)."""
    parent = os.path.dirname(os.path.abspath(sandbox_dir))
    return os.path.join(parent, "plugin_registry")


__all__ = ["PluginService", "default_registry_dir"]
