"""Contract 7 — Capabilities (developer-only).

Reads the REAL CapabilityStore (`swarm_engine.synthesis.capability_store`)
backed by the engine's sqlite file. The GUI's Capabilities screen is described
as "What REMOR has acquired, from its real capability store" with filter
chips All / Active / Superseded / Quarantined — those chips map 1:1 to the
statuses the store actually uses (active | superseded | quarantined, confirmed
against `CapabilityRecord.status` and the `plan_capabilities` schema); no
status is invented.

Developer gating — BOUNDED: there is no caller-authentication substrate
anywhere in the runtime (the oracle-binding audit confirmed: no authorization
layer exists), so no per-caller auth can be built here and none is invented.
Every response carries `"scope": "local-device-owner"`. This contract is safe
only on the single-user local GUI server; on any multi-user hosting it must
sit behind real authentication — that authentication does not exist.

Install (POST /api/capabilities/install) is the one MUTATING surface and
is held to a stricter rule than the reads: it requires the bearer-token
gate (runtime/services/auth.py). When no token is configured anywhere
(auth is None or not auth.is_gated()) the install FAILS CLOSED with a
typed 501 (install_requires_auth) -- code is never installed over
unauthenticated HTTP. When the gate IS configured, the HTTP layer's
_auth_denied enforces the bearer token (401) before the handler runs.
Installs are recorded in the existing AcquiredCodeStore (provenance +
never-executed evidence) and installed code is NEVER executed as part
of install.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.synthesis.capability_store import (  # noqa: E402
    CapabilityRecord, CapabilityStore)
from swarm_engine.governance.provenance import AcquiredCodeStore  # noqa: E402
from swarm_engine.services.contract_types import (  # noqa: E402
    contract_unavailable)

# Verified against CapabilityRecord.status and the plan_capabilities schema:
# these are the only statuses the store ever uses.
STATUSES = ("active", "superseded", "quarantined")
FILTERS = ("all",) + STATUSES

_SCOPE = "local-device-owner"
_GATING_NOTE = (
    "DEVELOPER-ONLY (BOUNDED): no caller-authentication substrate exists in "
    "the runtime, so per-user gating cannot be enforced here. This contract "
    "is valid only on the single-user local GUI server. Multi-user hosting "
    "must place it behind real authentication; that authentication does not "
    "exist.")

# -- install surface (S7) -------------------------------------------------
_INSTALL_MAX_CODE_BYTES = 1024 * 1024  # 1 MiB of source per install
_INSTALL_CODE_UNAUTH = "install_requires_auth"
_INSTALL_CODE_INVALID = "install_invalid_bundle"
# Names are allow-listed (no path separators possible by construction).
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.\-]{0,119}")
_ENTRYPOINT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,119}")


def _plan_summary(rec: CapabilityRecord) -> Dict[str, Any]:
    return {
        "capability_id": rec.capability_id,
        "name": rec.name,
        "goal": rec.goal,
        "version": rec.version,
        "parent_id": rec.parent_id,
        "status": rec.status,
        "created_at": rec.created_at,
        "use_count": rec.use_count,
        "success_rate": round(rec.success_rate, 3),
        "ops": list(rec.ops),
        "effects": list(rec.effects),
        "via": "plan_capabilities",
    }


def _acquired_summary(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "capability_id": entry["capability_id"],
        "name": entry["name"],
        "goal": None,
        "version": None,
        "parent_id": None,
        "status": entry.get("status", "active"),
        "created_at": entry.get("created_at"),
        "use_count": None,
        "success_rate": None,
        "ops": [],
        "effects": list(entry.get("effects") or []),
        "via": "acquired_code",
    }


def _sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120]


def _invalid(field: str, reason: str) -> Dict[str, Any]:
    return {"ok": False, "scope": _SCOPE,
            "code": _INSTALL_CODE_INVALID, "field": field,
            "error": f"invalid install bundle: {field}: {reason}"}


def _validate_install_bundle(bundle: Any) -> Tuple[bool, Any]:
    """Structural validation of an install bundle. Returns (ok, result)
    where result is the normalized bundle dict on success or a typed
    refusal dict on failure. Malformed bundles are REFUSED, never stored.
    """
    if not isinstance(bundle, dict):
        return False, _invalid("bundle", "must be a JSON object")
    name = bundle.get("name")
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return False, _invalid(
            "name", "required; 1-120 chars, must start alphanumeric, "
                    "then [A-Za-z0-9 _.-] only (no path separators, "
                    "no traversal possible)")
    code = bundle.get("code")
    if not isinstance(code, str) or not code:
        return False, _invalid("code", "required; must be a non-empty string")
    code_bytes = len(code.encode("utf-8"))
    if code_bytes > _INSTALL_MAX_CODE_BYTES:
        return False, _invalid(
            "code", f"{code_bytes} bytes exceeds the "
                    f"{_INSTALL_MAX_CODE_BYTES} byte install cap")
    entrypoint = bundle.get("entrypoint")
    if not isinstance(entrypoint, str) or not _ENTRYPOINT_RE.fullmatch(
            entrypoint):
        return False, _invalid(
            "entrypoint", "required; must look like a Python identifier "
                          "(1-120 chars, [A-Za-z_][A-Za-z0-9_]*)")
    source = bundle.get("source", "http_install")
    if not isinstance(source, str) or not source or len(source) > 200:
        return False, _invalid("source",
                               "optional; when present, 1-200 char string")
    effects = bundle.get("effects", [])
    if (not isinstance(effects, list)
            or any(not isinstance(e, str) or len(e) > 200 for e in effects)):
        return False, _invalid(
            "effects", "optional; when present, a list of short strings")
    spec = bundle.get("spec", {})
    if not isinstance(spec, dict):
        return False, _invalid("spec", "optional; when present, an object")
    return True, {"name": name, "code": code, "entrypoint": entrypoint,
                  "source": source, "effects": list(effects),
                  "spec": dict(spec)}


class CapabilityAPI:
    """Developer-only read/download facade over the real capability store."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.store = CapabilityStore(db_path)
        self.code = AcquiredCodeStore(db_path)

    def list_capabilities(self, status_filter: str = "all") -> Dict[str, Any]:
        if status_filter not in FILTERS:
            return {"ok": False, "scope": _SCOPE,
                    "error": f"unknown status filter {status_filter!r}; "
                             f"expected one of {list(FILTERS)}"}
        out: List[Dict[str, Any]] = []
        if status_filter == "all":
            statuses = STATUSES
        else:
            statuses = (status_filter,)
        for st in statuses:
            out.extend(_plan_summary(r)
                       for r in self.store.list(status=st, limit=1000))
            if status_filter == "all" or st in ("active", "quarantined"):
                # acquired_code only carries active/quarantined (its own
                # store schema); never map it onto "superseded".
                out.extend(_acquired_summary(e)
                           for e in self.code.all()
                           if e.get("status", "active") == st)
        out.sort(key=lambda d: (d["created_at"] or 0), reverse=True)
        return {
            "ok": True,
            "scope": _SCOPE,
            "developer_gating": _GATING_NOTE,
            "status_filter": status_filter,
            "count": len(out),
            "capabilities": out,
            "empty": len(out) == 0,
        }

    def get_capability(self, capability_id: str) -> Dict[str, Any]:
        rec = self.store.get(capability_id)
        if rec is not None:
            goal_keys = [k for k, v in self.store.goal_bindings().items()
                         if v == capability_id]
            detail = _plan_summary(rec)
            detail["goal_bindings"] = goal_keys
            detail["task_creation"] = (
                "This capability is bound to exact goal keys: " + ", ".join(goal_keys)
                if goal_keys else
                "Not bound to any goal key: the engine's task-creation path "
                "(resolve_goal / find_compatible) will not pick this "
                "capability up for new tasks until it is bound or ranked "
                "compatible. goal_bindings are the real task-creation "
                "handle — binding an exact goal key to a capability lets "
                "task creation reuse it without re-synthesis.")
            detail["history"] = [h.capability_id
                                 for h in self.store.history(capability_id)]
            detail["events"] = self.store.events(capability_id, limit=50)
            return {"ok": True, "scope": _SCOPE,
                    "developer_gating": _GATING_NOTE, "capability": detail}
        for entry in self.code.all():
            if entry["capability_id"] == capability_id:
                return {"ok": True, "scope": _SCOPE,
                        "developer_gating": _GATING_NOTE,
                        "capability": _acquired_summary(entry)}
        return {"ok": False, "scope": _SCOPE,
                "error": f"capability {capability_id!r} not found"}

    def download_capability(self, capability_id: str) -> Dict[str, Any]:
        """Return the capability's material bytes (base64) for download.

        Plan-native capabilities: the plan IS the canonical artefact; the
        payload is a text bundle of the human-readable rendering plus the
        canonical plan JSON. Code-acquired capabilities: the stored source.
        Filename is sanitized (traversal-safe) even though it never touches
        the filesystem — the GUI saves it client-side.
        """
        rec = self.store.get(capability_id)
        if rec is not None:
            rendered = self._rendered(rec.capability_id)
            payload = (
                f"# REMOR capability export\n"
                f"# id: {rec.capability_id}\n"
                f"# name: {rec.name}\n# version: {rec.version}\n"
                f"# status: {rec.status}\n# exported_at: {time.time()}\n\n"
                f"{rendered}\n"
            )
            filename = _sanitize_filename(
                f"{rec.capability_id}_v{rec.version}.txt")
            return self._download_ok(capability_id, filename, "text/plain",
                                     payload.encode("utf-8"))
        for entry in self.code.all():
            if entry["capability_id"] == capability_id:
                filename = _sanitize_filename(f"{entry['name']}.py")
                return self._download_ok(capability_id, filename,
                                         "text/x-python",
                                         (entry["code"] or "").encode("utf-8"))
        return {"ok": False, "scope": _SCOPE,
                "error": f"capability {capability_id!r} not found"}

    def _download_ok(self, capability_id: str, filename: str, mime: str,
                     data: bytes) -> Dict[str, Any]:
        return {
            "ok": True,
            "scope": _SCOPE,
            "developer_gating": _GATING_NOTE,
            "download": {
                "capability_id": capability_id,
                "filename": filename,
                "mime": mime,
                "bytes": len(data),
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        }

    def _rendered(self, capability_id: str) -> str:
        with self.store._conn() as c:
            row = c.execute("SELECT rendered FROM plan_capabilities "
                            "WHERE capability_id=?",
                            (capability_id,)).fetchone()
        return row[0] if row and row[0] else ""

    # -- install (S7: governed, caller-authenticated) ----------------------
    def install_capability(self, bundle: Any,
                           auth: Any = None) -> Dict[str, Any]:
        """Install a capability bundle into the acquired-code store.

        Fail-closed gate FIRST: when `auth` is None or not
        auth.is_gated() (no bearer token configured anywhere), the
        install is refused with typed 501 install_requires_auth -- code
        is never installed over unauthenticated HTTP. When the gate IS
        configured, the HTTP layer enforces the bearer token (401)
        before this handler runs.

        Then structural validation (typed refusals for malformed
        bundles), then provenance recording in the EXISTING
        AcquiredCodeStore -- no parallel store is invented. The
        installed code is NEVER executed as part of install; the
        evidence row records never_executed=True.
        """
        if auth is None or not auth.is_gated():
            return contract_unavailable(
                _INSTALL_CODE_UNAUTH,
                "capability install refused: no bearer token is "
                "configured, so the install cannot be caller-"
                "authenticated. Configure REMOR_API_TOKEN (or let the "
                "server provision <base_dir>/api_token) and present it "
                "as Authorization: Bearer <token>.",
                missing_substrate="configured bearer token "
                                 "(REMOR_API_TOKEN or <base_dir>/api_token)",
            )
        ok, validated = _validate_install_bundle(bundle)
        if not ok:
            return validated
        name = validated["name"]
        code = validated["code"]
        capability_id = "inst_" + uuid.uuid4().hex[:16]
        replaced = self.code.get(name) is not None
        evidence = {
            "installed_via": "http_install",
            "installed_at": time.time(),
            "authenticated": True,
            "never_executed": True,
            "sha256": hashlib.sha256(
                code.encode("utf-8")).hexdigest(),
            "code_bytes": len(code.encode("utf-8")),
        }
        self.code.save(name, capability_id, code,
                       validated["entrypoint"], validated["source"],
                       validated["effects"], validated["spec"], evidence)
        return {
            "ok": True,
            "scope": _SCOPE,
            "installed": True,
            "replaced": replaced,
            "capability_id": capability_id,
            "name": name,
            "entrypoint": validated["entrypoint"],
            "source": validated["source"],
            # Explicit: install stores and records; it never executes.
            "executed": False,
            "evidence": evidence,
        }


def routes_for_capabilities(api: CapabilityAPI,
                            auth: Any = None) -> Dict[Any, Any]:
    """Route table: (method, path) -> handler(body_dict) -> JSON dict.

    `api` is a constructed CapabilityAPI (db_path chosen by the host).
    `auth` is the bearer-token gate (Worker D's runtime/services/auth.py,
    exposed via build_services()["auth"]); the install handler consults
    auth.is_gated() and fails closed (typed 501) when no token is
    configured. When the gate is configured, the HTTP layer's
    _auth_denied enforces the bearer token (401) before dispatch.
    """
    def _list(body: Dict[str, Any]) -> Dict[str, Any]:
        return api.list_capabilities(
            str((body or {}).get("status", "all")))

    def _get(body: Dict[str, Any]) -> Dict[str, Any]:
        return api.get_capability(str((body or {}).get("capability_id", "")))

    def _download(body: Dict[str, Any]) -> Dict[str, Any]:
        return api.download_capability(
            str((body or {}).get("capability_id", "")))

    def _install(body: Dict[str, Any]) -> Dict[str, Any]:
        # The bearer gate itself is enforced by the HTTP layer
        # (_auth_denied, 401) when configured; is_gated() fails the
        # install closed (typed 501) when no token exists anywhere.
        return api.install_capability(body or {}, auth=auth)

    return {
        ("POST", "/api/capabilities/list"): _list,
        ("POST", "/api/capabilities/get"): _get,
        ("POST", "/api/capabilities/download"): _download,
        ("POST", "/api/capabilities/install"): _install,
    }
