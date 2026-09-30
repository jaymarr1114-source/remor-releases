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

Substrate: selected by capability profile via substrates.resolve_substrate
(X11/XTEST on the bench; AccessibilityService bridge on Android).
An Android target passes substrate=ANDROID_PROFILE -- the enforcement
logic above is substrate-independent and is never rewritten per
substrate: substrates compose under RemoteDispatchTarget, they do not
subclass or fork it.
"""
from __future__ import annotations

import os
import signal
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import channel as chan
from . import protocol as proto
from . import tls
from .session_model import (CONSENTED, LIVE, RemoteDispatchStore, Scope,
                            SessionError)
from .substrates import (ANDROID_PROFILE, CursorError, SubstrateRefusal,
                         X11_PROFILE, ExecutionTargetProfile, Substrate,
                         resolve_substrate)


class TargetRefusal(RuntimeError):
    pass


class RemoteDispatchTarget:
    def __init__(self, db_path: str, device_id: str, agent_id: str,
                 agent_token: str, host: str = "127.0.0.1", port: int = 0,
                 display: Optional[str] = None,
                 cert_dir: Optional[str] = None,
                 substrate: Optional[ExecutionTargetProfile] = None,
                 substrate_config: Optional[Dict[str, Any]] = None):
        self.store = RemoteDispatchStore(db_path)
        self.db_path = db_path
        self.device_id = device_id
        self.agent_id = agent_id
        self.agent_token = agent_token  # presented as identity proof
        # Substrate resolution is by capability profile, never by device
        # identity. Default keeps the historical bench behavior (x11).
        self._substrate: Substrate = resolve_substrate(
            substrate or X11_PROFILE)
        cfg: Dict[str, Any] = dict(substrate_config or {})
        if "display" not in cfg and display is not None:
            cfg["display"] = display
        cfg.setdefault("on_failure", self.store.log_event)
        self._substrate_config = cfg
        self._cursor = None  # created lazily via the substrate
        self._capture = None  # screen capture, created lazily per stream
        self._indicator = self._substrate.make_indicator(**cfg)
        # Substrate event channel (Android: app->Python user_kill).
        # Target-local entry points only; the controller has no path.
        self._substrate.wire_target_events(self)
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
        self._lock = threading.Lock()
        # one live connection per session: a second concurrent hello for
        # the same session is refused; the slot is released on disconnect
        # so a legitimate reconnect (fresh hello) still works.
        self._active: Dict[str, int] = {}
        # Processes launched by each session (pid per session_id), so a
        # kill can causally terminate the session's remote work (U-9).
        # Guarded by self._lock. end_session abandons them (graceful);
        # only kill terminates them (emergency brake).
        self._launched: Dict[str, List[int]] = {}
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
        self._close_capture()
        self._hide_indicator()

    def _close_capture(self) -> None:
        cap, self._capture = self._capture, None
        if cap is not None:
            try:
                cap.close()
            except Exception:
                pass

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
        self._close_capture()
        self._hide_indicator(session_id)
        # The target-local kill switch is the same emergency brake:
        # the session's launched processes die with it.
        out["processes_terminated"] = self._terminate_session_processes(
            session_id)
        # Release the target: a killed session no longer holds the
        # cursor, so a new session may connect.
        with self._lock:
            self._active.pop(session_id, None)
        return out

    def _terminate_session_processes(self, session_id: str) -> int:
        """Causally terminate everything this session launched at the
        target (U-9: kill means stop the remote work). SIGTERM goes to
        the whole process GROUP of each tracked pid -- launch_app uses
        start_new_session, so the child is a session leader and its
        entire tree dies. Brief grace, then SIGKILL survivors, then
        reap. Returns the number of pids signaled. A pid that already
        exited is skipped via an existence check first; the residual
        pid-reuse race (the OS recycling a dead pid between the check
        and killpg) is microscopic and noted here, not hidden."""
        with self._lock:
            pids = self._launched.pop(session_id, [])
        signaled = 0
        for pid in pids:
            try:
                os.kill(pid, 0)  # skip the already-dead
            except (ProcessLookupError, PermissionError):
                continue
            try:
                os.killpg(pid, signal.SIGTERM)
                signaled += 1
            except (ProcessLookupError, PermissionError):
                pass
        if signaled:
            time.sleep(0.5)
            for pid in pids:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            for pid in pids:
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, ProcessLookupError, OSError):
                    pass
            self.store.log_event("session_processes_terminated",
                                 session_id, {"signaled": signaled})
        return signaled

    # -- channel handlers ----------------------------------------------
    def _on_hello(self, msg: Dict[str, Any]) -> str:
        body = msg.get("body", {})
        session_id = msg.get("session_id", "")
        token = body.get("session_token", "")
        # A kill intent marks a kill-only control connection: it carries
        # no cursor control, only the session's kill switch. The session
        # token is verified exactly like any hello (a bad token is still
        # refused), and replay/sequence guards apply to its messages.
        kill_intent = (body.get("intent") == "kill")
        try:
            self.store.check_session_token(session_id, token)
        except SessionError as e:
            raise chan.ChannelError(f"hello refused: {e}")
        with self._lock:
            if self._active.get(session_id) and not kill_intent:
                raise chan.ChannelError(
                    "hello refused: session already has a live connection"
                    " (concurrent duplicate refused)")
            # A kill intent may preempt the session's own live control
            # connection: the user's kill switch must stay prompt during
            # a long-running dispatch. This does not weaken the
            # concurrent-hello guard -- control hellos (no kill intent)
            # are still refused while one is live, and the kill
            # connection itself can never drive the cursor.
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
            if not kill_intent:
                self._active[session_id] = 1
        # A kill-only connection neither marks the session live nor shows
        # the indicator: it exists solely to deliver the kill.
        if not kill_intent:
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
            self._close_capture()
            self._hide_indicator(session_id)
            # U-9 causal termination: the session's launched processes
            # are remote work at the target -- they die with the kill.
            procs = self._terminate_session_processes(session_id)
            return {"kind": "kill_ok",
                    "body": {"state": "killed",
                             "processes_terminated": procs}}
        if kind == "end":
            self.store.end_session(session_id)
            self._close_capture()
            self._hide_indicator(session_id)
            # Graceful end: launched processes are abandoned, not killed.
            with self._lock:
                self._launched.pop(session_id, None)
            return {"kind": "end_ok", "body": {"state": "ended"}}
        if kind == "get_frame":
            return self._on_get_frame(msg)
        if kind not in ("action", "action_batch"):
            raise chan.ChannelError(f"unknown kind {kind!r}")
        actions = body.get("actions", [body.get("action")])
        actions = [a for a in actions if a]
        if not actions:
            raise chan.ChannelError("no actions")
        # --- enforcement: every action, every time ---
        s = self._enforce_channel(session_id, msg.get("seq"),
                                  msg.get("nonce"), what="action")
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
            # U-9 causal termination: a kill landing between two actions
            # of one batch must stop the later actions, not just the
            # next keystroke. type() also checks per keystroke; this
            # covers move/click/scroll/key/launch_app between actions.
            _is_live()
            results.append(self._execute(session_id, action, scope,
                                         _is_live))
        # budget charged only for actions that actually executed
        self.store.charge_budget(session_id, len(actions))
        return {"kind": "action_ok", "body": {"results": results}}

    def _enforce_channel(self, session_id: str, seq, nonce,
                         what: str = "message"):
        """Per-message enforcement shared by actions and frame fetches:
        session live, consent live, exact seq + unseen nonce; the seq is
        consumed and the nonce recorded BEFORE application checks, so a
        refusal cannot desync the channel."""
        s = self.store._get_session(session_id)
        if s["state"] != LIVE:
            raise TargetRefusal(
                f"session {s['state']}: {what} refused")
        # consent still live? (checked per message, not cached from hello)
        if not self.store.consent_live(session_id):
            raise TargetRefusal(f"consent no longer live: {what} refused")
        with self._lock:
            if seq != s["seq_next"]:
                raise TargetRefusal(
                    f"seq mismatch: got {seq}, expected {s['seq_next']}"
                    " (replay or reorder refused)")
            if self._nonce_seen(session_id, nonce):
                raise TargetRefusal("duplicate nonce: replay refused")
        self.store.advance_seq(session_id)
        self._record_nonce(session_id, nonce, seq)
        return s

    def _on_get_frame(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        """Serve one stream frame. Enforcement is identical to actions:
        the stream lives only inside a live, consented session, and a
        kill/end/expiry refuses the next fetch -- refused, not frozen.
        Scope-clipping is applied to the capture rect BEFORE capture:
        the controller never receives pixels outside the granted
        bounds. Frames are never persisted here (see controller-side
        save_frame, gated on the record_frames scope grant)."""
        body = msg.get("body", {})
        session_id = msg.get("session_id", "")
        self._enforce_channel(session_id, msg.get("seq"), msg.get("nonce"),
                              what="frame")
        scope = self.store.get_scope(session_id)
        if not scope.screen_share:
            raise TargetRefusal(
                "screen sharing not in session scope: frame refused")
        if self._capture is None:
            try:
                self._capture = self._substrate.make_screen_capture(
                    **self._substrate_config)
            except CursorError as e:
                raise TargetRefusal(
                    f"screen capture unavailable: {e}")
        clip = {"x": scope.x_min, "y": scope.y_min,
                "w": scope.x_max - scope.x_min,
                "h": scope.y_max - scope.y_min}
        max_dim = body.get("max_dim")
        if not isinstance(max_dim, int) or max_dim <= 0:
            max_dim = None
        try:
            frame = self._capture.capture(clip, max_dim=max_dim)
        except SubstrateRefusal as e:
            # e.g. the Android target-local kill latch: the screen must
            # not keep streaming after the user pressed KILL, even in
            # the lost-event case.
            raise TargetRefusal(str(e))
        # A CursorError from capture (no X server, projection denied)
        # propagates as an explicit error frame -- never a fake frame.
        return {"kind": "frame_ok", "body": frame}

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

    def _execute(self, session_id: str, action: Dict[str, Any],
                 scope: Scope, is_live) -> Dict[str, Any]:
        # A substrate policy refusal (e.g. the Android app's local kill
        # latch) is an explicit action_refused, never a transport error.
        # All other CursorErrors keep their existing behavior, so the X11
        # substrate's wire semantics are unchanged.
        try:
            return self._execute_inner(session_id, action, scope, is_live)
        except SubstrateRefusal as e:
            raise TargetRefusal(str(e))

    def _execute_inner(self, session_id: str, action: Dict[str, Any],
                       scope: Scope, is_live) -> Dict[str, Any]:
        if self._cursor is None:
            self._cursor = self._substrate.make_cursor(
                **self._substrate_config)
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
            out = self._cursor.launch_app(action["argv"], scope.apps or [])
            # Track the session's remote processes: kill must causally
            # terminate them (U-9). end_session deliberately leaves them.
            pid = out.get("pid")
            if isinstance(pid, int) and pid > 0:
                with self._lock:
                    self._launched.setdefault(session_id, []).append(pid)
            return out
        raise TargetRefusal(f"unexecutable action {kind!r}")

    # -- live-session indicator (delegated to the substrate) -----------
    def _show_indicator(self, session_id: str) -> None:
        """Show the live-session indicator via the substrate. Indicator
        failure never breaks the session; the substrate records the
        miss and the session itself stays governed."""
        self._indicator.show(session_id)

    def _hide_indicator(self, session_id: Optional[str] = None) -> None:
        self._indicator.hide(session_id)

    def indicator_live(self) -> bool:
        """The indicator as the target's display stack sees it right
        now -- never a cached handle."""
        return self._indicator.live()
