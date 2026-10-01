"""Tap-to-pair client (RD-EASYPAIR-1): phone side of the pairing ceremony.

The phone opens the tablet's TLS channel (certificate pinned to the
fingerprint from the discovery beacon -- TOFU), sends pair_request, shows
the tablet-issued numeric code for the human comparison, and confirms.

Pairing frames (pre-hello on the target TLS port):
  C->T pair_request {phone_name, phone_id, device_id, agent_id, agent_token,
                     phone_api_url}
  T->C pair_pending {pairing_id, code} | pair_refused {reason}
  C->T pair_confirm {pairing_id}
  T->C pair_ok {agent_token} | pair_refused {reason}
  C->T pair_abort {pairing_id} -> T->C pair_aborted {}

Security properties (all enforced, not asserted):
- No pin, no connection: wrap_client fails closed without a fingerprint.
- A spoofed beacon's fingerprint fails the TLS pin -> TLSError, no frames.
- pair_ok is only meaningful after the human confirmed the code matches
  the tablet's screen AND the tablet owner allowed the request there.
  The client never auto-confirms.
- Every refusal carries its exact reason; transport errors are distinct
  from refusals.
"""
from __future__ import annotations

import socket
from typing import Any, Dict, Optional

from . import protocol
from . import tls


class PairingError(Exception):
    """Transport-level pairing failure (not a tablet refusal)."""


class PairingRefused(Exception):
    """The tablet refused the pairing; carries the tablet's reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class TapPairingClient:
    """One pairing attempt against one discovered tablet."""

    def __init__(self, host: str, port: int, cert_fingerprint: str,
                 timeout: float = 15.0):
        if not cert_fingerprint:
            raise PairingError("no pinned certificate fingerprint: refusing"
                               " to pair without target identity")
        self._host = host
        self._port = port
        self._fingerprint = cert_fingerprint
        self._timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._tls = None

    def connect(self) -> None:
        """TCP + TLS handshake with pin enforcement. Raises on failure."""
        try:
            raw = socket.create_connection((self._host, self._port),
                                           timeout=self._timeout)
        except OSError as e:
            raise PairingError(f"pairing transport failed: {e}")
        try:
            self._sock = raw
            self._tls = tls.wrap_client(raw, self._fingerprint)
            self._tls.settimeout(self._timeout)
        except tls.TLSError as e:
            try:
                raw.close()
            except OSError:
                pass
            self._sock = None
            raise PairingError(str(e))

    def close(self) -> None:
        if self._tls is not None:
            try:
                self._tls.close()
            except OSError:
                pass
            self._tls = None
        self._sock = None

    def _rpc(self, kind: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if self._tls is None:
            raise PairingError("not connected")
        try:
            protocol.send(self._tls, kind, "", 0, body)
            return protocol.decode(self._tls)
        except (OSError, ValueError, ConnectionError) as e:
            raise PairingError(f"pairing transport failed: {e}")

    @staticmethod
    def _check_refused(msg: Dict[str, Any]) -> Dict[str, Any]:
        if msg.get("kind") == "pair_refused":
            raise PairingRefused(
                str((msg.get("body") or {}).get("reason", "refused")))
        return msg

    def request(self, phone_name: str, phone_id: str, device_id: str,
                agent_id: str, agent_token: str,
                phone_api_url: str) -> Dict[str, str]:
        """Send pair_request. Returns {pairing_id, code} or raises."""
        msg = self._rpc("pair_request", {
            "phone_name": phone_name, "phone_id": phone_id,
            "device_id": device_id, "agent_id": agent_id,
            "agent_token": agent_token, "phone_api_url": phone_api_url,
        })
        self._check_refused(msg)
        if msg.get("kind") != "pair_pending":
            raise PairingError(
                f"unexpected pairing reply: {msg.get('kind')}")
        body = msg.get("body") or {}
        pairing_id = body.get("pairing_id")
        code = body.get("code")
        if not pairing_id or not code:
            raise PairingError("pair_pending missing pairing_id/code")
        return {"pairing_id": str(pairing_id), "code": str(code)}

    def confirm(self, pairing_id: str) -> Dict[str, str]:
        """Send pair_confirm (only after the human matched the code).

        Returns {agent_token} on pair_ok; raises PairingRefused otherwise.
        """
        msg = self._rpc("pair_confirm", {"pairing_id": pairing_id})
        self._check_refused(msg)
        if msg.get("kind") != "pair_ok":
            raise PairingError(
                f"unexpected pairing reply: {msg.get('kind')}")
        token = (msg.get("body") or {}).get("agent_token")
        if not token:
            raise PairingError("pair_ok missing agent_token")
        return {"agent_token": str(token)}

    def abort(self, pairing_id: str) -> None:
        """Best-effort abort; never raises."""
        try:
            self._rpc("pair_abort", {"pairing_id": pairing_id})
        except Exception:
            pass
