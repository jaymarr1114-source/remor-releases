"""HARNESS -- bench stand-in for the Android target app.

Implements the APP side of BridgeProtocol.md faithfully (framing,
command set, error cases, event/ack flow) against an internal virtual
device model: a virtual display, a tap/swipe log, a focusable text
field buffer, a launched-packages list, and overlay indicator state.

HONEST LABEL: this is NOT an Android device. It proves the Python
side speaks the bridge protocol correctly (exact frames, result
handling, error propagation, event/ack round-trip) -- mechanism
evidence only. Gesture execution, overlays, and the kill UI on a real
device are UNPROVEN until James's hardware acceptance.

capture_frame returns HARNESS-SYNTHESIZED pixel data (result carries
"synthesized": true and the bytes are a generated pattern, never a
real screen): it proves the capture_frame command path -- framing,
clip handling, kill-latch gating, error propagation -- not real
MediaProjection capture, which is UNPROVEN until the app implements
it against the real API.

Test-only helpers (focus_field/blur) are LOCAL methods, not protocol
commands: the real app derives focus from the real UI.
"""
from __future__ import annotations

import json
import socket
import threading
from typing import Any, Callable, Dict, List, Optional

GLOBAL_ACTIONS = {"back", "home", "recents", "notifications",
                  "quick_settings", "power_dialog"}


class HarnessBridge:
    """A virtual Android target app. See module docstring for the
    honesty label."""

    HARNESS = True  # label, checked by the proof battery

    def __init__(self, host: str = "127.0.0.1", port: int = 0,
                 width: int = 1080, height: int = 2400):
        self._host = host
        self.width = width
        self.height = height
        # virtual device model (real state transitions, in this process)
        self.tap_log: List[Dict[str, int]] = []
        self.swipe_log: List[Dict[str, int]] = []
        self.scroll_log: List[Dict[str, int]] = []
        self.global_actions: List[str] = []
        self.launched: List[str] = []
        self.field_buffer: List[str] = []  # the focused editable field
        self.focused_field: Optional[str] = None
        self.indicator_session: Optional[str] = None
        self.received_commands: List[str] = []
        # HARNESS model of the app's fail-closed user kill latch
        # (DispatchAccessibilityService.killLatchedSession): set the
        # instant the user presses KILL; while set, every actuator
        # command is refused; cleared only by indicator_show for a
        # different session (fresh consented session rearms).
        self.kill_latched_session: Optional[str] = None
        self._frame_no = 0  # harness-synthesized frame counter
        self._clients: List[socket.socket] = []
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._ack_event = threading.Event()
        self._ack: Optional[Dict[str, Any]] = None
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop,
                                        daemon=True)
        self._thread.start()

    # -- test-only local helpers (NOT protocol commands) ----------------
    def latch_kill(self, session_id: str = "<unknown>") -> None:
        """HARNESS: model the user pressing KILL in the app.

        Engages the fail-closed actuator latch WITHOUT sending the
        user_kill event -- i.e. the lost-event case the latch exists
        for. The Python target's session stays live; the app side must
        still refuse every actuator command.
        """
        with self._lock:
            self.kill_latched_session = session_id

    def focus_field(self, name: str = "field1") -> None:
        with self._lock:
            self.focused_field = name

    def blur(self) -> None:
        with self._lock:
            self.focused_field = None

    # -- server ---------------------------------------------------------
    def _accept_loop(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with self._lock:
                self._clients.append(conn)
            t = threading.Thread(target=self._client_loop, args=(conn,),
                                 daemon=True)
            t.start()

    def _client_loop(self, conn: socket.socket):
        try:
            f = conn.makefile("r", encoding="utf-8")
            for line in f:
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if "event_ack" in frame:
                    self._on_ack(frame)
                    continue
                reply = self._handle(frame)
                self._send(conn, reply)
        except Exception:
            pass
        finally:
            with self._lock:
                if conn in self._clients:
                    self._clients.remove(conn)
            try:
                conn.close()
            except Exception:
                pass

    def _send(self, conn: socket.socket, frame: Dict[str, Any]) -> None:
        data = (json.dumps(frame) + "\n").encode()
        with self._send_lock:
            conn.sendall(data)

    def _on_ack(self, frame: Dict[str, Any]) -> None:
        with self._lock:
            self._ack = frame
            self._ack_event.set()

    # -- app-side command implementation (the virtual device) -----------
    def _handle(self, frame: Dict[str, Any]) -> Dict[str, Any]:
        reply: Dict[str, Any] = {"id": frame.get("id")}
        try:
            cmd = frame["cmd"]
            params = frame.get("params", {})
            with self._lock:
                self.received_commands.append(cmd)
            result = self._dispatch(cmd, params)
            reply["ok"] = True
            reply["result"] = result
        except KeyError as e:
            reply["ok"] = False
            reply["error"] = f"missing param {e}"
        except Exception as e:  # noqa: BLE001 -- error goes on the wire
            reply["ok"] = False
            reply["error"] = str(e)[:300]
        return reply

    def _dispatch(self, cmd: str, p: Dict[str, Any]) -> Dict[str, Any]:
        # HARNESS model of the app-side kill latch gate
        # (BridgeServer.dispatch -> checkKillLatch()).
        if cmd in ("tap", "swipe", "scroll", "set_text",
                   "global_action", "launch", "capture_frame"):
            with self._lock:
                latched = self.kill_latched_session
            if latched is not None:
                raise RuntimeError(
                    f"KILL_LATCHED: local kill latched for session "
                    f"{latched}: input blocked until a fresh consented"
                    f" session")
        if cmd == "ping":
            return {"service_connected": True,
                    "display": {"width": self.width,
                                "height": self.height}}
        if cmd == "tap":
            x, y = int(p["x"]), int(p["y"])
            with self._lock:
                self.tap_log.append({"x": x, "y": y})
            return {"x": x, "y": y}
        if cmd == "swipe":
            dur = max(10, min(2000, int(p.get("duration_ms", 300))))
            rec = {"x1": int(p["x1"]), "y1": int(p["y1"]),
                   "x2": int(p["x2"]), "y2": int(p["y2"]),
                   "duration_ms": dur}
            with self._lock:
                self.swipe_log.append(rec)
            return rec
        if cmd == "scroll":
            x, y = int(p["x"]), int(p["y"])
            dx, dy = int(p["dx"]), int(p["dy"])
            x2 = max(0, min(self.width - 1, x - dx * 150))
            y2 = max(0, min(self.height - 1, y - dy * 150))
            rec = {"x": x, "y": y, "dx": dx, "dy": dy}
            with self._lock:
                self.scroll_log.append(rec)
                self.swipe_log.append({"x1": x, "y1": y, "x2": x2,
                                       "y2": y2, "duration_ms": 300})
            return rec
        if cmd == "set_text":
            text = p["text"]
            if not isinstance(text, str) or len(text) > 2000:
                raise ValueError("text must be str <= 2000 chars")
            with self._lock:
                if self.focused_field is None:
                    raise RuntimeError("no focused editable node")
                self.field_buffer.append(text)
            return {"entered_chars": len(text)}
        if cmd == "global_action":
            a = p["action"]
            if a not in GLOBAL_ACTIONS:
                raise ValueError(f"unknown global action {a}")
            with self._lock:
                self.global_actions.append(a)
            return {}
        if cmd == "launch":
            pkg = p["package"]
            with self._lock:
                self.launched.append(pkg)
            return {"package": pkg, "launched": True}
        if cmd == "indicator_show":
            sid = p["session_id"]
            with self._lock:
                # HARNESS model of the app rearm rule: a fresh consented
                # session (different id) rearms the kill latch; the
                # latched session itself can never rearm.
                latched = self.kill_latched_session
                if latched is not None and latched != sid:
                    self.kill_latched_session = None
                self.indicator_session = sid
            return {}
        if cmd == "indicator_hide":
            sid = p.get("session_id", "")
            with self._lock:
                if not sid or self.indicator_session == sid:
                    self.indicator_session = None
            return {}
        if cmd == "indicator_live":
            with self._lock:
                live = self.indicator_session is not None
            return {"live": live}
        if cmd == "capture_frame":
            return self._synth_frame(p)
        raise ValueError(f"unknown cmd {cmd}")

    def _synth_frame(self, p: Dict[str, Any]) -> Dict[str, Any]:
        """HARNESS-SYNTHESIZED frame: a generated pattern, never a real
        screen. Labeled synthesized=true in the result; proves the
        capture_frame command path only."""
        import base64
        import io
        import time
        from PIL import Image, ImageDraw
        clip = p.get("clip") or {}
        if clip:
            x = max(0, min(self.width - 1, int(clip.get("x", 0))))
            y = max(0, min(self.height - 1, int(clip.get("y", 0))))
            w = max(1, min(int(clip.get("w", 0)), self.width - x))
            h = max(1, min(int(clip.get("h", 0)), self.height - y))
        else:
            w, h = self.width, self.height
            mw, mh = int(p.get("max_w", 0)), int(p.get("max_h", 0))
            if mw <= 0 and mh <= 0:
                mw, mh = 480, 480  # default bench bound
            if mw > 0 and w > mw or mh > 0 and h > mh:
                s = min(mw / w if mw > 0 else 1.0,
                        mh / h if mh > 0 else 1.0)
                w, h = max(1, int(w * s)), max(1, int(h * s))
        with self._lock:
            self._frame_no += 1
            n = self._frame_no
        img = Image.new("RGB", (w, h),
                        ((n * 53) % 256, (n * 97) % 256,
                         (n * 37) % 256))
        d = ImageDraw.Draw(img)
        d.rectangle([8, 8, w - 9, h - 9], outline=(255, 255, 255))
        d.line([0, 0, w - 1, h - 1], fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return {"width": w, "height": h, "format": "png",
                "data_b64": base64.b64encode(buf.getvalue()).decode(
                    "ascii"),
                "ts": time.time(), "synthesized": True}

    # -- app -> python event injection (the KILL button path) -----------
    def send_event(self, event: Dict[str, Any],
                   timeout: float = 15.0) -> Dict[str, Any]:
        """Pretend the user pressed KILL: emit the event frame on a live
        client and wait for Python's event_ack. Returns the ack."""
        with self._lock:
            clients = list(self._clients)
            self._ack_event = threading.Event()
            self._ack = None
        if not clients:
            raise RuntimeError("HARNESS: no python client connected")
        data = (json.dumps(event) + "\n").encode()
        with self._send_lock:
            clients[0].sendall(data)
        with self._lock:
            ack_event = self._ack_event
        if not ack_event.wait(timeout):
            raise RuntimeError("HARNESS: event_ack timeout")
        with self._lock:
            return dict(self._ack or {})

    def stop(self):
        # Wake the accept loop deterministically before closing: a
        # self-connect guarantees the thread is not left parked in
        # accept() on the dead socket (where it could still accept a
        # connection that arrived just before close).
        try:
            wake = socket.create_connection(
                (self._host, self.port), timeout=1)
            wake.close()
        except Exception:
            pass
        try:
            self._srv.close()
        except Exception:
            pass
        with self._lock:
            for c in self._clients:
                try:
                    c.close()
                except Exception:
                    pass
