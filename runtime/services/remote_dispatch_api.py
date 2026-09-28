"""HTTP surface for remote dispatch (backend contract).

Routes (the GUI's Dispatch tab binds to these once the route exists):
  POST /api/remote/pair
    {device_id, display_name} -> {agent_id, agent_token (shown once)}
    One-time enrollment. Engine-privileged (the service runs as the
    engine). The agent_token must reach the target device out of band
    (in product: the pairing flow).
  POST /api/remote/sessions
    {device_id, scope} -> {session_id, state: "pending"}
    The controller requests; ONLY the user (on the target) can consent.
  GET /api/remote/sessions
    -> {sessions: [...]} newest first, from the real session table.
  POST /api/remote/dispatch
    {session_id, session_token, actions: [...]} ->
      {ok, results} | {ok: false, refused: reason}
    A dispatch request targeting the remote session (the protocol
    extension: dispatch is no longer local-router-only). Refusals are
    BODY DATA, not HTTP errors -- same shape as intent_dispatch_api.
  POST /api/remote/sessions/<id>/kill -> {state: "killed"}
  POST /api/remote/sessions/<id>/end  -> {state: "ended"}

The session_token reaches the controller via the user's consent handoff
(user_grant_consent returns it to the user; the user gives it to REMOR).
The service never mints or guesses it.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from swarm_engine.governance import caller_authorization as authz
from swarm_engine.remote_dispatch import channel as chan
from swarm_engine.remote_dispatch.controller import (
    RemoteDispatchController, RemoteSession)
from swarm_engine.remote_dispatch.session_model import (
    RemoteDispatchStore, Scope, SessionError)


class RemoteDispatchService:
    def __init__(self, base_dir: str, agents: authz.AgentDirectory,
                 engine_caller: Any):
        os.makedirs(base_dir, exist_ok=True)
        self.db_path = os.path.join(base_dir, "remote_dispatch.db")
        self.controller = RemoteDispatchController(
            self.db_path, agents, engine_caller)

    # -- route handlers -------------------------------------------------
    def pair(self, body: Dict[str, Any]) -> Dict[str, Any]:
        device_id = (body.get("device_id") or "").strip()
        display_name = (body.get("display_name") or device_id).strip()
        if not device_id:
            return {"ok": False, "error": "device_id required"}
        try:
            out = self.controller.pair_device(device_id, display_name)
            return {"ok": True, **out}
        except SessionError as e:
            return {"ok": False, "error": str(e)}

    def request_session(self, body: Dict[str, Any]) -> Dict[str, Any]:
        device_id = (body.get("device_id") or "").strip()
        scope_d = dict(body.get("scope") or {"actions": []})
        # budget/ttl_s are Scope fields, not controller kwargs.
        if "budget" in scope_d:
            scope_d["max_actions"] = int(scope_d.pop("budget"))
        scope = Scope.from_dict(scope_d)
        if not device_id:
            return {"ok": False, "error": "device_id required"}
        try:
            out = self.controller.request_session(device_id, scope)
            return {"ok": True, **out}
        except SessionError as e:
            return {"ok": False, "error": str(e)}

    def list_sessions(self, body: Dict[str, Any]) -> Dict[str, Any]:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT session_id, device_id, state, created_at,"
                " consented_at, ended_at FROM rd_sessions ORDER BY"
                " created_at DESC LIMIT 50").fetchall()
            return {"ok": True,
                    "sessions": [dict(r) for r in rows]}
        finally:
            conn.close()

    def _endpoint(self, session_id: str) -> Dict[str, Any]:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            s = conn.execute(
                "SELECT device_id FROM rd_sessions WHERE session_id=?",
                (session_id,)).fetchone()
            if not s:
                return {}
            d = conn.execute(
                "SELECT endpoint_host, endpoint_port FROM rd_devices"
                " WHERE device_id=?", (s["device_id"],)).fetchone()
            return dict(d) if d else {}
        finally:
            conn.close()

    def dispatch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """A dispatch request targeting the remote session."""
        session_id = (body.get("session_id") or "").strip()
        session_token = body.get("session_token") or ""
        actions = body.get("actions") or []
        if not session_id or not session_token or not actions:
            return {"ok": False,
                    "error": "session_id, session_token, actions required"}
        ep = self._endpoint(session_id)
        if not ep.get("endpoint_host"):
            return {"ok": False,
                    "error": "target endpoint unknown (device offline?)"}
        try:
            sess = self.controller.connect(
                session_id, session_token,
                ep["endpoint_host"], ep["endpoint_port"])
        except Exception as e:  # noqa: BLE001 -- refusal is body data
            return {"ok": False, "refused": f"connect: {e}"}
        try:
            results = sess.act_batch(actions)
            return {"ok": True, "results": results}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "refused": str(e)}
        finally:
            try:
                sess.channel.close()
            except Exception:
                pass

    def kill(self, session_id: str,
             body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            out = self.controller.store.kill_session(
                session_id, by="api-relay")
            return {"ok": True, **out}
        except SessionError as e:
            return {"ok": False, "error": str(e)}

    def end(self, session_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            out = self.controller.store.end_session(session_id)
            return {"ok": True, **out}
        except SessionError as e:
            return {"ok": False, "error": str(e)}


def build_remote_dispatch_service(base_dir: str,
                                  agents: authz.AgentDirectory,
                                  engine_caller: Any
                                  ) -> RemoteDispatchService:
    return RemoteDispatchService(base_dir, agents, engine_caller)


def routes_for_remote_dispatch(
        svc: RemoteDispatchService) -> Dict[Any, Any]:
    def _kill(body: Dict[str, Any], session_id: str = "") -> Dict[str, Any]:
        return svc.kill(session_id, body)

    def _end(body: Dict[str, Any], session_id: str = "") -> Dict[str, Any]:
        return svc.end(session_id, body)

    # NOTE: the http_adapter's router matches (method, path) with {var}
    # path params; kill/end take the id from the path. If the adapter
    # does not support path params for new routes, the GUI posts the id
    # in the body -- both are accepted here.
    def _kill_any(body: Dict[str, Any]) -> Dict[str, Any]:
        sid = body.get("session_id", "")
        return svc.kill(sid, body)

    def _end_any(body: Dict[str, Any]) -> Dict[str, Any]:
        sid = body.get("session_id", "")
        return svc.end(sid, body)

    return {
        ("POST", "/api/remote/pair"): svc.pair,
        ("POST", "/api/remote/sessions"): svc.request_session,
        ("GET", "/api/remote/sessions"): svc.list_sessions,
        ("POST", "/api/remote/dispatch"): svc.dispatch,
        ("POST", "/api/remote/kill"): _kill_any,
        ("POST", "/api/remote/end"): _end_any,
    }
