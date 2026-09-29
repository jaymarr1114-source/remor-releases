"""Remote-dispatch wire protocol: framed JSON messages over TCP.

Frame: 4-byte big-endian length + UTF-8 JSON. Every message:
  {"v": 1, "kind": <kind>, "session_id": ..., "seq": n, "nonce": hex, "body": {...}}

Message kinds:
  controller -> target: "hello" (handshake), "action" (cursor action),
                         "action_batch", "kill" (relay), "bye",
                         "get_frame" (request one stream frame)
  target -> controller: "hello_ok", "action_ok", "action_refused", "error",
                        "session_event", "frame_ok" (one captured frame)

A frame body is {"width","height","format":"png","data_b64","ts",
"synthesized"}: base64 PNG bytes captured on the target inside the
live, consented session, clipped to the session scope bounds at
capture time. get_frame carries the same per-message enforcement as
actions (session live, consent live, exact seq + unseen nonce); a
killed/ended/expired session or a scope without the screen_share
grant gets action_refused, never a frame.

Replay protection: the target keeps the highest seen seq per session and a
set of seen nonces; any duplicate nonce or seq <= high-water is refused.
The controller signs nothing -- authentication is the session token in the
hello handshake; afterwards the TCP connection is bound to the session.
A NEW connection must re-hello with the session token (no silent resume).
"""
from __future__ import annotations

import json
import secrets
import struct
from typing import Any, Dict, Tuple

VERSION = 1
_HEADER = struct.Struct(">I")
_MAX_FRAME = 4 * 1024 * 1024


def encode(kind: str, session_id: str, seq: int,
           body: Dict[str, Any]) -> bytes:
    msg = {"v": VERSION, "kind": kind, "session_id": session_id,
           "seq": seq, "nonce": secrets.token_hex(16), "body": body}
    raw = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_FRAME:
        raise ValueError("frame too large")
    return _HEADER.pack(len(raw)) + raw


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed mid-frame")
        buf += chunk
    return buf


def decode(sock) -> Dict[str, Any]:
    n = struct.unpack(">I", _recv_exact(sock, 4))[0]
    if n > _MAX_FRAME or n == 0:
        raise ValueError(f"bad frame length {n}")
    raw = _recv_exact(sock, n)
    msg = json.loads(raw.decode("utf-8"))
    if msg.get("v") != VERSION:
        raise ValueError(f"unsupported protocol version {msg.get('v')}")
    return msg


def send(sock, kind: str, session_id: str, seq: int,
         body: Dict[str, Any]) -> None:
    sock.sendall(encode(kind, session_id, seq, body))
