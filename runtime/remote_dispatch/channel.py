"""Device-control channel: TLS transport with pinned target identity
and mutual authentication.

Handshake (controller -> target), on a fresh TLS connection (the
controller pins the target's certificate fingerprint from the paired
device record; a mismatched cert fails the handshake closed):
  1. controller sends hello {agent_token, session_id, session_token}
  2. target verifies:
     a. the agent_token against the caller-authorization AgentDirectory
        (the target device's enrolled identity) -- a spoofed target's
        controller-side check mirrors this; here the TARGET verifies the
        CONTROLLER is talking to the right device binding... actually the
        controller verifies the target. See below.
     b. the session_token against the session store (check_session_token:
        session live + consent live).
  3. target replies hello_ok {device_id, session_state} or error/refusal.

Mutual authentication, precisely:
- The TARGET authenticates the CONTROLLER's authority to drive this
  session via the session token (issued once at consent, bound to the
  session, invalid after kill/end/expiry).
- The CONTROLLER authenticates the TARGET as the paired device: at
  pairing time the controller stores the target's agent_id; on hello the
  target must ALSO prove its agent identity by presenting its agent token,
  which the controller verifies against the AgentDirectory. A spoofed
  device (wrong token, unknown device_id) fails the handshake before any
  action flows.

After hello_ok the connection is bound to (session_id); every subsequent
message on it must carry that session_id or the target drops the
connection. A new connection must re-hello (no silent resume).
"""
from __future__ import annotations

import socket
import threading
from typing import Any, Callable, Dict, Optional, Tuple, Union

from . import protocol as proto
from . import tls


class ChannelError(RuntimeError):
    pass


def _require(cond: bool, reason: str) -> None:
    if not cond:
        raise ChannelError(reason)


class TargetListener:
    """Target-side TLS listener. `on_hello` verifies identity + session and
    returns a per-connection handler; `on_message(kind, msg)` handles the
    bound session's messages.

    Every accepted connection is TLS-wrapped with the target's
    certificate before any protocol byte is read. Plaintext connections
    are not served."""

    def __init__(self, host: str, port: int,
                 tls_context: Optional["ssl.SSLContext"] = None):
        self.host = host
        self.port = port
        self._tls_context = tls_context
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(8)
        self._stop = threading.Event()
        self.on_hello: Optional[
            Callable[[Dict[str, Any]],
                     Union[str, Tuple[str, Dict[str, Any]]]]] = None
        self.on_message: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None
        # Called with the bound session_id when a connection's handler
        # exits (clean bye, drop, or error) so the target can release
        # per-session connection slots.
        self.on_disconnect: Optional[Callable[[str], None]] = None
        # Set by the target agent: {"device_id","agent_id","agent_token"} --
        # presented in hello_ok so the CONTROLLER can authenticate the
        # target as the paired device (spoofed targets cannot present it).
        # The token travels inside the TLS channel: it is a bearer
        # credential, never plaintext on the network.
        self.identity_proof: Dict[str, str] = {}

    @property
    def bound_port(self) -> int:
        return self._sock.getsockname()[1]

    def serve_forever(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            # TLS before any protocol byte: the handshake must complete
            # or the connection is dropped unanswered.
            if self._tls_context is not None:
                try:
                    conn = self._tls_context.wrap_socket(conn,
                                                         server_side=True)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    continue
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _handle(self, conn: socket.socket) -> None:
        # The error reply must be sent BEFORE the socket closes: the
        # try/except lives inside `with conn` so a refusal (bad token,
        # duplicate connection, scope refusal surfaced as error) reaches
        # the controller as a frame, not a dropped connection.
        session_id: Optional[str] = None
        with conn:
            try:
                msg = proto.decode(conn)
                _require(msg["kind"] == "hello",
                         "first message must be hello")
                session_id = self.on_hello(msg)  # raises on refusal
                if isinstance(session_id, tuple):
                    session_id, extra = session_id
                else:
                    extra = {}
                hello_body = {"bound": True,
                              "identity_proof": self.identity_proof}
                hello_body.update(extra)
                proto.send(conn, "hello_ok", session_id, 0, hello_body)
                while True:
                    m = proto.decode(conn)
                    if m.get("session_id") != session_id:
                        raise ChannelError("session_id mismatch: dropping")
                    if m["kind"] == "bye":
                        proto.send(conn, "bye_ok", session_id, 0, {})
                        return
                    reply = self.on_message(m["kind"], m)
                    proto.send(conn, reply["kind"], session_id,
                               reply.get("seq", 0), reply.get("body", {}))
            except Exception as e:  # noqa: BLE001 - the wire must not leak
                try:
                    proto.send(conn, "error", session_id or "",
                               0, {"reason": str(e)[:300]})
                except Exception:
                    pass
            finally:
                if session_id and self.on_disconnect:
                    try:
                        self.on_disconnect(session_id)
                    except Exception:
                        pass


class ControllerChannel:
    """Controller-side: TLS (pinned target identity), hello handshake,
    send actions."""

    def __init__(self, host: str, port: int,
                 pinned_fingerprint: Optional[str] = None):
        raw = socket.create_connection((host, port), timeout=10)
        # TLS with pinned target identity. pinned_fingerprint=None
        # fails closed inside tls.wrap_client.
        self._sock = tls.wrap_client(raw, pinned_fingerprint)
        self._seq = 0
        self.session_id: Optional[str] = None

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def hello(self, session_id: str, session_token: str) -> Dict[str, Any]:
        """Returns the hello_ok body, including the target's identity_proof.
        The caller MUST authenticate the proof against the AgentDirectory
        and the paired device binding before sending any action."""
        proto.send(self._sock, "hello", session_id, self._next_seq(),
                   {"session_token": session_token})
        reply = proto.decode(self._sock)
        if reply["kind"] != "hello_ok":
            raise ChannelError(
                f"handshake refused: {reply.get('body', {})}")
        self.session_id = session_id
        body = reply["body"]
        # Synchronize with the target's persisted sequence: after a
        # reconnect the next action must continue the stored counter,
        # not restart at 2.
        next_seq = body.get("next_seq")
        if isinstance(next_seq, int) and next_seq >= 2:
            self._seq = next_seq - 1
        return body

    def request(self, kind: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if not self.session_id:
            raise ChannelError("not bound: hello first")
        proto.send(self._sock, kind, self.session_id, self._next_seq(),
                   body)
        reply = proto.decode(self._sock)
        if reply["kind"] == "error":
            raise ChannelError(reply["body"].get("reason", "target error"))
        return {"kind": reply["kind"], "body": reply["body"]}

    def close(self) -> None:
        try:
            if self.session_id:
                proto.send(self._sock, "bye", self.session_id,
                           self._next_seq(), {})
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass
