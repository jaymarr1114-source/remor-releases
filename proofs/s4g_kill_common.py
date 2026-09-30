"""S4G-KILL-CAUSAL shared bench harness.

BENCH SUBSTRATE (never device proof): real RemoteDispatchTarget on Xvfb
(:99) with the X11 cursor substrate, real pinned-TLS channel, real
RemoteDispatchService (the /api/remote/* handler layer). Kill is issued
through the real svc.kill() path -- the same code the HTTP route calls.

Run: python3 proofs/s4g_kill_livefire.py   (from the repo root)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "pylib"))

from swarm_engine.governance import caller_authorization as authz  # noqa: E402
from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.remote_dispatch import tls as rd_tls  # noqa: E402
from swarm_engine.remote_dispatch.controller import (  # noqa: E402
    RemoteDispatchController)
from swarm_engine.remote_dispatch.session_model import (  # noqa: E402
    RemoteDispatchStore, Scope)
from swarm_engine.remote_dispatch.target_agent import (  # noqa: E402
    RemoteDispatchTarget)
from swarm_engine.services.remote_dispatch_api import (  # noqa: E402
    build_remote_dispatch_service)

CHROME = "/opt/meta-chromium/chrome"
PAGE_PATH = "/tmp/s4gkillpage.html"
PAGE = ("""<!doctype html><html><head><title>S4GKILL</title></head><body>"""
        """<input id="t" style="width:800px;font-size:16px">"""
        """</body></html>""")

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise AssertionError(f"FAILED: {name} {detail}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def chrome_window_geometry():
    from Xlib.display import Display
    d = Display()
    try:
        for c in d.screen().root.query_tree().children:
            try:
                g = c.get_geometry()
                if g.width == 900 and g.height == 700:
                    return (g.x, g.y, g.width, g.height)
            except Exception:
                pass
        return None
    finally:
        d.close()


class CDP:
    """Readback-only verification channel (never drives input)."""

    def __init__(self, port):
        import websocket
        targets = json.load(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
        page = next(t for t in targets if t["type"] == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"])
        self._id = 0

    def evaluate(self, expr):
        self._id += 1
        self.ws.send(json.dumps(
            {"id": self._id, "method": "Runtime.evaluate",
             "params": {"expression": expr, "returnByValue": True}}))
        resp = json.loads(self.ws.recv())
        res = resp["result"]["result"]
        return res.get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass  # browser may already be gone (e.g. terminated by a kill)


def wait_for_cdp(port, timeout=40.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            return CDP(port)
        except Exception:
            time.sleep(0.5)
    raise AssertionError("timeout waiting for CDP page target")


def wait_for(pred, timeout=30.0, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for {what}")


class Bench:
    """The whole live bench: engine, service, target, one device."""

    def __init__(self, scratch):
        self.scratch = scratch
        if os.path.exists(scratch):
            shutil.rmtree(scratch)
        os.makedirs(scratch, exist_ok=True)

        eng = SwarmEngine(db_path=os.path.join(scratch, "eng.db"))
        agents = authz.AgentDirectory(eng.oracle_registry)
        eng_caller = eng.oracle

        self.svc = build_remote_dispatch_service(
            scratch, agents, eng_caller)
        self.ctrl = self.svc.controller
        self.db = os.path.join(scratch, "remote_dispatch.db")

        cert_dir = os.path.join(scratch, "certs")
        cert_path, _ = rd_tls.ensure_cert(cert_dir)
        fp = rd_tls.fingerprint(cert_path)
        pair = self.ctrl.pair_device("bench-kill-1", "Kill bench target",
                                     cert_fingerprint=fp)
        self.agent_id = pair["agent_id"]
        agent_token = pair["agent_token"]

        store = RemoteDispatchStore(self.db)
        self.user_token = store.register_user("bench-user")

        self.port = 18471
        self.target = RemoteDispatchTarget(
            self.db, "bench-kill-1", self.agent_id, agent_token,
            host="127.0.0.1", port=self.port, cert_dir=cert_dir)
        self.target.start()
        assert self.target.port == self.port
        # endpoint announcement rides target.start(); the service's
        # _endpoint() reads it from rd_devices.

    def new_session(self, apps=("/opt/meta-chromium/chrome", "/bin/sleep"),
                    max_actions=500):
        scope = Scope(
            actions=["move", "click", "scroll", "type", "key",
                     "launch_app"],
            x_min=0, y_min=0, x_max=1600, y_max=1200,
            apps=list(apps), max_actions=max_actions, ttl_s=600)
        req = self.ctrl.request_session("bench-kill-1", scope)
        sid = req["session_id"]
        consent = self.target.store.user_grant_consent(
            sid, self.user_token, ttl_s=600)
        token = consent["session_token"]
        sess = self.ctrl.connect(sid, token, "127.0.0.1", self.port)
        return sid, token, sess

    def kill_via_api(self, sid, token):
        """The REAL kill path: svc.kill -> _relay_kill_to_target over the
        pinned-TLS kill-intent channel, then the controller store kill."""
        return self.svc.kill(sid, {"session_token": token})

    def session_state(self, sid):
        return self.ctrl.store._get_session(sid)["state"]

    def launch_browser(self, sess):
        """Launch Chromium via remote dispatch; return (cdp, click_xy)."""
        cdp_port = free_port()
        r = sess.act({"type": "launch_app", "app": CHROME, "argv": [
            CHROME, "--no-first-run", "--no-sandbox",
            "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-session-crashed-bubble",
            f"--remote-debugging-port={cdp_port}",
            "--remote-allow-origins=*",
            "--window-position=60,60", "--window-size=900,700",
            f"--app=file://{PAGE_PATH}"]})
        pid = r[0].get("pid", 0)
        assert pid > 0, f"browser launch failed: {r}"
        cdp = wait_for_cdp(cdp_port, timeout=40)
        url = wait_for(
            lambda: cdp.evaluate("location.href") or None,
            timeout=30, what="page load")
        assert url == f"file://{PAGE_PATH}", url
        wait_for(
            lambda: cdp.evaluate("document.getElementById('t') !== null")
            or None, timeout=30, what="input in DOM")
        rect = json.loads(cdp.evaluate(
            "JSON.stringify({r: document.getElementById('t')"
            ".getBoundingClientRect().toJSON(),"
            " iw: window.innerWidth, ih: window.innerHeight})"))
        rx, ry = rect["r"]["x"], rect["r"]["y"]
        rw, rh = rect["r"]["width"], rect["r"]["height"]
        ih = rect["ih"]
        wx, wy, ww, wh = wait_for(
            lambda: chrome_window_geometry(), timeout=30,
            what="chromium window")
        click_x = int(wx + rx + rw / 2)
        click_y = int(wy + (wh - ih) + ry + rh / 2)
        sess.act_batch([
            {"type": "move", "x": click_x, "y": click_y},
            {"type": "click", "x": click_x, "y": click_y, "button": 1},
        ])
        focused = wait_for(
            lambda: cdp.evaluate("document.activeElement.id") or None,
            timeout=15, what="input focus (click landed)")
        assert focused == "t", f"input not focused: {focused}"
        return cdp, pid

    def input_len(self, cdp):
        return cdp.evaluate(
            "document.getElementById('t').value.length")

    def close(self):
        try:
            self.target.stop()
        except Exception:
            pass


def pkill_chrome():
    subprocess.run(["pkill", "-x", "chrome"], check=False)
    time.sleep(1)


@contextlib.contextmanager
def count_keystroke_liveness(cursor):
    """LABELED OBSERVATION (not a mechanism change): wraps the target
    cursor's type() so the per-keystroke is_live() calls -- the REAL
    kill check -- are counted. The real is_live still runs per
    keystroke and the real interrupt still raises; only the count is
    new. Needed because Xlib buffers injected keystrokes until
    sync(), so mid-flight progress is not DOM-observable: the
    in-flight moment must be observed at the target, where the kill
    actually lands."""
    orig_type = cursor.type
    counter = {"n": 0}

    def counting_type(text, is_live=None):
        def counting_is_live():
            counter["n"] += 1
            if is_live is not None:
                is_live()

        return orig_type(text, is_live=counting_is_live)

    cursor.type = counting_type
    try:
        yield counter
    finally:
        cursor.type = orig_type


@contextlib.contextmanager
def slow_substrate_keystroke(delay_s=0.0005):
    """LABELED SIMULATION (bench only): slows X11 keystroke injection to
    widen the kill observation window. The native substrate types
    ~5k chars/s with 6x run-to-run variance, so a mid-flight kill cannot
    be timed reliably; with 0.5ms per fake_input a 2000-char type takes
    ~4s. The mechanism under test -- the per-keystroke is_live() check
    in X11Cursor.type that raises on kill -- is untouched and real; only
    substrate speed is simulated. Same justification as T7's slow move.
    Safe: the driver process never calls xtest directly; only the
    in-process target's cursor does."""
    from Xlib.ext import xtest
    orig = xtest.fake_input

    def slow(*a, **k):
        time.sleep(delay_s)
        return orig(*a, **k)

    xtest.fake_input = slow
    try:
        yield
    finally:
        xtest.fake_input = orig
