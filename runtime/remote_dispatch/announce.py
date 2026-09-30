"""Target->controller endpoint announcement: the wire client and the
shared field validation.

The announcement is how a paired target tells the controller WHERE it
is reachable. It is authenticated by the target's pairing agent token
(a bearer credential delivered out of band at pairing) and
replay-guarded by a (timestamp, nonce) pair the controller's store
checks strictly-increasing. The announcement carries the target's
certificate fingerprint so the controller's TLS pinning has something
to check the handshake against.

Validation here is fail-closed and mirrors the pairing-input rule from
GUI-RD-PAIRFIX-1 (device_id charset [A-Za-z0-9_:.-], length cap): the
service handler uses exactly this validator, so the wire format and
the acceptance rule cannot drift apart.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import re
import secrets
import time
import urllib.parse
from typing import Any, Dict, Optional, Tuple

# GUI-RD-PAIRFIX-1 rule, enforced server-side too: pairing input is
# strictly validated and this mission must not regress it.
_DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_:.-]{1,128}\Z")
_HOSTNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,253}\Z")
_HEX_RE = re.compile(r"[0-9a-fA-F]+\Z")


class AnnounceError(RuntimeError):
    """The announcement could not be delivered or was refused."""


def validate_device_id(device_id: Any) -> str:
    """Return the stripped device_id or raise AnnounceError with the
    exact reason. The charset/length rule is the GUI-RD-PAIRFIX-1 rule;
    enforcing it here keeps the announcement from becoming a bypass."""
    did = (device_id or "")
    if not isinstance(did, str):
        raise AnnounceError("invalid device_id: not a string")
    did = did.strip()
    if not _DEVICE_ID_RE.fullmatch(did):
        raise AnnounceError(
            "invalid device_id: must match [A-Za-z0-9_:.-]{1,128}")
    return did


def validate_host(host: Any) -> str:
    h = (host or "")
    if not isinstance(h, str):
        raise AnnounceError("invalid host: not a string")
    h = h.strip()
    if not h:
        raise AnnounceError("invalid host: empty")
    try:
        ipaddress.ip_address(h)
        return h
    except ValueError:
        pass
    if not _HOSTNAME_RE.fullmatch(h):
        raise AnnounceError(
            f"invalid host {h!r}: not an IP address or hostname")
    return h


def validate_port(port: Any) -> int:
    try:
        p = int(str(port).strip())
    except (ValueError, AttributeError, TypeError):
        raise AnnounceError(f"invalid port {port!r}: not an integer")
    if not 1 <= p <= 65535:
        raise AnnounceError(f"invalid port {p}: out of range 1..65535")
    return p


def validate_fingerprint(fp: Any) -> Optional[str]:
    if fp is None or (isinstance(fp, str) and not fp.strip()):
        return None
    if not isinstance(fp, str) or not _HEX_RE.fullmatch(fp.strip()) \
            or len(fp.strip()) != 64:
        raise AnnounceError(
            "invalid cert_fingerprint: must be 64 hex chars")
    return fp.strip().lower()


def validate_ann_ts(ann_ts: Any) -> float:
    if isinstance(ann_ts, bool) or not isinstance(ann_ts, (int, float)):
        raise AnnounceError("invalid ann_ts: must be a unix timestamp")
    if not ann_ts > 0:
        raise AnnounceError("invalid ann_ts: must be positive")
    return float(ann_ts)


def validate_ann_nonce(ann_nonce: Any) -> str:
    n = (ann_nonce or "")
    if not isinstance(n, str) or not n.strip():
        raise AnnounceError("invalid ann_nonce: empty")
    n = n.strip()
    if len(n) > 128 or not _HEX_RE.fullmatch(n):
        raise AnnounceError("invalid ann_nonce: must be 1..128 hex chars")
    return n.lower()


def validate_announcement(body: Dict[str, Any]
                          ) -> Tuple[str, str, str, int, Optional[str],
                                     float, str]:
    """Validate a raw announcement body. Returns
    (device_id, agent_token, host, port, cert_fingerprint, ann_ts,
    ann_nonce); raises AnnounceError with the exact reason on any
    invalid field. The service handler calls this first, before any
    authentication, so malformed input is refused without touching
    identity machinery."""
    if not isinstance(body, dict):
        raise AnnounceError("invalid announcement: body must be an object")
    token = body.get("agent_token")
    if not isinstance(token, str) or not token:
        raise AnnounceError("invalid agent_token: required")
    return (validate_device_id(body.get("device_id")),
            token,
            validate_host(body.get("host")),
            validate_port(body.get("port")),
            validate_fingerprint(body.get("cert_fingerprint")),
            validate_ann_ts(body.get("ann_ts")),
            validate_ann_nonce(body.get("ann_nonce")))


def build_announcement(device_id: str, agent_token: str, host: str,
                       port: int, cert_fingerprint: str) -> Dict[str, Any]:
    """Build a well-formed announcement body for the target to send."""
    return {"device_id": device_id,
            "agent_token": agent_token,
            "host": host,
            "port": port,
            "cert_fingerprint": cert_fingerprint,
            "ann_ts": time.time(),
            "ann_nonce": secrets.token_hex(16)}


def post_endpoint_announcement(api_base_url: str, body: Dict[str, Any],
                               timeout: float = 10.0) -> Dict[str, Any]:
    """POST an announcement to the controller's API over a real HTTP
    connection. Returns the parsed response body. Raises AnnounceError
    if the transport fails, the response is not JSON, or the
    controller refused (the refusal reason is in the message)."""
    url = api_base_url.rstrip("/") + "/api/remote/announce"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        conn_cls = http.client.HTTPSConnection
    elif parts.scheme == "http":
        conn_cls = http.client.HTTPConnection
    else:
        raise AnnounceError(f"unsupported announce URL scheme {url!r}")
    payload = json.dumps(body).encode("utf-8")
    conn = conn_cls(parts.hostname, parts.port or
                    (443 if parts.scheme == "https" else 80),
                    timeout=timeout)
    try:
        conn.request("POST", parts.path or "/api/remote/announce",
                     body=payload,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(payload))})
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException) as e:
        raise AnnounceError(f"announcement transport failed: {e}")
    finally:
        conn.close()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AnnounceError(
            f"announcement got non-JSON reply (http {resp.status})")
    if resp.status != 200 or not data.get("ok"):
        raise AnnounceError(
            "announcement refused: "
            f"{data.get('error', f'http {resp.status}')}")
    return data
