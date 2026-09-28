"""REMOTE-DISPATCH-1 proof battery.

End-to-end through the real channel (TLS + framed protocol) against a
real X11 target (Xvfb :99): pair (with TLS certificate pinning) ->
request -> user consent -> connect (mutual auth) -> move/click/scroll/
type -> directed task (launch Chromium, click a real input, type a real
string, verify the page received it) -> kill -> chain audit.

Nothing here is mocked: TLS is real (openssl-generated self-signed
cert, fingerprint pinned at pairing), XTEST events move the real X
server cursor, Chromium is a real process. The page is a file:// URL
(this Chromium build blocks local-network HTTP navigation).
Typed-text verification reads the live DOM back over Chrome DevTools
Protocol -- readback only; all driving goes through the
remote-dispatch channel.

Run: python3 proofs/rd1_proof.py
Requires: Xvfb on :99, /opt/meta-chromium/chrome, websocket-client.
Writes: proofs/rd1_manifest.json (for the fresh-process proof).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

os.environ.setdefault("DISPLAY", ":99")
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "pylib")))

from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.governance import caller_authorization as authz  # noqa: E402
from swarm_engine.remote_dispatch.controller import (  # noqa: E402
    RemoteDispatchController, RemoteSession)
from swarm_engine.remote_dispatch.session_model import (  # noqa: E402
    RemoteDispatchStore, Scope)
from swarm_engine.remote_dispatch.target_agent import (  # noqa: E402
    RemoteDispatchTarget)
from swarm_engine.remote_dispatch import tls as rd_tls  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(HERE, "rd1_scratch")
CHROME = "/opt/meta-chromium/chrome"
PAGE_PATH = "/tmp/rd1page.html"

PAGE = """<!doctype html><html><head><title>RD1TYPE</title></head>
<body style="margin:0">
<input id="t" style="position:absolute;left:200px;top:200px;width:600px;
height:60px;font-size:30px" autocomplete="off">
</body></html>"""


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def chrome_window_geometry():
    """Outer (x, y, w, h) of the 900x700 Chromium app window."""
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
        self._wsmod = websocket
        targets = json.load(
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
        page = next(t for t in targets if t["type"] == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"])
        self._id = 0

    def evaluate(self, expr):
        self._id += 1
        self.ws.send(json.dumps(
            {"id": self._id, "method": "Runtime.evaluate",
             "params": {"expression": expr,
                        "returnByValue": True}}))
        resp = json.loads(self.ws.recv())
        res = resp["result"]["result"]
        return res.get("value")

    def close(self):
        self.ws.close()


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
        time.sleep(0.25)
    raise AssertionError(f"timeout waiting for {what}")


CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise AssertionError(f"FAILED: {name} {detail}")


def main():
    import shutil
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH, exist_ok=True)
    db = os.path.join(SCRATCH, "rd1.db")
    eng_db = os.path.join(SCRATCH, "eng.db")

    # ---- engine + caller authorization (existing machinery) ----
    eng = SwarmEngine(db_path=os.path.join(SCRATCH, "eng.db"))
    agents = authz.AgentDirectory(eng.oracle_registry)
    eng_caller = eng.oracle

    ctrl = RemoteDispatchController(db, agents, eng_caller)

    # ---- 1. pair the device (TLS pinning ceremony) ------------------
    # The target device generates its identity (self-signed cert) on
    # first boot; the pairing user confirms the fingerprint out of band
    # (bench: the harness reads it from the provisioned cert dir) and
    # the controller pins it. Any other certificate is refused.
    cert_dir = os.path.join(SCRATCH, "rd1_certs")
    cert_path, _key_path = rd_tls.ensure_cert(cert_dir)
    fp = rd_tls.fingerprint(cert_path)
    check("target cert generated", os.path.exists(cert_path), fp[:16])
    pair = ctrl.pair_device("bench-xvfb-1", "Bench Xvfb target",
                            cert_fingerprint=fp)
    check("pair: device enrolled", pair["agent_id"].startswith("rd-target-"))
    agent_id, agent_token = pair["agent_id"], pair["agent_token"]

    # ---- 2. user registers ---------------------------------------
    store = RemoteDispatchStore(db)
    user_token = store.register_user("james")
    check("user registered", user_token.startswith("rduser_"))

    # ---- 3. target comes online ----------------------------------
    PORT = 18431
    target = RemoteDispatchTarget(
        db, "bench-xvfb-1", agent_id, agent_token,
        host="127.0.0.1", port=PORT, cert_dir=cert_dir)
    check("target reuses pinned cert",
          target.cert_fingerprint() == fp, target.cert_fingerprint()[:16])
    target.start()
    check("target listener up", target.port == PORT)

    # ---- 4. request session + user consents (target-side) ---------
    scope = Scope(actions=["move", "click", "scroll", "type", "launch_app"],
                  x_min=0, y_min=0, x_max=1600, y_max=1200,
                  apps=[CHROME], max_actions=200, ttl_s=600)
    req = ctrl.request_session("bench-xvfb-1", scope)
    sid = req["session_id"]
    check("session pending", req["state"] == "pending", sid[:13])

    consent = store.user_grant_consent(sid, user_token, ttl_s=600)
    check("user consent granted (target-side only)",
          consent["state"] == "consented" and consent["session_token"])
    session_token = consent["session_token"]

    # ---- 5. connect: mutual authentication ------------------------
    sess = ctrl.connect(sid, session_token, "127.0.0.1", PORT)
    check("connect: session bound", isinstance(sess, RemoteSession))
    check("indicator on when live (server-verified)",
          target.indicator_live())

    # ---- 6. move / click / scroll through the real channel -------
    r = sess.act({"type": "move", "x": 400, "y": 300})
    check("move executed", r and r[0]["x"] == 400 and r[0]["y"] == 300,
          str(r))
    r = sess.act({"type": "scroll", "dx": 0, "dy": -2})
    check("scroll executed", r and "x" in r[0], str(r))

    # ---- 7. directed task: launch Chromium, click input, type -----
    # The page is file:// (this Chromium build blocks local-network
    # HTTP navigation). Typed text is read back from the live DOM via
    # CDP -- readback only; every input event goes through the
    # remote-dispatch channel.
    with open(PAGE_PATH, "w") as f:
        f.write(PAGE)
    subprocess.run(["pkill", "-x", "chrome"], check=False)
    time.sleep(1)
    cdp_port = free_port()
    r = sess.act({"type": "launch_app", "app": CHROME, "argv": [
        CHROME, "--no-first-run", "--no-sandbox",
        "--disable-dev-shm-usage", "--disable-gpu",
        "--disable-session-crashed-bubble",
        f"--remote-debugging-port={cdp_port}",
        "--remote-allow-origins=*",
        "--window-position=60,60", "--window-size=900,700",
        f"--app=file://{PAGE_PATH}"]})
    check("chromium launched via remote dispatch",
          r and r[0].get("pid", 0) > 0, str(r))

    cdp = wait_for_cdp(cdp_port, timeout=40)
    url = cdp.evaluate("location.href")
    check("page loaded in chromium", url == f"file://{PAGE_PATH}", url)
    rect = cdp.evaluate(
        "JSON.stringify({r: document.getElementById('t')"
        ".getBoundingClientRect().toJSON(),"
        " iw: window.innerWidth, ih: window.innerHeight})")
    rect = json.loads(rect)
    rx, ry = rect["r"]["x"], rect["r"]["y"]
    rw, rh = rect["r"]["width"], rect["r"]["height"]
    ih = rect["ih"]
    wx, wy, ww, wh = wait_for(
        lambda: chrome_window_geometry(), timeout=30,
        what="chromium window")
    # viewport origin: horizontal chrome ~0 in --app mode; vertical
    # decorations (title + infobars) sit above the viewport.
    click_x = int(wx + rx + rw / 2)
    click_y = int(wy + (wh - ih) + ry + rh / 2)
    print(f"    window=({wx},{wy},{ww},{wh}) input=({rx},{ry},{rw},{rh})"
          f" -> click=({click_x},{click_y})", flush=True)

    r = sess.act_batch([
        {"type": "move", "x": click_x, "y": click_y},
        {"type": "click", "x": click_x, "y": click_y, "button": 1},
    ])
    check("move+click batch executed", all("x" in x for x in r), str(r))
    focused = wait_for(
        lambda: cdp.evaluate("document.activeElement.id") or None,
        timeout=15, what="input focus (click landed)")
    check("click focused the real input", focused == "t", focused)

    typed = "hello remor 42"
    r = sess.act({"type": "type", "text": typed})
    check("type executed", r and r[0]["typed_chars"] == len(typed),
          str(r))
    got = wait_for(
        lambda: cdp.evaluate("document.getElementById('t').value")
        or None,
        timeout=15, what="typed value in live DOM")
    check("page received the exact typed string",
          got == typed, repr(got))
    cdp.close()

    # ---- 8. user kill: immediate, target-local --------------------
    target.user_kill(sid, user_token)
    check("indicator off after kill (server-verified)",
          not target.indicator_live())
    try:
        sess.act({"type": "move", "x": 10, "y": 10})
        check("action after kill refused", False, "action was accepted!")
    except Exception as e:
        check("action after user kill refused", "killed" in str(e),
              str(e)[:80])

    # ---- 9. dead session stays dead -------------------------------
    try:
        ctrl.connect(sid, session_token, "127.0.0.1", PORT)
        check("connect to killed session refused", False)
    except Exception as e:
        check("connect to killed session refused", True, str(e)[:70])

    # ---- 10. chain audit ------------------------------------------
    ok, bad = store.audit_chain()
    check("event chain verifies", ok, f"bad={bad}")

    subprocess.run(["pkill", "-x", "chrome"], check=False)

    manifest = {"db": db, "port": PORT, "device_id": "bench-xvfb-1",
                "agent_id": agent_id, "agent_token": agent_token,
                "killed_session": sid}
    with open(os.path.join(HERE, "rd1_manifest.json"), "w") as f:
        json.dump(manifest, f)
    target.stop()

    green = sum(1 for _, c, _ in CHECKS if c)
    print(f"\nRD1 main proof: {green}/{len(CHECKS)} checks green", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
