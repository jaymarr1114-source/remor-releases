"""Android target-side cursor primitives via the DispatchAccessibilityService bridge.

The bridge is a localhost JSON-line socket served by the Android target
app (see android/BridgeProtocol.md). This module is the Python side:
it translates the six dispatch primitives into bridge commands and
returns the service's REAL results. Failures raise CursorError --
they propagate to the controller as explicit error frames, never as
faked ok.

Honest substrate classification (Android/AccessibilityService):
  CAN: tap (single finger), swipe (scroll/drag), set-text on the
       focused editable node, global actions (back/home/recents),
       app launch via launcher intent, overlay indicator, user-local
       kill (via bridge event).
  CANNOT: hover cursor -- a touch UI has no pointer. move() stages the
       next tap point and reports the staged coordinates; it moves
       nothing visible. multi-touch/pinch. raw key-event injection
       (no XTEST equivalent; keys map to global actions or raise).
       per-keystroke interruption of type(): set-text is atomic, so
       liveness is checked before dispatch and the kill still gates
       the NEXT action. pressure/tilt. keycodes beyond the mapped set.
"""
from __future__ import annotations

import itertools
import json
import socket
import threading
from typing import Any, Callable, Dict, List, Optional

from .substrates import (ANDROID_PROFILE, CursorError, ScreenCapture,
                         Substrate, SubstrateIndicator, SubstrateRefusal,
                         TargetCursor)

BRIDGE_DEFAULT_HOST = "127.0.0.1"
BRIDGE_DEFAULT_PORT = 47631
_BRIDGE_TIMEOUT_S = 30.0

# X keysym names -> Android global actions. Anything unmapped raises:
# there is no raw key-event injection on this substrate.
_KEYMAP = {
    "BackSpace": "back",
    "Escape": "back",
    "Home": "home",
    "Menu": "recents",
}


class AndroidBridge:
    """Full-duplex bridge client. Requests are {"id","cmd","params"};
    replies are {"id","ok","result"/"error"}. The app may also send
    {"event",...} frames at any time; a reader thread dispatches them
    to registered handlers (e.g. user_kill)."""

    def __init__(self, host: str = BRIDGE_DEFAULT_HOST,
                 port: int = BRIDGE_DEFAULT_PORT):
        self._host = host
        self._port = port
        self._ids = itertools.count(1)
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]],
                                           Dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        try:
            self._sock = socket.create_connection(
                (host, port), timeout=_BRIDGE_TIMEOUT_S)
        except OSError as e:
            raise CursorError(
                f"android bridge unreachable at {host}:{port}: {e}")
        self._file = self._sock.makefile("r", encoding="utf-8")
        self._reader = threading.Thread(target=self._read_loop,
                                        daemon=True)
        self._reader.start()

    def on_event(self, name: str,
                 handler: Callable[[Dict[str, Any]],
                                   Dict[str, Any]]) -> None:
        """Register an app->Python event handler. The handler's return
        value becomes the event_ack result; a raise becomes an ack
        error. Handlers must never kill the reader thread."""
        with self._lock:
            self._handlers[name] = handler

    def _send(self, frame: Dict[str, Any]) -> None:
        data = (json.dumps(frame) + "\n").encode()
        with self._send_lock:
            self._sock.sendall(data)

    def _read_loop(self) -> None:
        try:
            for line in self._file:
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if "event" in frame:
                    self._handle_event(frame)
                elif "id" in frame:
                    with self._cond:
                        self._pending[frame["id"]] = frame
                        self._cond.notify_all()
        except Exception:
            pass

    def _handle_event(self, frame: Dict[str, Any]) -> None:
        name = frame.get("event", "")
        with self._lock:
            h = self._handlers.get(name)

        def run() -> None:
            # Handlers run OFF the reader thread: a handler may itself
            # make synchronous bridge requests (e.g. user_kill hides
            # the indicator), and those replies are consumed by the
            # reader thread. Running the handler on the reader would
            # deadlock.
            ok, error = True, None
            result: Dict[str, Any] = {}
            if h is None:
                ok, error = False, f"no handler for event {name!r}"
            else:
                try:
                    result = h(frame) or {}
                except Exception as e:  # noqa: BLE001 -- ack the failure
                    ok, error = False, str(e)[:300]
            ack: Dict[str, Any] = {"event_ack": name, "ok": ok}
            if ok:
                ack["result"] = result
            else:
                ack["error"] = error
            try:
                self._send(ack)
            except Exception:
                pass

        t = threading.Thread(target=run, daemon=True,
                             name=f"bridge-event-{name}")
        t.start()

    def request(self, cmd: str,
                params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rid = next(self._ids)
        frame = {"id": rid, "cmd": cmd, "params": params or {}}
        try:
            self._send(frame)
        except OSError as e:
            raise CursorError(f"android bridge send failed: {e}")
        deadline = threading.Event()
        with self._cond:
            ok = self._cond.wait_for(
                lambda: rid in self._pending, timeout=_BRIDGE_TIMEOUT_S)
            reply = self._pending.pop(rid, None)
        if not ok or reply is None:
            raise CursorError(
                f"android bridge: no reply to {cmd!r} within"
                f" {_BRIDGE_TIMEOUT_S}s")
        if not reply.get("ok"):
            err = str(reply.get("error", "bridge command failed"))
            if "KILL_LATCHED" in err:
                # The target app's local kill latch: a policy refusal,
                # not an execution failure. The target agent converts
                # this to an explicit action_refused.
                raise SubstrateRefusal(
                    f"android bridge refused {cmd!r}: {err}")
            raise CursorError(
                f"android bridge {cmd!r} failed: {err}")
        return reply.get("result", {})

    def ping(self) -> Dict[str, Any]:
        return self.request("ping")

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


class AndroidCursor(TargetCursor):
    def __init__(self, bridge: Optional[AndroidBridge] = None,
                 host: str = BRIDGE_DEFAULT_HOST,
                 port: int = BRIDGE_DEFAULT_PORT):
        self._bridge = bridge or AndroidBridge(host, port)
        self._owns_bridge = bridge is None
        # Staged tap point: Android has no hover cursor, so move()
        # records where the NEXT tap/scroll lands and reports it.
        self._staged: Optional[Dict[str, int]] = None
        info = self._bridge.ping()
        if not info.get("service_connected"):
            raise CursorError(
                "android bridge reachable but AccessibilityService not"
                " connected: enable the REMOR dispatch service on target")
        disp = info.get("display", {})
        self._display = {"width": int(disp.get("width", 0)),
                         "height": int(disp.get("height", 0))}

    # -- primitives ---------------------------------------------------
    def move(self, x: int, y: int) -> Dict[str, int]:
        # No pointer exists on a touch UI: stage the point for the next
        # tap/scroll and report exactly that. Documented, not faked.
        self._staged = {"x": int(x), "y": int(y)}
        return dict(self._staged)

    def _tap_point(self, x: Optional[int],
                   y: Optional[int]) -> Dict[str, int]:
        if x is not None and y is not None:
            return {"x": int(x), "y": int(y)}
        if self._staged is not None:
            return dict(self._staged)
        w, h = self._display["width"], self._display["height"]
        if w <= 0 or h <= 0:
            raise CursorError(
                "click: no coordinates and no staged point and unknown"
                " display size")
        return {"x": w // 2, "y": h // 2}

    def click(self, button: int = 1, x: Optional[int] = None,
              y: Optional[int] = None) -> Dict[str, int]:
        if button != 1:
            raise CursorError(
                f"click: button {button} unsupported on touch substrate"
                " (no right/middle click)")
        pt = self._tap_point(x, y)
        res = self._bridge.request("tap", pt)
        self._staged = {"x": int(res.get("x", pt["x"])),
                        "y": int(res.get("y", pt["y"]))}
        return dict(self._staged)

    def scroll(self, dx: int = 0, dy: int = 0) -> Dict[str, int]:
        if dx == 0 and dy == 0:
            raise CursorError("scroll: dx and dy both zero")
        pt = self._tap_point(None, None)
        # Bound a single scroll action, mirroring the X11 substrate's
        # bound (20 wheel steps there; here, one swipe per action).
        res = self._bridge.request(
            "scroll", {"x": pt["x"], "y": pt["y"],
                       "dx": int(dx), "dy": int(dy)})
        return {"x": int(res.get("x", pt["x"])),
                "y": int(res.get("y", pt["y"])),
                "dx": int(res.get("dx", dx)),
                "dy": int(res.get("dy", dy))}

    def type(self, text: str, is_live=None) -> Dict[str, int]:
        if len(text) > 2000:
            raise CursorError("type: text too long")
        # Atomic on this substrate: the only interruption point is
        # before dispatch. Kill still gates the NEXT action.
        if is_live is not None:
            is_live()  # raises if the session is no longer live
        res = self._bridge.request("set_text", {"text": text})
        entered = int(res.get("entered_chars", 0))
        return {"typed_chars": entered}

    def key(self, keysym: str) -> Dict[str, str]:
        action = _KEYMAP.get(keysym)
        if action is None:
            raise CursorError(
                f"key: {keysym!r} has no Android mapping (no raw"
                " key-event injection on this substrate)")
        self._bridge.request("global_action", {"action": action})
        return {"key": keysym, "android_action": action}

    def launch_app(self, argv: List[str],
                   allowed: List[str]) -> Dict[str, Any]:
        name = argv[0] if argv else ""
        if name not in allowed:
            raise CursorError(f"app {name!r} not in allowlist")
        res = self._bridge.request("launch", {"package": name})
        # No pid on Android: startActivity is fire-and-forget. Report
        # what the service actually did, not a fabricated pid.
        return {"package": res.get("package", name),
                "launched": bool(res.get("launched", False))}

    def close(self) -> None:
        if self._owns_bridge:
            self._bridge.close()


class AndroidIndicator(SubstrateIndicator):
    """The live-session indicator is the target app's overlay banner
    (WindowManager overlay with session id + KILL button). live()
    asks the app; it is never a cached handle."""

    def __init__(self, bridge: Optional[AndroidBridge] = None,
                 host: str = BRIDGE_DEFAULT_HOST,
                 port: int = BRIDGE_DEFAULT_PORT,
                 on_failure: Optional[Callable] = None):
        self._bridge = bridge or AndroidBridge(host, port)
        self._owns_bridge = bridge is None
        self._on_failure = on_failure

    def show(self, session_id: str) -> None:
        try:
            self._bridge.request("indicator_show",
                                 {"session_id": session_id})
        except CursorError:
            # Indicator failure must never break the session; the
            # session itself is still governed. Record the miss visibly.
            if self._on_failure:
                self._on_failure("indicator_failed", session_id, {})

    def hide(self, session_id: Optional[str] = None) -> None:
        try:
            self._bridge.request("indicator_hide",
                                 {"session_id": session_id or ""})
        except CursorError:
            pass

    def live(self) -> bool:
        try:
            res = self._bridge.request("indicator_live")
            return bool(res.get("live"))
        except CursorError:
            return False


class AndroidScreenCapture(ScreenCapture):
    """Screen capture on the Android substrate via the bridge
    `capture_frame` command.

    The REAL capture path is the target app's MediaProjection flow
    (BridgeProtocol.md): the app acquires the projection, renders to
    an ImageReader surface, and returns PNG bytes. Fail-closed: if
    the app has no media-projection permission it returns
    CAPTURE_UNAVAILABLE and this raises CursorError -- never a stale
    or fabricated frame.

    A latched user kill refuses capture like any actuator command
    (KILL_LATCHED -> SubstrateRefusal): in the lost-event case the
    screen must not keep streaming after the user pressed KILL.
    """

    def __init__(self, bridge: Optional[AndroidBridge] = None,
                 host: str = BRIDGE_DEFAULT_HOST,
                 port: int = BRIDGE_DEFAULT_PORT):
        self._bridge = bridge or AndroidBridge(host, port)
        self._owns_bridge = bridge is None

    def capture(self, clip: Optional[Dict[str, int]] = None,
                max_dim: Optional[int] = None) -> Dict[str, Any]:
        # Scope-clipping is applied by the target agent before this
        # call; the clip here is defense-in-depth on the same rect.
        params: Dict[str, Any] = {
            "max_w": int(max_dim) if max_dim else 0,
            "max_h": int(max_dim) if max_dim else 0,
        }
        if clip:
            params["clip"] = {k: int(clip.get(k, 0))
                              for k in ("x", "y", "w", "h")}
        res = self._bridge.request("capture_frame", params)
        # Pass the harness label through untouched: synthesized frames
        # are only ever admissible when labeled as such.
        return {"width": int(res["width"]), "height": int(res["height"]),
                "format": str(res.get("format", "png")),
                "data_b64": str(res["data_b64"]),
                "ts": float(res.get("ts", 0.0)),
                "synthesized": bool(res.get("synthesized", False))}

    def close(self) -> None:
        if self._owns_bridge:
            self._bridge.close()


class AndroidSubstrate(Substrate):
    profile = ANDROID_PROFILE

    def __init__(self):
        self._bridge: Optional[AndroidBridge] = None
        self._bridge_key = None

    def _shared(self, config: Dict[str, Any]) -> AndroidBridge:
        key = (config.get("bridge_host", BRIDGE_DEFAULT_HOST),
               int(config.get("bridge_port", BRIDGE_DEFAULT_PORT)))
        if self._bridge is None or self._bridge_key != key:
            if self._bridge is not None:
                try:
                    self._bridge.close()
                except Exception:
                    pass
            self._bridge = AndroidBridge(key[0], key[1])
            self._bridge_key = key
        return self._bridge

    def make_cursor(self, **config) -> TargetCursor:
        return AndroidCursor(bridge=self._shared(config))

    def make_indicator(self, **config) -> SubstrateIndicator:
        return AndroidIndicator(bridge=self._shared(config),
                                on_failure=config.get("on_failure"))

    def make_screen_capture(self, **config) -> ScreenCapture:
        return AndroidScreenCapture(bridge=self._shared(config))

    def wire_target_events(self, target) -> None:
        """App->Python events. user_kill goes through the target's own
        target-local entry point -- the same function the bench calls.
        The controller has no path to it."""
        bridge = self._shared(getattr(target, "_substrate_config", {}))

        def _on_user_kill(frame: Dict[str, Any]) -> Dict[str, Any]:
            return target.user_kill(frame.get("session_id", ""),
                                    frame.get("user_token", ""))

        bridge.on_event("user_kill", _on_user_kill)
