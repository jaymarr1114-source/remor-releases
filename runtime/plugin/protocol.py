"""The versioned bot protocol (remor-plugin/1) and manifest validation.

Protocol shape — the user states the outcome, the plugin works, it reports
back:

  * Task envelope (JSON on the plugin's stdin):
      {"protocol": "remor-plugin/1",
       "task": {...the user-stated outcome, a JSON object...},
       "limits": {"timeout_s": 30.0}}
  * Result envelope (single JSON document on the plugin's stdout):
      {"ok": true, "result": {...}, "report": "human-readable summary"}
    or
      {"ok": false, "error": "what went wrong inside the plugin"}

Manifest (manifest.json at the package root) — hash-pinned, never unsigned:

  {"name": "echo",
   "kind": "tool.echo",
   "version": "1.0.0",
   "protocol": "remor-plugin/1",
   "entry": "main.py",
   "sha256": "<hex sha256 of the entry file>",
   "permissions": {"filesystem": "plugin-dir-only", "network": false},
   "description": "..."}

The sha256 pin is the trust anchor: registration pins it, and the loader
re-verifies it on every load. A package with no manifest, a manifest with
no pin, or a pin that does not match the entry bytes is unsigned code and
is never executed — PLUGIN_MANIFEST_INVALID.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Tuple

from swarm_engine.plugin.errors import (
    PLUGIN_MANIFEST_INVALID,
    PLUGIN_PROTOCOL_VIOLATION,
    PluginError,
)

PROTOCOL = "remor-plugin/1"
SUPPORTED_PROTOCOLS = (PROTOCOL,)

_MANIFEST_REQUIRED = ("name", "kind", "version", "protocol", "entry",
                      "sha256", "permissions")

_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_KIND_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")  # namespaced: at least one dot
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _fail(reason: str, detail: Dict[str, Any] | None = None) -> PluginError:
    return PluginError(PLUGIN_MANIFEST_INVALID, reason, detail)


def sha256_file(path: str) -> str:
    """Hex sha256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_entry(package_dir: str, entry: str) -> str:
    """Resolve the manifest's entry to an absolute path, refusing every
    escape: absolute entries, '..' components, and symlinks anywhere in
    the resolved path."""
    if not isinstance(entry, str) or not entry:
        raise _fail("manifest entry must be a non-empty string",
                    {"entry": entry})
    if os.path.isabs(entry):
        raise _fail("manifest entry must be a relative path",
                    {"entry": entry})
    parts = entry.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise _fail("manifest entry must not contain '.', '..' or empty "
                    "components", {"entry": entry})
    pkg_real = os.path.realpath(package_dir)
    raw_candidate = os.path.join(pkg_real, *parts)
    if os.path.islink(raw_candidate):
        raise _fail("manifest entry must not be a symlink",
                    {"entry": entry})
    candidate = os.path.realpath(raw_candidate)
    if os.path.commonpath([pkg_real, candidate]) != pkg_real:
        raise _fail("manifest entry escapes the package directory",
                    {"entry": entry})
    if not os.path.isfile(candidate):
        raise _fail("manifest entry is not a regular file",
                    {"entry": entry, "resolved": candidate})
    return candidate


def validate_manifest(manifest: Dict[str, Any],
                      package_dir: str) -> Tuple[Dict[str, Any], str]:
    """Validate a manifest dict against the package on disk.

    Returns (normalized_manifest, entry_absolute_path). Raises PluginError
    (PLUGIN_MANIFEST_INVALID) on anything unsigned, malformed, or escaping.
    """
    if not isinstance(manifest, dict):
        raise _fail("manifest.json must decode to a JSON object",
                    {"type": type(manifest).__name__})
    missing = [k for k in _MANIFEST_REQUIRED if k not in manifest]
    if missing:
        raise _fail("manifest is missing required keys",
                    {"missing": missing})
    name, kind = manifest["name"], manifest["kind"]
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise _fail("manifest name must match ^[a-z0-9_]+$",
                    {"name": name})
    if not isinstance(kind, str) or not _KIND_RE.match(kind):
        raise _fail("manifest kind must be namespaced "
                    "(dotted, e.g. 'tool.echo')", {"kind": kind})
    if manifest["protocol"] not in SUPPORTED_PROTOCOLS:
        raise _fail("unsupported bot protocol",
                    {"protocol": manifest["protocol"],
                     "supported": list(SUPPORTED_PROTOCOLS)})
    perms = manifest["permissions"]
    if not isinstance(perms, dict):
        raise _fail("manifest permissions must be an object",
                    {"permissions": perms})
    if perms.get("filesystem") != "plugin-dir-only":
        raise _fail("plugin must declare filesystem:'plugin-dir-only'",
                    {"permissions": perms})
    if perms.get("network") is not False:
        raise _fail("plugin must declare network:false",
                    {"permissions": perms})
    pin = manifest["sha256"]
    if not isinstance(pin, str) or not _SHA_RE.match(pin):
        raise _fail("manifest sha256 must be a 64-char lowercase hex pin",
                    {"sha256": pin})
    entry_path = _resolve_entry(package_dir, manifest["entry"])
    actual = sha256_file(entry_path)
    if actual != pin:
        raise _fail("entry bytes do not match the manifest sha256 pin "
                    "(unsigned or tampered code is never executed)",
                    {"entry": manifest["entry"],
                     "pinned": pin, "actual": actual})
    normalized = {
        "name": name,
        "kind": kind,
        "version": str(manifest["version"]),
        "protocol": manifest["protocol"],
        "entry": manifest["entry"],
        "sha256": pin,
        "permissions": {"filesystem": "plugin-dir-only",
                        "network": False},
        "description": str(manifest.get("description", "")),
    }
    return normalized, entry_path


def load_manifest_file(package_dir: str) -> Dict[str, Any]:
    """Read and JSON-parse manifest.json from a package dir."""
    path = os.path.join(package_dir, "manifest.json")
    if not os.path.isfile(path) or os.path.islink(path):
        raise _fail("package has no manifest.json (unsigned code is "
                    "never executed)", {"package_dir": package_dir})
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise _fail(f"manifest.json is unreadable: {exc}",
                    {"package_dir": package_dir})
    return data


def task_envelope(task: Dict[str, Any], timeout_s: float) -> bytes:
    """Serialize the stdin envelope for one plugin run."""
    return (json.dumps({
        "protocol": PROTOCOL,
        "task": task,
        "limits": {"timeout_s": timeout_s},
    }).encode("utf-8") + b"\n")


def parse_result_envelope(stdout_text: str) -> Dict[str, Any]:
    """Parse and validate the plugin's stdout result envelope."""
    text = (stdout_text or "").strip()
    if not text:
        raise PluginError(
            PLUGIN_PROTOCOL_VIOLATION,
            "plugin produced no output on stdout",
            {"stdout_bytes": 0})
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise PluginError(
            PLUGIN_PROTOCOL_VIOLATION,
            f"plugin stdout is not a JSON result envelope: {exc}",
            {"stdout_head": text[:200]})
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise PluginError(
            PLUGIN_PROTOCOL_VIOLATION,
            "plugin result envelope must be a JSON object with a "
            "boolean 'ok' field",
            {"stdout_head": text[:200]})
    return data


__all__ = [
    "PROTOCOL",
    "SUPPORTED_PROTOCOLS",
    "sha256_file",
    "validate_manifest",
    "load_manifest_file",
    "task_envelope",
    "parse_result_envelope",
]
