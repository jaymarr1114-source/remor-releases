"""Target-side agent: the software on the target device that REMOR drives.

The target is the enforcement point. It does NOT trust the controller:
- hello: verifies the session token against the session store
  (session live + consent live). No token, no session.
- every action: re-verifies session state (kill/end/expiry checked per
  action, not cached), consent liveness, scope (type, bounds, app
  allowlist, budget), and replay guards (exact seq match + unseen nonce).
- consent grant and user kill are TARGET-LOCAL entry points requiring the
  USER token. They are never exposed over the channel; the controller has
  no path to them. The channel "kill" is a fail-closed relay convenience.
- while a session is live the target shows a real on-screen indicator
  window; it is destroyed on kill/end.

Substrate: X11/XTEST on the bench (cursor_x11.X11Cursor). An Android
target would subclass RemoteDispatchTarget with an AccessibilityService
substrate -- the enforcement logic above is substrate-independent.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import channel as chan
from . import protocol as proto
from . import tls
from .cursor_x11 import X11Cursor
from .session_model import (CONSENTED, LIVE, RemoteDispatchStore, Scope,
                            SessionError)


INDICATOR_WM_NAME = "REMOR Remote Session LIVE"


class TargetRefusal(RuntimeError):
    pass


class RemoteDispatchTarget:
    def __init__(self, db_path: str, device_id: str, agent_id: str,
                 agent_token: str, host: str = "127.0.0.1", port: int = 0,
                 display: Optional[str] = None,
                 cert_dir: Optional[str] = None):
        self.store = RemoteDispatchStore(db_path)
        self.db_path = db_path
        self.device_id = device_id
        self.agent_id = agent_id
        self.agent_token = agent_token  # presented as identity proof
        self._cursor: Optional[X11Cursor] = None
        self._display = display
        # TLS identity: self-signed cert, generated once per target.
        # The fingerprint is the target's pinned identity; the
        # controller binds it at pairing and refuses any other cert.
        cert_dir = cert_dir or os.path.join(
            os.path.dirname(os.path.abspath(db_path)), "rd1_certs")
        self._cert_path, self._key_path = tls.ensure_cert(cert_dir)
        self._tls_ctx = tls.server_context(self._cert_path, self._key_path)
        self._listener = chan.TargetListener(
            host, port, tls_context=self._tls_ctx)
        self._listener.identity_proof = {"device_id": device_id,
                                         "agent_id": agent_id,
                                         "agent_token": agent_token}
        self._listener.on_hello = self._on_hello
        self._listener.on_message = self._dispatch_message
        # per-session indicator windows: {session_id: (win, display)}
        self._indicators: Dict[str, Any] = {}
        self._lock = threading.Lock()
        # one live connection per session: a second concurrent hello for
        # the same session is refused; the slot is released on disconnect
        # so a legitimate reconnect (fresh hello) still works.
        self._active: Dict[str, int] = {}
        self._listener.on_disconnect = self._on_disconnect

    @property
    def port(self) -> int:
        return self._listener.bound_port

    def cert_fingerprint(self) -> str:
        """The target's pinned TLS identity (SHA-256 of the cert)."""
        return tls.fingerprint(self._cert_path)

    # -- lifecycle ----------------------------------------------------
    def start(self) -> None:
        self.store.announce_endpoint(self.device_id, self._listener.host,
                                     self.port)
        self._thread = threading.Thread(
            target=self._listener.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._listener.stop()
        self._hide_indicator()

    # -- target-local USER entry points (never on the channel) --------
    def user_grant_consent(self, session_id: str, user_token: str,
                           ttl_s: Optional[float] = None) -> Dict[str, Any]:
        """The user's confirmation. In product: the on-target dialog calls
        this. The controller cannot reach it."""
        return self.store.user_grant_consent(session_id, user_token, ttl_s)

    def user_kill(self, session_id: str, user_token: str) -> Dict[str, Any]:
        """The user's kill switch. Works even if the controller is
        malicious or unresponsive: enforcement is in _on_message's
        per-action session check."""
        self.store._check_user(user_token)  # authenticate the user
        out = self.store.kill_session(session_id, by="user:target")
        self._hide_indicator(session_id)
        # Release the target: a killed session no longer holds the
        # cursor, so a new session may connect.
        with self._lock:
            self._active.pop(session_id, None)
        return out

    # -- channel handlers ----------------------------------------------
    def _on_hello(self, msg: Dict[str, Any]) -> str:
        body = msg.get("body", {})
        session_id = msg.get("session_id", "")
        token = body.get("session_token", "")
        try:
            self.store.check_session_token(session_id, token)
        except SessionError as e:
            raise chan.ChannelError(f"hello refused: {e}")
        with self._lock:
            if self._active.get(session_id):
                raise chan.ChannelError(
                    "hello refused: session already has a live connection"
                    " (concurrent duplicate refused)")
            # Target-wide exclusivity: the cursor is a single shared
            # resource, so at most one session may hold a live
            # remote-control connection at a time. Stale entries for
            # sessions that died (killed/ended) without a clean
            # disconnect are reclaimed here.
            for other in list(self._active):
                if other == session_id:
                    continue
                try:
                    st = self.store._get_session(other)["state"]
                except Exception:
                    st = "gone"
                if st in ("live", "consented"):
                    raise chan.ChannelError(
                        "hello refused: target busy with another live"
                        f" session ({other[:13]}...): one live"
                        " remote-control session per target")
                self._active.pop(other, None)
            self._active[session_id] = 1
        try:
            self.store.mark_live(session_id)
            self._show_indicator(session_id)
        except Exception:
            with self._lock:
                self._active.pop(session_id, None)
            raise
        # A reconnecting controller restarts its counter at hello=1; tell
        # it the persisted next action sequence so it can synchronize.
        next_seq = self.store._get_session(session_id)["seq_next"]
        return session_id, {"next_seq": next_seq}

    def _on_disconnect(self, session_id: str) -> None:
        with self._lock:
            self._active.pop(session_id, None)

    def _dispatch_message(self, kind: str,
                          msg: Dict[str, Any]) -> Dict[str, Any]:
        """Refusals are explicit action_refused replies (the controller
        distinguishes refusal from transport error)."""
        try:
            return self._on_message(kind, msg)
        except TargetRefusal as e:
            return {"kind": "action_refused", "body": {"reason": str(e)}}

    def _on_message(self, kind: str,
                    msg: Dict[str, Any]) -> Dict[str, Any]:
        body = msg.get("body", {})
        session_id = msg.get("session_id", "")
        if kind == "kill":
            # Fail-closed relay convenience: killing only ever stops.
            self.store.kill_session(session_id, by="controller-relay")
            self._hide_indicator(session_id)
            return {"kind": "kill_ok", "body": {"state": "killed"}}
        if kind == "end":
            self.store.end_session(session_id)
            self._hide_indicator(session_id)
            return {"kind": "end_ok", "body": {"state": "ended"}}
        if kind not in ("action", "action_batch"):
            raise chan.ChannelError(f"unknown kind {kind!r}")
        actions = body.get("actions", [body.get("action")])
        actions = [a for a in actions if a]
        if not actions:
            raise chan.ChannelError("no actions")
        # --- enforcement: every action, every time ---
        s = self.store._get_session(session_id)
        if s["state"] != LIVE:
            raise TargetRefusal(
                f"session {s['state']}: action refused")
        # consent still live? (checked per action, not cached from hello)
        if not self.store.consent_live(session_id):
            raise TargetRefusal("consent no longer live: action refused")
        # replay guards: exact seq + unseen nonce
        seq, nonce = msg.get("seq"), msg.get("nonce")
        with self._lock:
            if seq != s["seq_next"]:
                raise TargetRefusal(
                    f"seq mismatch: got {seq}, expected {s['seq_next']}"
                    " (replay or reorder refused)")
            if self._nonce_seen(session_id, nonce):
                raise TargetRefusal("duplicate nonce: replay refused")
        # the message is new: consume its sequence number and record the
        # nonce BEFORE application checks, so a scope refusal cannot
        # desync the channel (the next message still lines up).
        self.store.advance_seq(session_id)
        self._record_nonce(session_id, nonce, seq)
        scope = self.store.get_scope(session_id)
        if s["actions_used"] + len(actions) > scope.max_actions:
            raise TargetRefusal("session action budget exhausted")
        # validate every action BEFORE executing any: a batch is
        # all-or-nothing on scope, so a refused 3rd action cannot leave
        # the first two executed but unaccounted.
        for action in actions:
            ok, reason = scope.allows(action)
            if not ok:
                raise TargetRefusal(f"scope refused action: {reason}")

        def _is_live() -> None:
            # raises (not False) so cursor_x11 stays substrate-pure:
            # whatever this raises propagates out of the type loop.
            st = self.store._get_session(session_id)
            if st["state"] != LIVE or not self.store.consent_live(
                    session_id):
                raise TargetRefusal(
                    "session killed/expired during action: interrupted")

        results = []
        for action in actions:
            results.append(self._execute(action, scope, _is_live))
        # budget charged only for actions that actually executed
        self.store.charge_budget(session_id, len(actions))
        return {"kind": "action_ok", "body": {"results": results}}

    def _nonce_seen(self, session_id: str, nonce: str) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM rd_seen_nonces WHERE session_id=? AND"
                " nonce=?", (session_id, nonce)).fetchone()
            return row is not None
        finally:
            conn.close()

    def _record_nonce(self, session_id: str, nonce: str, seq: int) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO rd_seen_nonces (session_id, nonce,"
                " seq, seen_at) VALUES (?,?,?,?)",
                (session_id, nonce, seq, time.time()))
            conn.commit()
        finally:
            conn.close()

    def _execute(self, action: Dict[str, Any],
                 scope: Scope, is_live) -> Dict[str, Any]:
        if self._cursor is None:
            self._cursor = X11Cursor(self._display)
        kind = action["type"]
        if kind == "move":
            return self._cursor.move(action["x"], action["y"])
        if kind == "click":
            return self._cursor.click(action.get("button", 1),
                                      action.get("x"), action.get("y"))
        if kind == "scroll":
            return self._cursor.scroll(action.get("dx", 0),
                                       action.get("dy", 0))
        if kind == "type":
            return self._cursor.type(action["text"], is_live=is_live)
        if kind == "key":
            return self._cursor.key(action["keysym"])
        if kind == "launch_app":
            return self._cursor.launch_app(action["argv"],
                                           scope.apps or [])
        raise TargetRefusal(f"unexecutable action {kind!r}")

    # -- live-session indicator (real X11 window) ------------------------
    def _show_indicator(self, session_id: str) -> None:
        """A mapped, override-redirect red banner named
        INDICATOR_WM_NAME on the target's own display, tracked per
        session. Verified by querying the X server (indicator_live),
        not by a cached handle."""
        self._hide_indicator(session_id)
        try:
            from Xlib import X
            from Xlib.display import Display
            d = Display(self._display or os.environ.get("DISPLAY", ":99"))
            screen = d.screen()
            win = screen.root.create_window(
                screen.width_in_pixels - 360, 10, 340, 44, 0,
                screen.root_depth, X.InputOutput, X.CopyFromParent,
                override_redirect=True,
                background_pixel=screen.black_pixel)
            cmap = screen.default_colormap
            red = cmap.alloc_named_color("red").pixel
            win.change_attributes(background_pixel=red)
            win.set_wm_name(INDICATOR_WM_NAME)
            win.map()
            try:
                gc = win.create_gc(foreground=screen.white_pixel,
                                   background=red)
                font = d.open_font("fixed")
                gc.change(font=font.fid)
                win.draw_text(gc, 12, 28,
                              f"REMOTE SESSION LIVE {session_id[:13]}")
            except Exception:
                pass  # text is decoration; the window is the signal
            d.sync()
            self._indicators[session_id] = (win, d)
        except Exception:
            # indicator failure must never break the session; the session
            # itself is still governed. Record the miss visibly.
            self.store.log_event("indicator_failed", session_id, {})

    def _hide_indicator(self, session_id: Optional[str] = None) -> None:
        targets = ([session_id] if session_id
                   else list(self._indicators.keys()))
        for sid in targets:
            entry = self._indicators.pop(sid, None)
            if not entry:
                continue
            win, d = entry
            try:
                win.destroy()
                d.sync()
                d.close()
            except Exception:
                pass

    def _find_indicator(self):
        """The indicator window as the X server sees it right now."""
        from Xlib.display import Display
        d = Display(self._display or os.environ.get("DISPLAY", ":99"))
        try:
            found = []

            def walk(w):
                for c in w.query_tree().children:
                    try:
                        if c.get_wm_name() == INDICATOR_WM_NAME:
                            found.append(c)
                    except Exception:
                        pass
                    walk(c)
            walk(d.screen().root)
            return found[0] if found else None
        finally:
            d.close()

    def indicator_live(self) -> bool:
        return self._find_indicator() is not None
