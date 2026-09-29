"""DISPATCH-SCREEN-STREAM proof battery.

Proves the screen-streaming capability end to end: real X11 frame
capture (XGetImage on a live Xvfb server -- real pixels, verified
color), streaming over the existing TLS session channel, and the
governance properties as first-class results: kill stops the stream
(refused, not frozen), no frames post-expiry, scope-clipping at
capture, no persistence by default, replay guards on frame fetches,
and the Android bridge capture path against the harness (frames
LABELED harness-synthesized in every claim).

Run: python3 proofs/dss_proof.py   (requires Xvfb on :99)
"""
import base64
import io
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("DISPLAY", ":99")
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "pylib")))

from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.governance import caller_authorization as authz  # noqa: E402
from swarm_engine.remote_dispatch import protocol as proto  # noqa: E402
from swarm_engine.remote_dispatch import tls as rd_tls  # noqa: E402
from swarm_engine.remote_dispatch.controller import (  # noqa: E402
    ControllerError, RemoteDispatchController, RemoteSession)
from swarm_engine.remote_dispatch.cursor_android import (  # noqa: E402
    AndroidScreenCapture)
from swarm_engine.remote_dispatch.cursor_x11 import (  # noqa: E402
    X11ScreenCapture)
from swarm_engine.remote_dispatch.session_model import (  # noqa: E402
    RemoteDispatchStore, Scope)
from swarm_engine.remote_dispatch.substrates import (  # noqa: E402
    ANDROID_PROFILE, CursorError, Substrate, SubstrateRefusal,
    X11_PROFILE, resolve_substrate)
from swarm_engine.remote_dispatch.target_agent import (  # noqa: E402
    RemoteDispatchTarget)
from swarm_engine.remote_dispatch.android.harness_bridge import (  # noqa: E402
    HarnessBridge)

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(HERE, "dss_scratch")
PORT = 18471

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), str(detail)[:160]))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}"[:160] if detail else ""))


def decode_png(frame):
    from PIL import Image
    return Image.open(io.BytesIO(base64.b64decode(frame["data_b64"])))


def fresh_pair(scratch, port, profile, scope, bridge=None, ttl_s=600):
    """Pair -> request -> user consent -> connect. Returns dict."""
    db = os.path.join(scratch, "dss.db")
    eng = SwarmEngine(db_path=os.path.join(scratch, "eng.db"))
    agents = authz.AgentDirectory(eng.oracle_registry)
    eng_caller = eng.oracle
    ctrl = RemoteDispatchController(db, agents, eng_caller)
    cert_dir = os.path.join(scratch, "certs")
    cert_path, _ = rd_tls.ensure_cert(cert_dir)
    fp = rd_tls.fingerprint(cert_path)
    pair = ctrl.pair_device("dss-target-1", "DSS target",
                            cert_fingerprint=fp)
    store = RemoteDispatchStore(db)
    user_token = store.register_user("dss-user")
    cfg = {}
    if bridge is not None:
        cfg["bridge_host"] = "127.0.0.1"
        cfg["bridge_port"] = bridge.port
    target = RemoteDispatchTarget(
        db, "dss-target-1", pair["agent_id"], pair["agent_token"],
        host="127.0.0.1", port=port, cert_dir=cert_dir,
        substrate=profile, substrate_config=cfg,
        display=":99" if profile is X11_PROFILE else None)
    target.start()
    req = ctrl.request_session("dss-target-1", scope)
    sid = req["session_id"]
    consent = store.user_grant_consent(sid, user_token, ttl_s=ttl_s)
    sess = ctrl.connect(sid, consent["session_token"], "127.0.0.1", port)
    return {"ctrl": ctrl, "store": store, "target": target, "sess": sess,
            "sid": sid, "user_token": user_token, "db": db,
            "scratch": scratch}


def section_x11_capture():
    print("== A. X11 real capture (live X server) ==")
    from Xlib.display import Display
    from Xlib import X
    d = Display(":99")
    scr = d.screen()
    dw, dh = scr.width_in_pixels, scr.height_in_pixels
    cap = X11ScreenCapture(display=":99")

    f = cap.capture()
    check("A1 full frame has display dims",
          (f["width"], f["height"]) == (dw, dh), f"{f['width']}x{f['height']}")
    check("A2 frame is PNG, not synthesized",
          f["format"] == "png" and f["synthesized"] is False)
    img = decode_png(f)
    check("A3 PNG decodes to RGB image",
          img.size == (dw, dh) and img.mode == "RGB", str(img.size))
    check("A4 frame fits protocol max frame",
          len(base64.b64decode(f["data_b64"])) < proto._MAX_FRAME)

    # color fidelity: real red block -> red pixels in the frame
    w = scr.root.create_window(300, 300, 100, 100, 0, scr.root_depth,
                               X.InputOutput, X.CopyFromParent)
    w.map()
    d.sync()
    red = scr.default_colormap.alloc_named_color("red").pixel
    gc = w.create_gc(foreground=red)
    w.fill_rectangle(gc, 0, 0, 100, 100)
    d.sync()
    fr = cap.capture({"x": 300, "y": 300, "w": 100, "h": 100})
    px = decode_png(fr).getpixel((50, 50))
    check("A5 real pixels, correct color (red block)",
          px[0] > 200 and px[1] < 80 and px[2] < 80, str(px))

    fc = cap.capture({"x": 100, "y": 50, "w": 200, "h": 120})
    check("A6 clip respected at capture",
          (fc["width"], fc["height"]) == (200, 120))
    fd = cap.capture(max_dim=320)
    check("A7 downscale respected",
          max(fd["width"], fd["height"]) <= 320,
          f"{fd['width']}x{fd['height']}")

    # liveness: draw something new, the next frame must differ
    before = cap.capture({"x": 300, "y": 300, "w": 100, "h": 100})
    blue = scr.default_colormap.alloc_named_color("blue").pixel
    gc2 = w.create_gc(foreground=blue)
    w.fill_rectangle(gc2, 0, 0, 100, 100)
    d.sync()
    after = cap.capture({"x": 300, "y": 300, "w": 100, "h": 100})
    check("A8 consecutive frames differ (live, not cached)",
          before["data_b64"] != after["data_b64"])
    cap.close()
    w.destroy()
    d.sync()
    d.close()


def section_stream_e2e():
    print("== B. stream over the real TLS session channel ==")
    scratch = os.path.join(SCRATCH, "e2e")
    os.makedirs(scratch, exist_ok=True)
    scope = Scope(actions=["move"], x_min=0, y_min=0, x_max=1280,
                  y_max=800, max_actions=200, ttl_s=600,
                  screen_share=True)
    p = fresh_pair(scratch, PORT, X11_PROFILE, scope)
    sess, target, store = p["sess"], p["target"], p["store"]

    frames = [sess.get_frame() for _ in range(3)]
    check("B1 three frames stream through the channel",
          all(f["format"] == "png" for f in frames))
    imgs = [decode_png(f) for f in frames]
    check("B2 frames decode, display-sized, real (not synthesized)",
          all(i.size == (1280, 800) and i.mode == "RGB" for i in imgs)
          and all(f["synthesized"] is False for f in frames))
    used = store._get_session(p["sid"])["actions_used"]
    check("B3 frames not charged to the action budget", used == 0,
          f"actions_used={used}")
    # channel still in sync: an action works after frames
    r = sess.act({"type": "move", "x": 500, "y": 400})
    check("B4 action still works after frames (seq in sync)",
          r and r[0]["x"] == 500 and r[0]["y"] == 400)
    sess.end()
    target.stop()


def section_kill_governance():
    print("== C. kill stops the stream (refused, not frozen) ==")
    scratch = os.path.join(SCRATCH, "kill")
    os.makedirs(scratch, exist_ok=True)
    scope = Scope(actions=["move"], screen_share=True, ttl_s=600)
    p = fresh_pair(scratch, PORT + 1, X11_PROFILE, scope)
    sess, target = p["sess"], p["target"]
    f = sess.get_frame()
    check("C1 frame flows while live", f["format"] == "png")

    sess.kill()  # controller-relayed kill
    t0 = time.time()
    refused = 0
    try:
        sess.get_frame()
    except ControllerError as e:
        refused = 1
        reason = str(e)
    dt = time.time() - t0
    check("C2 post-kill frame refused (controller relay)",
          refused == 1 and "refused" in reason, reason[:100])
    check("C3 refusal is prompt, not a frozen hang", dt < 5.0,
          f"{dt:.2f}s")
    target.stop()

    # target-local user kill path
    scratch2 = os.path.join(SCRATCH, "kill2")
    os.makedirs(scratch2, exist_ok=True)
    p2 = fresh_pair(scratch2, PORT + 2, X11_PROFILE, scope)
    sess2, target2 = p2["sess"], p2["target"]
    sess2.get_frame()
    target2.user_kill(p2["sid"], p2["user_token"])
    # raw channel fetch: the session is dead at the target even though
    # this controller object still thinks it is live
    reply = p2["sess"].channel.request("get_frame", {})
    check("C4 target-local user kill stops the stream",
          reply["kind"] == "action_refused",
          f"{reply['kind']}: {reply.get('body', {}).get('reason','')}"[:100])
    target2.stop()

    # burst: zero frames after kill
    scratch3 = os.path.join(SCRATCH, "kill3")
    os.makedirs(scratch3, exist_ok=True)
    p3 = fresh_pair(scratch3, PORT + 3, X11_PROFILE, scope)
    sess3, target3 = p3["sess"], p3["target"]
    sess3.get_frame()
    target3.user_kill(p3["sid"], p3["user_token"])
    n_refused = 0
    for _ in range(5):
        r = p3["sess"].channel.request("get_frame", {})
        if r["kind"] == "action_refused":
            n_refused += 1
    check("C5 burst of 5 post-kill fetches: zero frames, all refused",
          n_refused == 5, f"{n_refused}/5 refused")
    target3.stop()


def section_expiry_and_scope():
    print("== D. expiry + scope governance ==")
    # expired consent: no frames
    scratch = os.path.join(SCRATCH, "expiry")
    os.makedirs(scratch, exist_ok=True)
    scope = Scope(actions=["move"], screen_share=True, ttl_s=600)
    p = fresh_pair(scratch, PORT + 4, X11_PROFILE, scope, ttl_s=1)
    p["sess"].get_frame()
    time.sleep(1.6)
    reply = p["sess"].channel.request("get_frame", {})
    reason = str(reply.get("body", {}).get("reason", ""))
    check("D1 no frames after consent expiry",
          reply["kind"] == "action_refused" and "consent" in reason.lower(),
          f"{reply['kind']}: {reason}"[:100])
    p["target"].stop()

    # scope without the screen_share grant
    scratch2 = os.path.join(SCRATCH, "noscope")
    os.makedirs(scratch2, exist_ok=True)
    scope2 = Scope(actions=["move"], ttl_s=600)  # screen_share=False
    p2 = fresh_pair(scratch2, PORT + 5, X11_PROFILE, scope2)
    reply = p2["sess"].channel.request("get_frame", {})
    reason = str(reply.get("body", {}).get("reason", ""))
    check("D2 frame without screen_share grant refused explicitly",
          reply["kind"] == "action_refused" and "screen" in reason.lower(),
          f"{reply['kind']}: {reason}"[:100])
    p2["target"].stop()

    # scope-clipping at capture: 100x100 scope -> 100x100 frame
    scratch3 = os.path.join(SCRATCH, "clip")
    os.makedirs(scratch3, exist_ok=True)
    scope3 = Scope(actions=["move"], x_min=400, y_min=300, x_max=500,
                   y_max=400, screen_share=True, ttl_s=600)
    p3 = fresh_pair(scratch3, PORT + 6, X11_PROFILE, scope3)
    f = p3["sess"].get_frame()
    check("D3 scope-clipping at capture (100x100, never full frame)",
          (f["width"], f["height"]) == (100, 100),
          f"{f['width']}x{f['height']}")
    p3["target"].stop()


def section_persistence():
    print("== E. no persistence by default ==")
    scratch = os.path.join(SCRATCH, "persist")
    os.makedirs(scratch, exist_ok=True)
    scope = Scope(actions=["move"], screen_share=True, ttl_s=600)
    p = fresh_pair(scratch, PORT + 7, X11_PROFILE, scope)
    sess, target = p["sess"], p["target"]
    f = sess.get_frame()
    try:
        sess.save_frame(f, os.path.join(scratch, "frame.png"))
        saved = True
    except ControllerError as e:
        saved, reason = False, str(e)
    check("E1 save_frame refused without record_frames grant",
          saved is False and "record_frames" in reason, reason[:100])
    # target never writes frames itself
    pngs = [fn for fn in os.listdir(scratch)
            if fn.endswith((".png", ".jpg"))]
    check("E2 target persisted zero frames", pngs == [], str(pngs))
    sess.end()
    target.stop()

    # with the grant, the sanctioned path works
    scratch2 = os.path.join(SCRATCH, "persist2")
    os.makedirs(scratch2, exist_ok=True)
    scope2 = Scope(actions=["move"], screen_share=True,
                   record_frames=True, ttl_s=600)
    p2 = fresh_pair(scratch2, PORT + 8, X11_PROFILE, scope2)
    f2 = p2["sess"].get_frame()
    out = p2["sess"].save_frame(f2, os.path.join(scratch2, "frame.png"))
    ok = os.path.exists(out) and decode_png(f2).size == (1280, 800)
    check("E3 save_frame works WITH the record_frames grant", ok)
    p2["sess"].end()
    p2["target"].stop()


def section_replay():
    print("== F. replay guards on frame fetches ==")
    scratch = os.path.join(SCRATCH, "replay")
    os.makedirs(scratch, exist_ok=True)
    scope = Scope(actions=["move"], screen_share=True, ttl_s=600)
    p = fresh_pair(scratch, PORT + 9, X11_PROFILE, scope)
    sess = p["sess"]
    ch = sess.channel
    # legitimate fetch consumes seq N; replay the same seq manually
    import secrets as _secrets
    legit_seq = ch._seq + 1
    proto.send(ch._sock, "get_frame", sess.session_id, legit_seq,
               {"nonce": _secrets.token_hex(16)})
    # NOTE: proto.send builds its own nonce; emulate exactly:
    reply = proto.decode(ch._sock)
    check("F1 manual frame fetch ok", reply["kind"] == "frame_ok")
    ch._seq = legit_seq
    # replay the identical seq again -> refused
    msg = {"v": 1, "kind": "get_frame", "session_id": sess.session_id,
           "seq": legit_seq, "nonce": _secrets.token_hex(16), "body": {}}
    import json as _json, struct as _struct
    raw = _json.dumps(msg).encode()
    ch._sock.sendall(_struct.pack(">I", len(raw)) + raw)
    reply2 = proto.decode(ch._sock)
    check("F2 duplicate seq on get_frame refused",
          reply2["kind"] == "action_refused",
          f"{reply2['kind']}: {reply2.get('body', {}).get('reason','')}"[:100])
    # channel still usable afterwards at the next seq
    f = sess.get_frame()
    check("F3 stream continues after refused replay",
          f["format"] == "png")
    sess.end()
    p["target"].stop()


def section_android_harness():
    print("== G. Android bridge capture path (HARNESS-SYNTHESIZED) ==")
    hb = HarnessBridge(width=1080, height=2400)
    try:
        cap = AndroidScreenCapture(host="127.0.0.1", port=hb.port)
        f = cap.capture()
        check("G1 harness frame labeled synthesized",
              f.get("synthesized") is True, f"synth={f.get('synthesized')}")
        img = decode_png(f)
        check("G2 harness PNG decodes", img.mode == "RGB",
              f"{f['width']}x{f['height']}")
        fc = cap.capture({"x": 10, "y": 20, "w": 100, "h": 100})
        check("G3 harness honors clip",
              (fc["width"], fc["height"]) == (100, 100))
        f1b, f2b = cap.capture(), cap.capture()
        check("G4 harness frames advance (not a still)",
              f1b["data_b64"] != f2b["data_b64"])
        cap.close()

        # kill latch gates capture like actuators
        hb.latch_kill("sess_x")
        cap2 = AndroidScreenCapture(host="127.0.0.1", port=hb.port)
        try:
            cap2.capture()
            latched = False
        except SubstrateRefusal as e:
            latched = "KILL_LATCHED" in str(e)
        check("G5 latched kill refuses capture (KILL_LATCHED)",
              latched is True)
        cap2.close()
    finally:
        hb.stop()

    # full e2e Android: target + harness -> labeled frame on the stream
    hb2 = HarnessBridge(width=1080, height=2400)
    try:
        scratch = os.path.join(SCRATCH, "android")
        os.makedirs(scratch, exist_ok=True)
        scope = Scope(actions=["click"], screen_share=True, ttl_s=600)
        p = fresh_pair(scratch, PORT + 10, ANDROID_PROFILE, scope,
                       bridge=hb2)
        f = p["sess"].get_frame()
        check("G6 android-target stream frame, labeled synthesized",
              f.get("synthesized") is True and f["format"] == "png")
        decode_png(f)  # raises if not a real PNG
        check("G7 android frame PNG decodes", True)
        p["sess"].end()
        p["target"].stop()
    finally:
        hb2.stop()


def section_failclosed():
    print("== H. fail-closed paths ==")
    # a substrate with no capture path refuses the stream explicitly
    class NoCapture(Substrate):
        profile = X11_PROFILE

        def make_cursor(self, **c):
            raise CursorError("n/a")

        def make_indicator(self, **c):
            raise CursorError("n/a")

    try:
        NoCapture().make_screen_capture()
        dflt = False
    except CursorError as e:
        dflt = "unsupported" in str(e)
    check("H1 default make_screen_capture fails closed", dflt)

    # resolve_substrate still pair-keyed (no identity branching)
    a = resolve_substrate(ANDROID_PROFILE)
    x = resolve_substrate(X11_PROFILE)
    check("H2 substrate resolution unchanged",
          type(a).__name__ == "AndroidSubstrate"
          and type(x).__name__ == "X11Substrate")


def section_action_regression():
    print("== I. action-path regression (refactor safety) ==")
    scratch = os.path.join(SCRATCH, "reg")
    os.makedirs(scratch, exist_ok=True)
    scope = Scope(actions=["move", "click"], screen_share=True,
                  ttl_s=600)
    p = fresh_pair(scratch, PORT + 11, X11_PROFILE, scope)
    sess, target = p["sess"], p["target"]
    r = sess.act({"type": "move", "x": 500, "y": 400})
    check("I1 move still works after enforcement refactor",
          r and r[0]["x"] == 500 and r[0]["y"] == 400)
    sess.get_frame()
    r = sess.act({"type": "click", "x": 500, "y": 400})
    check("I2 click works interleaved with frames",
          r and "x" in r[0])
    # scope refusal on actions still explicit
    try:
        sess.act({"type": "launch_app", "argv": ["x"], "app": "x"})
        ref = False
    except ControllerError as e:
        ref = "not in session scope" in str(e) or "refused" in str(e)
    check("I3 out-of-scope action still refused", ref)
    sess.end()
    target.stop()


def main():
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH, exist_ok=True)
    t0 = time.time()
    try:
        section_x11_capture()
        section_stream_e2e()
        section_kill_governance()
        section_expiry_and_scope()
        section_persistence()
        section_replay()
        section_android_harness()
        section_failclosed()
        section_action_regression()
    finally:
        pass
    npass = sum(1 for _, c, _ in CHECKS if c)
    nfail = len(CHECKS) - npass
    print(f"\n==== DSS battery: {npass}/{len(CHECKS)} pass, "
          f"{nfail} fail ({time.time()-t0:.1f}s) ====")
    for name, cond, detail in CHECKS:
        if not cond:
            print(f"  FAILED: {name} -- {detail}")
    shutil.rmtree(SCRATCH, ignore_errors=True)
    sys.exit(0 if nfail == 0 else 1)


if __name__ == "__main__":
    main()
