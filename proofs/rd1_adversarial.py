"""REMOTE-DISPATCH-1 adversarial battery.

Every case is executed for real against the live target; each asserts a
REFUSAL (fail-closed), never a skipped check. The channel is TLS with
pinned target identity; adversarial cases include TLS-level attacks
(wrong pin, unencrypted connection). Run after rd1_proof.py:
    python3 proofs/rd1_adversarial.py
"""
import json
import os
import socket
import sqlite3
import struct
import sys
import tempfile
import threading
import time

os.environ.setdefault("DISPLAY", ":99")
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "pylib")))

from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.governance import caller_authorization as authz  # noqa: E402
from swarm_engine.remote_dispatch import channel as chan  # noqa: E402
from swarm_engine.remote_dispatch import protocol as proto  # noqa: E402
from swarm_engine.remote_dispatch import tls as rd_tls  # noqa: E402
from swarm_engine.remote_dispatch.controller import (  # noqa: E402
    RemoteDispatchController)
from swarm_engine.remote_dispatch.session_model import (  # noqa: E402
    RemoteDispatchStore, Scope, SessionError)
from swarm_engine.remote_dispatch.target_agent import (  # noqa: E402
    RemoteDispatchTarget)

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(HERE, "rd1_adv_scratch")
PORT = 18432
CHROME = "/opt/meta-chromium/chrome"

# The pinned target identity, set in main() after the cert ceremony.
PIN = None


def tls_channel(host="127.0.0.1", port=PORT, pin=None):
    """A controller channel with the pinned target identity."""
    return chan.ControllerChannel(host, port,
                                  pinned_fingerprint=pin or PIN)


def tls_raw_socket(host="127.0.0.1", port=PORT, pin=None):
    """A raw TLS socket (for protocol-level adversarial frames)."""
    raw = socket.create_connection((host, port), timeout=10)
    return rd_tls.wrap_client(raw, pin or PIN)

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise AssertionError(f"FAILED: {name} {detail}")


def raw_frame(kind, sid, seq, nonce, body):
    """A frame with a fully controlled nonce (proto.encode always mints
    a fresh one, which cannot test the duplicate-nonce guard)."""
    msg = {"v": 1, "kind": kind, "session_id": sid, "seq": seq,
           "nonce": nonce, "body": body}
    raw = json.dumps(msg, separators=(",", ":")).encode()
    return struct.pack(">I", len(raw)) + raw


def live_session(ctrl, store, user_token, scope, ttl=600):
    req = ctrl.request_session("bench-xvfb-1", scope)
    sid = req["session_id"]
    consent = store.user_grant_consent(sid, user_token, ttl_s=ttl)
    return sid, consent["session_token"]


def main():
    global PIN
    import shutil
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH, exist_ok=True)
    db = os.path.join(SCRATCH, "rd1_adv.db")
    eng_db = os.path.join(SCRATCH, "eng.db")
    eng = SwarmEngine(db_path=eng_db)
    agents = authz.AgentDirectory(eng.oracle_registry)
    ctrl = RemoteDispatchController(db, agents, eng.oracle)
    # TLS pinning ceremony (same as the main proof).
    cert_dir = os.path.join(SCRATCH, "rd1_certs")
    cert_path, _ = rd_tls.ensure_cert(cert_dir)
    PIN = rd_tls.fingerprint(cert_path)
    pair = ctrl.pair_device("bench-xvfb-1", "Bench Xvfb target",
                            cert_fingerprint=PIN)
    store = RemoteDispatchStore(db)
    user_token = store.register_user("james")
    target = RemoteDispatchTarget(
        db, "bench-xvfb-1", pair["agent_id"], pair["agent_token"],
        host="127.0.0.1", port=PORT, cert_dir=cert_dir)
    check("target cert matches pin",
          target.cert_fingerprint() == PIN)
    target.start()
    S = lambda: Scope(actions=["move", "click", "scroll", "type"],
                      x_min=0, y_min=0, x_max=200, y_max=200,
                      apps=[CHROME], max_actions=100)

    # A1. consent bypass: wrong session token at hello ----------------
    sid, tok = live_session(ctrl, store, user_token, S())
    raw = tls_channel()
    try:
        raw.hello(sid, "fabricated-token")
        check("A1 hello with fabricated token refused", False)
    except chan.ChannelError as e:
        check("A1 hello with fabricated token refused", True,
              str(e)[:70])
    finally:
        raw.close()
    try:
        store.user_grant_consent(sid, "wrong-user-token")
        check("A1 consent with wrong user token refused", False)
    except SessionError as e:
        check("A1 consent with wrong user token refused", True,
              str(e)[:60])

    # A2. scope escape ------------------------------------------------
    sid, tok = live_session(ctrl, store, user_token, S())
    sess = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    try:
        sess.act({"type": "move", "x": 800, "y": 600})
        check("A2 out-of-bounds move refused", False)
    except Exception as e:
        check("A2 out-of-bounds move refused", "outside session bounds"
              in str(e), str(e)[:70])
    try:
        sess.act({"type": "launch_app", "app": "/bin/evil",
                  "argv": ["/bin/evil"]})
        check("A2 launch_app not in scope refused", False)
    except Exception as e:
        check("A2 launch_app not in scope refused",
              "not in session scope" in str(e), str(e)[:70])
    sess.end()
    # allowlist escape: scope permits launch_app, but not THIS app
    app_scope = Scope(actions=["launch_app"], apps=[CHROME],
                      max_actions=100)
    sid, tok = live_session(ctrl, store, user_token, app_scope)
    sess = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    try:
        sess.act({"type": "launch_app", "app": "/bin/evil",
                  "argv": ["/bin/evil"]})
        check("A2 non-allowlisted app refused", False)
    except Exception as e:
        check("A2 non-allowlisted app refused", "allowlist" in str(e),
              str(e)[:70])
    try:
        sess.act({"type": "format_disk"})
        check("A2 unknown action refused", False)
    except Exception as e:
        check("A2 unknown action refused", "unknown action" in str(e),
              str(e)[:70])
    sess.end()
    # batch atomicity: 2nd action out of scope -> whole batch refused,
    # nothing executed
    sid, tok = live_session(ctrl, store, user_token, S())
    sess = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    before = store._get_session(sid)["actions_used"]
    try:
        sess.act_batch([{"type": "move", "x": 10, "y": 10},
                        {"type": "move", "x": 900, "y": 900}])
        check("A2b batch with OOB action refused atomically", False)
    except Exception as e:
        after = store._get_session(sid)["actions_used"]
        check("A2b batch with OOB action refused atomically",
              "outside session bounds" in str(e) and after == before,
              f"{str(e)[:60]} used {before}->{after}")
    sess.end()

    # A3. controller kill relay + action after -----------------------
    sid, tok = live_session(ctrl, store, user_token, S())
    sess = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    sess.act({"type": "move", "x": 50, "y": 50})
    sess.kill()
    try:
        sess.act({"type": "move", "x": 60, "y": 60})
        check("A3 action after controller kill refused", False)
    except Exception as e:
        check("A3 action after controller kill refused",
              "killed" in str(e), str(e)[:70])
    check("A3 indicator off", not target.indicator_live())

    # A4. spoofed target ----------------------------------------------
    rogue = RemoteDispatchTarget(
        db, "bench-xvfb-1", pair["agent_id"], "WRONG-TOKEN",
        host="127.0.0.1", port=PORT + 1, cert_dir=cert_dir)
    rogue.start()
    sid, tok = live_session(ctrl, store, user_token, S())
    try:
        ctrl.connect(sid, tok, "127.0.0.1", PORT + 1)
        check("A4 spoofed target refused", False)
    except Exception as e:
        check("A4 spoofed target refused",
              "identity" in str(e).lower() or "authentication" in
              str(e).lower(), str(e)[:80])
    rogue.stop()

    # A27. TLS pin mismatch: a target with a DIFFERENT certificate is
    # refused at the handshake, before any protocol byte is exchanged.
    rogue_dir = os.path.join(SCRATCH, "rd1_rogue_certs")
    rogue_cert, _ = rd_tls.ensure_cert(rogue_dir)
    rogue_fp = rd_tls.fingerprint(rogue_cert)
    check("A27 rogue cert differs", rogue_fp != PIN, rogue_fp[:16])
    rogue2 = RemoteDispatchTarget(
        db, "bench-xvfb-1", pair["agent_id"], pair["agent_token"],
        host="127.0.0.1", port=PORT + 2, cert_dir=rogue_dir)
    rogue2.start()
    sid, tok = live_session(ctrl, store, user_token, S())
    try:
        tls_channel(port=PORT + 2)
        check("A27 wrong-pin target refused", False)
    except Exception as e:
        check("A27 wrong-pin target refused",
              "fingerprint" in str(e).lower() or
              "impersonation" in str(e).lower(), str(e)[:80])
    # The controller's own connect() path also refuses: it pins from
    # the device record, so a spoofed endpoint never reaches hello.
    try:
        ctrl.connect(sid, tok, "127.0.0.1", PORT + 2)
        check("A27 connect() refuses wrong-pin target", False)
    except Exception as e:
        check("A27 connect() refuses wrong-pin target",
              "fingerprint" in str(e).lower() or
              "impersonation" in str(e).lower() or
              "handshake" in str(e).lower(), str(e)[:80])
    rogue2.stop()

    # A28. plaintext connection refused: without the TLS handshake the
    # target drops the connection unanswered -- no protocol response,
    # no hello_ok, nothing to attack.
    sid, tok = live_session(ctrl, store, user_token, S())
    plain = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    plain.settimeout(3)
    try:
        proto.send(plain, "hello", sid, 1, {"session_token": tok})
        try:
            m = proto.decode(plain)
            check("A28 plaintext gets no answer", False,
                  f"unexpected reply: {m.get('kind')}")
        except Exception:
            check("A28 plaintext gets no answer", True,
                  "connection dropped before any protocol reply")
    finally:
        plain.close()

    # A29. missing pin fails closed: the channel refuses to connect
    # without a pinned target identity at all.
    try:
        chan.ControllerChannel("127.0.0.1", PORT,
                               pinned_fingerprint=None)
        check("A29 unpinned channel refused", False)
    except Exception as e:
        check("A29 unpinned channel refused",
              "pin" in str(e).lower(), str(e)[:70])

    # A5. replay guards -----------------------------------------------
    sid, tok = live_session(ctrl, store, user_token, S())
    s = tls_raw_socket()
    proto.send(s, "hello", sid, 1, {"session_token": tok})
    m = proto.decode(s)
    assert m["kind"] == "hello_ok", m
    n2 = "aa" * 16
    body = {"action": {"type": "move", "x": 30, "y": 30}}
    f2 = raw_frame("action", sid, 2, n2, body)
    s.sendall(f2)
    m = proto.decode(s)
    check("A5 first action accepted", m["kind"] == "action_ok",
          m["kind"])
    s.sendall(f2)  # exact bytes again
    m = proto.decode(s)
    check("A5 exact-byte replay refused", m["kind"] == "action_refused",
          f"{m['kind']} {str(m.get('body'))[:60]}")
    # same nonce, current seq -> the nonce guard fires specifically
    s.sendall(raw_frame("action", sid, 3, n2, body))
    m = proto.decode(s)
    check("A5 duplicate-nonce refused",
          m["kind"] == "action_refused" and "nonce" in
          str(m.get("body", {})), f"{m['kind']} {str(m.get('body'))[:60]}")
    # fresh nonce, jumped seq -> the sequence guard fires
    s.sendall(raw_frame("action", sid, 9, "bb" * 16, body))
    m = proto.decode(s)
    check("A5 sequence jump refused",
          m["kind"] == "action_refused" and "seq" in
          str(m.get("body", {})), f"{m['kind']} {str(m.get('body'))[:60]}")
    s.close()
    with open(os.path.join(HERE, "rd1_replay_frame.bin"), "wb") as fh:
        fh.write(f2)  # nonce n2, seq 2: the accepted-then-replayed frame
    with open(os.path.join(HERE, "rd1_adv_manifest.json"), "w") as fh:
        json.dump({"db": db, "port": PORT, "device_id": "bench-xvfb-1",
                   "agent_id": pair["agent_id"],
                   "agent_token": pair["agent_token"],
                   "user_token": user_token,
                   "replay_session": sid, "replay_token": tok,
                   "replay_nonce": n2,
                   "cert_fingerprint": PIN,
                   "cert_dir": cert_dir}, fh)

    # A6. expired consent ----------------------------------------------
    sid, tok = live_session(ctrl, store, user_token, S(), ttl=2)
    sess = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    time.sleep(2.6)
    try:
        sess.act({"type": "move", "x": 10, "y": 10})
        check("A6 action on expired consent refused", False)
    except Exception as e:
        check("A6 action on expired consent refused",
              "no longer live" in str(e), str(e)[:70])
    try:
        sess.end()
    except Exception:
        pass

    # A7. budget escape ------------------------------------------------
    small = Scope(actions=["move"], x_min=0, y_min=0, x_max=200,
                  y_max=200, max_actions=2)
    sid, tok = live_session(ctrl, store, user_token, small)
    sess = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    sess.act({"type": "move", "x": 5, "y": 5})
    sess.act({"type": "move", "x": 6, "y": 6})
    try:
        sess.act({"type": "move", "x": 7, "y": 7})
        check("A7 budget escape refused", False)
    except Exception as e:
        check("A7 budget escape refused", "budget" in str(e),
              str(e)[:70])
    try:
        sess.end()
    except Exception:
        pass
    # batch charging: 2-action batch against budget 2, then one more
    sid, tok = live_session(ctrl, store, user_token, small)
    sess = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    sess.act_batch([{"type": "move", "x": 5, "y": 5},
                    {"type": "move", "x": 6, "y": 6}])
    try:
        sess.act({"type": "move", "x": 7, "y": 7})
        check("A7b batch charged per-action", False)
    except Exception as e:
        check("A7b batch charged per-action", "budget" in str(e),
              str(e)[:70])
    try:
        sess.end()
    except Exception:
        pass

    # A8. concurrent duplicate connection -------------------------------
    sid, tok = live_session(ctrl, store, user_token, S())
    sess = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    dup = tls_channel()
    try:
        dup.hello(sid, tok)
        check("A8 concurrent duplicate refused", False)
    except chan.ChannelError as e:
        check("A8 concurrent duplicate refused", "already has a live"
              in str(e), str(e)[:70])
    finally:
        dup.close()
    sess.channel.close()  # release the slot
    time.sleep(0.6)
    sess2 = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    r = sess2.act({"type": "move", "x": 11, "y": 12})
    check("A8 reconnect after close works (fresh hello)",
          r and r[0]["x"] == 11, str(r))
    # A8b. reconnect AFTER actions: the controller must continue the
    # persisted sequence counter, not restart at 2.
    r = sess2.act({"type": "move", "x": 21, "y": 22})
    check("A8 action before second disconnect", r and r[0]["x"] == 21,
          str(r))
    sess2.channel.close()
    time.sleep(0.6)
    sess3 = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    r = sess3.act({"type": "move", "x": 31, "y": 32})
    check("A8b reconnect-after-action continues sequence",
          r and r[0]["x"] == 31, str(r))
    sess3.end()

    # A30. one live session per target: a second session cannot connect
    # while the first holds the cursor; after the first ends, the
    # second connects fine.
    sid_a, tok_a = live_session(ctrl, store, user_token, S())
    sess_a = ctrl.connect(sid_a, tok_a, "127.0.0.1", PORT)
    sid_b, tok_b = live_session(ctrl, store, user_token, S())
    try:
        ctrl.connect(sid_b, tok_b, "127.0.0.1", PORT)
        check("A30 second live session refused (target busy)", False)
    except Exception as e:
        check("A30 second live session refused (target busy)",
              "busy" in str(e).lower(), str(e)[:70])
    sess_a.end()
    time.sleep(0.6)
    sess_b = ctrl.connect(sid_b, tok_b, "127.0.0.1", PORT)
    r = sess_b.act({"type": "move", "x": 41, "y": 42})
    check("A30 session connects after prior ends",
          r and r[0]["x"] == 41, str(r))
    sess_b.end()

    # A9. event-chain tamper -------------------------------------------
    # Tamper a COPY: the primary DB must stay valid for the
    # fresh-process proof that runs next against this same file.
    import shutil
    tampered = db + ".tampered"
    if os.path.exists(tampered):
        os.remove(tampered)
    shutil.copy(db, tampered)
    conn = sqlite3.connect(tampered)
    conn.execute("UPDATE rd_events SET detail_json='{}' WHERE rowid=1")
    conn.commit()
    conn.close()
    store2 = RemoteDispatchStore(tampered)
    ok, bad = store2.audit_chain()
    check("A9 chain tamper detected", not ok and bad, f"bad rows: {bad}")
    store2.close()
    os.remove(tampered)

    # A10. long type interrupted by target-local user kill -------------
    # Mechanism (deterministic): the per-keystroke liveness check stops
    # injection mid-stream when the session dies.
    from swarm_engine.remote_dispatch.cursor_x11 import X11Cursor
    from swarm_engine.remote_dispatch.target_agent import TargetRefusal
    cur = X11Cursor(":99")
    seen = []

    def dying():
        seen.append(1)
        if len(seen) > 10:
            raise TargetRefusal("session killed/expired during action")

    try:
        cur.type("k" * 100, is_live=dying)
        check("A10a typing stops when session dies", False,
              "no interruption raised")
    except TargetRefusal as e:
        check("A10a typing stops when session dies",
              len(seen) == 11, f"keystrokes before stop: {len(seen)}")

    # Wiring (integration): a real in-flight type action, killed via the
    # target-local user kill switch, is refused as interrupted and NOT
    # charged to the session budget.
    sid, tok = live_session(ctrl, store, user_token, S())
    sess = ctrl.connect(sid, tok, "127.0.0.1", PORT)
    outcome = {}

    def do_type():
        try:
            outcome["results"] = sess.act(
                {"type": "type", "text": "k" * 2000})
        except Exception as e:  # noqa: BLE001 - the refusal IS the assert
            outcome["error"] = e

    th = threading.Thread(target=do_type, daemon=True)
    th.start()
    # No sleep: the kill is a direct local call, the type action must
    # cross the socket first, so the kill reliably lands first or
    # mid-flight; either way the action must not complete.
    target.user_kill(sid, user_token)  # the user's kill switch
    th.join(timeout=60)
    err = str(outcome.get("error", outcome.get("results")))
    check("A10b in-flight type refused after user kill",
          th.is_alive() is False and "error" in outcome
          and ("interrupted" in err or "killed" in err), err[:90])
    s = store._get_session(sid)
    check("A10b interrupted action not charged to budget",
          s["actions_used"] == 0, f"actions_used={s['actions_used']}")
    check("A10b session killed", s["state"] == "killed", s["state"])
    try:
        sess.end()
    except Exception:
        pass

    target.stop()
    green = sum(1 for _, c, _ in CHECKS if c)
    print(f"\nRD1 adversarial: {green}/{len(CHECKS)} green", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
