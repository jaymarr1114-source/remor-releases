"""Loading with re-verified hashes: no unsigned code ever executes.

Every load re-validates the manifest and re-hashes the entry bytes from
the registry-owned copy. Registration-time pinning is necessary but not
sufficient — the loader trusts nothing it has not just hashed.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from swarm_engine.plugin.errors import (
    PLUGIN_MANIFEST_INVALID,
    PLUGIN_UNKNOWN,
    PluginError,
)
from swarm_engine.plugin.protocol import (
    load_manifest_file,
    validate_manifest,
)
from swarm_engine.plugin.registry import PluginRegistry


class LoadedPlugin:
    """A plugin whose manifest and entry hash were just verified."""

    def __init__(self, kind: str, name: str, manifest: Dict[str, Any],
                 package_dir: str, entry_path: str) -> None:
        self.kind = kind
        self.name = name
        self.manifest = manifest
        self.package_dir = package_dir
        self.entry_path = entry_path

    @property
    def ref(self) -> str:
        return f"{self.kind}/{self.name}"


def parse_ref(ref: str) -> tuple[str | None, str]:
    """Parse 'kind/name' or a bare 'name' (kind resolved by the loader)."""
    ref = (ref or "").strip()
    if "/" in ref:
        kind, name = ref.split("/", 1)
        return kind.strip() or None, name.strip()
    return None, ref


class PluginLoader:
    """Loads plugins from a registry, verifying on every load."""

    def __init__(self, registry: PluginRegistry) -> None:
        self.registry = registry

    def _load_entry(self, entry: Dict[str, Any]) -> LoadedPlugin:
        package_dir = self.registry.package_dir(entry)
        # Re-read the manifest from the installed copy and re-validate
        # everything, including the sha256 pin against current bytes.
        manifest_raw = load_manifest_file(package_dir)
        try:
            manifest, entry_path = validate_manifest(manifest_raw,
                                                     package_dir)
        except PluginError as exc:
            raise PluginError(
                PLUGIN_MANIFEST_INVALID,
                f"registered plugin {entry['kind']}/{entry['name']} "
                f"failed load-time verification: {exc.reason}",
                {"kind": entry["kind"], "name": entry["name"],
                 "cause": exc.code, "detail": exc.detail})
        if (manifest["kind"] != entry["kind"]
                or manifest["name"] != entry["name"]):
            raise PluginError(
                PLUGIN_MANIFEST_INVALID,
                "installed manifest identity does not match the "
                "registry entry",
                {"kind": entry["kind"], "name": entry["name"]})
        return LoadedPlugin(entry["kind"], entry["name"], manifest,
                            package_dir, entry_path)

    def load(self, kind: str, name: str) -> LoadedPlugin:
        entry = self.registry.get(kind, name)
        if entry is None:
            raise PluginError(
                PLUGIN_UNKNOWN,
                f"no plugin registered as {kind}/{name}",
                {"kind": kind, "name": name})
        return self._load_entry(entry)

    def load_ref(self, ref: str) -> LoadedPlugin:
        """Load by 'kind/name', or by bare name when it is unambiguous."""
        kind, name = parse_ref(ref)
        if not name:
            raise PluginError(
                PLUGIN_UNKNOWN, "empty plugin reference", {"ref": ref})
        if kind:
            return self.load(kind, name)
        listed = self.registry.list()["plugins"]
        matches = [e for e in listed if e["name"] == name]
        if not matches:
            raise PluginError(
                PLUGIN_UNKNOWN, f"no plugin registered as {name!r}",
                {"ref": ref})
        if len(matches) > 1:
            kinds = sorted(e["kind"] for e in matches)
            raise PluginError(
                PLUGIN_UNKNOWN,
                f"plugin name {name!r} is ambiguous across kinds; "
                f"use kind/name",
                {"ref": ref, "kinds": kinds})
        entry = matches[0]
        return self.load(entry["kind"], entry["name"])


__all__ = ["LoadedPlugin", "PluginLoader", "parse_ref"]
