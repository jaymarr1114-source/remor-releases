"""REMOR unified HTTP adoption layer (stdlib only).

One server, two route groups, no duplicated routes.

Engine front (Track 2B/3 — served from the engine-bound _Service):
  POST /api/intent/dispatch        conversational intent dispatch
                                  (IntentDispatchService): normalize ->
                                  classify (chat/ambiguous answered by the
                                  chat handler, never a task slot) ->
                                  metering gate -> scheduler run executing
                                  the real NL dispatch. Exact statuses
                                  (200/400/429/500/504); refusals are HTTP
                                  200 body data, never the contract table.
  GET  /api/dispatches[?limit=]    intent-dispatch audit log (newest first)
  GET  /api/capabilities           capability inventory + effective_status
  POST /api/capabilities/<id>/restore  governed capability recovery
  GET  /api/dispatch_evidence/<id> dispatch evidence (Track 3, read-only)
  GET  /api/voice/status           voice substrate status
                                  (TTS proven-bounded, STT unavailable)
  POST /api/voice/stt -> 501 honest refusal (no STT machinery exists)
  POST /api/voice/tts             governed TTS -> real WAV (+ download)
  GET  /api/media/status           media substrate status (proven-bounded)
  POST /api/media/image|/video|/song -> governed generation -> real bytes
  GET  /api/media/files/<name>     download one generated artifact (bytes)
  GET  /api/health                 liveness

Availability (coming-soon inventory, data for the GUI Coming Soon page):
  GET  /api/availability/coming-soon   (available read; every entry inside
                                       is a genuinely-unavailable
                                       capability in the shared typed shape)

Third-track services (backend-FF — runs/projects/files/artifacts):
  POST /api/runs                       {goal, examples?, conversation_id?, project_id?}
                                       guarded: 35/day + 25-queue via metering [S5]
  GET  /api/runs[?project_id=]         list_runs, or per-project linkage [S6]
  GET  /api/runs/<id>
  GET  /api/runs/<id>/events
  POST /api/runs/<id>/pause|resume|stop|cancel
  POST /api/projects                   {kind: blank|zip|dir, project_id?, path?}
  GET  /api/projects[?archive=1|?include_retired=1]
  GET  /api/projects/<id>
  POST /api/projects/<id>/transition   {to_state, reason?}
  POST /api/projects/<id>/retire       {reason?} -> internal archive [S12]
  POST /api/projects/<id>/resume       retired -> analyzing [S12]
  DELETE /api/projects/<id>            403 typed refusal (never deleted) [S12]
  POST /api/projects/<id>/run_loop     {max_rounds?}
  GET  /api/projects/<id>/loop/<job_id>
  POST /api/projects/<id>/loop/<job_id>/stop
Backend-contract table (contract-first precedence; incl. metering + agents):
  POST /api/metering/submit            guarded submit (same 35/25 gate) [S5]
  GET  /api/metering[/tier|/can-submit|/token-balances|/chat-turns]
  POST /api/agents                     register (3-agent cap enforced) [S4]
  GET  /api/agents[/{id}]              list / get
  POST /api/agents/{id}/destroy        destroy (frees a slot)
  GET  /api/agent-templates[/{id}/token-balance]
Auth: TWO layered gates (both must pass).
  (1) bearer-token gate on EVERY route (AuthGate, <base_dir>/api_token) [S10];
  (2) operator-auth gate on every MUTATING route (X-Agent-Id / X-Agent-Token
  headers or body["caller"]; AgentDirectory on the engine's oracle registry;
  unauthenticated -> 401, unpermitted -> 403; first boot provisions the
  OPERATOR identity, token to <base_dir>/operator.token, 0600).
  GET  /api/files?path=<rel>           list_dir
  GET  /api/files/content?path=<rel>   read_text
  POST /api/files/write                {path, content}
  GET  /api/files/stat?path=<rel>      stat (S3)
  POST /api/files/mkdir                {path} (S3)
  GET  /api/files/refusals             security refusal log (S3)
  GET  /api/files/archive/list?path=<rel>         zip entry listing (S2)
  GET  /api/files/archive/read?path=<rel>&entry=<name>  zip entry bytes, b64 (S2)
  POST /api/files/archive/extract      {path, dest} (S2)
  POST /api/artifacts                  {name, language, code, metadata?}
  GET  /api/artifacts
  GET  /api/artifacts/<id>?revision=n
  GET  /api/artifacts/<id>/revisions
  DELETE /api/artifacts/<id>
  POST /api/artifacts/<id>/run         {revision?, timeout?}

Backend-contract services (S1 — merged route table, dispatched FIRST):
  Tasks:     POST /api/tasks, GET /api/tasks?limit=, GET /api/tasks/item?task_id=,
             POST /api/tasks/cancel, GET /api/tasks/queue,
             POST /api/tasks/recurring -> real persistent recurrence
             scheduler (interval specs; gated by REMOR_RECURRENCE_ENABLED),
             GET /api/tasks/recurring -> schedule list,
             POST /api/tasks/recurring/cancel -> cancel schedule,
             GET /api/tasks/recurring/exposure -> gate + tier decision
  Routing:   POST /api/projects/chats, GET /api/projects/chats?project_id=,
             POST /api/routing/route, GET /api/routing/resolve?conversation_id=,
             POST /api/routing/auto -> 501 (no classifier substrate)
  Execute:   POST /api/execute {code, name?, language?, timeout?},
             POST /api/execute/plugin (real: runtime/plugin registry)
  Binary:    POST /api/artifacts/binary {name, bytes_b64, content_type?},
             GET /api/artifacts/binary/<id>, DELETE /api/artifacts/binary/<id>,
             GET /api/artifacts/all,
             POST /api/artifacts/audio/mix -> 501 (owned by the parallel
             media-substrate mission; the audit's NO-SUBSTRATE label for
             audio is rescinded)
  Caps:      POST /api/capabilities/list {status?},
             POST /api/capabilities/get {capability_id},
             POST /api/capabilities/download {capability_id}
             POST /api/capabilities/install {name, code, entrypoint, ...}
               [S7] bearer-gated: 401 without token; typed 501
               install_requires_auth when no token is configured (fail
               closed); never executes installed code
  Evidence:  POST /api/evidence/add {kind, text, source?},
             POST /api/evidence/get {id}, POST /api/evidence/list {kind?, limit?},
             POST /api/evidence/documents/get {name},
             POST /api/evidence/documents/save {name, markdown}
  Agents:    GET /api/agents, GET /api/agents/<agent_id>,
             POST /api/agents {template_id, version?, actor?},
             POST /api/agents/<agent_id>/destroy {actor?, reason?},
             GET /api/agent-templates, POST /api/agents/purchase-permanent -> 501
             (no payment substrate),
             GET /api/agent-templates/<tid>/token-balance
  Metering:  GET /api/metering, GET /api/metering/tier, GET /api/metering/can-submit,
             POST /api/metering/submit {goal, examples?, ...},
             GET /api/metering/token-balances -> 501 (no token meter),
             GET /api/metering/chat-turns -> 501 (no chat substrate)

PRECEDENCE RULE (ordering guarantee): on every request, the merged
contract route table is consulted FIRST for /api/ paths it matches; only
when no contract pattern matches does the request fall through to the
legacy if-chains. This matters for /api/artifacts/all (legacy would try
_aid("all") -> 500), /api/projects/chats (legacy would 404 via
get_project("chats")), and /api/capabilities/list (no legacy route).
Contract routes are exact-pattern matched with {var} templates, so
legacy paths like /api/artifacts/<int> keep their old handlers.

Contract status mapping (_contract_result): ok -> 200; typed
{"unavailable": ...} -> 501 (honest absence, never simulated); typed
{"limit": ...} -> 429 (real quota refusal); "not found" -> 404; else via
_status_for. GET contract requests merge the URL query string into the
body dict (single values).

Auth gates (two layers, both must pass):
(1) bearer-token gate (shared interface, Worker D owns the
implementation): build_services() returns an "auth" entry — an object
with check(method, path, headers) -> Optional[(code, body)] (None =
allow). _Handler calls it at the very top of each do_* BEFORE contract
dispatch; a non-None verdict is sent as-is and the request stops there.
Absent "auth" -> permissive default (no check), so wiring tests run
without the gate. `path` is the URL path without the query string.
(2) operator-auth gate (Phase 3 operator auth): every MUTATING route
(classify_mutating: intent_dispatch, runs_*, projects_*, files_write,
artifacts_*, capability_restore) requires an HTTP caller credential
(X-Agent-Id / X-Agent-Token headers, or body["caller"]), authenticated
against the AgentDirectory bound to the engine's oracle registry. The
caller must hold a live HTTP permission covering the route
(allow-once / allow-for-project / effectively-unlimited); routes that
trigger downstream choke points additionally require the underlying
decision classes (ROUTE_DECISION_CLASSES). Unauthenticated -> 401,
authenticated-but-unpermitted -> 403, both with explicit reasons. First
boot provisions the OPERATOR identity (effectively-unlimited): the token
is written once to <base_dir>/operator.token (0600) and printed to
stdout. The gate runs after body parsing (it reads body["caller"]) and
before the route handlers; malformed JSON is still a 400.

Health (S11): GET /api/health reports liveness plus the scheduler's
queue_depth() and the service anchor's verify_all() result. verify_all()
is executed LIVE on every health call — it is read-only (fail-closed
journal verification against live chain heads, no mutation) and cheap on
a small journal; documented here so callers know the value is current,
not cached.

Media boot note (canonical): the boot-time media admission threads the
engine's own caller authority through runtime/media/wiring.py
(engine.admit_as_engine), which canonical's caller-authenticated
admission accepts. A medium whose admission refuses for any other reason
(e.g. its substrate is absent in this environment) is recorded per medium
like any other admission refusal — the medium stays unadmitted and its
routes honestly 501 (medium_not_admitted) — and boot continues.

Threading discipline (KD-2): the SwarmEngine is thread-affine (the oracle
registry holds a thread-bound sqlite connection). This server is
single-threaded BY DESIGN: the engine is constructed on the serving thread
and every request is handled on that same thread. Do not wrap this in a
ThreadingHTTPServer. `serve()` starts the serving thread itself, so the
engine still boots on the thread that serves.

JSON discipline: every route uses the strict reader (256KB cap, 400 on
malformed or non-object JSON). Service-dict results map via _status_for:
ok -> 200, "not found"/"unknown ..." -> 404, illegal transition -> 409,
else 400. Nothing here fabricates data.

Run: python3 -m swarm_engine.services.http_adapter --db <path> --dir <svcdata>
     --port 8471
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.governance.caller_authorization import (
    AgentDirectory, AuthorizationError, CallerContext)
from swarm_engine.services import voice as voice_svc
from swarm_engine.services import media as media_svc
# [Worker 3 / item 4] conversational intake: the one-shot task path
# (/api/runs) is routed through the same normalize -> classify gate as
# POST /api/intent/dispatch. Both are pure (no state, no I/O).
from swarm_engine.services.message_text import (
    normalize_message_text as _normalize_message_text)
from swarm_engine.services import discriminator as _discriminator
from swarm_engine.services.contract_types import (
    dispatch as _contract_dispatch,
    _match as _pattern_match,
)
from swarm_engine.services.unavailable import CapabilityUnavailable
from swarm_engine.synthesis.integrity import (
    RestoreRefused, effective_status, get_quarantine_reason,
    restore_everywhere)
from swarm_engine.synthesis.intent_router import IntentRouter
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher, DispatchResult

MAX_BODY = 256 * 1024


def _contract_hit(routes: Dict[Any, Any], method: str, path: str) -> bool:
    """True when the merged contract route table has a route for
    (method, path). Consulted before the legacy if-chains (see the
    precedence rule in the module docstring)."""
    method = method.upper()
    for (m, pattern) in routes:
        if m == method and _pattern_match(pattern, path) is not None:
            return True
    return False


# ---------------------------------------------------------------------------
# HTTP caller authentication + authorization (Phase 3 operator auth, C).
#
# Every MUTATING route requires a caller credential: X-Agent-Id /
# X-Agent-Token headers, or body["caller"] = {"agent_id": ..., "token": ...}.
# The credential is authenticated against the AgentDirectory bound to the
# engine's oracle registry (same wiring as every agent<->REMOR choke point:
# AgentDirectory(engine.oracle_registry) -- no parallel identity store).
# Unauthenticated mutating request -> 401 with an explicit reason;
# authenticated but without a covering HTTP permission -> 403.
#
# Read-only GETs stay OPEN by deliberate decision: they expose operational
# state and change nothing. POST /api/voice/* and /api/media/* are not
# classified mutating either: they terminate in an honest 501 before any
# state change when their substrate is unadmitted, so authentication would
# gate nothing (the bearer gate in _auth_denied still applies to them).
#
# Permission spectrum, enforced per route via AgentDirectory HTTP
# permission grants:
#   allow-once            -- one authorized mutating call, then consumed;
#   allow-for-project     -- project-scoped mutating routes for one
#                            project_id only (cross-project or non-project
#                            use refused);
#   effectively-unlimited -- every mutating route (the operator class).
# Where a route triggers a downstream agent<->REMOR choke point, the caller
# must ALSO hold the underlying decision class, not just HTTP access
# (ROUTE_DECISION_CLASSES); e.g. the restore route requires
# agent:restore + trust:transition, enforced by restore_everywhere itself.
# ---------------------------------------------------------------------------

#: (method, route name, path regex, project-group index or None). The
#: project group names the project_id a project-scoped permission is
#: checked against; None marks non-project routes (an allow-for-project
#: grant never covers them).
_MUTATING_ROUTES = (
    ("POST", "intent_dispatch",
     r"/api/intent/dispatch", None),
    ("POST", "runs_submit",
     r"/api/runs", None),
    ("POST", "runs_control",
     r"/api/runs/[^/]+/(pause|resume|stop|cancel)", None),
    ("POST", "projects_create",
     r"/api/projects", None),
    ("POST", "projects_transition",
     r"/api/projects/([^/]+)/transition", 1),
    ("POST", "projects_run_loop",
     r"/api/projects/([^/]+)/run_loop", 1),
    ("POST", "projects_loop_stop",
     r"/api/projects/([^/]+)/loop/[^/]+/stop", 1),
    ("POST", "files_write",
     r"/api/files/write", None),
    ("POST", "artifacts_save",
     r"/api/artifacts", None),
    ("POST", "artifacts_run",
     r"/api/artifacts/[^/]+/run", None),
    ("DELETE", "artifacts_delete",
     r"/api/artifacts/[^/]+", None),
    ("POST", "capability_restore",
     r"/api/capabilities/([A-Za-z0-9_.\-]+)/restore", None),
    # Phase 4: organizational learning (repair evidence/admission,
    # versioned repair lifecycle, dispatch knowledge completion).
    # [phase 4b] the ENDPOINTS below are not yet brought into this adapter
    # (they need the Phase-4 service apparatus -- separate extraction items
    # outside #27's operator-auth scope); the gate classification is kept
    # verbatim so the endpoints land pre-gated, and unauthenticated calls
    # to these paths fail closed at the gate today instead of 404ing.
    ("POST", "repairs_submit",
     r"/api/repairs/submit", None),
    ("POST", "repairs_admit",
     r"/api/repairs/([A-Za-z0-9_.\-]+)/admit", None),
    ("POST", "repairs_apply",
     r"/api/repairs/([A-Za-z0-9_.\-]+)/apply", None),
    ("POST", "repairs_revoke",
     r"/api/repairs/([A-Za-z0-9_.\-]+)/revoke", None),
    ("POST", "repairs_rollback",
     r"/api/repairs/([A-Za-z0-9_.\-]+)/rollback", None),
    ("POST", "repairs_quarantine",
     r"/api/repairs/([A-Za-z0-9_.\-]+)/quarantine", None),
    ("POST", "repairs_unquarantine",
     r"/api/repairs/([A-Za-z0-9_.\-]+)/unquarantine", None),
    ("POST", "dispatch_complete_knowledge",
     r"/api/dispatch/complete_knowledge", None),
)

#: Concrete path templates for the mutating routes, for tests that sweep
#: the auth gate programmatically. {placeholders} are filled with real ids
#: from test setup.
MUTATING_ROUTE_TEMPLATES = (
    ("POST", "/api/intent/dispatch"),
    ("POST", "/api/runs"),
    ("POST", "/api/runs/{run_id}/pause"),
    ("POST", "/api/runs/{run_id}/resume"),
    ("POST", "/api/runs/{run_id}/stop"),
    ("POST", "/api/runs/{run_id}/cancel"),
    ("POST", "/api/projects"),
    ("POST", "/api/projects/{project_id}/transition"),
    ("POST", "/api/projects/{project_id}/run_loop"),
    ("POST", "/api/projects/{project_id}/loop/{job_id}/stop"),
    ("POST", "/api/files/write"),
    ("POST", "/api/artifacts"),
    ("POST", "/api/artifacts/{artifact_id}/run"),
    ("DELETE", "/api/artifacts/{artifact_id}"),
    ("POST", "/api/capabilities/{capability_id}/restore"),
    # Phase 4: organizational learning (swept by the auth battery).
    # [phase 4b] endpoints not yet brought (see _MUTATING_ROUTES note);
    # templates kept verbatim so the sweep stays complete when they land.
    ("POST", "/api/repairs/submit"),
    ("POST", "/api/repairs/{repair_id}/admit"),
    ("POST", "/api/repairs/{repair_id}/apply"),
    ("POST", "/api/repairs/{repair_id}/revoke"),
    ("POST", "/api/repairs/{repair_id}/rollback"),
    ("POST", "/api/repairs/{repair_id}/quarantine"),
    ("POST", "/api/repairs/{repair_id}/unquarantine"),
    ("POST", "/api/dispatch/complete_knowledge"),
)

#: Route name -> downstream decision classes the HTTP caller must hold IN
#: ADDITION to an HTTP permission (the choke points re-enforce these;
#: this is the adapter's uniform-gate copy).
ROUTE_DECISION_CLASSES = {
    "capability_restore": ("agent:restore", "trust:transition"),
    # Phase 4: each repair route re-enforces the downstream decision
    # class at the choke point itself; this is the adapter's uniform-gate
    # copy. complete_knowledge carries the service's own auth (attributed
    # agent or engine) and needs no extra class here.
    # [phase 4b] kept verbatim with the Phase-4 table entries above.
    "repairs_submit": ("agent:repair_submit",),
    "repairs_admit": ("agent:repair_apply",),
    "repairs_apply": ("agent:repair_apply",),
    "repairs_revoke": ("agent:repair_apply",),
    "repairs_rollback": ("agent:repair_apply",),
    "repairs_quarantine": ("agent:repair_apply",),
    "repairs_unquarantine": ("agent:repair_apply",),
}

#: Pinned identity id for the provisioned operator.
OPERATOR_AGENT_ID = "remor:operator"
#: Plaintext operator token file, written once at first boot (mode 0600).
OPERATOR_TOKEN_FILE = "operator.token"


def classify_mutating(method: str, path: str):
    """Classify a request as a mutating route.

    Returns (route_name, project_id) or None for non-mutating requests.
    project_id is the path's project for project-scoped routes, else None.
    """
    for m, name, pattern, pgroup in _MUTATING_ROUTES:
        if m != method:
            continue
        mtch = re.fullmatch(pattern, path)
        if mtch:
            pid = mtch.group(pgroup) if pgroup else None
            return name, pid
    return None


def _auth_status(exc: Exception) -> int:
    """Map an AuthorizationError to 401 (credential missing/bad) or 403
    (authenticated but not permitted), by the refusal's explicit reason."""
    msg = str(exc)
    if msg.startswith("unauthenticated") or "authentication failed" in msg:
        return 401
    return 403


class _Handler(BaseHTTPRequestHandler):
    server_version = "REMOR/1.0"

    # -- plumbing ---------------------------------------------------------
    def _send(self, code: int, obj: Any):
        blob = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _send_bytes(self, code: int, data: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise ValueError("malformed JSON")
        if not isinstance(obj, dict):
            raise ValueError("top-level JSON must be an object")
        return obj

    def _result(self, res: Dict[str, Any], ok_status: int = 200):
        """Map a service result dict to an HTTP response (FF _status_for)."""
        if res.get("ok"):
            return self._send(ok_status, res)
        return self._send(_status_for(res), res)

    def _contract_result(self, res: Dict[str, Any]):
        """Status mapping for contract-table results: ok -> 200, typed
        unavailability -> 501, typed quota refusal -> 429, not-found ->
        404, everything else via _status_for."""
        if res.get("ok"):
            return self._send(200, res)
        if "unavailable" in res:
            return self._send(501, res)
        if "limit" in res:
            return self._send(429, res)
        return self._send(_status_for(res), res)

    def _auth_denied(self, method: str) -> bool:
        """Shared auth gate (Worker D owns the implementation).

        Calls svc.ff["auth"].check(method, path, headers) when present;
        a non-None (code, body) verdict is sent as-is and True is
        returned (the caller must stop). Absent "auth" -> permissive
        default, False.
        """
        svc: "_Service" = self.server.svc  # type: ignore
        auth = svc.ff.get("auth")
        if auth is None:
            return False
        path = urllib.parse.urlparse(self.path).path
        verdict = auth.check(method, path, self.headers)
        if verdict is None:
            return False
        code, body = verdict
        self._send(code, body)
        return True

    def _submit_guarded(self, body: Dict[str, Any]):
        """[Worker D / S5] POST /api/runs goes through the REAL quota gate.

        metering.guarded_submit() enforces, in order: the 35/day cap
        (can_submit() over real scheduler_runs rows), then the 25-queue
        cap (the scheduler's real queue_depth()), then the real
        scheduler.submit(). Quota refusals are typed limit payloads ->
        429 via _contract_result (same mapping as the contract table).

        [Worker 3 / item 4] conversational-intake gate: the one-shot
        task text is normalized (dictation artifacts stripped at the
        edges, before the text becomes a goal) and classified through
        the SAME 3-way classifier as POST /api/intent/dispatch, BEFORE
        the metering gate. chat/ambiguous turns (e.g. a bare "Learn")
        are answered by the chat handler -- HTTP 200, clarification or
        answer -- and NEVER reach guarded_submit: no scheduler_runs row,
        no task slot consumed. Only task turns continue to the quota
        gate below, unchanged. The gate lives in this helper (not the
        contract table) so the quota-gate ordering is preserved.

        Worker C: this helper is the /api/runs POST path. It is NOT in
        contract_routes (no /api/runs entry there), so contract-first
        dispatch cannot shadow it -- but if /api/runs is ever added to
        the contract table, that entry must call guarded_submit too.
        """
        # Dictation-artifact normalization FIRST, mirroring
        # intent_dispatch(): edge junk stripped before the text becomes
        # a goal, before the discriminator sees it, and before the
        # metering gate. Inner content is byte-preserved.
        goal = _normalize_message_text(body.get("goal", ""))
        # Conversational-minimum gate: BEFORE the metering gate.
        mode = _discriminator.classify(goal).get("mode")
        if mode in ("chat", "ambiguous"):
            try:
                from swarm_engine.services import chat_handler as _chat
            except ImportError as exc:
                # Loud, not silent: a conversational turn must never be
                # re-routed into the task pipeline just because the chat
                # handler module is absent.
                raise RuntimeError(
                    "swarm_engine.services.chat_handler is missing: this "
                    "turn classified as %r (conversational minimum) and "
                    "must be answered by the chat handler; refusing to "
                    "route it into the task pipeline" % (mode,)) from exc
            # The build_services dict (svc.ff) is the services map the
            # chat handler answers from -- the same map wired onto the
            # intent service (services["intent"].services = services).
            return self._send(200, _chat.answer(
                goal, self.server.svc.ff,  # type: ignore
                ambiguous=(mode == "ambiguous")))
        metering = self.server.svc.ff["metering"]  # type: ignore
        return self._contract_result(metering.guarded_submit(
            goal, examples=body.get("examples"),
            metadata=body.get("metadata"),
            conversation_id=body.get("conversation_id"),
            project_id=body.get("project_id")))

    def log_message(self, fmt, *args):  # keep stderr quiet in tests
        pass

    # -- routing ----------------------------------------------------------
    def _health(self, svc: "_Service") -> Dict[str, Any]:
        """Liveness + scheduler queue depth + live service-anchor verify.

        verify_all() is read-only (fail-closed journal verification
        against live chain heads, no mutation) and is executed LIVE on
        every call, so the value is current, not cached — documented here
        per the S11 decision to report the live result rather than a
        last-known one.
        """
        out: Dict[str, Any] = {"ok": True}
        scheduler = svc.ff.get("scheduler")
        try:
            out["queue_depth"] = (scheduler.queue_depth()
                                  if scheduler is not None else None)
        except Exception as exc:
            out["queue_depth"] = None
            out["queue_depth_error"] = f"{type(exc).__name__}: {exc}"
        anchor = svc.ff.get("service_anchor")
        if anchor is None:
            out["anchor"] = {"ok": None,
                             "detail": "no service anchor attached"}
        else:
            try:
                ok, detail = anchor.verify_all()
                out["anchor"] = {"ok": bool(ok), "detail": detail}
            except Exception as exc:
                out["anchor"] = {"ok": False,
                                 "detail": f"{type(exc).__name__}: {exc}"}
        return out

    def do_GET(self):
        if self._auth_denied("GET"):
            return
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        svc: "_Service" = self.server.svc  # type: ignore
        # --- backend-contract table FIRST (precedence rule) ---
        contract_routes = svc.ff.get("contract_routes") or {}
        if path.startswith("/api/") and _contract_hit(
                contract_routes, "GET", path):
            # Query string merged into the body dict, single values.
            body = {k: v[0] for k, v in qs.items()}
            return self._contract_result(
                _contract_dispatch(contract_routes, "GET", path, body))
        # --- engine front (Track 2B/3) ---
        if path == "/api/health":
            return self._send(200, self._health(svc))
        if path == "/api/capabilities":
            return self._send(200, {"capabilities": svc.capabilities()})
        if path == "/api/dispatches":
            # Intent-dispatch audit log (A): the IntentDispatchService owns
            # POST /api/intent/dispatch, so its audit table is the log the
            # GUI Dispatch view reads (append-only, newest first). `limit`
            # is an integer >= 0; unparseable values fall back to 50 rather
            # than failing the read.
            try:
                limit = int((qs.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            return self._send(200, {"dispatches":
                                    svc.ff["intent"].dispatch_history(limit)})
        m = re.fullmatch(r"/api/dispatch_evidence/([A-Za-z0-9_\-]+)", path)
        if m:
            return self._send(*svc.dispatch_evidence(m.group(1)))
        if path == "/api/voice/status":
            return self._send(200, voice_svc.status())
        if path == "/api/media/status":
            return self._send(200, media_svc.status())
        m = re.fullmatch(r"/api/media/files/([A-Za-z0-9_\-\.]+)", path)
        if m:
            code, payload = svc.media_file_bytes(m.group(1))
            if code != 200:
                return self._send(code, payload)
            return self._send_bytes(200, payload["data"],
                                    payload["content_type"])
        # --- third-track services (FF group, proven order) ---
        try:
            ff = svc.ff
            scheduler = ff["scheduler"]
            projects = ff["projects"]
            files = ff["files"]
            artifacts = ff["artifacts"]
            if path == "/api/runs":
                # [Worker D / S6] per-project linkage query: the
                # scheduler_run_projects side table (survives reopen).
                pid = (qs.get("project_id") or [None])[0]
                if pid:
                    return self._send(200, {
                        "ok": True, "project_id": pid,
                        "runs": scheduler.project_runs(pid)})
                return self._send(200, scheduler.list_runs(
                    limit=int((qs.get("limit") or ["50"])[0])))
            if path.startswith("/api/runs/") and path.endswith("/events"):
                rid = path.split("/")[3]
                evs = scheduler.events(rid)
                if evs is None:
                    return self._send(404, {"error": "run not found"})
                return self._send(200, evs)
            if path.startswith("/api/runs/"):
                rec = scheduler.get_run(path.split("/")[3])
                if rec is None:
                    return self._send(404, {"error": "run not found"})
                return self._send(200, rec)
            if path == "/api/projects":
                # [Worker D / S12] retired projects are excluded from the
                # default listing (internal archive, neither active nor
                # destroyed); ?archive=1 lists the archive itself.
                qflag = (qs.get("archive") or [""])[0].lower() in (
                    "1", "true", "yes")
                if qflag:
                    return self._send(200, {
                        "ok": True,
                        "archive": projects.project_archive()})
                include_retired = (qs.get("include_retired") or [""])[0].lower() in (
                    "1", "true", "yes")
                return self._send(200, projects.list_projects(
                    include_retired=include_retired))
            if "/loop/" in path and path.startswith("/api/projects/"):
                parts = path.split("/")
                pid, job_id = parts[3], parts[5]
                return self._result(projects.loop_status(job_id))
            if path.startswith("/api/projects/"):
                return self._result(projects.get_project(path.split("/")[3]))
            if path == "/api/files":
                return self._result(
                    files.list_dir((qs.get("path") or [""])[0]))
            if path == "/api/files/content":
                return self._result(
                    files.read_text((qs.get("path") or [""])[0]))
            if path == "/api/files/stat":  # S3
                return self._result(
                    files.stat((qs.get("path") or [""])[0]))
            if path == "/api/files/refusals":  # S3
                return self._send(200, {"ok": True,
                                        "refusals": files.refusals()})
            if path == "/api/files/archive/list":  # S2
                return self._result(
                    files.list_archive((qs.get("path") or [""])[0]))
            if path == "/api/files/archive/read":  # S2
                res = files.read_inside((qs.get("path") or [""])[0],
                                        (qs.get("entry") or [""])[0])
                if res.get("ok"):
                    # bytes -> base64 so the payload stays JSON.
                    res = dict(res)
                    res["data_b64"] = base64.b64encode(
                        res.pop("data")).decode("ascii")
                return self._result(res)
            if path == "/api/artifacts":
                return self._send(200, artifacts.list_artifacts())
            if path.startswith("/api/artifacts/") and path.endswith("/revisions"):
                return self._result(artifacts.revisions(
                    _aid(path.split("/")[3])))
            if path.startswith("/api/artifacts/"):
                rev = (qs.get("revision") or [None])[0]
                rev = int(rev) if rev is not None else None
                return self._result(artifacts.get(_aid(path.split("/")[3]),
                                                  revision=rev))
        except Exception as exc:  # honest 500, never silent
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self._auth_denied("POST"):
            return
        path = urllib.parse.urlparse(self.path).path
        svc: "_Service" = self.server.svc  # type: ignore
        # --- backend-contract table FIRST (precedence rule) ---
        contract_routes = svc.ff.get("contract_routes") or {}
        if path.startswith("/api/") and _contract_hit(
                contract_routes, "POST", path):
            try:
                body = self._read_json()
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._contract_result(
                _contract_dispatch(contract_routes, "POST", path, body))
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        # --- operator-auth gate (C): every mutating route requires an
        # authenticated caller holding a live HTTP permission covering the
        # route (401/403 with explicit reasons). The gate runs AFTER body
        # parsing (it also accepts body["caller"]) so malformed JSON is
        # still a 400, and BEFORE the route handlers so unauthenticated
        # mutating requests never reach them. The bearer gate above runs
        # first; BOTH gates must pass.
        route = classify_mutating("POST", path)
        caller: Optional[CallerContext] = None
        if route is not None:
            name, project_id = route
            try:
                caller = svc.authorize_mutating(
                    self.headers, body, name, project_id)
            except AuthorizationError as exc:
                return self._send(_auth_status(exc),
                                  {"ok": False, "refusal": str(exc)})
        # --- engine front (Track 2B/3) ---
        if path == "/api/intent/dispatch":
            # Conversational intent dispatch (A): normalize -> classify
            # (chat/ambiguous answered by the chat handler, never consuming
            # a task slot) -> metering gate -> scheduler run executing the
            # real NL dispatch path. Exact statuses (200/400/429/500/504);
            # refusals are HTTP 200 body data, NOT in the contract table.
            try:
                code, obj = svc.ff["intent"].intent_dispatch(body)
            except Exception as exc:  # honest 500, never silent
                # The service raises (loud by design) for faults like the
                # missing chat handler; without this the exception kills
                # the connection (RemoteDisconnected) instead of the
                # contracted 500 {ok, error}.
                return self._send(500, {"ok": False,
                                        "error": f"{type(exc).__name__}: "
                                                 f"{exc}"})
            return self._send(code, obj)
        m = re.fullmatch(r"/api/capabilities/([A-Za-z0-9_.\-]+)/restore", path)
        if m:
            # Governed restore: canonical's restore_everywhere requires the
            # HTTP CALLER's own credentials (caller=), never the engine's
            # oracle handle (the old authority= handle was forgeable). The
            # operator gate above already enforced agent:restore +
            # trust:transition; restore_everywhere re-enforces them.
            return self._send(*svc.restore_capability(m.group(1), body, caller))
        if path == "/api/voice/stt":
            # Honest unavailability: no STT machinery exists anywhere in
            # the substrate; a transcript is never fabricated.
            return self._send(*svc.unavailable(
                lambda: voice_svc.transcribe()))
        if path == "/api/voice/tts":
            # Governed TTS: bearer auth (checked first, above) -> scoped
            # WRITE_FS grant -> admitted capability -> real WAV bytes.
            return self._send(*svc.media_tts(body))
        if path == "/api/media/song":
            # Governed song assembly -> real WAV bytes.
            return self._send(*svc.media_song(body))
        if path == "/api/media/image":
            # Governed image generation -> real PNG bytes.
            return self._send(*svc.media_image(body))
        if path == "/api/media/video":
            # Governed video generation -> real MP4 bytes.
            return self._send(*svc.media_video(body))
        # --- third-track services (FF group, proven order) ---
        try:
            ff = svc.ff
            scheduler = ff["scheduler"]
            projects = ff["projects"]
            files = ff["files"]
            artifacts = ff["artifacts"]
            if path == "/api/runs":
                # [Worker D / S5] guarded: 35/day + 25-queue enforced here.
                # The raw scheduler.submit() bypass is closed.
                return self._submit_guarded(body)
            if path.startswith("/api/runs/"):
                parts = path.split("/")
                rid, action = parts[3], parts[4] if len(parts) > 4 else ""
                fn = {"pause": scheduler.pause_run,
                      "resume": scheduler.resume_run,
                      "stop": scheduler.stop_run,
                      "cancel": scheduler.cancel_run}.get(action)
                if fn is None:
                    return self._send(404, {"error": "not found"})
                return self._result(fn(rid))
            if path == "/api/projects":
                return self._result(projects.create(body))
            if path.startswith("/api/projects/") and path.endswith("/transition"):
                return self._result(projects.transition(
                    path.split("/")[3], body.get("to_state", ""),
                    reason=body.get("reason", "")))
            # [Worker D / S12] retirement / resume: the locked Phase 2
            # internal archive (RETIRED state), restorable with full state.
            if path.startswith("/api/projects/") and path.endswith("/retire"):
                return self._result(projects.retire_project(
                    path.split("/")[3], reason=body.get("reason", "")))
            if path.startswith("/api/projects/") and path.endswith("/resume"):
                return self._result(
                    projects.project_resume(path.split("/")[3]))
            if path.startswith("/api/projects/") and path.endswith("/run_loop"):
                return self._result(projects.run_loop(
                    path.split("/")[3],
                    max_rounds=int(body.get("max_rounds", 4))))
            if "/loop/" in path and path.endswith("/stop") \
                    and path.startswith("/api/projects/"):
                parts = path.split("/")
                return self._result(projects.stop_loop(parts[5]))
            if path == "/api/files/write":
                return self._result(files.write_text(
                    body.get("path", ""), body.get("content", "")))
            if path == "/api/files/mkdir":  # S3
                return self._result(files.mkdir(body.get("path", "")))
            if path == "/api/files/archive/extract":  # S2
                return self._result(files.extract(
                    body.get("path", ""), body.get("dest", "")))
            if path == "/api/artifacts":
                return self._result(artifacts.save(
                    body.get("name", ""), body.get("language", ""),
                    body.get("code", ""), metadata=body.get("metadata")))
            if path.startswith("/api/artifacts/") and path.endswith("/run"):
                res = artifacts.run(
                    _aid(path.split("/")[3]),
                    revision=body.get("revision"),
                    timeout=float(body.get("timeout", 30)))
                return self._result(res)
        except Exception as exc:  # honest 500, never silent
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self._auth_denied("DELETE"):
            return
        path = urllib.parse.urlparse(self.path).path
        svc: "_Service" = self.server.svc  # type: ignore
        # --- backend-contract table FIRST (precedence rule) ---
        contract_routes = svc.ff.get("contract_routes") or {}
        if path.startswith("/api/") and _contract_hit(
                contract_routes, "DELETE", path):
            return self._contract_result(
                _contract_dispatch(contract_routes, "DELETE", path, {}))
        # --- operator-auth gate (C): the artifact delete route is
        # mutating. No body is parsed for DELETE; the credential comes from
        # the X-Agent-Id / X-Agent-Token headers. Runs after the contract
        # table per the precedence rule, before the legacy handlers.
        route = classify_mutating("DELETE", path)
        if route is not None:
            name, project_id = route
            try:
                svc.authorize_mutating(self.headers, {}, name, project_id)
            except AuthorizationError as exc:
                return self._send(_auth_status(exc),
                                  {"ok": False, "refusal": str(exc)})
        try:
            if path.startswith("/api/projects/"):
                # [Worker D / S12] locked Phase 2: projects are NEVER
                # permanently deleted. delete_project answers with the
                # typed refusal (code permanent_deletion_refused) naming
                # the retire route; 403, not a silent no-op.
                res = svc.ff["projects"].delete_project(path.split("/")[3])
                return self._send(200 if res.get("ok") else 403, res)
            if path.startswith("/api/artifacts/"):
                return self._result(svc.ff["artifacts"].delete(
                    _aid(path.split("/")[3])))
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


class _Service:
    """The engine-bound service object the handler delegates to.

    Builds BOTH fronts: the Track 2B/3 engine front (SwarmEngine +
    IntentRouter + NLToolDispatcher, booting on the constructing thread —
    the serving thread — per the KD-2 thread-affinity discipline) and the
    third-track service dict (scheduler, projects, files, artifacts,
    intent dispatch, metering, ...).

    Also binds the Phase-3 operator-auth machinery (AgentDirectory on the
    engine's oracle registry + OPERATOR provisioning) used by the
    mutating-route gate in _Handler.
    """

    def __init__(self, db_path: str, base_dir: Optional[str] = None,
                 enable_dispatch_learning: bool = True):
        db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.engine = SwarmEngine(db_path=db_path)
        self.router = IntentRouter(self.engine)
        self.dispatcher = NLToolDispatcher(self.engine, self.router)
        if base_dir is None:
            base_dir = os.path.join(
                os.path.dirname(os.path.abspath(db_path)), "service_data")
        self.base_dir = base_dir
        # [C / operator auth] Caller authorization: bind the AgentDirectory
        # to the engine's own oracle registry -- the same wiring every
        # agent<->REMOR choke point uses (AgentDirectory(engine.oracle_registry)),
        # not a parallel identity store. Fail closed when the engine has no registry.
        oreg = getattr(self.engine, "oracle_registry", None)
        if oreg is None:
            raise RuntimeError(
                "http_adapter: engine has no oracle registry -- caller "
                "authorization cannot be verified; refusing boot")
        self.agents = AgentDirectory(oreg)
        # [Worker 1 / LIVE DISPATCH FLOW] Dispatch-learning wiring: boot (or
        # re-attach) the real organizational store at the deployment-convention
        # path (<engine-db-dir>/agent_org.db, where the read-only
        # dispatch_evidence endpoint below locates it) and bind it as the
        # dispatcher's native learning hook. Attribute is always set: None
        # when the kill switch is off or boot failed, in which case the
        # dispatcher keeps its learning=None behavior exactly as before.
        self.dispatch_learning = None
        if enable_dispatch_learning:
            try:
                self.dispatch_learning = self._boot_dispatch_learning()
            except Exception as exc:  # fail closed: dispatch still works,
                # just without the learning side effect; the failure is
                # logged, never swallowed invisibly.
                print(f"http_adapter: dispatch learning unavailable "
                      f"({type(exc).__name__}: {exc}); continuing without "
                      f"it", flush=True)
        self.dispatcher.learning = self.dispatch_learning
        # The operator token file is written under base_dir at first boot;
        # the directory must exist before _provision_operator runs.
        os.makedirs(self.base_dir, exist_ok=True)
        # Operator provisioning runs BEFORE build_services: a fail-closed
        # provisioning refusal (missing/stale token file) must abort boot
        # before any scheduler/service thread is started, never leave
        # threads running behind a half-constructed service.
        self.operator_id, self.operator_fresh_token = self._provision_operator()
        self.ff = build_services(base_dir)
        # [Media wiring] wire the verified media substrate behind the
        # governed HTTP media fronts (auth + scoped WRITE_FS grant +
        # governed dispatch with persisted records). Fail-closed at boot.
        self._wire_media()
        # [REMOTE-DISPATCH-1] wire the remote-dispatch service: pair /
        # session / dispatch / kill / end routes. Built over the
        # Handler's canonical AgentDirectory (engine.oracle_registry) --
        # not a parallel identity store -- and merged into the contract
        # route table. Fail-closed at boot.
        self._wire_remote_dispatch()

    # -- dispatch learning (Worker 1 / LIVE DISPATCH FLOW) ----------------------------
    def _boot_dispatch_learning(self):
        """Boot (or re-attach) the real org backing the learning service.

        Org store lives at the deployment-convention path
        <engine-db-dir>/agent_org.db -- the same file the read-only
        ``dispatch_evidence`` endpoint reads. Mirrors the ensure_review_board
        / deploy_org precedents: explicit anchor genesis on first boot,
        re-attach (journal-verified) on later boots, and an auto-anchor
        wrapper so every legitimate store write re-anchors the journal tip
        (without it a later re-attach would fail AnchorMismatch on the
        evidence rows written since the last anchor).

        HONEST BOUNDARY (documented at the seam): completion is NEVER
        auto-invoked from the dispatcher. A live dispatch carries no
        technique metadata (technique_name / code / entrypoint /
        generality_cases), and fabricating it would be simulation. Only the
        native evidence capture runs automatically; ``complete_dispatch_knowledge``
        is invoked explicitly by an authorized caller (the engine as root
        authority, or the attributed agent) who can supply the real
        technique metadata.
        """
        from swarm_engine.agent_org.org import RemorOrganization
        from swarm_engine.governance.anchor import collect_anchor_heads
        from swarm_engine.governance.caller_authorization import (
            AgentDirectory as _AgentDirectory)
        from swarm_engine.governance.oracle_binding import ENGINE_PRODUCER_ID
        from swarm_engine.services.dispatch_learning import (
            DispatchLearningService)
        org_dir = os.path.dirname(self.engine.db_path) or "."
        org = RemorOrganization.boot(org_dir)
        eng_caller = org.oregistry.engine_handle()
        # Explicit anchor genesis on first boot (no lazy/implicit genesis).
        if not org.anchor.journal_exists():
            org.anchor.initialize(
                collect_anchor_heads(org.store, org.oregistry),
                ENGINE_PRODUCER_ID, caller=eng_caller)
        # Auto-anchor every legitimate store write, exactly the
        # deploy_org/ensure_review_board pattern.
        store, oreg, anchor = org.store, org.oregistry, org.anchor

        def _anchor_now():
            anchor.anchor(collect_anchor_heads(store, oreg), reason="verdict",
                          authority=ENGINE_PRODUCER_ID, caller=eng_caller)

        orig_insert = store.insert

        def insert_and_anchor(table, fields):
            seq = orig_insert(table, fields)
            _anchor_now()
            return seq

        store.insert = insert_and_anchor
        orig_chained = oreg._insert_chained

        def chained_and_anchor(table, fields):
            res = orig_chained(table, fields)
            _anchor_now()
            return res

        oreg._insert_chained = chained_and_anchor
        return DispatchLearningService(
            org, _AgentDirectory(org.oregistry), self.engine)

    def complete_dispatch_knowledge(
            self, *, dispatch_id: str, agent_id: str, assignment_id: str,
            args: Dict[str, Any], result_value: Any, technique_name: str,
            code: str, entrypoint: str, problem_class: str,
            tags: List[str], io_contract: Dict[str, Any],
            params: Dict[str, Any], generality_cases: List[Any],
            caller: Any):
        """Governed production entry for dispatch -> knowledge admission.

        Delegates to DispatchLearningService.complete_dispatch_knowledge:
        idempotent evidence capture -> independent review (subprocess
        re-execution) -> L2 admission with held-out generality cases.
        All trust gates (caller auth, review, admission) live in the
        service and are unchanged. Raises RuntimeError when the learning
        service is unavailable (kill switch off or boot failure) -- fail
        closed, never a silent no-op.
        """
        if self.dispatch_learning is None:
            raise RuntimeError(
                "dispatch learning is disabled on this service "
                "(enable_dispatch_learning=False or the learning service "
                "failed to boot); complete_dispatch_knowledge refused -- "
                "no silent no-op")
        return self.dispatch_learning.complete_dispatch_knowledge(
            dispatch_id=dispatch_id, agent_id=agent_id,
            assignment_id=assignment_id, args=args,
            result_value=result_value, technique_name=technique_name,
            code=code, entrypoint=entrypoint, problem_class=problem_class,
            tags=tags, io_contract=io_contract, params=params,
            generality_cases=generality_cases, caller=caller)

    # -- operator provisioning (C, Phase 3 operator auth) -------------------------------------------
    def _provision_operator(self):
        """Provision the OPERATOR identity on first boot.

        Returns (agent_id, fresh_token_or_None). The token is generated
        (256-bit), ONLY its sha256 is stored; the plaintext is written once
        to <base_dir>/operator.token (mode 0600, atomic O_EXCL create) and
        printed to stdout at boot. Fail-closed matrix:
          * no identity + no token file -> provision (the only mint path);
          * identity exists (live OR destroyed) + token file missing ->
            REFUSE boot: the operator is locked out; minting a second
            operator silently would orphan the original credential. Restore
            operator.token from backup. There is no rotation path for the
            pinned ID: register_agent refuses an already-existing agent_id,
            so a destroyed remor:operator is never re-minted (a fresh
            engine DB is required for a new operator identity).
          * no identity + stale token file (DB wiped) -> REFUSE boot:
            remove/rename the stale file to provision a fresh operator.
          * live identity + token file -> the file is validated
            fail-closed (regular file, mode 0600, content authenticates
            as the operator); grants re-ensured idempotently (never
            duplicated, never re-printed).
          * live identity + token file -> normal boot; grants re-ensured
            idempotently (never duplicated, never re-printed).
          * destroyed identity + token file -> boot continues WITHOUT
            re-provisioning: a destroyed operator is not resurrected by
            reboot (its credential stays dead); the stale file is left
            untouched.
        The operator holds effectively-unlimited HTTP permission (every
        mutating route) plus the downstream decision classes the restore
        route requires (agent:restore, trust:transition). It does NOT hold
        DECISION_GRANT: the HTTP operator can mutate, not mint identities
        or issue grants -- issuance stays with the engine (root authority).
        """
        agents = self.agents
        token_path = os.path.join(self.base_dir, OPERATOR_TOKEN_FILE)
        cur = agents.reg._conn.cursor()
        cur.execute("SELECT 1 FROM ob_agents WHERE agent_id=?",
                    (OPERATOR_AGENT_ID,))
        identity_exists = cur.fetchone() is not None
        file_exists = os.path.exists(token_path)

        if identity_exists and not file_exists:
            raise RuntimeError(
                "http_adapter: OPERATOR identity exists in the engine DB "
                f"but {token_path} is missing -- refusing to mint a "
                "duplicate operator (the original credential would be "
                "silently orphaned and the operator locked out). Restore "
                "operator.token from backup. To rotate: destroy the "
                "operator identity via a DECISION_GRANT holder (the "
                "engine), delete the token file, and reboot.")
        if not identity_exists and file_exists:
            raise RuntimeError(
                "http_adapter: no OPERATOR identity in the engine DB but "
                f"{token_path} already exists (stale file, e.g. the DB was "
                "wiped) -- refusing boot. Remove or rename the stale file "
                "to provision a fresh operator.")
        if identity_exists:
            if agents._destroyed(OPERATOR_AGENT_ID):
                print("[remor] OPERATOR identity is destroyed: no operator "
                      "provisioned at boot (a destroyed credential is not "
                      "resurrected by reboot).", flush=True)
            else:
                self._validate_operator_token_file(token_path)
                self._ensure_operator_grants()
                print("[remor] OPERATOR identity present; "
                      f"token file {token_path} unchanged.", flush=True)
            return OPERATOR_AGENT_ID, None

        # First boot: mint the operator via the engine (root authority).
        # register_agent mints the 256-bit token itself and returns the
        # credential -- the ONLY place the plaintext exists. The token
        # written to operator.token MUST be the returned one, not a
        # separately generated value (a past bug wrote a different token
        # than the registered hash, locking the operator out at first
        # boot; caught by the boot smoke test).
        engine_caller = self.engine.oracle
        cred = agents.register_agent(
            engine_caller, agent_id=OPERATOR_AGENT_ID,
            source="operator provisioning (http_adapter first boot)",
            decision_classes=("agent:restore", "trust:transition"))
        token = cred.token
        agents.issue_http_permission(
            engine_caller, OPERATOR_AGENT_ID, "effectively-unlimited")
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                     0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
        except BaseException:
            try:
                os.unlink(token_path)
            except OSError:
                pass
            raise
        # Belt and braces: O_EXCL already made creation atomic, but verify
        # the mode survived the process umask.
        os.chmod(token_path, 0o600)
        print("[remor] OPERATOR identity provisioned: "
              f"{OPERATOR_AGENT_ID}", flush=True)
        print("[remor] OPERATOR token (shown ONCE, never stored in "
              "plaintext):", flush=True)
        print(token, flush=True)
        print(f"[remor] plaintext also written to {token_path} (mode 0600). "
              "Protect it like a password: anyone holding it IS the "
              "operator.", flush=True)
        return OPERATOR_AGENT_ID, token

    def _validate_operator_token_file(self, token_path: str) -> None:
        """Fail-closed validation of an existing operator.token.

        The file must be a regular file (symlinks refused -- a symlink
        could be swapped to exfiltrate or substitute the credential),
        mode 0600, and its content must authenticate as the operator
        against the stored credential hash. Any mismatch REFUSES boot
        rather than overwriting: silently replacing the file could lock
        out the legitimate operator or bless an attacker-supplied token.
        """
        try:
            fd = os.open(token_path,
                         os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            raise RuntimeError(
                "http_adapter: OPERATOR token file "
                f"{token_path} cannot be opened without following "
                f"symlinks -- refusing boot ({exc})")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeError(
                    "http_adapter: OPERATOR token file "
                    f"{token_path} is not a regular file -- refusing "
                    "boot")
            if stat.S_IMODE(st.st_mode) != 0o600:
                raise RuntimeError(
                    "http_adapter: OPERATOR token file "
                    f"{token_path} has mode "
                    f"{oct(stat.S_IMODE(st.st_mode))}, expected 0o600 -- "
                    "refusing boot")
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
            fd = None  # ownership moved to the file object; it closed fd
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if not self.agents.authenticate(OPERATOR_AGENT_ID, token):
            raise RuntimeError(
                "http_adapter: OPERATOR token file "
                f"{token_path} does not authenticate as "
                f"{OPERATOR_AGENT_ID} -- refusing boot rather than "
                "overwriting a credential that no longer matches the "
                "stored identity")

    def _ensure_operator_grants(self) -> None:
        """Idempotent: grant the live operator anything it is missing
        (effectively-unlimited HTTP permission + restore decision
        classes). Never duplicates live grants."""
        agents = self.agents
        engine_caller = self.engine.oracle
        live_classes = {a["decision_class"]
                        for a in agents.agent_grants(
                            OPERATOR_AGENT_ID)["authorizations"]}
        for cls in ("agent:restore", "trust:transition"):
            if cls not in live_classes:
                agents.issue_grant(engine_caller, OPERATOR_AGENT_ID, cls)
        live_scopes = {p["scope_class"] for p in
                       agents.live_http_permissions(OPERATOR_AGENT_ID)}
        if "effectively-unlimited" not in live_scopes:
            agents.issue_http_permission(
                engine_caller, OPERATOR_AGENT_ID, "effectively-unlimited")

    # -- HTTP caller identity (C) --------------------------------------------
    @staticmethod
    def http_caller(headers: Any, body: Dict[str, Any]):
        """Resolve the HTTP caller's credential: X-Agent-Id / X-Agent-Token
        headers, or body["caller"] = {"agent_id": ..., "token": ...}.
        Returns a CallerContext, or None when no credential was supplied."""
        hid = htok = None
        if headers is not None:
            hid = headers.get("X-Agent-Id")
            htok = headers.get("X-Agent-Token")
        if hid and htok:
            return CallerContext(agent_id=hid, token=htok)
        cbody = body.get("caller") if isinstance(body, dict) else None
        if isinstance(cbody, dict) and cbody.get("agent_id") \
                and cbody.get("token"):
            return CallerContext(agent_id=str(cbody["agent_id"]),
                                 token=str(cbody["token"]))
        return None

    def authorize_mutating(self, headers: Any, body: Dict[str, Any],
                           route_name: str,
                           project_id: Optional[str] = None) -> CallerContext:
        """Auth gate for one mutating route. Returns the authenticated
        CallerContext; raises AuthorizationError with an explicit reason
        (401-style unauthenticated / 403-style unauthorized).

        The caller must hold a live HTTP permission covering the route
        (the permission spectrum: allow-once / allow-for-project /
        effectively-unlimited) AND, where the route triggers a downstream
        agent<->REMOR choke point, the underlying decision class -- not
        just HTTP access (e.g. restore requires agent:restore and
        trust:transition). A single-use (allow-once) HTTP grant is
        consumed only after EVERY authorization check passes: a call that
        ends 401/403 never burns the grant."""
        from swarm_engine.governance.caller_authorization import require_all
        caller = self.http_caller(headers, body)
        agent_id, grant_id = self.agents.http_coverage(
            caller, route_name, project_id)
        extra = ROUTE_DECISION_CLASSES.get(route_name)
        if extra:
            require_all(self.engine.oracle_registry, self.agents, caller,
                        extra, route_name)
        if grant_id is not None:
            self.agents.consume_http_grant(agent_id, grant_id)
        return caller

    # -- capability inventory --------------------------------------------
    def capabilities(self):
        import sqlite3
        out = []
        con = sqlite3.connect(self.engine.db_path)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT capability_id, name, goal, version, status "
                "FROM plan_capabilities ORDER BY created_at DESC LIMIT 1000"
            ).fetchall()
        finally:
            con.close()
        for r in rows:
            try:
                eff = effective_status(self.engine, r["capability_id"])
            except Exception as exc:
                eff = {"effective": "unknown", "error": str(exc)}
            # M5 frozen quarantine-reason API: surface WHY a capability is
            # quarantined in the list-view JSON (no more bare QUARANTINED
            # badges). Fail-closed: a reason lookup failure must never break
            # the capabilities listing.
            try:
                qr = get_quarantine_reason(r["capability_id"],
                                           engine=self.engine)
            except Exception:
                qr = {"reason": None, "since": None, "system": "none"}
            out.append({
                "capability_id": r["capability_id"],
                "name": r["name"],
                "goal": r["goal"],
                "version": r["version"],
                "store_status": r["status"],
                "effective_status": eff.get("effective"),
                "consistent": eff.get("consistent"),
                "trust": eff.get("trust"),
                "lifecycle": eff.get("lifecycle"),
                "quarantine_reason": qr.get("reason"),
                "quarantine_since": qr.get("since"),
                "quarantine_system": qr.get("system"),
            })
        return out

    # -- intent dispatch ---------------------------------------------------
    # NOTE (phase 4b, D3): the old engine-front sync dispatch
    # (svc.intent_dispatch -> self.dispatcher.dispatch, with the
    # Artifact-Lab media registration) is REMOVED. POST /api/intent/dispatch
    # routes to the brought IntentDispatchService (conversational path:
    # normalize -> classify-before-metering -> scheduler run), which is the
    # phase-3 recorded disposition for this route. The artifact-lab
    # registration lived only on that sync path; media artifacts remain
    # downloadable via /api/media/files/<name> and the binary artifact
    # store (/api/artifacts/binary). _Service.dispatcher (NLToolDispatcher)
    # is retained: the governed media fronts (_dispatch_media) and the
    # dispatch-evidence reads below use it.

    # -- dispatch evidence (Track 3) --------------------------------------
    def dispatch_evidence(self, evidence_id: str):
        """Read one dispatch-evidence record from the organizational store.

        Read-only: this endpoint never creates trust, it only exposes the
        chained evidence rows. The org store is located by deployment
        convention (<engine-db-dir>/agent_org.db); absent -> 404.
        """
        import os
        import sqlite3
        org_db = os.path.join(os.path.dirname(self.engine.db_path),
                              "agent_org.db")
        if not os.path.exists(org_db):
            return 404, {"error": "no organizational store at this deployment"}
        con = sqlite3.connect(org_db)
        try:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT evidence_id, dispatch_id, agent_id, assignment_id,"
                " request_text, route_via, route_score, capability_id,"
                " capability_version, plan_fingerprint, args_json,"
                " input_digest, result_json, result_digest, ok, error,"
                " evidence_json, created_at FROM ao_dispatch_evidence"
                " WHERE evidence_id=? ORDER BY seq DESC LIMIT 1",
                (evidence_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            return 404, {"error": f"unknown dispatch evidence {evidence_id!r}"}
        return 200, {"evidence": dict(row)}

    # -- remote dispatch (REMOTE-DISPATCH-1) ---------------------------------
    def _wire_remote_dispatch(self):
        """Mount the remote-dispatch route handlers on the contract table.

        The service is built over the Handler's canonical AgentDirectory
        (engine.oracle_registry) -- the same identity machinery every
        agent<->REMOR choke point uses -- and the engine's oracle as the
        grant-holding caller for pairing. No duplicate stores, routers,
        or identity systems. Routes inherit the contract table's auth
        and status mapping via _contract_dispatch.
        """
        from swarm_engine.services.remote_dispatch_api import (
            build_remote_dispatch_service, routes_for_remote_dispatch)
        rd_dir = os.path.join(self.base_dir, "remote_dispatch")
        svc = build_remote_dispatch_service(
            rd_dir, self.agents, self.engine.oracle)
        self.ff["remote_dispatch"] = svc
        self.ff["contract_routes"].update(
            routes_for_remote_dispatch(svc))

    # -- media front (wired to the verified substrate) -------------------------
    def _wire_media(self):
        """Wire the verified media substrate behind the governed HTTP
        media fronts, exactly as runtime/media/wiring.py specifies:

          1. register the four media primitives on the engine's public
             registry API (needs_ctx=True; each enforces
             ctx.governor.check(WRITE_FS, path) against the concrete
             target before touching the substrate -- lazily imported,
             fail-closed);
          2. issue a scoped WRITE_FS grant for <base_dir>/media through
             the governor's oracle-backed authority path (the engine's
             own handle); deny-by-default everywhere else;
          3. admit the four capabilities through the REAL admission path
             (type check -> effect ceiling -> permission check -> live
             smoke test -> store -> goal binding), and keep the admitted
             capability ids for governed dispatch.

        Per-medium fail-closed: a medium whose admission refuses (e.g.
        its substrate is absent in this environment) is left UNADMITTED
        -- never half-wired -- and the service boots with the media that
        did admit. ``media_cap_ids`` therefore contains ONLY admitted
        media; ``_dispatch_media`` refuses requests for unadmitted media
        honestly (501 medium_not_admitted) instead of KeyErroring.

        Caller-auth note (canonical): the boot-time admission threads the
        engine's own caller authority via media/wiring.py
        (engine.admit_as_engine), which canonical's caller-authenticated
        admission accepts. A medium whose admission refuses for any other
        reason is recorded per medium like any other admission refusal --
        the medium stays unadmitted and its routes honestly 501 -- and
        boot CONTINUES.
        """
        from swarm_engine.media.wiring import (
            admit_image_spec_capability, admit_media_capabilities,
            ensure_media_write_grant, register_media_primitives)
        media_dir = os.path.abspath(os.path.join(self.base_dir, "media"))
        os.makedirs(media_dir, exist_ok=True)
        self.media_out_dir = media_dir
        # governed output dir for the NL dispatcher's arg synthesis
        self.dispatcher.media_out_dir = media_dir
        register_media_primitives(self.engine)
        ensure_media_write_grant(self.engine, media_dir)
        # The engine's own caller authority is threaded through by
        # media/wiring.py (engine.admit_as_engine); no AuthorizationError
        # catch is needed -- a refusal for any other reason is recorded
        # per medium inside the returned report (fail-closed).
        report = admit_media_capabilities(
            self.engine, media_dir, attempt_smoke=True)
        self.media_cap_ids = {
            m: report[m]["capability_id"]
            for m in ("voice", "song", "image", "video")
            if report[m].get("admitted")}
        self.media_admission = report
        # spec-driven image renderer: same real admission path (same
        # engine-authority caller threading).
        spec_report = admit_image_spec_capability(
            self.engine, media_dir, attempt_smoke=True)
        self.media_admission["image_spec"] = spec_report
        if spec_report.get("admitted"):
            self.media_cap_ids["image_spec"] = spec_report["capability_id"]
        refused = [m for m in ("voice", "song", "image", "video")
                   if not report[m].get("admitted")]
        if refused:
            print(f"[media] boot continues without unadmitted media: "
                  f"{refused} -- requests for them are refused honestly",
                  flush=True)

    def _dispatch_media(self, medium: str, args: Dict[str, Any],
                        request_text: str):
        """Governed execution of one admitted media capability.

        dispatch_by_id skips only the NL routing step (the caller names
        the capability explicitly; the record marks route_via=
        "direct_by_id" so the provenance is explicit). Every other
        invocation-time check runs identically: existence, plan
        fingerprint, effective status, argument validation, real
        Composer execution (WRITE_FS enforced per target by the
        primitive against the scoped grant), and the persisted dispatch
        record in the dispatcher's own table.

        A medium that was not admitted at boot (refused admission, e.g.
        absent substrate) is refused here with ``medium_not_admitted``
        instead of raising KeyError: the admission refusal is the honest
        answer, and the service must not pretend the medium exists.
        """
        if medium not in self.media_cap_ids:
            entry = (self.media_admission or {}).get(medium, {})
            reasons = [f"media capability {medium!r} was not admitted at "
                       f"boot and is unavailable"]
            if entry.get("refusal_stage"):
                reasons.append(f"admission refusal stage: "
                               f"{entry['refusal_stage']}")
            reasons.extend(entry.get("reasons") or [])
            return DispatchResult(ok=False, refusal="medium_not_admitted",
                                  reasons=reasons)
        return self.dispatcher.dispatch_by_id(
            self.media_cap_ids[medium], args,
            producer="http:media-front", request_text=request_text)

    @staticmethod
    def _media_http_result(medium: str, res, download: Optional[str],
                           bounds) -> Tuple[int, Dict[str, Any]]:
        if not res.ok:
            if res.refusal == "bad_arguments":
                return 400, {"ok": False, "refusal": res.refusal,
                             "reasons": res.reasons}
            if res.refusal == "medium_not_admitted":
                # Honest unavailability: the medium was refused admission
                # at boot (e.g. absent substrate), so the service cannot
                # serve it. 501, not 500 -- this is not a server error.
                return 501, {"ok": False, "refusal": res.refusal,
                             "reasons": res.reasons,
                             "medium": medium}
            return 500, {"ok": False, "refusal": res.refusal,
                         "reasons": res.reasons}
        value = res.result if isinstance(res.result, dict) else {}
        if not value.get("ok", True):
            # Substrate clean refusal (e.g. empty text/prompt): the HTTP
            # request was well-formed, but the substrate refused it.
            # Typed 422 + ok: False (the Phase 4 honesty convention).
            return 422, {"ok": False, "error": value.get("error"),
                         "dispatch_id": res.dispatch_id,
                         "capability_id": res.capability_id,
                         "bounds": list(bounds)}
        out = {"ok": True, "medium": medium,
               "dispatch_id": res.dispatch_id,
               "capability_id": res.capability_id,
               "result": value, "bounds": list(bounds)}
        if download:
            out["download"] = f"/api/media/files/{download}"
        return 200, out

    def _media_out_path(self, stem: str, ext: str) -> Tuple[str, str]:
        """Server-chosen artifact name under the granted out-dir.

        Callers never choose the path: the only writable target is the
        granted media dir, and the primitive's WRITE_FS check enforces
        it per target at execution time.
        """
        name = f"{stem}_{uuid.uuid4().hex[:16]}.{ext}"
        return name, os.path.join(self.media_out_dir, name)

    def media_tts(self, body: Dict[str, Any]):
        text = body.get("text", "")
        voice = body.get("voice") or "default"
        name, path = self._media_out_path("tts", "wav")
        res = self._dispatch_media(
            "voice", {"text": text, "voice": voice, "path": path},
            "POST /api/voice/tts")
        ok_result = (res.ok and isinstance(res.result, dict)
                     and res.result.get("ok"))
        return self._media_http_result(
            "voice", res, name if ok_result else None,
            voice_svc.VOICE_BOUNDS)

    def media_song(self, body: Dict[str, Any]):
        lyrics = body.get("lyrics", "")
        spec = body.get("spec") or {}
        if not isinstance(spec, dict):
            return 400, {"ok": False,
                         "error": "spec must be a JSON object"}
        name, path = self._media_out_path("song", "wav")
        work_dir = os.path.join(
            self.media_out_dir, "_work", f"song_{uuid.uuid4().hex[:16]}")
        res = self._dispatch_media(
            "song", {"lyrics": lyrics, "spec": spec, "path": path,
                     "work_dir": work_dir},
            "POST /api/media/song")
        ok_result = (res.ok and isinstance(res.result, dict)
                     and res.result.get("ok"))
        return self._media_http_result(
            "song", res, name if ok_result else None,
            media_svc.SONG_BOUNDS)

    @staticmethod
    def _media_int(value, default: int) -> Tuple[bool, Any]:
        if value is None:
            return True, default
        if isinstance(value, bool):
            return False, value
        try:
            return True, int(value)
        except (TypeError, ValueError):
            return False, value

    def media_image(self, body: Dict[str, Any]):
        prompt = body.get("prompt", "")
        ok_w, width = self._media_int(body.get("width"), 512)
        ok_h, height = self._media_int(body.get("height"), 512)
        if not (ok_w and ok_h):
            return 400, {"ok": False,
                         "error": "width/height must be integers"}
        seed = body.get("seed")
        name, path = self._media_out_path("image", "png")
        res = self._dispatch_media(
            "image", {"prompt": prompt, "width": width, "height": height,
                      "seed": seed, "path": path},
            "POST /api/media/image")
        ok_result = (res.ok and isinstance(res.result, dict)
                     and res.result.get("ok"))
        return self._media_http_result(
            "image", res, name if ok_result else None,
            media_svc.IMAGE_BOUNDS)

    def media_video(self, body: Dict[str, Any]):
        prompt = body.get("prompt", "")
        dur = body.get("duration_s", body.get("seconds", 4.0))
        if isinstance(dur, bool):
            return 400, {"ok": False,
                         "error": "duration_s must be a number"}
        try:
            duration_s = float(dur)
        except (TypeError, ValueError):
            return 400, {"ok": False,
                         "error": "duration_s must be a number"}
        ok_f, fps = self._media_int(body.get("fps"), 24)
        ok_w, width = self._media_int(body.get("width"), 640)
        ok_h, height = self._media_int(body.get("height"), 360)
        if not (ok_f and ok_w and ok_h):
            return 400, {"ok": False,
                         "error": "fps/width/height must be integers"}
        seed = body.get("seed")
        name, path = self._media_out_path("video", "mp4")
        res = self._dispatch_media(
            "video", {"prompt": prompt, "path": path,
                      "duration_s": duration_s, "fps": fps,
                      "width": width, "height": height, "seed": seed},
            "POST /api/media/video")
        ok_result = (res.ok and isinstance(res.result, dict)
                     and res.result.get("ok"))
        return self._media_http_result(
            "video", res, name if ok_result else None,
            media_svc.VIDEO_BOUNDS)

    def media_file_bytes(self, name: str):
        """Fetch one generated media artifact as raw bytes.

        Traversal-proof: basename-only, strict name pattern, extension
        whitelist, and the resolved path must stay under media_out_dir
        (the only dir the grant covers).
        """
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}\.(wav|png|mp4)",
                             name or ""):
            return 404, {"error": "unknown media file"}
        path = os.path.abspath(os.path.join(self.media_out_dir, name))
        if not path.startswith(self.media_out_dir + os.sep):
            return 404, {"error": "unknown media file"}
        if not os.path.isfile(path):
            return 404, {"error": "unknown media file"}
        with open(path, "rb") as fh:
            data = fh.read()
        ctype = {"wav": "audio/wav", "png": "image/png",
                 "mp4": "video/mp4"}[name.rsplit(".", 1)[1]]
        return 200, {"data": data, "content_type": ctype}

    # -- governed restore ---------------------------------------------------
    def restore_capability(self, capability_id: str, body: Dict[str, Any],
                           caller=None):
        """Governed capability recovery (Track 2B).

        caller: the HTTP caller's CallerContext (authenticated by the
        operator-auth gate before this runs). It is FORWARDED to
        canonical's restore_everywhere, which requires the CALLER's own
        credentials -- the old authority=self.engine.oracle handle was
        forgeable (any object with a producer_id attribute passed), so the
        engine's own handle must never be passed for an HTTP caller.
        Unauthenticated callers are refused 401 before this runs, so a None
        caller here means the request never passed the mutating gate: fail
        closed rather than restoring.
        """
        reason = body.get("reason") or ""
        try:
            actions = restore_everywhere(
                self.engine, capability_id,
                caller=caller, reason=reason)
        except AuthorizationError as exc:
            return _auth_status(exc), {"ok": False, "refusal": str(exc),
                                       "capability_id": capability_id}
        except RestoreRefused as exc:
            return 409, {"ok": False, "refusal": str(exc),
                         "capability_id": capability_id}
        except Exception as exc:
            return 500, {"ok": False,
                         "error": f"{type(exc).__name__}: {exc}",
                         "capability_id": capability_id}
        return 200, {"ok": True, "capability_id": capability_id,
                      "actions": {k: v for k, v in actions.items()
                                  if k != "record"}}

    # -- honest refusals -----------------------------------------------------
    @staticmethod
    def unavailable(fn):
        try:
            fn()
        except CapabilityUnavailable as exc:
            return 501, {"ok": False, "unavailable": exc.as_dict()}
        return 200, {"ok": True}


def _status_for(result: Dict[str, Any]) -> int:
    if result.get("ok"):
        return 200
    err = str(result.get("error", "")).lower()
    if "not found" in err or "unknown " in err:
        return 404
    if "not legal" in err or "illegal" in err:
        return 409
    return 400


def _aid(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        raise ValueError(f"bad artifact id: {s!r}")


def build_services(base_dir: str) -> Dict[str, Any]:
    """Create all third-track services on scratch dirs.

    Returns the service dict (scheduler/projects/files/artifacts/base_dir/
    intent/...). No engine boots here: the scheduler boots its engine on
    its own worker thread, and the projects engine proxy boots on first
    touch (the project loop worker), preserving the KD-2 thread-affinity
    discipline.
    """
    from swarm_engine.services.scheduler import RunScheduler
    from swarm_engine.services.projects import ProjectService
    from swarm_engine.services.files import ScopedFileService
    from swarm_engine.services.artifacts import ArtifactStore
    from swarm_engine.services.tasks_api import (
        ScheduledTaskService, routes_for_tasks_api)
    from swarm_engine.services.routing import (
        ConversationRouter, routes_for_routing)
    from swarm_engine.services.execute_api import routes_for_execute
    from swarm_engine.services.binary_artifacts import (
        BinaryArtifactStore, routes_for_binary_artifacts)
    from swarm_engine.services.capability_api import (
        CapabilityAPI, routes_for_capabilities)
    from swarm_engine.services.evidence import (
        EvidenceStore, routes_for_evidence)
    from swarm_engine.services.agent_api import (
        build_agent_service, routes_for_agents)
    from swarm_engine.services.metering import (
        build_metering_service, routes_for_metering)
    # [A / conversational contract] intent dispatch service: the GUI's
    # "Run" button speaks intent (goal text, conversation_id); this owns
    # the route the GUI posts to. normalize -> classify -> (task: metering
    # gate -> scheduler) | (chat/ambiguous: chat handler, no task slot).
    from swarm_engine.services.intent_dispatch_api import (
        build_intent_dispatch_service)
    # [A / availability] typed coming-soon inventory the GUI reads.
    from swarm_engine.services.availability import routes_for_availability
    # [Worker C / Task 3] persistent recurrence scheduler: sqlite store +
    # pumper thread firing through metering.guarded_submit (real quota
    # guards). Constructed on the metering service so fires are metered
    # exactly like manual submits. Exposure is gated by
    # REMOR_RECURRENCE_ENABLED (default off); see recurrence.py for the
    # open free-vs-paid decision.
    from swarm_engine.services.recurrence import (
        RecurrenceService, routes_for_recurrence)
    # [Worker D / S10] bearer-token gate: AuthGate provisions
    # <base_dir>/api_token (0600) on first boot; _Handler._auth_denied
    # calls auth.check(method, path, headers) at the top of every do_*.
    # [Worker E / S7] constructed BEFORE the contract routes so the
    # capability-install route can consult auth.is_gated() and fail
    # closed (typed 501 install_requires_auth) when no token is
    # configured anywhere. NOTE: base_dir must exist first -- the gate
    # provisions its token file inside it.
    from swarm_engine.services.auth import AuthGate

    os.makedirs(base_dir, exist_ok=True)
    auth_gate = AuthGate(base_dir=base_dir)
    sched_db = os.path.join(base_dir, "scheduler.db")
    runtime_db = os.path.join(base_dir, "runtime.db")
    projects_db = os.path.join(base_dir, "projects.db")
    projects_root = os.path.join(base_dir, "projects")
    browser_root = os.path.join(base_dir, "browser")
    sandbox_dir = os.path.join(base_dir, "artifact_sandbox")
    artifacts_db = os.path.join(base_dir, "artifacts.db")
    for d in (projects_root, browser_root, sandbox_dir):
        os.makedirs(d, exist_ok=True)

    scheduler = RunScheduler(db_path=sched_db, runtime_db_path=runtime_db)
    projects = ProjectService(
        db_path=projects_db,
        projects_root=projects_root,
        engine=_lazy_engine(os.path.join(base_dir, "projects_runtime.db")),
    )
    files = ScopedFileService(root=browser_root, writable=True)
    artifacts = ArtifactStore(db_path=artifacts_db, sandbox_dir=sandbox_dir)
    service_anchor = _attach_service_anchor(
        base_dir, scheduler, artifacts, authority="remor:services")

    # ---- backend-contract services (S1; all on scratch DBs under base_dir,
    # constructed here on the serving thread — same construction as the
    # third-track reference adapter, but NOT its ThreadingHTTPServer:
    # this server stays single-threaded per the KD-2 discipline) ---------
    tasks = ScheduledTaskService(
        os.path.join(base_dir, "tasks.db"), scheduler)
    router = ConversationRouter(
        os.path.join(base_dir, "routing.db"),
        project_exists=lambda pid: bool(
            projects.get_project(pid).get("ok")),
    )
    # routes_for_execute constructs its own ExecuteService over `artifacts`.
    binary = BinaryArtifactStore(artifacts)
    # Scratch capability DB: CapabilityStore creates its schema if missing.
    capabilities = CapabilityAPI(os.path.join(base_dir, "capabilities.db"))
    evidence = EvidenceStore(os.path.join(base_dir, "evidence.db"))
    agents = build_agent_service(os.path.join(base_dir, "agents"))
    metering = build_metering_service(scheduler)
    # [A] intent dispatch: normalize -> classify -> (task: metering gate ->
    # scheduler) | (chat/ambiguous: chat handler, never a task slot).
    # Dedicated engine dir (intent.db) + real metering. The service
    # registers its executor on the scheduler so kind="intent_dispatch"
    # runs execute the real NL dispatch as their work (never a full
    # engine run). Constructed AFTER metering so task turns pass the
    # metering gate before the scheduler accepts work; the metering
    # service is built over the SAME scheduler, so the gate sees every
    # submission.
    intent = build_intent_dispatch_service(
        os.path.join(base_dir, "intent"), scheduler, metering)
    recurrence = RecurrenceService(
        os.path.join(base_dir, "recurrence.db"),
        metering.guarded_submit)

    contract_routes: Dict[Any, Any] = {}
    contract_routes.update(routes_for_tasks_api(tasks))
    # [Worker C / Task 3] AFTER routes_for_tasks_api: the recurrence
    # routes deliberately overwrite its ("POST", "/api/tasks/recurring")
    # 501 entry — the substrate now exists.
    contract_routes.update(routes_for_recurrence(recurrence))
    contract_routes.update(routes_for_routing(router))
    contract_routes.update(routes_for_execute(artifacts, sandbox_dir))
    contract_routes.update(routes_for_binary_artifacts(binary))
    # [Worker E / S7] the install route needs the bearer gate for its
    # fail-closed is_gated() check (typed 501 when no token configured).
    contract_routes.update(routes_for_capabilities(capabilities,
                                                   auth=auth_gate))
    contract_routes.update(routes_for_evidence(evidence))
    contract_routes.update(routes_for_agents(agents))
    contract_routes.update(routes_for_metering(metering))
    # [A] availability: typed coming-soon inventory (GET
    # /api/availability/coming-soon), a data route for the GUI Coming Soon
    # page. Registered here (not in the engine front) so it inherits the
    # contract table's exact 200/400/404/409 status mapping.
    contract_routes.update(routes_for_availability())

    services = {
        "scheduler": scheduler, "projects": projects,
        "files": files, "artifacts": artifacts,
        "base_dir": base_dir, "service_anchor": service_anchor,
        "tasks": tasks, "router": router, "binary": binary,
        "capabilities": capabilities, "evidence": evidence,
        "agents": agents, "metering": metering, "intent": intent,
        "recurrence": recurrence,
        "contract_routes": contract_routes,
        # [Worker D / S10] the real bearer-token gate. _Handler._auth_denied
        # (top of every do_*) calls auth.check(method, path, headers):
        # None = allow, (401, typed body) = refuse. is_gated() lets
        # sensitive routes fail closed when no token is configured
        # ([Worker E / S7] capability install -> typed 501).
        "auth": auth_gate,
    }
    # [A] the intent service's chat handler answers from the shared stores
    # (capability list, evidence, metering, dispatch history); the service
    # reference is how it reaches them.
    services["intent"].services = services
    return services


def service_anchor_paths(base_dir: str) -> Tuple[str, str]:
    """(journal_path, key_path) for the service anchor of one deployment.

    The anchor dir stays ``<base_dir>/../anchor_store`` -- outside the
    databases' parent dir, so the multi-DB placement check passes -- but
    the journal/key file names are scoped to THIS base_dir
    (``services_<basename>.anchor.{journal,key}``), matching the
    ``default_anchor_paths()`` per-database convention.

    A fixed shared journal name is a real defect: two ``build_services()``
    deployments whose base_dirs share one parent (e.g. two e2e runs with
    mkdtemp dirs directly under /tmp) would share one journal, and the
    second boot fail-closes against the first deployment's heads.
    Scoping the file names keeps each deployment's anchor independent
    while a re-boot of the SAME base_dir reuses its own journal.
    """
    base_abs = os.path.abspath(base_dir)
    stem = "services_" + (os.path.basename(base_abs) or "root")
    anchor_dir = os.path.normpath(os.path.join(base_abs, "..", "anchor_store"))
    return (os.path.join(anchor_dir, stem + ".anchor.journal"),
            os.path.join(anchor_dir, stem + ".anchor.key"))


def _attach_service_anchor(base_dir: str, scheduler, artifacts,
                           authority: str = "remor:services"):
    """Create, bind, and initialize-or-verify the service anchor journal.

    One CrossDbAnchor covers both service DBs (scopes
    ``scheduler:scheduler_runs`` + ``artifact:artifacts``). The journal
    and key live in ``<base_dir>/../anchor_store`` -- outside every
    protected DB's parent dir (``base_dir``), enforced by the anchor's
    multi-DB placement check -- with file names scoped per base_dir via
    service_anchor_paths() so sibling deployments never share a journal.

    Boot discipline (fail-closed):
    - journal exists -> verify_all() must pass, else raise;
    - journal missing -> both chain tables must be pristine (empty),
      then initialize(authority); a non-empty chain table with no
      journal is REFUSED (possible journal-deletion cover);
    - legacy (pre-chain) schema -> the stores already raised
      LegacySchemaError at construction; the operator migrates
      explicitly via migrate_legacy_*_db().
    """
    from swarm_engine.governance.anchor import (
        ChainAuditError, CrossDbAnchor)
    journal, key_path = service_anchor_paths(base_dir)
    os.makedirs(os.path.dirname(journal), exist_ok=True)
    anchor = CrossDbAnchor(
        journal, key_path,
        [("scheduler", scheduler.db_path),
         ("artifact", artifacts.db_path)])
    anchor.bind([("scheduler", scheduler), ("artifact", artifacts)])
    scheduler.attach_anchor(anchor, authority=authority)
    artifacts.attach_anchor(anchor, authority=authority)
    if anchor.journal_exists():
        ok, msg = anchor.verify_all()
        if not ok:
            raise ChainAuditError(
                f"service anchor verify failed at boot: {msg}")
    else:
        pristine = _chain_table_empty(
            scheduler.db_path, "scheduler_runs") and _chain_table_empty(
            artifacts.db_path, "artifacts")
        if not pristine:
            raise ChainAuditError(
                "service anchor journal is missing but the service "
                "databases are not pristine: refusing boot (possible "
                "journal-deletion cover). Restore the journal from "
                "backup or re-initialize explicitly.")
        anchor.initialize(authority)
    return anchor


def _chain_table_empty(db_path: str, table: str) -> bool:
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()


def _lazy_engine(db_path: str):
    """Engine proxy that boots on first attribute use (i.e. on the thread
    that first touches it — the project loop worker — satisfying the
    engine's thread-affinity for its sqlite connections)."""

    class _Proxy:
        _engine = None

        def _boot(self):
            if self._engine is None:
                from swarm_engine.core.engine import SwarmEngine
                self._engine = SwarmEngine(db_path=db_path)
            return self._engine

        def __getattr__(self, name):
            return getattr(self._boot(), name)

    return _Proxy()


def close_services(svc: Dict[str, Any]) -> None:
    """Best-effort shutdown of the third-track + contract service dict.

    The tasks dispatcher thread is stopped first so no new run is
    dispatched while the scheduler shuts down. The recurrence pumper is
    stopped alongside it (it fires through guarded_submit, so it must
    stop before the scheduler does).
    """
    for key in ("tasks", "recurrence", "scheduler"):
        try:
            svc[key].close()
        except Exception:
            pass


def run(db_path: str, host: str = "127.0.0.1", port: int = 8471,
        base_dir: Optional[str] = None) -> Tuple[HTTPServer, "_Service"]:
    """Create the service and its server. Single-threaded BY DESIGN: the
    engine is thread-affine (OracleRegistry holds a thread-bound sqlite
    connection -- known pre-existing limitation), so all requests are
    handled in the serving thread, the same thread that constructs the
    engine here. Call run() and serve_forever() in the same thread
    (main() does; the track2b/track2_new tests do). Do not wrap this in a
    ThreadingHTTPServer."""
    svc = _Service(db_path, base_dir)
    server = HTTPServer((host, port), _Handler)
    server.svc = svc  # type: ignore
    return server, svc


def serve(base_dir: str, host: str = "127.0.0.1", port: int = 0):
    """Start the unified adapter in a background thread.

    The background thread IS the serving thread: it constructs the
    _Service (the SwarmEngine boots there, satisfying thread-affinity) and
    then serves single-threaded. Returns
    (server, services_dict, thread, base_url); services_dict is the
    third-track service dict for close_services().
    """
    db_path = os.path.join(os.path.abspath(base_dir), "engine.db")
    server = HTTPServer((host, port), _Handler)
    ready = threading.Event()
    box: Dict[str, Any] = {}

    def target():
        svc = _Service(db_path, base_dir)
        server.svc = svc  # type: ignore
        box["svc"] = svc
        ready.set()
        server.serve_forever()

    thread = threading.Thread(target=target, daemon=True, name="remor-adapter")
    thread.start()
    if not ready.wait(timeout=180):
        raise RuntimeError("unified adapter: service failed to start")
    svc = box["svc"]
    return server, svc.ff, thread, f"http://{host}:{server.server_address[1]}"


def main():
    ap = argparse.ArgumentParser(
        description="REMOR unified HTTP API (engine front + third-track services)")
    ap.add_argument("--db", default=None,
                    help="engine DB path (default: <dir>/engine.db)")
    ap.add_argument("--dir", default=os.path.join(os.getcwd(), "remor_service_data"),
                    help="third-track service data dir")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8471)
    args = ap.parse_args()
    base_dir = os.path.abspath(args.dir)
    db_path = args.db or os.path.join(base_dir, "engine.db")
    server, svc = run(db_path, args.host, args.port, base_dir=base_dir)
    print(f"REMOR API on http://{args.host}:{args.port} "
          f"db={db_path} dir={base_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        close_services(svc.ff)


if __name__ == "__main__":
    main()
