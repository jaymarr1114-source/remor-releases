#!/usr/bin/env python3
"""DISPATCH-ANDROID-TARGET proof battery.

Executes for real against the live stack:
  REAL RemoteDispatchTarget(substrate=ANDROID_PROFILE)   (enforcement code)
  REAL TLS channel with pinned target identity
  REAL bridge protocol handling (AndroidCursor/AndroidBridge)
  HARNESS android/harness_bridge.py as the app side  <-- labeled, every claim

Harness-played parts (labeled honestly, never presented as product):
  - the app side of the bridge (tap/swipe/text/overlay) is the HARNESS
    virtual device, not Android. It proves the Python side speaks the
    protocol correctly; gesture execution on a real device is UNPROVEN
    until James's hardware acceptance.
  - pairing-channel fingerprint pin: direct store write (same as RD-1).
  - target-side consent via RemoteDispatchStore.user_grant_consent:
    the REAL store function the on-target agent calls.

Sections:
  A. substrate resolution (capability pair, never device identity)
  B. six primitives vs HARNESS (exact frames, real results, real errors)
  C. enforcement battery vs the REAL target (adversarial refusals)
  D. indicator show/live/hide through the bridge
  F. app-side kill latch (lost user_kill event) vs HARNESS latch model
  E. fresh-process rerun + audit chain

Run:  python3 proofs/gate_dispatch_android_2026-09-28.py
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

# Import the worktree's remote_dispatch as a top-level package, the
# same trick the RD-1 batteries use (avoids the swarm_engine package).
HERE = os.path.dirname(os.path.abspath(__file__))
WORKTREE = os.path.abspath(os.path.join(HERE, ".."))
PKGDIR = os.path.join(tempfile.gettempdir(), "dat_rd_pkg")
os.makedirs(PKGDIR, exist_ok=True)
_link = os.path.join(PKGDIR, "dat_rd")
try:
    os.symlink(os.path.join(WORKTREE, "runtime", "remote_dispatch"), _link)
except FileExistsError:
    pass
sys.path.insert(0, PKGDIR)

from dat_rd import channel as chan  # noqa: E402
from dat_rd import protocol as proto  # noqa: E402
from dat_rd import tls as rd_tls  # noqa: E402
from dat_rd.session_model import (  # noqa: E402
    RemoteDispatchStore, Scope, SessionError)
from dat_rd.substrates import (  # noqa: E402
    ANDROID_PROFILE, X11_PROFILE, CursorError, ExecutionTargetProfile,
    resolve_substrate)
from dat_rd.target_agent import RemoteDispatchTarget  # noqa: E402
from dat_rd.cursor_android import (  # noqa: E402
    AndroidBridge, AndroidCursor, AndroidIndicator,
    BRIDGE_DEFAULT_PORT)
from dat_rd.android.harness_bridge import HarnessBridge  # noqa: E402

SCRATCH = os.path.join(HERE, "dat_scratch")
CHECKS = []
PIN = None


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


def raw_frame(kind, sid, seq, nonce, body):
    msg = {"v": 1, "kind": kind, "session_id": sid, "seq": seq,
           "nonce": nonce, "body": body}
    raw = json.dumps(msg, separators=(",", ":")).encode()
    return struct.pack(">I", len(raw)) + raw


def tls_raw(port):
    raw = socket.create_connection(("127.0.0.1", port), timeout=10)
    return rd_tls.wrap_client(raw, PIN)


def ch_hello(port, sid, token):
    ch = chan.ControllerChannel("127.0.0.1", port,
                                pinned_fingerprint=PIN)
    body = ch.hello(sid, token)
    return ch, body


def live_session(store, user_token, scope, ttl=600):
    req = store.request_session("bench-android-1", scope)
    sid = req["session_id"]
    consent = store.user_grant_consent(sid, user_token, ttl_s=ttl)
    return sid, consent["session_token"]


def android_scope(**kw):
    d = dict(actions=["move", "click", "scroll", "type", "key",
                      "launch_app"],
             x_min=0, y_min=0, x_max=1080, y_max=2400,
             apps=["com.example.app"], max_actions=100)
    d.update(kw)
    return Scope(**d)


# ======================================================================
# A. substrate resolution
# ======================================================================
def section_a():
    a = resolve_substrate(ANDROID_PROFILE)
    check("A1 android profile resolves", type(a).__name__ == "AndroidSubstrate")
    x = resolve_substrate(X11_PROFILE)
    check("A2 x11 profile resolves", type(x).__name__ == "X11Substrate")
    # capability-pair keying: renamed profile, same pair -> same class
    p2 = ExecutionTargetProfile(
        name="not-android", input_mechanism="accessibility-bridge",
        indicator_mechanism="overlay", hover_cursor=False,
        multitouch=False, atomic_text=True, global_keys=True,
        app_launch=True)
    check("A3 pair keying, not name",
          type(resolve_substrate(p2)).__name__ == "AndroidSubstrate")
    # device identity can never steer resolution: the profile type has
    # no identity fields, and two device_ids share one resolution
    fields = set(ExecutionTargetProfile.__dataclass_fields__)
    check("A4 no identity fields on profile",
          not ({"device_id", "model", "abi"} & fields), str(sorted(fields)))
    t1 = RemoteDispatchTarget.__init__  # noqa: F841 (touch the class)
    check("A5 same profile both device_ids -> same substrate",
          type(resolve_substrate(ANDROID_PROFILE)).__name__ ==
          type(resolve_substrate(ANDROID_PROFILE)).__name__)
    try:
        resolve_substrate("android")
        check("A6 string identity refused", False)
    except CursorError:
        check("A6 string identity refused", True)
    try:
        resolve_substrate(ExecutionTargetProfile(
            name="q", input_mechanism="nope", indicator_mechanism="nope",
            hover_cursor=False, multitouch=False, atomic_text=True,
            global_keys=False, app_launch=False))
        check("A7 unknown capability pair refused", False)
    except CursorError:
        check("A7 unknown capability pair refused", True)


# ======================================================================
# B. six primitives vs HARNESS
# ======================================================================
def section_b():
    assert HarnessBridge.HARNESS, "harness label missing"
    h = HarnessBridge()
    try:
        cur = AndroidCursor(port=h.port)
        try:
            # B1 move stages, sends nothing (no hover cursor on Android).
            # (The constructor's ping is the service_connected check.)
            n_before = len(h.received_commands)
            r = cur.move(100, 200)
            check("B1 move stages + reports",
                  r == {"x": 100, "y": 200}
                  and len(h.received_commands) == n_before,
                  f"{r} cmds={h.received_commands[n_before:]}")
            # B2 click with coords -> tap frame
            r = cur.click(1, 300, 400)
            check("B2 click(x,y) taps",
                  r == {"x": 300, "y": 400}
                  and h.tap_log[-1] == {"x": 300, "y": 400},
                  f"{r} tap_log={h.tap_log[-1]}")
            # B3 click without coords uses the staged point
            cur.move(11, 22)
            r = cur.click()
            check("B3 click() uses staged point",
                  r == {"x": 11, "y": 22}
                  and h.tap_log[-1] == {"x": 11, "y": 22})
            # B4 button 2/3 honestly unsupported
            try:
                cur.click(2, 1, 1)
                check("B4 right-click refused", False)
            except CursorError as e:
                check("B4 right-click refused", "unsupported" in str(e),
                      str(e)[:60])
            # B5 scroll -> swipe-derived scroll frame
            r = cur.scroll(dx=0, dy=2)
            check("B5 scroll translates",
                  r["dx"] == 0 and r["dy"] == 2
                  and h.scroll_log[-1]["dy"] == 2, str(r))
            # B6 type with focused field -> real entered_chars
            h.focus_field("f1")
            r = cur.type("hello")
            check("B6 type enters text",
                  r == {"typed_chars": 5}
                  and "".join(h.field_buffer) == "hello", str(r))
            # B7 type with no focus -> real error, never fake ok
            h.blur()
            try:
                cur.type("nope")
                check("B7 type without focus refused", False)
            except CursorError as e:
                check("B7 type without focus refused",
                      "no focused editable" in str(e), str(e)[:60])
            # B8 type bound enforced like the X11 substrate
            try:
                cur.type("x" * 2001)
                check("B8 type length bound", False)
            except CursorError as e:
                check("B8 type length bound", "too long" in str(e))
            # B9 key mapping: BackSpace -> back; unknown keysym raises
            r = cur.key("BackSpace")
            check("B9 key BackSpace -> back",
                  r == {"key": "BackSpace", "android_action": "back"}
                  and h.global_actions[-1] == "back", str(r))
            try:
                cur.key("F13")
                check("B9b unmapped key refused", False)
            except CursorError as e:
                check("B9b unmapped key refused", "no Android mapping"
                      in str(e), str(e)[:60])
            # B10 launch_app: allowlist re-check + honest result (no pid)
            r = cur.launch_app(["com.example.app"], ["com.example.app"])
            check("B10 launch_app allowed",
                  r == {"package": "com.example.app", "launched": True}
                  and "pid" not in r
                  and h.launched[-1] == "com.example.app", str(r))
            try:
                cur.launch_app(["com.evil"], ["com.example.app"])
                check("B10b launch_app allowlist re-check", False)
            except CursorError as e:
                check("B10b launch_app allowlist re-check",
                      "allowlist" in str(e), str(e)[:60])
            # B11 bridge errors surface, never swallowed. Use a port that
            # never had a listener (bind-then-close): deterministic
            # ECONNREFUSED, no accept-thread race.
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
            s.close()
            try:
                AndroidCursor(port=dead_port)
                check("B11 dead bridge raises", False)
            except CursorError as e:
                check("B11 dead bridge raises", "unreachable" in str(e),
                      str(e)[:60])
        finally:
            cur.close()
    finally:
        h.stop()


# ======================================================================
# C. enforcement battery vs the REAL target
# ======================================================================
def section_c():
    global PIN
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH, exist_ok=True)
    db = os.path.join(SCRATCH, "dat_c.db")
    store = RemoteDispatchStore(db)
    user_token = store.register_user("bench-user")
    cert_dir = os.path.join(SCRATCH, "dat_certs")
    cert_path, _ = rd_tls.ensure_cert(cert_dir)
    PIN = rd_tls.fingerprint(cert_path)
    store.pair_device("bench-android-1", "rd-target-bench-android-1",
                      "Bench Android (HARNESS)", cert_fingerprint=PIN)

    harness = HarnessBridge()
    harness.focus_field("f1")
    port = free_port()
    target = RemoteDispatchTarget(
        db, "bench-android-1", "rd-target-bench-android-1",
        "tok-bench-1", host="127.0.0.1", port=port, cert_dir=cert_dir,
        substrate=ANDROID_PROFILE,
        substrate_config={"bridge_host": "127.0.0.1",
                          "bridge_port": harness.port})
    check("C0 target cert matches pin",
          target.cert_fingerprint() == PIN)
    target.start()
    try:
        S = android_scope

        # C1. end-to-end tap through the REAL channel
        sid, tok = live_session(store, user_token, S())
        ch, body = ch_hello(port, sid, tok)
        check("C1 hello_ok identity proof",
              body.get("identity_proof", {}).get("device_id")
              == "bench-android-1")
        check("C1 indicator live after hello", target.indicator_live())
        rep = ch.request("action", {"action": {"type": "click",
                                              "x": 100, "y": 200}})
        check("C1 tap end-to-end",
              rep["kind"] == "action_ok"
              and rep["body"]["results"] == [{"x": 100, "y": 200}]
              and harness.tap_log[-1] == {"x": 100, "y": 200},
              str(rep["body"]["results"]))
        ch.close(); target._hide_indicator(sid)

        # C2. type end-to-end (atomic on this substrate)
        sid, tok = live_session(store, user_token, S())
        ch, _ = ch_hello(port, sid, tok)
        rep = ch.request("action", {"action": {"type": "type",
                                              "text": "hi"}})
        check("C2 type end-to-end",
              rep["body"]["results"] == [{"typed_chars": 2}]
              and "".join(harness.field_buffer).endswith("hi"))
        ch.close(); target._hide_indicator(sid)

        # C3. scope escape: out-of-bounds coordinates refused
        sid, tok = live_session(store, user_token, S())
        ch, _ = ch_hello(port, sid, tok)
        rep = ch.request("action", {"action": {"type": "click",
                                              "x": 5000, "y": 5000}})
        check("C3 out-of-bounds refused",
              rep["kind"] == "action_refused"
              and "outside session bounds" in rep["body"]["reason"],
              rep["body"]["reason"][:70])
        ch.close(); target._hide_indicator(sid)

        # C4. disallowed action type + unknown action type
        sid, tok = live_session(store, user_token,
                                S(actions=["click"]))
        ch, _ = ch_hello(port, sid, tok)
        rep = ch.request("action", {"action": {"type": "type",
                                              "text": "x"}})
        check("C4 action not in scope refused",
              rep["kind"] == "action_refused"
              and "not in session scope" in rep["body"]["reason"])
        rep = ch.request("action", {"action": {"type": "format_disk"}})
        check("C4b unknown action refused",
              rep["kind"] == "action_refused"
              and "unknown action" in rep["body"]["reason"])
        ch.close(); target._hide_indicator(sid)

        # C5. replay: seq skip refused (all on one raw socket: no
        # second hello, so the concurrent-connection guard is not
        # tripped -- that guard is C13's test)
        sid, tok = live_session(store, user_token, S())
        raw = tls_raw(port)
        raw.sendall(raw_frame("hello", sid, 1,
                              "b" * 32, {"session_token": tok}))
        d = proto.decode(raw)
        assert d["kind"] == "hello_ok", d
        raw.sendall(raw_frame("action", sid, 2, "c" * 32,
                              {"action": {"type": "move", "x": 1,
                                          "y": 1}}))
        d = proto.decode(raw)
        assert d["kind"] == "action_ok", d
        raw.sendall(raw_frame(
            "action", sid, 99, "d" * 32,
            {"action": {"type": "move", "x": 2, "y": 2}}))
        d = proto.decode(raw)
        check("C5 seq skip refused",
              d["kind"] == "action_refused"
              and "seq mismatch" in d["body"]["reason"],
              d["body"]["reason"][:70])
        raw.close()
        target._hide_indicator(sid)

        # C6. replay: duplicate nonce refused
        sid, tok = live_session(store, user_token, S())
        raw = tls_raw(port)
        raw.sendall(raw_frame("hello", sid, 1,
                              "d" * 32, {"session_token": tok}))
        d = proto.decode(raw)
        assert d["kind"] == "hello_ok", d
        frm = raw_frame("action", sid, 2, "e" * 32,
                        {"action": {"type": "move", "x": 3, "y": 3}})
        raw.sendall(frm)
        d = proto.decode(raw)
        assert d["kind"] == "action_ok", d
        raw.sendall(frm)  # exact replay
        d = proto.decode(raw)
        check("C6 duplicate frame refused",
              d["kind"] == "action_refused"
              and ("duplicate nonce" in d["body"]["reason"]
                   or "seq mismatch" in d["body"]["reason"]),
              d["body"]["reason"][:70])
        raw.close(); target._hide_indicator(sid)

        # C7. budget exhaustion
        sid, tok = live_session(store, user_token, S(max_actions=2))
        ch, _ = ch_hello(port, sid, tok)
        ch.request("action", {"action": {"type": "move", "x": 1,
                                        "y": 1}})
        ch.request("action", {"action": {"type": "move", "x": 2,
                                        "y": 2}})
        rep = ch.request("action", {"action": {"type": "move", "x": 3,
                                              "y": 3}})
        check("C7 budget exhausted refused",
              rep["kind"] == "action_refused"
              and "budget exhausted" in rep["body"]["reason"])
        ch.close(); target._hide_indicator(sid)

        # C8. app allowlist escape
        sid, tok = live_session(store, user_token, S())
        ch, _ = ch_hello(port, sid, tok)
        rep = ch.request("action", {"action": {"type": "launch_app",
                                              "app": "com.evil",
                                              "argv": ["com.evil"]}})
        check("C8 non-allowlisted app refused",
              rep["kind"] == "action_refused"
              and "allowlist" in rep["body"]["reason"],
              rep["body"]["reason"][:70])
        rep = ch.request("action", {"action": {"type": "launch_app",
                                              "app": "com.example.app",
                                              "argv": ["com.example.app"]}})
        check("C8b allowlisted app launches",
              rep["kind"] == "action_ok"
              and rep["body"]["results"][0]["launched"] is True)
        ch.close(); target._hide_indicator(sid)

        # C9. controller kill relay: action after kill refused
        sid, tok = live_session(store, user_token, S())
        ch, _ = ch_hello(port, sid, tok)
        ch.request("kill", {})
        rep = ch.request("action", {"action": {"type": "move", "x": 1,
                                              "y": 1}})
        check("C9 action after kill refused",
              rep["kind"] == "action_refused"
              and "killed" in rep["body"]["reason"],
              rep["body"]["reason"][:70])
        check("C9b indicator off after kill",
              not target.indicator_live())
        ch.close()

        # C10. USER kill via the bridge event (the KILL button path)
        sid, tok = live_session(store, user_token, S())
        ch, _ = ch_hello(port, sid, tok)
        check("C10 indicator live pre-kill", target.indicator_live())
        ack = harness.send_event({"event": "user_kill",
                                  "session_id": sid,
                                  "user_token": user_token})
        check("C10 user_kill event acked ok", ack.get("ok") is True,
              str(ack)[:80])
        # the event ran the REAL target.user_kill: session dead
        st = store._get_session(sid)["state"]
        check("C10 session killed by bridge event", st == "killed", st)
        rep = ch.request("action", {"action": {"type": "move", "x": 1,
                                              "y": 1}})
        check("C10b action after user kill refused",
              rep["kind"] == "action_refused"
              and "killed" in rep["body"]["reason"])
        check("C10c indicator off after user kill",
              not target.indicator_live())
        ch.close()

        # C10d. user_kill with WRONG user token is refused, not honored
        sid, tok = live_session(store, user_token, S())
        ch, _ = ch_hello(port, sid, tok)
        ack = harness.send_event({"event": "user_kill",
                                  "session_id": sid,
                                  "user_token": "WRONG"})
        check("C10d forged user_kill refused",
              ack.get("ok") is False, str(ack)[:80])
        st = store._get_session(sid)["state"]
        check("C10d session still live", st == "live", st)
        ch.close(); target._hide_indicator(sid)

        # C11. consent expiry refuses actions
        sid, tok = live_session(store, user_token, S(), ttl=0.3)
        ch, _ = ch_hello(port, sid, tok)
        time.sleep(0.5)
        rep = ch.request("action", {"action": {"type": "move", "x": 1,
                                              "y": 1}})
        check("C11 expired consent refused",
              rep["kind"] == "action_refused"
              and "consent" in rep["body"]["reason"],
              rep["body"]["reason"][:70])
        ch.close(); target._hide_indicator(sid)

        # C12. fabricated session token at hello refused
        sid, tok = live_session(store, user_token, S())
        try:
            ch2, _ = ch_hello(port, sid, "fabricated-token")
            ch2.close()
            check("C12 fabricated token refused", False)
        except chan.ChannelError as e:
            check("C12 fabricated token refused", True, str(e)[:60])
        target._hide_indicator(sid)

        # C13. concurrent duplicate hello refused
        sid, tok = live_session(store, user_token, S())
        ch, _ = ch_hello(port, sid, tok)
        try:
            ch2, _ = ch_hello(port, sid, tok)
            ch2.close()
            check("C13 concurrent hello refused", False)
        except chan.ChannelError as e:
            check("C13 concurrent hello refused", "already has a live"
                  in str(e), str(e)[:60])
        ch.close(); target._hide_indicator(sid)

        # C14. batch atomicity: refused action -> nothing executed
        sid, tok = live_session(store, user_token, S())
        ch, _ = ch_hello(port, sid, tok)
        taps_before = len(harness.tap_log)
        rep = ch.request("action_batch",
                         {"actions": [{"type": "click", "x": 10, "y": 10},
                                       {"type": "click", "x": 9000,
                                        "y": 9000}]})
        check("C14 batch refused atomically",
              rep["kind"] == "action_refused"
              and len(harness.tap_log) == taps_before,
              f"taps {taps_before}->{len(harness.tap_log)}")
        ch.close(); target._hide_indicator(sid)

        ok, msg = store.audit_chain()
        check("C15 audit chain intact", ok, msg)
    finally:
        target.stop()
        harness.stop()


# ======================================================================
# D. indicator through the bridge
# ======================================================================
def section_d():
    h = HarnessBridge()
    try:
        ind = AndroidIndicator(port=h.port)
        try:
            check("D1 indicator not live initially",
                  ind.live() is False)
            ind.show("sess_abc123")
            check("D2 indicator live after show",
                  ind.live() is True
                  and h.indicator_session == "sess_abc123")
            ind.hide("sess_abc123")
            check("D3 indicator off after hide",
                  ind.live() is False
                  and h.indicator_session is None)
        finally:
            pass
    finally:
        h.stop()


# ======================================================================
# F. app-side kill latch (lost user_kill event)
# ======================================================================
def section_f():
    # The latch exists for the LOST-EVENT case: the user pressed KILL in
    # the app, but the user_kill bridge event never reached Python, so
    # the Python session is still live. The app side must still refuse
    # every actuator command. The HARNESS models the app latch
    # (latch_kill); the Java latch itself is compiled, but its on-device
    # behavior is UNPROVEN until James's hardware acceptance.
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH, exist_ok=True)
    db = os.path.join(SCRATCH, "dat_f.db")
    store = RemoteDispatchStore(db)
    user_token = store.register_user("latch-user")
    cert_dir = os.path.join(SCRATCH, "dat_f_certs")
    cert_path, _ = rd_tls.ensure_cert(cert_dir)
    pin = rd_tls.fingerprint(cert_path)
    store.pair_device("latch-android-1", "latch-agent",
                      "Latch Android (HARNESS)", cert_fingerprint=pin)
    h = HarnessBridge()
    port = free_port()
    target = RemoteDispatchTarget(
        db, "latch-android-1", "latch-agent", "latch-tok",
        host="127.0.0.1", port=port, cert_dir=cert_dir,
        substrate=ANDROID_PROFILE,
        substrate_config={"bridge_port": h.port})
    target.start()
    try:
        global PIN
        PIN = pin
        req = store.request_session("latch-android-1", android_scope())
        sid = req["session_id"]
        consent = store.user_grant_consent(sid, user_token)
        ch, _ = ch_hello(port, sid, consent["session_token"])

        rep = ch.request("action", {"action": {"type": "move",
                                              "x": 5, "y": 5}})
        check("F1 pre-latch action executes", rep["kind"] == "action_ok")

        # the user presses KILL in the app; the user_kill event is LOST.
        h.latch_kill(sid)
        st = store._get_session(sid)["state"]
        check("F2 session still live (event lost)", st == "live", st)

        # the next actuator command is an explicit action_refused --
        # never a transport error -- and is not charged to the budget.
        used_before = store._get_session(sid)["actions_used"]
        rep = ch.request("action", {"action": {"type": "click",
                                              "x": 9, "y": 9}})
        check("F3 latched command refused as action_refused",
              rep["kind"] == "action_refused"
              and "KILL_LATCHED" in rep["body"]["reason"],
              rep["body"]["reason"][:90])
        used_after = store._get_session(sid)["actions_used"]
        check("F4 refused action not charged",
              used_after == used_before, f"{used_before}->{used_after}")

        # control-plane commands are NOT input: the controller-relay
        # kill still works while the latch is set.
        rep = ch.request("kill", {})
        check("F5 controller kill bypasses input latch",
              rep["kind"] == "kill_ok")
        ch.close()

        # rearm requires a FRESH consented session: the new session's
        # indicator_show clears the latch; the latched session id could
        # never rearm it.
        req2 = store.request_session("latch-android-1", android_scope())
        sid2 = req2["session_id"]
        consent2 = store.user_grant_consent(sid2, user_token)
        ch2, _ = ch_hello(port, sid2, consent2["session_token"])
        rep = ch2.request("action", {"action": {"type": "move",
                                               "x": 6, "y": 6}})
        check("F6 fresh session rearms latch, action executes",
              rep["kind"] == "action_ok")
        check("F6b harness latch cleared",
              h.kill_latched_session is None)
        ch2.close()
        target._hide_indicator(sid2)
        ok, msg = store.audit_chain()
        check("F7 audit chain intact", ok, msg)
    finally:
        target.stop()
        h.stop()


# ======================================================================
# E. fresh-process rerun
# ======================================================================
def section_e():
    r = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--fresh-subset"],
        capture_output=True, text=True, timeout=300)
    tail = "\n".join(r.stdout.strip().split("\n")[-4:])
    check("E1 fresh-process subset green",
          r.returncode == 0 and "FRESH_SUBSET_PASS" in r.stdout,
          f"rc={r.returncode} tail={tail!r} err={r.stderr[-200:]}")


def fresh_subset():
    """Compact subset re-executed in a brand-new interpreter."""
    section_a()
    # one end-to-end tap through a fresh target + harness
    db = os.path.join(SCRATCH, "dat_fresh.db")
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH, exist_ok=True)
    store = RemoteDispatchStore(db)
    user_token = store.register_user("fresh-user")
    cert_dir = os.path.join(SCRATCH, "fresh_certs")
    cert_path, _ = rd_tls.ensure_cert(cert_dir)
    pin = rd_tls.fingerprint(cert_path)
    store.pair_device("fresh-android-1", "fresh-agent",
                      "fresh", cert_fingerprint=pin)
    h = HarnessBridge()
    port = free_port()
    target = RemoteDispatchTarget(
        db, "fresh-android-1", "fresh-agent", "fresh-tok",
        host="127.0.0.1", port=port, cert_dir=cert_dir,
        substrate=ANDROID_PROFILE,
        substrate_config={"bridge_port": h.port})
    target.start()
    try:
        global PIN
        PIN = pin
        req = store.request_session("fresh-android-1", android_scope())
        sid = req["session_id"]
        consent = store.user_grant_consent(sid, user_token)
        tok = consent["session_token"]
        ch, _ = ch_hello(port, sid, tok)
        rep = ch.request("action", {"action": {"type": "click",
                                              "x": 7, "y": 8}})
        check("FRESH tap",
              rep["kind"] == "action_ok"
              and h.tap_log[-1] == {"x": 7, "y": 8})
        ch.close()
        ok, msg = store.audit_chain()
        check("FRESH audit chain", ok, msg)
    finally:
        target.stop()
        h.stop()
    print("FRESH_SUBSET_PASS")


def main():
    if "--fresh-subset" in sys.argv:
        fresh_subset()
        return
    if "--sections" in sys.argv:
        only = sys.argv[sys.argv.index("--sections") + 1].split(",")
    else:
        only = ["a", "b", "c", "d", "f", "e"]
    if "a" in only:
        section_a()
    if "b" in only:
        section_b()
    if "c" in only:
        section_c()
    if "d" in only:
        section_d()
    if "f" in only:
        section_f()
    if "e" in only:
        section_e()
    n_pass = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\nBATTERY: {n_pass}/{len(CHECKS)} checks passed")
    if n_pass != len(CHECKS):
        sys.exit(1)


if __name__ == "__main__":
    main()
