"""Operational HTTP adoption layer (stdlib only).

Exposes the reachable Track 2 fronts over real HTTP so the GUI backend,
operators, and tests exercise the same code paths as in-process callers:

  POST /api/intent/dispatch        NL tool dispatch (real Composer path)
  GET  /api/dispatches             dispatch history (audit log)
  GET  /api/capabilities           capability inventory + effective_status
  POST /api/capabilities/<id>/restore  governed capability recovery
  GET  /api/voice/status           voice substrate status (UNAVAILABLE)
  POST /api/voice/stt | /api/voice/tts -> 501 honest refusal
  GET  /api/media/status           media substrate status (UNAVAILABLE)
  POST /api/media/image | /api/media/video -> 501 honest refusal
  GET  /api/health                 liveness

Restore authority: this service is operator tooling. The restore endpoint
authorizes with the engine's own oracle handle (remor:engine), the same
identity the engine uses for its internal trust transitions. Do not
expose this endpoint beyond the operator's trust boundary without adding
caller authentication.

Run: python3 -m swarm_engine.services.http_adapter --db <path> --port 8471
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.services import voice as voice_svc
from swarm_engine.services import media as media_svc
from swarm_engine.services.unavailable import CapabilityUnavailable
from swarm_engine.synthesis.integrity import (
    RestoreRefused, effective_status, restore_everywhere)
from swarm_engine.synthesis.intent_router import IntentRouter
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher

MAX_BODY = 256 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = "REMOR-Track2/1.0"

    # -- plumbing ---------------------------------------------------------
    def _send(self, code: int, obj: Any):
        blob = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

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

    def log_message(self, fmt, *args):  # keep stderr quiet in tests
        pass

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        svc: "_Service" = self.server.svc  # type: ignore
        if path == "/api/health":
            return self._send(200, {"ok": True})
        if path == "/api/capabilities":
            return self._send(200, {"capabilities": svc.capabilities()})
        if path == "/api/dispatches":
            return self._send(200, {"dispatches": svc.dispatcher.history()})
        m = re.fullmatch(r"/api/dispatch_evidence/([A-Za-z0-9_\-]+)", path)
        if m:
            return self._send(*svc.dispatch_evidence(m.group(1)))
        if path == "/api/voice/status":
            return self._send(200, voice_svc.status())
        if path == "/api/media/status":
            return self._send(200, media_svc.status())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        svc: "_Service" = self.server.svc  # type: ignore
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        if path == "/api/intent/dispatch":
            code, obj = svc.intent_dispatch(body)
            return self._send(code, obj)
        m = re.fullmatch(r"/api/capabilities/([A-Za-z0-9_.\-]+)/restore", path)
        if m:
            return self._send(*svc.restore_capability(m.group(1), body))
        if path == "/api/voice/stt":
            return self._send(*svc.unavailable(
                lambda: voice_svc.transcribe()))
        if path == "/api/voice/tts":
            return self._send(*svc.unavailable(
                lambda: voice_svc.speak(body.get("text", ""))))
        if path == "/api/media/image":
            return self._send(*svc.unavailable(
                lambda: media_svc.generate_image(body.get("prompt", ""))))
        if path == "/api/media/video":
            return self._send(*svc.unavailable(
                lambda: media_svc.generate_video(body.get("prompt", ""))))
        return self._send(404, {"error": "not found"})


class _Service:
    """The engine-bound service object the handler delegates to."""

    def __init__(self, db_path: str):
        self.engine = SwarmEngine(db_path=db_path)
        self.router = IntentRouter(self.engine)
        self.dispatcher = NLToolDispatcher(self.engine, self.router)

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
            })
        return out

    # -- intent dispatch ---------------------------------------------------
    def intent_dispatch(self, body: Dict[str, Any]):
        text = body.get("text")
        args = body.get("args")
        producer = body.get("producer") or "http:operator"
        res = self.dispatcher.dispatch(text, args, producer=producer)
        return 200, res.as_dict()

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

    # -- governed restore ---------------------------------------------------
    def restore_capability(self, capability_id: str, body: Dict[str, Any]):
        reason = body.get("reason") or ""
        try:
            actions = restore_everywhere(
                self.engine, capability_id,
                authority=self.engine.oracle, reason=reason)
        except RestoreRefused as exc:
            return 409, {"ok": False, "refusal": str(exc),
                         "capability_id": capability_id}
        except Exception as exc:
            return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}",
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


def run(db_path: str, host: str = "127.0.0.1", port: int = 8471):
    """Create the service and its server. Single-threaded BY DESIGN: the
    engine is thread-affine (OracleRegistry holds a thread-bound sqlite
    connection -- known pre-existing limitation), so all requests are
    handled in the serving thread, the same thread that constructs the
    engine here. Call run() and serve_forever() in the same thread
    (main() does). Do not wrap this in a ThreadingHTTPServer."""
    svc = _Service(db_path)
    server = HTTPServer((host, port), _Handler)
    server.svc = svc  # type: ignore
    return server, svc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8471)
    args = ap.parse_args()
    server, _ = run(args.db, args.host, args.port)
    print(f"REMOR Track2 API on http://{args.host}:{args.port} db={args.db}",
          flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
