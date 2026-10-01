"""Disk-persisted plugin registry.

Layout under <root>/:
  registry.json            — the entry table (atomic writes)
  .lock                    — fcntl lock for read-modify-write cycles
  packages/<kind...>/<name>/  — the pinned package bits (registry-owned copy)
  runs/                    — per-run scratch (managed by sandbox.py)

No in-memory-only theater: every mutation hits the disk atomically, so
killing the process loses nothing. Concurrent writers serialize on the
lock file.
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import shutil
from typing import Any, Dict, List, Optional

from swarm_engine.plugin.errors import (
    PLUGIN_MANIFEST_INVALID,
    PLUGIN_REGISTRY_ERROR,
    PLUGIN_UNKNOWN,
    PluginError,
)
from swarm_engine.plugin.protocol import (
    load_manifest_file,
    validate_manifest,
)

_REGISTRY_FILE = "registry.json"
_LOCK_FILE = ".lock"
_PACKAGES_DIR = "packages"


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _copy_package_tree(src: str, dst: str) -> None:
    """Copy a validated package tree, refusing symlinks anywhere.

    shutil.copytree(symlinks=False) would silently copy symlink TARGETS
    (a classic escape); instead walk the tree and copy regular files
    only, raising on any symlink, fifo, socket, or device node.
    """
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        # Refuse symlinked directories before descending.
        for dirname in list(dirnames):
            full = os.path.join(dirpath, dirname)
            if os.path.islink(full) or not os.path.isdir(full):
                raise PluginError(
                    PLUGIN_MANIFEST_INVALID,
                    "package tree contains a non-directory entry "
                    "(symlinks are never accepted)",
                    {"path": os.path.relpath(full, src)})
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full) or not os.path.isfile(full):
                raise PluginError(
                    PLUGIN_MANIFEST_INVALID,
                    "package tree contains a non-regular file "
                    "(symlinks are never accepted)",
                    {"path": os.path.relpath(full, src)})
            shutil.copy2(full, os.path.join(target_dir, filename))


class PluginRegistry:
    """The on-disk plugin registry."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self._packages = os.path.join(self.root, _PACKAGES_DIR)
        self._registry_path = os.path.join(self.root, _REGISTRY_FILE)
        self._lock_path = os.path.join(self.root, _LOCK_FILE)
        os.makedirs(self._packages, exist_ok=True)
        # Touch the lock file so fcntl always has something to lock.
        with open(self._lock_path, "a"):
            pass

    # -- low-level table I/O -------------------------------------------
    def _read_table(self) -> List[Dict[str, Any]]:
        try:
            with open(self._registry_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise PluginError(
                PLUGIN_REGISTRY_ERROR,
                f"registry table is unreadable: {exc}",
                {"path": self._registry_path})
        if not isinstance(data, list):
            raise PluginError(
                PLUGIN_REGISTRY_ERROR,
                "registry table is corrupt (not a list)",
                {"path": self._registry_path})
        return data

    def _write_table(self, table: List[Dict[str, Any]]) -> None:
        tmp = self._registry_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(table, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._registry_path)
        except OSError as exc:
            raise PluginError(
                PLUGIN_REGISTRY_ERROR,
                f"registry table write failed: {exc}",
                {"path": self._registry_path})

    def _locked(self):
        """Context manager: exclusive fcntl lock for read-modify-write."""
        lock_path = self._lock_path

        class _Guard:
            def __enter__(self_):
                self_._fh = open(lock_path, "w")
                fcntl.flock(self_._fh.fileno(), fcntl.LOCK_EX)
                return self_

            def __exit__(self_, *exc):
                fcntl.flock(self_._fh.fileno(), fcntl.LOCK_UN)
                self_._fh.close()
                return False

        return _Guard()

    # -- public API ------------------------------------------------------
    def register(self, package_dir: str,
                 overwrite: bool = False) -> Dict[str, Any]:
        """Validate, pin, and install a plugin package.

        The manifest is validated (schema + hash pin) BEFORE any bytes
        are copied; the registry then owns an immutable-feeling copy.
        Returns the registry entry.
        """
        package_dir = os.path.abspath(package_dir)
        if not os.path.isdir(package_dir):
            raise PluginError(
                PLUGIN_MANIFEST_INVALID,
                "package_dir is not a directory",
                {"package_dir": package_dir})
        manifest_raw = load_manifest_file(package_dir)
        manifest, _entry = validate_manifest(manifest_raw, package_dir)
        kind, name = manifest["kind"], manifest["name"]

        with self._locked():
            table = self._read_table()
            existing = [e for e in table
                        if e["kind"] == kind and e["name"] == name]
            if existing and not overwrite:
                raise PluginError(
                    PLUGIN_MANIFEST_INVALID,
                    f"plugin {kind}/{name} is already registered "
                    "(pass overwrite=True to replace)",
                    {"kind": kind, "name": name})
            dest = os.path.join(
                self._packages, *kind.split("."), name)
            if os.path.lexists(dest):
                shutil.rmtree(dest)
            _copy_package_tree(package_dir, dest)
            # Re-verify the pin against the INSTALLED copy: the registry
            # never trusts bytes it has not hashed itself.
            installed_entry = os.path.join(dest, manifest["entry"])
            from swarm_engine.plugin.protocol import sha256_file
            if sha256_file(installed_entry) != manifest["sha256"]:
                shutil.rmtree(dest, ignore_errors=True)
                raise PluginError(
                    PLUGIN_MANIFEST_INVALID,
                    "installed copy failed its own hash pin",
                    {"kind": kind, "name": name})
            entry = {
                "kind": kind,
                "name": name,
                "version": manifest["version"],
                "protocol": manifest["protocol"],
                "entry": manifest["entry"],
                "sha256": manifest["sha256"],
                "description": manifest["description"],
                "package_path": os.path.relpath(dest, self.root),
                "registered_at": _utcnow(),
            }
            table = [e for e in table
                     if not (e["kind"] == kind and e["name"] == name)]
            table.append(entry)
            self._write_table(table)
        return {"ok": True, "entry": entry}

    def list(self, kind: Optional[str] = None) -> Dict[str, Any]:
        """List registered plugins, optionally filtered by kind."""
        with self._locked():
            table = self._read_table()
        if kind is not None:
            table = [e for e in table if e["kind"] == kind]
        return {"ok": True, "plugins": table}

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        """Return the registry entry, or None when unknown."""
        with self._locked():
            table = self._read_table()
        for entry in table:
            if entry["kind"] == kind and entry["name"] == name:
                return entry
        return None

    def package_dir(self, entry: Dict[str, Any]) -> str:
        """Absolute path of the installed package for an entry."""
        return os.path.join(self.root, entry["package_path"])

    def remove(self, kind: str, name: str) -> Dict[str, Any]:
        """Remove a plugin: entry row + installed bits, atomically."""
        with self._locked():
            table = self._read_table()
            remaining = [e for e in table
                         if not (e["kind"] == kind and e["name"] == name)]
            if len(remaining) == len(table):
                raise PluginError(
                    PLUGIN_UNKNOWN,
                    f"no plugin registered as {kind}/{name}",
                    {"kind": kind, "name": name})
            victim = [e for e in table
                      if e["kind"] == kind and e["name"] == name][0]
            self._write_table(remaining)
            victim_dir = os.path.join(self.root, victim["package_path"])
            if os.path.isdir(victim_dir) and not os.path.islink(victim_dir):
                # Refuse to rmtree anything outside the packages dir.
                victim_real = os.path.realpath(victim_dir)
                pkgs_real = os.path.realpath(self._packages)
                if os.path.commonpath([pkgs_real, victim_real]) != pkgs_real:
                    raise PluginError(
                        PLUGIN_REGISTRY_ERROR,
                        "refusing to remove a package outside the "
                        "registry packages dir",
                        {"path": victim["package_path"]})
                shutil.rmtree(victim_real)
        return {"ok": True, "removed": {"kind": kind, "name": name}}


__all__ = ["PluginRegistry"]
