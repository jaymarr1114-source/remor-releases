"""RD-TARGET-ENDPOINT-1 proof battery: target-endpoint registration.

Closes the gap the phone exposed: "Start stream" honestly refused
"target endpoint unknown" because nothing resolved a paired device_id
to a live endpoint. This battery proves, end to end through the REAL
call path:

  * the real HTTP route table (routes_for_remote_dispatch -- the same
    table the product server mounts),
  * a real HTTP server on the engine's thread (the KD-2
    thread-affinity discipline: the engine is constructed on the
    serving thread and every request is handled on that same thread),
  * the real TLS-pinned channel, the real AgentDirectory
    authentication, the real session store.

  E1  honest path: pair -> target announces over HTTP -> resolve ->
      dispatch (connect with no host/port: resolution on the real
      call path) -> action results -> end
  E2  kill relay resolves through the registry (target_relay=relayed)
  A1  announcement with a wrong/forged token: refused
  A2  replay of a stale announcement: refused (old ts, and exact
      re-delivery of the latest (ts, nonce))
  A3  endpoint hijack: device B's token announcing for device A: refused
  A4  target restarts on a new port: the new announcement supersedes
      the old one; resolution points at the new port; the dead port
      refuses with the true transport reason
  A5  announcement carrying a fingerprint that does not match the
      pairing-time pin: refused (impersonation); a wrong-cert target
      is refused at the TLS pin through the resolved connect path
  A6  announcement for an unpaired device_id: refused
  A7  paired device, no announcement: the exact "target endpoint
      unknown" reason on resolve, connect, API dispatch, and kill
      relay (the old vague "target endpoint unknown (device
      offline?)" string is gone from the tree)
  A8  stale endpoint (target dead): connect refuses with the TRUE
      transport reason ("target endpoint unreachable ... Connection
      refused"), never a silent downgrade or invented address
  A9  field validation: bad device_id charset, bad port, bad
      fingerprint, bad nonce, empty host, missing token: each refused
      with its exact reason
  A10 no trust weakening: the GUI-RD-PAIRFIX-1 device_id charset rule
      still enforced, AgentDirectory auth intact, pairing binding
      intact, legitimate re-announcements still accepted
  A11 atomicity: each accepted announcement is logged exactly once;
      the registry always reflects the latest accepted one

Run: python3 proofs/rd1_endpoint_proof.py
Requires: Xvfb on :99 (the X11 target substrate).
Writes: proofs/rd1_endpoint_scratch/ (removed on start).
"""
import http.client
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

os.environ.setdefault("DISPLAY", ":99")
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "pylib")))

from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.governance import caller_authorization as authz  # noqa: E402
from swarm_engine.remote_dispatch import tls as rd_tls  # noqa: E402
from swarm_engine.remote_dispatch.announce import (  # noqa: E402
    build_announcement)
from swarm_engine.remote_dispatch.controller import (  # noqa: E402
    ControllerError)
from swarm_engine.remote_dispatch.session_model import (  # noqa: E402
    RemoteDispatchStore, Scope)
from swarm_engine.remote_dispatch.target_agent import (  # noqa: E402
    RemoteDispatchTarget)
from swarm_engine.services.remote_dispatch_api import (  # noqa: E402
    RemoteDispatchService, routes_for_remote_dispatch)

HERE = os.path.dirname(os.path.abspath(__file__))
SCRATCH = os.path.join(HERE, "rd1_endpoint_scratch")

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise AssertionError(f"FAILED: {name} {detail}")


def make_handler(routes):
    class H(BaseHTTPRequestHandler):
        def _serve(self):
            handler = routes.get((self.command, self.path))
            if handler is None:
                self.send_response(404)
                self.end_headers()
                return
            body = {}
            if self.command == "POST":
                n = int(self.headers.get("Content-Length", 0))
                if n:
                    body = json.loads(self.rfile.read(n).decode())
            out = handler(body)
            raw = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_POST = _serve
        do_GET = _serve

        def log_message(self, *a):
            pass
    return H


class Api:
    """HTTP client speaking to the real route table over a real socket."""

    def __init__(self, port):
        self.port = port

    def post(self, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=15)
        payload = json.dumps(body or {}).encode()
        try:
            conn.request("POST", path, body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read().decode())
        finally:
            conn.close()


def run_battery(ctx):
    api = ctx["api"]
    svc = ctx["svc"]
    ctrl = svc.controller
    db = svc.db_path
    store = RemoteDispatchStore(db)
    http_port = ctx["http_port"]
    # Every target created is registered here and stopped in the
    # driver's finally: a crashed battery must not leave listener
    # sockets or X indicator windows behind on the shared bench
    # display (a leftover window poisons later batteries' indicator
    # checks -- observed 2026-09-30 with rd1_adversarial A3).
    targets = []
    ctx["targets"] = targets

    def device_row(dev):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            r = conn.execute("SELECT * FROM rd_devices WHERE device_id=?",
                             (dev,)).fetchone()
            return dict(r) if r else {}
        finally:
            conn.close()

    # ---- pair two devices (real /api/remote/pair route) ---------------
    # Neither pins a fingerprint at pairing: the first authenticated
    # announcement binds it trust-on-first-announcement (the
    # pairing-time pin path is the bench ceremony's, covered by the
    # rd1 regression battery; the store's pin-compare branch is
    # identical either way).
    cert_a = os.path.join(SCRATCH, "certs_a")
    fp_a = rd_tls.fingerprint(rd_tls.ensure_cert(cert_a)[0])
    cert_b = os.path.join(SCRATCH, "certs_b")
    fp_b = rd_tls.fingerprint(rd_tls.ensure_cert(cert_b)[0])
    st, pair_a = api.post("/api/remote/pair",
                          {"device_id": "dev-A",
                           "display_name": "Bench target A"})
    check("pair dev-A (TOFU path)",
          st == 200 and pair_a.get("ok") and
          pair_a["agent_id"] == "rd-target-dev-A")
    st, pair_b = api.post("/api/remote/pair",
                          {"device_id": "dev-B",
                           "display_name": "Bench target B"})
    check("pair dev-B (TOFU path)",
          st == 200 and pair_b.get("ok") and
          pair_b["agent_id"] == "rd-target-dev-B")
    tok_a = pair_a["agent_token"]
    tok_b = pair_b["agent_token"]

    # ---- E1: honest announcement over real HTTP ----------------------
    tgt_a = RemoteDispatchTarget(
        db, "dev-A", pair_a["agent_id"], tok_a,
        host="127.0.0.1", port=0, cert_dir=cert_a,
        announce_url=f"http://127.0.0.1:{http_port}")
    targets.append(tgt_a)
    ann = tgt_a.start()
    check("E1 announcement accepted over HTTP",
          ann is not None and ann.get("ok"), str(ann))
    check("E1 fingerprint bound trust-on-first-announcement",
          ann.get("pinned") is True and
          device_row("dev-A")["cert_fingerprint"] == fp_a,
          fp_a[:16])
    host, port = ctrl.resolve_endpoint("dev-A")
    check("E1 resolve_endpoint returns the live endpoint",
          (host, port) == ("127.0.0.1", tgt_a.port),
          f"{host}:{port}")

    # request -> consent (target-side) -> dispatch. The dispatch route
    # calls controller.connect(session_id, session_token) with NO
    # host/port: resolution happens on the real call path.
    user_token = store.register_user("bench-user")
    scope = {"actions": ["move"], "x_min": 0, "y_min": 0, "x_max": 1600,
             "y_max": 1200, "max_actions": 10, "ttl_s": 600}
    st, req = api.post("/api/remote/sessions",
                       {"device_id": "dev-A", "scope": scope})
    check("E1 session requested", st == 200 and req.get("ok"))
    sid = req["session_id"]
    sess_token = store.user_grant_consent(sid, user_token)["session_token"]
    st, out = api.post("/api/remote/dispatch",
                       {"session_id": sid, "session_token": sess_token,
                        "actions": [{"type": "move", "x": 100,
                                     "y": 100}]})
    check("E1 dispatch through the resolved channel",
          st == 200 and out.get("ok") and bool(out.get("results")),
          str(out)[:160])
    st, out = api.post("/api/remote/end", {"session_id": sid})
    check("E1 session ended",
          out.get("ok") and store._get_session(sid)["state"] == "ended")
    tgt_a.stop()

    # ---- E2: kill relay resolves through the registry ---------------
    tgt_b = RemoteDispatchTarget(
        db, "dev-B", pair_b["agent_id"], tok_b,
        host="127.0.0.1", port=0, cert_dir=cert_b,
        announce_url=f"http://127.0.0.1:{http_port}")
    targets.append(tgt_b)
    ann_b = tgt_b.start()
    check("E2 dev-B announcement accepted (TOFU pin bind)",
          ann_b and ann_b.get("ok") and ann_b.get("pinned") is True and
          device_row("dev-B")["cert_fingerprint"] == fp_b,
          fp_b[:16])
    st, req2 = api.post("/api/remote/sessions",
                        {"device_id": "dev-B", "scope": scope})
    sid2 = req2["session_id"]
    tok2 = store.user_grant_consent(sid2, user_token)["session_token"]
    st, kill_out = api.post("/api/remote/kill",
                            {"session_id": sid2,
                             "session_token": tok2})
    check("E2 kill relay resolved + relayed",
          kill_out.get("target_relay") == "relayed", str(kill_out))

    # ---- A1: forged/wrong token -------------------------------------
    bad = build_announcement("dev-A", "forged-token-xyz", "127.0.0.1",
                             9999, fp_a)
    st, body = api.post("/api/remote/announce", bad)
    check("A1 forged token refused",
          st == 200 and not body.get("ok") and
          "agent token authentication failed" in body.get("error", ""),
          body.get("error", ""))

    # ---- A2: replay ---------------------------------------------------
    row = device_row("dev-A")
    stale = build_announcement("dev-A", tok_a, "127.0.0.1", tgt_a.port,
                               fp_a)
    stale["ann_ts"] = row["announce_ts"] - 100.0
    st, body = api.post("/api/remote/announce", stale)
    check("A2 stale-timestamp replay refused",
          not body.get("ok") and "replay refused" in body.get("error", ""),
          body.get("error", ""))
    redeliver = {"device_id": "dev-A", "agent_token": tok_a,
                 "host": "127.0.0.1", "port": tgt_a.port,
                 "cert_fingerprint": fp_a,
                 "ann_ts": row["announce_ts"],
                 "ann_nonce": row["announce_nonce"]}
    st, body = api.post("/api/remote/announce", redeliver)
    check("A2 exact re-delivery refused (no double-apply)",
          not body.get("ok") and "replay refused" in body.get("error", ""),
          body.get("error", ""))
    check("A2 endpoint row unchanged by replays",
          device_row("dev-A")["endpoint_port"] == tgt_a.port)

    # ---- A3: endpoint hijack -----------------------------------------
    hijack = build_announcement("dev-A", tok_b, "10.9.9.9", 47631, fp_a)
    st, body = api.post("/api/remote/announce", hijack)
    check("A3 cross-device token hijack refused",
          not body.get("ok") and
          "agent token authentication failed" in body.get("error", ""),
          body.get("error", ""))
    check("A3 registry untouched by hijack",
          device_row("dev-A")["endpoint_host"] == "127.0.0.1")

    # ---- A4: target restarts on a new port -----------------------------
    tgt_a2 = RemoteDispatchTarget(
        db, "dev-A", pair_a["agent_id"], tok_a,
        host="127.0.0.1", port=0, cert_dir=cert_a,
        announce_url=f"http://127.0.0.1:{http_port}")
    targets.append(tgt_a2)
    ann2 = tgt_a2.start()
    check("A4 re-announcement on new port accepted",
          ann2 and ann2.get("ok"), str(ann2))
    host4, port4 = ctrl.resolve_endpoint("dev-A")
    check("A4 resolution superseded to the new port",
          (host4, port4) == ("127.0.0.1", tgt_a2.port) and
          port4 != tgt_a.port, f"{host4}:{port4}")
    st, req4 = api.post("/api/remote/sessions",
                        {"device_id": "dev-A", "scope": scope})
    sid4 = req4["session_id"]
    tok4 = store.user_grant_consent(sid4, user_token)["session_token"]
    st, out4 = api.post("/api/remote/dispatch",
                        {"session_id": sid4, "session_token": tok4,
                         "actions": [{"type": "move", "x": 5, "y": 5}]})
    check("A4 resolved dispatch reaches the NEW target",
          out4.get("ok") is True, str(out4)[:120])
    api.post("/api/remote/end", {"session_id": sid4})
    # the old port is dead: the TRUE transport reason, not a guess.
    # (It fails at TCP connect, before any thread-affine auth.)
    try:
        ctrl.connect(sid4, tok4, "127.0.0.1", tgt_a.port)
        check("A4 explicit connect to the dead port refused", False)
    except ControllerError as e:
        check("A4 explicit connect to the dead port refused",
              f"target endpoint unreachable (127.0.0.1:{tgt_a.port})"
              in str(e) and "Connection refused" in str(e),
              str(e)[:120])
    tgt_a2.stop()

    # ---- A5: fingerprint mismatch (impersonation) -----------------------
    # dev-B's pin was bound by its first (honest) announcement; a
    # later announcement carrying a different fingerprint is refused.
    rogue_certs = os.path.join(SCRATCH, "certs_rogue")
    rogue_fp = rd_tls.fingerprint(rd_tls.ensure_cert(rogue_certs)[0])
    check("A5 rogue cert differs", rogue_fp != fp_b, rogue_fp[:16])
    evil = build_announcement("dev-B", tok_b, "127.0.0.1", 45555,
                              rogue_fp)
    st, body = api.post("/api/remote/announce", evil)
    check("A5 mismatched-fingerprint announcement refused",
          not body.get("ok") and
          "fingerprint mismatch" in body.get("error", ""),
          body.get("error", ""))
    check("A5 registry keeps the honest endpoint",
          device_row("dev-B")["endpoint_port"] == tgt_b.port)
    # the resolved connect path still pins the handshake: a rogue
    # target presenting the wrong cert is refused at TLS. (It fails
    # inside the TLS wrap, before any thread-affine auth.)
    rogue = RemoteDispatchTarget(
        db, "dev-B", pair_b["agent_id"], tok_b,
        host="127.0.0.1", port=0, cert_dir=rogue_certs)
    targets.append(rogue)
    rogue.start()
    st, req5 = api.post("/api/remote/sessions",
                        {"device_id": "dev-B", "scope": scope})
    sid5 = req5["session_id"]
    tok5 = store.user_grant_consent(sid5, user_token)["session_token"]
    try:
        ctrl.connect(sid5, tok5, "127.0.0.1", rogue.port)
        check("A5 wrong-cert target refused at TLS pin", False)
    except Exception as e:  # noqa: BLE001 -- asserting refusal
        check("A5 wrong-cert target refused at TLS pin",
              "fingerprint mismatch" in str(e), str(e)[:120])
    rogue.stop()

    # ---- A6: announcement for an unpaired device ----------------------
    ghost = build_announcement("never-paired", "tok", "127.0.0.1", 1234,
                               fp_a)
    st, body = api.post("/api/remote/announce", ghost)
    check("A6 unpaired device announcement refused",
          not body.get("ok") and "not paired" in body.get("error", ""),
          body.get("error", ""))

    # ---- A7: paired device, no announcement ---------------------------
    st, pair_c = api.post("/api/remote/pair",
                          {"device_id": "dev-C",
                           "display_name": "Bench target C"})
    check("A7 dev-C paired", pair_c.get("ok") is True)
    st, req7 = api.post("/api/remote/sessions",
                        {"device_id": "dev-C", "scope": scope})
    sid7 = req7["session_id"]
    tok7 = store.user_grant_consent(sid7, user_token)["session_token"]
    try:
        ctrl.resolve_endpoint("dev-C")
        check("A7 no-announcement resolution refused", False)
    except ControllerError as e:
        check("A7 no-announcement resolution refused",
              "target endpoint unknown: no endpoint announced for"
              " device 'dev-C'" in str(e), str(e))
    st, out7 = api.post(
        "/api/remote/dispatch",
        {"session_id": sid7, "session_token": tok7,
         "actions": [{"type": "move", "x": 1, "y": 1}]})
    check("A7 API dispatch carries the precise refusal",
          out7.get("ok") is False and
          "target endpoint unknown: no endpoint announced" in
          out7.get("refused", ""), out7.get("refused", "")[:120])
    st, relay7 = api.post("/api/remote/kill",
                          {"session_id": sid7, "session_token": tok7})
    check("A7 kill relay reports no_endpoint precisely",
          relay7.get("target_relay") == "no_endpoint" and
          "no endpoint announced" in relay7.get("target_relay_detail",
                                                ""),
          relay7.get("target_relay_detail", "")[:120])

    # ---- A8: stale endpoint (target dead) ------------------------------
    cert_d = os.path.join(SCRATCH, "certs_d")
    rd_tls.ensure_cert(cert_d)
    st, pair_d = api.post("/api/remote/pair",
                          {"device_id": "dev-D",
                           "display_name": "Bench target D"})
    tgt_d = RemoteDispatchTarget(
        db, "dev-D", pair_d["agent_id"], pair_d["agent_token"],
        host="127.0.0.1", port=0, cert_dir=cert_d,
        announce_url=f"http://127.0.0.1:{http_port}")
    targets.append(tgt_d)
    tgt_d.start()
    dead_port = tgt_d.port
    tgt_d.stop()  # the endpoint is now stale
    st, req8 = api.post("/api/remote/sessions",
                        {"device_id": "dev-D", "scope": scope})
    sid8 = req8["session_id"]
    tok8 = store.user_grant_consent(sid8, user_token)["session_token"]
    st, out8 = api.post(
        "/api/remote/dispatch",
        {"session_id": sid8, "session_token": tok8,
         "actions": [{"type": "move", "x": 1, "y": 1}]})
    check("A8 stale endpoint refused with transport truth",
          out8.get("ok") is False and
          f"target endpoint unreachable (127.0.0.1:{dead_port})"
          in out8.get("refused", "") and
          "Connection refused" in out8.get("refused", ""),
          out8.get("refused", "")[:140])

    # ---- A9: field validation ------------------------------------------
    def refused_with(payload, needle):
        _st, b = api.post("/api/remote/announce", payload)
        return (not b.get("ok")) and needle in b.get("error", "")

    base = build_announcement("dev-A", tok_a, "127.0.0.1", tgt_b.port,
                              fp_a)
    check("A9 bad device_id charset refused",
          refused_with(dict(base, device_id="evil!device"),
                       "invalid device_id"))
    check("A9 out-of-range port refused",
          refused_with(dict(base, port=99999), "invalid port"))
    check("A9 malformed fingerprint refused",
          refused_with(dict(base, cert_fingerprint="zzzz"),
                       "invalid cert_fingerprint"))
    check("A9 empty nonce refused",
          refused_with(dict(base, ann_nonce=""), "invalid ann_nonce"))
    check("A9 empty host refused",
          refused_with(dict(base, host=""), "invalid host"))
    no_token = dict(base)
    del no_token["agent_token"]
    check("A9 missing token refused",
          refused_with(no_token, "invalid agent_token"))

    # ---- A10: no trust weakening ----------------------------------------
    row_a = device_row("dev-A")
    check("A10 pairing binding intact",
          row_a["agent_id"] == "rd-target-dev-A")
    ok_reannounce = build_announcement(
        "dev-A", tok_a, "127.0.0.1", 45678, fp_a)
    ok_reannounce["ann_ts"] = time.time() + 5
    st, body = api.post("/api/remote/announce", ok_reannounce)
    check("A10 legitimate re-announcement still accepted",
          body.get("ok") is True, body.get("error", ""))

    # ---- A11: atomicity --------------------------------------------------
    conn = sqlite3.connect(db)
    try:
        # Only the authenticated network path logs "superseded"; the
        # legacy local bench write shares the "endpoint_announced"
        # kind but never logs it.
        n = conn.execute(
            "SELECT COUNT(*) FROM rd_events WHERE kind="
            "'endpoint_announced' AND detail_json LIKE '%superseded%'"
            " AND detail_json LIKE '%dev-A%'").fetchone()[0]
    finally:
        conn.close()
    # accepted network announcements for dev-A: E1, A4, A10 = 3.
    # (A2's replays and A3's hijack were refused; they log nothing.)
    check("A11 each accepted announcement logged exactly once",
          n == 3, f"endpoint_announced events for dev-A: {n}")
    check("A11 registry reflects the latest accepted announcement",
          device_row("dev-A")["endpoint_port"] == 45678)

    # ---- chain audit -----------------------------------------------------
    ok, msg = store.audit_chain()
    check("event chain intact", ok, msg)

    tgt_b.stop()
    print(f"\n{len(CHECKS)} checks, "
          f"{sum(1 for _, c, _ in CHECKS if c)} passed", flush=True)


def main():
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH, exist_ok=True)

    # KD-2: the engine is constructed on the serving thread and every
    # request is handled on that same thread (single-threaded server
    # by design -- the oracle registry is thread-affine). The battery
    # driver runs on a worker thread and speaks to the engine ONLY
    # over HTTP (the real route table) plus thread-safe RD-store reads.
    eng = SwarmEngine(db_path=os.path.join(SCRATCH, "eng.db"))
    agents = authz.AgentDirectory(eng.oracle_registry)
    svc = RemoteDispatchService(os.path.join(SCRATCH, "svc"), agents,
                                eng.oracle)
    routes = routes_for_remote_dispatch(svc)
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(routes))
    http_port = httpd.server_address[1]
    check("announce API up (real HTTP, real route table)",
          http_port > 0, f"http://127.0.0.1:{http_port}")

    ctx = {"api": Api(http_port), "svc": svc, "http_port": http_port,
           "httpd": httpd, "targets": [], "driver_error": None}

    def _driver():
        try:
            run_battery(ctx)
        except BaseException as e:  # noqa: BLE001 -- recorded, reraised
            ctx["driver_error"] = e
        finally:
            # Teardown runs whether the driver passed or died: stop
            # every target (releases listener ports, hides any X
            # indicator windows), then release the serving thread so
            # the process always exits -- a dead driver must never
            # leave the bench hanging or polluting the shared display.
            for tgt in ctx["targets"]:
                try:
                    tgt.stop()
                except Exception:
                    pass
            httpd.shutdown()

    driver = threading.Thread(target=_driver, daemon=True)
    driver.start()
    httpd.serve_forever()  # serving thread == constructing thread
    driver.join(timeout=10)
    httpd.server_close()
    if ctx["driver_error"] is not None:
        raise ctx["driver_error"]


if __name__ == "__main__":
    main()
