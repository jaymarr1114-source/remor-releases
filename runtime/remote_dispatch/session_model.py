"""Remote-dispatch session model: devices, sessions, consent, scope.

Trust posture (first-class, not bolt-on):
- A target device pairs once, binding a device_id to a caller-authorization
  agent identity (governance/caller_authorization.py). The controller
  authenticates the target's agent token at channel establishment; a spoofed
  target cannot present it.
- Consent is granted ONLY by the user principal, on the target side, via
  `grant_consent` which requires a user token the controller never holds.
  In product this is the on-target confirmation dialog; on the bench the
  proof harness plays the user explicitly through the same entry point.
  The controller code path can request a session but can never grant
  consent -- there is no controller-reachable consent-grant function.
- Scope is enforced by the TARGET before every action (action type, pointer
  bounds, app allowlist, action budget, expiry). A malicious controller
  sending out-of-scope actions gets refusals, not execution.
- Kill is enforced by the target: the session state is checked before every
  action; KILLED/ENDED sessions refuse everything. The kill entry point is
  target-side (user) with a controller-relayed convenience path.
- Lifecycle events are tamper-evident: chained digests (prev_digest /
  row_digest) in the same pattern as governance/oracle_binding.py.

Honest limits (stated, not hidden):
- On the bench the controller and target share one SQLite file (same trust
  domain as the bench). The kill/consent guarantees hold against a
  controller operating THROUGH THE PROTOCOL (buggy or malicious messages);
  a controller with raw DB write access could rewrite rows -- same honest
  limit as caller_authorization's in-process callers sharing memory. In
  product the session store lives on the target and the controller has no
  write access, so the guarantee is strictly stronger there.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import functools
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rd_devices (
    device_id    TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    paired_at    REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    endpoint_host TEXT,
    endpoint_port INTEGER,
    cert_fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS rd_user_tokens (
    user_id      TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rd_sessions (
    session_id       TEXT PRIMARY KEY,
    device_id        TEXT NOT NULL REFERENCES rd_devices(device_id),
    state            TEXT NOT NULL,
    scope_json       TEXT NOT NULL,
    session_token_hash TEXT,
    -- seq 1 is reserved for the hello handshake frame; the first action
    -- frame must carry seq 2. Replay guard: received action seq must
    -- equal seq_next exactly.
    seq_next         INTEGER NOT NULL DEFAULT 2,
    actions_used     INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    consented_at     REAL,
    ended_at         REAL
);
CREATE TABLE IF NOT EXISTS rd_consents (
    consent_id  TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES rd_sessions(session_id),
    granted_by  TEXT NOT NULL,
    granted_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    revoked_at  REAL
);
CREATE TABLE IF NOT EXISTS rd_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,
    session_id  TEXT,
    detail_json TEXT NOT NULL,
    prev_digest TEXT NOT NULL,
    row_digest  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rd_seen_nonces (
    session_id TEXT NOT NULL,
    nonce      TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    seen_at    REAL NOT NULL,
    PRIMARY KEY (session_id, nonce)
);
"""

# States
PENDING = "pending"
CONSENTED = "consented"
LIVE = "live"
KILLED = "killed"
ENDED = "ended"
EXPIRED = "expired"
_TERMINAL = {KILLED, ENDED, EXPIRED}

_ACTIONS = {"move", "click", "scroll", "type", "key", "launch_app"}


class SessionError(RuntimeError):
    """Refusal with an explicit reason -- never a silent downgrade."""


def _digest(payload: str) -> str:
    return hmac.new(b"remor-remote-dispatch-chain", payload.encode(),
                    hashlib.sha256).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str)


def _locked(fn):
    """The target listener serves each connection on its own thread, so
    every store operation must be thread-safe. One RLock guards the
    single connection (check_same_thread=False); reentrancy allows
    log_event() inside other locked methods."""
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self._lock:
            return fn(self, *a, **kw)
    return wrapper


@dataclass
class Scope:
    actions: List[str]
    x_min: int = 0
    y_min: int = 0
    x_max: int = 10_000
    y_max: int = 10_000
    apps: Optional[List[str]] = None
    max_actions: int = 200
    ttl_s: float = 600.0
    # Screen-streaming grants. screen_share gates the get_frame stream:
    # the target refuses frames unless the consented scope carries it.
    # record_frames gates persistence of frames on the controller side:
    # the sanctioned save path refuses without it, so frames are never
    # persisted by default. Both are surfaced in the consent UI.
    screen_share: bool = False
    record_frames: bool = False

    def allows(self, action: Dict[str, Any]) -> Tuple[bool, str]:
        kind = action.get("type")
        if kind not in _ACTIONS:
            return False, f"unknown action type {kind!r}"
        if kind not in self.actions:
            return False, f"action {kind!r} not in session scope"
        if kind in ("move", "click"):
            x, y = action.get("x"), action.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return False, "move/click require numeric x,y"
            if not (self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max):
                return False, (f"coordinates ({x},{y}) outside session "
                                f"bounds [{self.x_min},{self.x_max}]x"
                                f"[{self.y_min},{self.y_max}]")
        if kind == "type":
            text = action.get("text", "")
            if not isinstance(text, str) or len(text) > 2000:
                return False, "type: text must be str <= 2000 chars"
        if kind == "launch_app":
            app = action.get("app")
            if self.apps is None or app not in self.apps:
                return False, f"app {app!r} not in session app allowlist"
        return True, "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {"actions": self.actions, "x_min": self.x_min,
                "y_min": self.y_min, "x_max": self.x_max, "y_max": self.y_max,
                "apps": self.apps, "max_actions": self.max_actions,
                "ttl_s": self.ttl_s, "screen_share": self.screen_share,
                "record_frames": self.record_frames}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Scope":
        s = Scope(actions=list(d.get("actions", [])))
        for k in ("x_min", "y_min", "x_max", "y_max", "max_actions"):
            if k in d:
                setattr(s, k, d[k])
        if "ttl_s" in d:
            s.ttl_s = d["ttl_s"]
        s.apps = d.get("apps")
        s.screen_share = bool(d.get("screen_share", False))
        s.record_frames = bool(d.get("record_frames", False))
        return s


class RemoteDispatchStore:
    """SQLite-backed session/consent/device store with chained event log."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        # check_same_thread=False: the target listener handles each
        # connection on its own thread. All access is serialized by
        # self._lock via the @_locked decorator.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # migration: cert_fingerprint column for TLS pinning
        # (REMOTE-DISPATCH-1); ALTER is a no-op-safe guarded by PRAGMA.
        cols = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(rd_devices)")]
        if "cert_fingerprint" not in cols:
            self._conn.execute(
                "ALTER TABLE rd_devices ADD COLUMN cert_fingerprint TEXT")
        self._conn.commit()

    # -- chained event log -------------------------------------------
    @_locked
    def _head_digest(self) -> str:
        cur = self._conn.execute(
            "SELECT row_digest FROM rd_events ORDER BY seq DESC LIMIT 1")
        row = cur.fetchone()
        return row["row_digest"] if row else "GENESIS:rd_events"

    @_locked
    def log_event(self, kind: str, session_id: Optional[str],
                  detail: Dict[str, Any]) -> None:
        prev = self._head_digest()
        body = _canonical({"kind": kind, "session_id": session_id,
                           "detail": detail})
        digest = _digest(prev + "\x00" + body)
        self._conn.execute(
            "INSERT INTO rd_events (ts, kind, session_id, detail_json,"
            " prev_digest, row_digest) VALUES (?,?,?,?,?,?)",
            (time.time(), kind, session_id, json.dumps(detail), prev,
             digest))
        self._conn.commit()

    @_locked
    def audit_chain(self) -> Tuple[bool, str]:
        prev = "GENESIS:rd_events"
        for row in self._conn.execute(
                "SELECT * FROM rd_events ORDER BY seq ASC"):
            d = dict(row)
            if d["prev_digest"] != prev:
                return False, f"chain break at seq={d['seq']}"
            body = _canonical({"kind": d["kind"],
                               "session_id": d["session_id"],
                               "detail": json.loads(d["detail_json"])})
            if not hmac.compare_digest(
                    _digest(prev + "\x00" + body), d["row_digest"]):
                return False, f"row_digest mismatch at seq={d['seq']}"
            prev = d["row_digest"]
        return True, "rd_events chain intact"

    # -- devices ------------------------------------------------------
    @_locked
    def pair_device(self, device_id: str, agent_id: str,
                    display_name: str,
                    cert_fingerprint: Optional[str] = None) -> Dict[str, Any]:
        if self._conn.execute(
                "SELECT 1 FROM rd_devices WHERE device_id=?",
                (device_id,)).fetchone():
            raise SessionError(f"device {device_id!r} already paired")
        self._conn.execute(
            "INSERT INTO rd_devices (device_id, agent_id, display_name,"
            " paired_at, status, cert_fingerprint) VALUES (?,?,?,?,"
            " 'active', ?)",
            (device_id, agent_id, display_name, time.time(),
             cert_fingerprint))
        self._conn.commit()
        self.log_event("device_paired", None,
                       {"device_id": device_id, "agent_id": agent_id})
        return {"device_id": device_id, "agent_id": agent_id,
                "display_name": display_name}

    @_locked
    def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM rd_devices WHERE device_id=?",
            (device_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def announce_endpoint(self, device_id: str, host: str,
                          port: int) -> None:
        """Called by the target agent at startup: where the controller
        can reach it. In product the same announcement rides the pairing
        channel; the controller never guesses."""
        self._conn.execute(
            "UPDATE rd_devices SET endpoint_host=?, endpoint_port=?"
            " WHERE device_id=?", (host, port, device_id))
        self._conn.commit()
        self.log_event("endpoint_announced", None,
                       {"device_id": device_id, "port": port})

    # -- user principals (consent authority) --------------------------
    @_locked
    def register_user(self, user_id: str) -> str:
        """Create a user principal; returns the token (shown once)."""
        token = "rduser_" + secrets.token_hex(24)
        self._conn.execute(
            "INSERT OR REPLACE INTO rd_user_tokens (user_id, token_hash,"
            " created_at) VALUES (?,?,?)",
            (user_id, hashlib.sha256(token.encode()).hexdigest(),
             time.time()))
        self._conn.commit()
        return token

    @_locked
    def _check_user(self, user_token: str) -> str:
        want = hashlib.sha256(user_token.encode()).hexdigest()
        for row in self._conn.execute("SELECT user_id, token_hash FROM"
                                      " rd_user_tokens"):
            if hmac.compare_digest(row["token_hash"], want):
                return row["user_id"]
        raise SessionError("consent refused: invalid user token")

    # -- sessions ------------------------------------------------------
    @_locked
    def request_session(self, device_id: str,
                        scope: Scope) -> Dict[str, Any]:
        dev = self.get_device(device_id)
        if not dev or dev["status"] != "active":
            raise SessionError(f"device {device_id!r} not paired/active")
        sid = "sess_" + secrets.token_hex(12)
        now = time.time()
        self._conn.execute(
            "INSERT INTO rd_sessions (session_id, device_id, state,"
            " scope_json, created_at) VALUES (?,?,?,?,?)",
            (sid, device_id, PENDING, json.dumps(scope.to_dict()), now))
        self._conn.commit()
        self.log_event("session_requested", sid,
                       {"device_id": device_id,
                        "scope": scope.to_dict()})
        return {"session_id": sid, "state": PENDING}

    @_locked
    def user_grant_consent(self, session_id: str, user_token: str,
                           ttl_s: Optional[float] = None) -> Dict[str, Any]:
        """TARGET-SIDE ONLY. The controller has no user token and no path
        to this function over the channel -- consent is granted by the
        user's confirmation on the target, never by the controller."""
        user_id = self._check_user(user_token)
        s = self._get_session(session_id)
        if s["state"] != PENDING:
            raise SessionError(
                f"consent refused: session is {s['state']}, not pending")
        scope = Scope.from_dict(json.loads(s["scope_json"]))
        ttl = ttl_s if ttl_s is not None else scope.ttl_s
        now = time.time()
        token = "rdsess_" + secrets.token_hex(32)  # shown once
        cid = "cons_" + secrets.token_hex(12)
        self._conn.execute(
            "INSERT INTO rd_consents (consent_id, session_id, granted_by,"
            " granted_at, expires_at) VALUES (?,?,?,?,?)",
            (cid, session_id, f"user:{user_id}", now, now + ttl))
        self._conn.execute(
            "UPDATE rd_sessions SET state=?, session_token_hash=?,"
            " consented_at=? WHERE session_id=?",
            (CONSENTED, hashlib.sha256(token.encode()).hexdigest(), now,
             session_id))
        self._conn.commit()
        self.log_event("consent_granted", session_id,
                       {"granted_by": f"user:{user_id}",
                        "expires_in_s": ttl})
        return {"session_id": session_id, "state": CONSENTED,
                "session_token": token}

    @_locked
    def _get_session(self, session_id: str) -> Dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM rd_sessions WHERE session_id=?",
            (session_id,)).fetchone()
        if not row:
            raise SessionError(f"unknown session {session_id!r}")
        return dict(row)

    @_locked
    def check_session_token(self, session_id: str,
                            token: str) -> Dict[str, Any]:
        s = self._get_session(session_id)
        if s["state"] in _TERMINAL:
            raise SessionError(f"session {s['state']}: refused")
        if not s["session_token_hash"] or not hmac.compare_digest(
                s["session_token_hash"],
                hashlib.sha256(token.encode()).hexdigest()):
            raise SessionError("invalid session token")
        # consent must be live
        c = self._conn.execute(
            "SELECT * FROM rd_consents WHERE session_id=? ORDER BY"
            " granted_at DESC LIMIT 1", (session_id,)).fetchone()
        now = time.time()
        if not c or c["revoked_at"] is not None or c["expires_at"] <= now:
            raise SessionError("consent not live: refused")
        return s

    @_locked
    def mark_live(self, session_id: str) -> None:
        s = self._get_session(session_id)
        if s["state"] == CONSENTED:
            self._conn.execute(
                "UPDATE rd_sessions SET state=? WHERE session_id=?",
                (LIVE, session_id))
            self._conn.commit()
            self.log_event("session_live", session_id, {})

    @_locked
    def kill_session(self, session_id: str, by: str) -> Dict[str, Any]:
        s = self._get_session(session_id)
        if s["state"] in _TERMINAL:
            return {"session_id": session_id, "state": s["state"]}
        self._conn.execute(
            "UPDATE rd_consents SET revoked_at=? WHERE session_id=? AND"
            " revoked_at IS NULL", (time.time(), session_id))
        self._conn.execute(
            "UPDATE rd_sessions SET state=?, ended_at=? WHERE session_id=?",
            (KILLED, time.time(), session_id))
        self._conn.commit()
        self.log_event("session_killed", session_id, {"by": by})
        return {"session_id": session_id, "state": KILLED}

    @_locked
    def end_session(self, session_id: str) -> Dict[str, Any]:
        s = self._get_session(session_id)
        if s["state"] not in _TERMINAL:
            self._conn.execute(
                "UPDATE rd_sessions SET state=?, ended_at=? WHERE"
                " session_id=?", (ENDED, time.time(), session_id))
            self._conn.commit()
            self.log_event("session_ended", session_id, {})
        return {"session_id": session_id, "state": ENDED}

    @_locked
    def advance_seq(self, session_id: str) -> None:
        """Consume one message sequence number. Called once per message
        that passes the replay guards -- even if the action inside is
        later refused on scope: the seq guards the MESSAGE (replay/
        reorder), so a refusal must not desync the channel."""
        self._conn.execute(
            "UPDATE rd_sessions SET seq_next = seq_next + 1"
            " WHERE session_id=?", (session_id,))
        self._conn.commit()

    @_locked
    def charge_budget(self, session_id: str, n_actions: int) -> None:
        """Charge the action budget. Called only after actions actually
        executed -- a refused action costs no budget."""
        self._conn.execute(
            "UPDATE rd_sessions SET actions_used = actions_used + ?"
            " WHERE session_id=?", (n_actions, session_id))
        self._conn.commit()

    @_locked
    def get_scope(self, session_id: str) -> Scope:
        s = self._get_session(session_id)
        return Scope.from_dict(json.loads(s["scope_json"]))

    @_locked
    def consent_live(self, session_id: str) -> bool:
        c = self._conn.execute(
            "SELECT * FROM rd_consents WHERE session_id=? ORDER BY"
            " granted_at DESC LIMIT 1", (session_id,)).fetchone()
        now = time.time()
        return bool(c and c["revoked_at"] is None
                    and c["expires_at"] > now)

    @_locked
    def list_sessions(self) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT session_id, device_id, state, created_at,"
            " consented_at, ended_at FROM rd_sessions ORDER BY"
            " created_at DESC").fetchall()
        return [dict(r) for r in rows]

    @_locked
    def close(self) -> None:
        self._conn.close()
