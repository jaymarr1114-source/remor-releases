"""Controller side: pair devices, request sessions, drive the channel.

The controller NEVER grants consent and NEVER executes cursor actions
itself -- it requests, the user (on the target) consents, and the target
executes within scope. Pairing enrolls the target's agent identity via
the caller-authorization AgentDirectory (the engine caller must hold
DECISION_GRANT); the target agent is granted the remote-dispatch
decision class at pairing.

At hello the controller authenticates the target's identity proof
(agent_id + token) against the AgentDirectory AND the pairing binding:
a spoofed device cannot present the enrolled agent's token.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..governance import caller_authorization as authz
from ..governance import oracle_binding as ob
from . import channel as chan
from .session_model import RemoteDispatchStore, Scope, SessionError

# The decision class lives in governance/oracle_binding.py (added to the
# closed grantable set); the engine holds it as root and it is granted
# to the target's agent at pairing.
DECISION_REMOTE_DISPATCH = ob.DECISION_REMOTE_DISPATCH


class ControllerError(RuntimeError):
    pass


class RemoteDispatchController:
    def __init__(self, db_path: str, agents: authz.AgentDirectory,
                 engine_caller: Any):
        self.store = RemoteDispatchStore(db_path)
        self.agents = agents
        self.engine_caller = engine_caller

    # -- pairing (one-time per device) ---------------------------------
    def pair_device(self, device_id: str, display_name: str,
                    cert_fingerprint: Optional[str] = None
                    ) -> Dict[str, Any]:
        """Enroll a target device. Returns the agent credential -- the
        token is shown ONCE and must be delivered to the target device
        out of band (in product: the pairing flow).

        cert_fingerprint pins the target's TLS identity: the controller
        refuses to complete a handshake against any other certificate.
        """
        cred = self.agents.register_agent(
            self.engine_caller,
            decision_classes=(DECISION_REMOTE_DISPATCH,),
            agent_id=f"rd-target-{device_id}")
        self.store.pair_device(device_id, cred.agent_id, display_name,
                               cert_fingerprint=cert_fingerprint)
        return {"device_id": device_id, "agent_id": cred.agent_id,
                "agent_token": cred.token, "display_name": display_name}

    # -- session ---------------------------------------------------------
    def request_session(self, device_id: str,
                        scope: Scope) -> Dict[str, Any]:
        return self.store.request_session(device_id, scope)

    def connect(self, session_id: str, session_token: str, host: str,
                port: int,
                intent: Optional[str] = None,
                verify_proof: bool = True) -> "RemoteSession":
        """Establish the channel: TLS (pinned target identity) + hello +
        mutual authentication. Raises ControllerError if the target's
        identity proof fails. intent="kill" marks a kill-only control
        connection (no cursor control): the target still verifies the
        session token and all replay guards, but the hello is not refused
        as a concurrent duplicate of the session's live control
        connection, so the user's kill switch stays prompt during a
        long-running dispatch. verify_proof=False skips ONLY the
        thread-affine registry authentication of the identity proof (the
        kill relay runs on the bypass thread, never the engine thread):
        TLS pinning, the paired-device binding checks, and the target's
        own session-token verification still apply, and the target
        remains the authorization gate for the kill itself."""
        sess = self.store._get_session(session_id)
        dev = self.store.get_device(sess["device_id"])
        pin = (dev or {}).get("cert_fingerprint")
        if not pin:
            raise ControllerError(
                "no pinned certificate for device "
                f"{sess['device_id']!r}: refusing to connect without"
                " target identity")
        ch = chan.ControllerChannel(host, port, pinned_fingerprint=pin)
        try:
            body = ch.hello(session_id, session_token, intent=intent)
        except chan.ChannelError as e:
            ch.close()
            raise ControllerError(f"handshake failed: {e}")
        proof = body.get("identity_proof", {})
        agent_id = proof.get("agent_id", "")
        agent_token = proof.get("agent_token", "")
        if verify_proof and not self.agents.authenticate(agent_id,
                                                        agent_token):
            ch.close()
            raise ControllerError(
                "target identity proof FAILED authentication "
                "(spoofed target refused)")
        if not dev or dev["agent_id"] != agent_id:
            ch.close()
            raise ControllerError(
                "target identity does not match the paired device binding")
        # sess and dev were resolved pre-handshake for the TLS pin; the
        # proof's device_id must agree with the pinned device.
        if proof.get("device_id", "") != dev["device_id"]:
            ch.close()
            raise ControllerError("session/device mismatch")
        return RemoteSession(self.store, ch, session_id)


class RemoteSession:
    """A bound, mutually-authenticated session channel."""

    def __init__(self, store: RemoteDispatchStore,
                 channel: chan.ControllerChannel, session_id: str):
        self.store = store
        self.channel = channel
        self.session_id = session_id

    def act(self, action: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Fail fast on a dead session: the target remains the authority
        # (its per-action check cannot be bypassed), but there is no
        # point sending bytes down a killed session's channel.
        try:
            st = self.store._get_session(self.session_id)["state"]
        except SessionError:
            st = "gone"
        if st != "live":
            raise ControllerError(
                f"session {st}: action refused (not live)")
        reply = self.channel.request("action", {"action": action})
        if reply["kind"] == "action_refused":
            raise ControllerError(
                f"target refused: {reply['body'].get('reason')}")
        if reply["kind"] != "action_ok":
            raise ControllerError(f"unexpected reply {reply['kind']}")
        return reply["body"]["results"]

    def act_batch(self, actions: List[Dict[str, Any]]
                  ) -> List[Dict[str, Any]]:
        reply = self.channel.request("action_batch", {"actions": actions})
        if reply["kind"] == "action_refused":
            raise ControllerError(
                f"target refused: {reply['body'].get('reason')}")
        if reply["kind"] != "action_ok":
            raise ControllerError(f"unexpected reply {reply['kind']}")
        return reply["body"]["results"]

    def kill(self) -> Dict[str, Any]:
        """Fail-closed relay: asks the target to kill. The user's direct
        target-side kill does not depend on this. The channel is closed
        so the target is released for a later session."""
        try:
            self.channel.request("kill", {})
        except chan.ChannelError:
            pass
        try:
            self.channel.close()
        finally:
            return self.store.kill_session(self.session_id,
                                           by="controller-relay")

    def get_frame(self, max_dim: int = 0) -> Dict[str, Any]:
        """Fetch one stream frame from the target. The target enforces
        session liveness, consent liveness, replay guards, the
        screen_share scope grant, and scope-clipping at capture; a
        killed/ended/expired session or ungranted scope raises
        ControllerError (refused, never a frozen or fabricated frame).
        Frames are observation only: they are never charged to the
        session action budget."""
        try:
            st = self.store._get_session(self.session_id)["state"]
        except SessionError:
            st = "gone"
        if st != "live":
            raise ControllerError(
                f"session {st}: frame refused (not live)")
        body: Dict[str, Any] = {}
        if isinstance(max_dim, int) and max_dim > 0:
            body["max_dim"] = max_dim
        reply = self.channel.request("get_frame", body)
        if reply["kind"] == "action_refused":
            raise ControllerError(
                f"target refused frame: {reply['body'].get('reason')}")
        if reply["kind"] != "frame_ok":
            raise ControllerError(f"unexpected reply {reply['kind']}")
        return reply["body"]

    def save_frame(self, frame: Dict[str, Any], path: str) -> str:
        """The sanctioned persistence path for stream frames. Refuses
        unless the session scope carries the record_frames grant, so
        frames are never persisted by default. Returns the path."""
        scope = self.store.get_scope(self.session_id)
        if not scope.record_frames:
            raise ControllerError(
                "frame persistence refused: session scope does not"
                " grant record_frames")
        import base64
        with open(path, "wb") as f:
            f.write(base64.b64decode(frame["data_b64"]))
        return path

    def end(self) -> Dict[str, Any]:
        # tell the target first so it drops its indicator; the store
        # transition is the authority either way.
        try:
            if self.channel.session_id:
                self.channel.request("end", {})
        except Exception:
            pass
        try:
            self.channel.close()
        finally:
            return self.store.end_session(self.session_id)
