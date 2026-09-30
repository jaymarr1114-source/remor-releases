"""HTTP surface for remote dispatch (backend contract).

Routes (the GUI's Dispatch tab binds to these once the route exists):
  POST /api/remote/pair
    {device_id, display_name} -> {agent_id, agent_token (shown once)}
    One-time enrollment. Engine-privileged (the service runs as the
    engine). The agent_token must reach the target device out of band
    (in product: the pairing flow).
  POST /api/remote/announce
    {device_id, agent_token, host, port, cert_fingerprint, ann_ts,
     ann_nonce} -> {ok, device_id, endpoint_host, endpoint_port,
                    pinned}
    The target's endpoint announcement (RD-TARGET-ENDPOINT-1):
    authenticated by the pairing agent token, replay-guarded by the
    store's (ann_ts, ann_nonce) check, the announced fingerprint
    matched against the pairing-time pin (or bound
    trust-on-first-announcement). The controller resolves a paired
    device_id to this live endpoint on the connect path; every
    refusal (unknown device, never announced, stale/unreachable
    endpoint) carries its exact reason.
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
    {session_id, session_token} -> the kill is RELAYED to the active
    execution target over its authenticated channel (pinned-TLS hello),
    then the controller store is killed. The body reports the real
    relay outcome: target_relay is "relayed" | "already_terminal" |
    "no_endpoint" | "no_token" | "relay_failed" (+ target_relay_detail).
    The local store is never presented as a target kill: a kill that
    cannot reach the target still kills locally but reports it honestly.
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
from swarm_engine.remote_dispatch.announce import (
    AnnounceError, validate_announcement)
from swarm_engine.remote_dispatch.controller import (
    ControllerError, RemoteDispatchController, RemoteSession)
from swarm_engine.remote_dispatch.session_model import (
    ENDED, EXPIRED, KILLED, RemoteDispatchStore, Scope, SessionError)


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

    def announce(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """POST /api/remote/announce: the target's endpoint
        announcement (RD-TARGET-ENDPOINT-1).

        Body: {device_id, agent_token, host, port, cert_fingerprint,
               ann_ts, ann_nonce}. Authenticated by the pairing agent
        token (the same bearer credential the target presents in the
        hello identity proof); replay-guarded by the store's
        (ann_ts, ann_nonce) check; the announced certificate
        fingerprint is matched against the pairing-time pin (or bound
        trust-on-first-announcement). Every refusal carries its exact
        reason -- a forged token, a replayed announcement, an endpoint
        hijack for another device's id, or a fingerprint mismatch all
        fail closed here and never reach the registry.
        """
        try:
            (device_id, agent_token, host, port, cert_fp, ann_ts,
             ann_nonce) = validate_announcement(body or {})
        except AnnounceError as e:
            return {"ok": False, "error": str(e)}
        dev = self.controller.store.get_device(device_id)
        if not dev:
            return {"ok": False,
                    "error": f"unknown device {device_id!r}: not paired"}
        agent_id = dev["agent_id"]
        if not self.controller.agents.authenticate(agent_id, agent_token):
            return {"ok": False,
                    "error": "announcement refused: agent token"
                             " authentication failed"}
        try:
            out = self.controller.store.record_endpoint_announcement(
                device_id, host, port, cert_fp, ann_ts, ann_nonce)
        except SessionError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **out}

    def dispatch(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """A dispatch request targeting the remote session."""
        session_id = (body.get("session_id") or "").strip()
        session_token = body.get("session_token") or ""
        actions = body.get("actions") or []
        if not session_id or not session_token or not actions:
            return {"ok": False,
                    "error": "session_id, session_token, actions required"}
        try:
            # The endpoint resolves from the device's announced endpoint
            # (RD-TARGET-ENDPOINT-1): no announcement, stale endpoint,
            # or unreachable endpoint each refuse here with the exact
            # reason -- the old "target endpoint unknown (device
            # offline?)" pre-check is gone, replaced by the precise
            # resolution refusal or the true transport error.
            sess = self.controller.connect(session_id, session_token)
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

    def _relay_kill_to_target(self, session_id: str,
                              session_token: str) -> Dict[str, Any]:
        """Deliver the protocol "kill" to the active execution target and
        report exactly what happened. Never raises.

        The target-side handler (target_agent._on_message "kill") kills
        the target-side session, closes capture, and hides the LIVE
        indicator synchronously before replying "kill_ok", so a
        delivered request means the target stopped. Every failure mode
        returns an honest status instead of a fake "killed":
          relayed          target confirmed the kill over the
                           authenticated channel
          already_terminal controller store already terminal; no channel
                           opened (the target refuses hello on terminal
                           sessions, so relaying is meaningless)
          no_endpoint      no known endpoint for the session's device
          no_token         no session_token supplied, so the
                           authenticated channel cannot be opened
          relay_failed     the connect or the kill request failed
          unknown_session  session_id not in the controller store

        This opens a direct authenticated channel -- RemoteSession.kill()
        swallows delivery status, and the UI must report the real
        outcome, so delivery is observed here. The protocol is unchanged:
        "kill" over the pinned-TLS hello-authenticated channel, reusing
        RemoteDispatchController.connect(); no device-identity branch,
        no new message kind, no new trust machinery.
        """
        try:
            s = self.controller.store._get_session(session_id)
        except SessionError as e:
            return {"target_relay": "unknown_session",
                    "target_relay_detail": str(e)}
        if s["state"] in (KILLED, ENDED, EXPIRED):
            return {"target_relay": "already_terminal",
                    "target_relay_detail": s["state"]}
        try:
            # Resolve through the announced-endpoint registry
            # (RD-TARGET-ENDPOINT-1): the no_endpoint status now carries
            # the exact resolution reason (unknown device, unpaired,
            # never announced) instead of the old vague string.
            host, port = self.controller.resolve_endpoint(s["device_id"])
        except (ControllerError, SessionError) as e:
            return {"target_relay": "no_endpoint",
                    "target_relay_detail": str(e)}
        if not session_token:
            return {"target_relay": "no_token",
                    "target_relay_detail":
                        "no session_token: cannot open the authenticated"
                        " kill channel"}
        try:
            # intent="kill": a kill-only control connection. The target
            # still verifies the session token and all replay guards, but
            # the hello is not refused as a concurrent duplicate of a
            # live control connection, so the kill stays prompt during a
            # long-running dispatch. The kill connection can never drive
            # the cursor. verify_proof=False: the relay runs on the
            # kill-bypass thread (never the engine thread), where the
            # thread-affine registry authentication is unavailable; TLS
            # pinning, the paired-device binding checks, and the target's
            # own session-token verification still apply, and the target
            # remains the authorization gate for the kill itself.
            sess = self.controller.connect(
                session_id, session_token,
                host, port, intent="kill",
                verify_proof=False)
        except Exception as e:  # noqa: BLE001 -- refusal is body data
            return {"target_relay": "relay_failed",
                    "target_relay_detail": f"connect: {e}"}
        try:
            reply = sess.channel.request("kill", {})
            if reply.get("kind") != "kill_ok":
                return {"target_relay": "relay_failed",
                        "target_relay_detail":
                            "unexpected target reply"
                            f" {reply.get('kind')!r}"}
            return {"target_relay": "relayed", "target_relay_detail": ""}
        except chan.ChannelError as e:
            return {"target_relay": "relay_failed",
                    "target_relay_detail": f"kill request: {e}"}
        finally:
            try:
                sess.channel.close()
            except Exception:
                pass

    def kill(self, session_id: str,
             body: Dict[str, Any]) -> Dict[str, Any]:
        session_token = (body or {}).get("session_token") or ""
        relay = self._relay_kill_to_target(session_id, session_token)
        if relay["target_relay"] == "unknown_session":
            return {"ok": False, "error": relay["target_relay_detail"]}
        try:
            out = self.controller.store.kill_session(
                session_id, by="api-relay")
        except SessionError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **out, **relay}

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
        ("POST", "/api/remote/announce"): svc.announce,
        ("POST", "/api/remote/sessions"): svc.request_session,
        ("GET", "/api/remote/sessions"): svc.list_sessions,
        ("POST", "/api/remote/dispatch"): svc.dispatch,
        ("POST", "/api/remote/kill"): _kill_any,
        ("POST", "/api/remote/end"): _end_any,
    }
