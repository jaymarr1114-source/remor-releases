"""REMOTE-DISPATCH-1 fresh-process proof.

Runs in a NEW process after rd1_adversarial.py: re-opens the same DBs,
proves session state + replay guards + audit chain survived, proves a
restarted target still refuses a killed session and a replayed frame
(nonce table persisted), and smokes the HTTP API against the live target.

Run: python3 proofs/rd1_fresh_process.py
"""
import json
import os
import socket
import struct
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
    RemoteDispatchController)
from swarm_engine.remote_dispatch.session_model import (  # noqa: E402
    RemoteDispatchStore, Scope)
from swarm_engine.remote_dispatch.target_agent import (  # noqa: E402
    RemoteDispatchTarget)
from swarm_engine.services.remote_dispatch_api import (  # noqa: E402
    build_remote_dispatch_service, routes_for_remote_dispatch)

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = "/opt/meta-chromium/chrome"
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise AssertionError(f"FAILED: {name} {detail}")


def raw_frame(kind, sid, seq, nonce, body):
    msg = {"v": 1, "kind": kind, "session_id": sid, "seq": seq,
           "nonce": nonce, "body": body}
    raw = json.dumps(msg, separators=(",", ":")).encode()
    return struct.pack(">I", len(raw)) + raw


def main():
    with open(os.path.join(HERE, "rd1_adv_manifest.json")) as f:
        m = json.load(f)
    db, port = m["db"], m["port"]
    eng_db = os.path.join(os.path.dirname(db), "eng.db")

    # 1. state survived the process boundary ---------------------------
    store = RemoteDispatchStore(db)
    ok, bad = store.audit_chain()
    check("F1 event chain verifies in fresh process", ok, f"bad={bad}")
    s = store._get_session(m["replay_session"])
    check("F2 replay session state persisted",
          s["state"] in ("live", "consented"), s["state"])
    killed = [r for r in store.list_sessions()
              if r["state"] == "killed"]
    check("F3 killed sessions stayed killed", len(killed) >= 1,
          f"{len(killed)} killed")

    # 2. restarted target honors old state -----------------------------
    eng = SwarmEngine(db_path=eng_db)
    agents = authz.AgentDirectory(eng.oracle_registry)
    ctrl = RemoteDispatchController(db, agents, eng.oracle)
    # The restarted target reuses its persisted certificate: the
    # pinned fingerprint in the device record still authenticates it.
    target = RemoteDispatchTarget(
        db, m["device_id"], m["agent_id"], m["agent_token"],
        host="127.0.0.1", port=port, cert_dir=m["cert_dir"])
    check("F3b restarted target cert matches pin",
          target.cert_fingerprint() == m["cert_fingerprint"],
          target.cert_fingerprint()[:16])
    target.start()
    time.sleep(0.5)

    sid, tok, nonce = (m["replay_session"], m["replay_token"],
                       m["replay_nonce"])
    s2 = rd_tls.wrap_client(
        socket.create_connection(("127.0.0.1", port), timeout=10),
        m["cert_fingerprint"])
    proto.send(s2, "hello", sid, 1, {"session_token": tok})
    m2 = proto.decode(s2)
    check("F4 hello on old session still accepted",
          m2["kind"] == "hello_ok", m2["kind"])
    # same nonce as the dead process used, current seq -> the persisted
    # nonce table (not memory) must refuse it
    s2.sendall(raw_frame("action", sid, 3, nonce,
                         {"action": {"type": "move", "x": 1, "y": 1}}))
    m2 = proto.decode(s2)
    check("F5 replayed nonce refused after restart",
          m2["kind"] == "action_refused" and "nonce" in str(m2.get("body")),
          f"{m2['kind']} {str(m2.get('body'))[:60]}")
    s2.close()

    # 3. HTTP API smoke against the live target -------------------------
    tmp = tempfile.mkdtemp(prefix="rd1api_")
    svc = build_remote_dispatch_service(tmp, agents, eng.oracle)
    # bind the whole service (routes read svc.db_path directly in two
    # places) to the real DB, not just its controller.
    svc.db_path = db
    svc.controller = RemoteDispatchController(db, agents, eng.oracle)
    routes = routes_for_remote_dispatch(svc)

    r = routes[("POST", "/api/remote/sessions")]({
        "device_id": m["device_id"],
        "scope": {"actions": ["move"], "x_min": 0, "y_min": 0,
                  "x_max": 1600, "y_max": 1200, "max_actions": 10}})
    check("F6 API request session", r["ok"], str(r)[:80])
    sid3 = r["session_id"]
    r = routes[("GET", "/api/remote/sessions")]({})
    check("F7 API list sessions (history populates)",
          r["ok"] and any(x["session_id"] == sid3
                          for x in r["sessions"]),
          f"{len(r['sessions'])} sessions")
    user_row = store._conn.execute(
        "SELECT token_hash FROM rd_user_tokens LIMIT 1").fetchone()
    # the DB holds only the token HASH: the fresh process cannot mint
    # consent from the database alone (trust property). The bench
    # harness plays the user, so it carries the token in the manifest.
    check("F8 DB holds only the user-token hash",
          user_row is not None and
          user_row["token_hash"] != m["user_token"])
    consent = store.user_grant_consent(sid3, m["user_token"])
    r = routes[("POST", "/api/remote/dispatch")]({
        "session_id": sid3, "session_token": consent["session_token"],
        "actions": [{"type": "move", "x": 123, "y": 321}]})
    check("F9 API dispatch moves the real cursor",
          r["ok"] and r["results"][0]["x"] == 123, str(r)[:100])
    r = routes[("POST", "/api/remote/dispatch")]({
        "session_id": sid3, "session_token": consent["session_token"],
        "actions": [{"type": "launch_app", "app": "/bin/evil",
                     "argv": ["/bin/evil"]}]})
    check("F10 API dispatch refusal is body data",
          r["ok"] is False and "refused" in r, str(r)[:80])
    r = routes[("POST", "/api/remote/kill")]({"session_id": sid3})
    check("F11 API kill", r["ok"] and r["state"] == "killed",
          str(r)[:60])
    r = routes[("POST", "/api/remote/dispatch")]({
        "session_id": sid3, "session_token": consent["session_token"],
        "actions": [{"type": "move", "x": 1, "y": 1}]})
    check("F12 API dispatch after kill refused",
          r["ok"] is False, str(r)[:80])
    r = routes[("POST", "/api/remote/end")]({"session_id": sid3})
    check("F13 API end", r["ok"], str(r)[:60])

    target.stop()
    green = sum(1 for _, c, _ in CHECKS if c)
    print(f"\nRD1 fresh-process: {green}/{len(CHECKS)} green", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
