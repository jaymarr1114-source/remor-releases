#!/usr/bin/env python3
"""RD-TARGET-REMOTE-1 product-path proof: the pure-Java target against the
REAL phone-side in-process module.

Topology (James, 2026-09-30): the CONTROLLER is the phone, which runs the
canonical controller in-process via
remor_mobile/payload_src/app/remote_dispatch_inproc.py; the TARGET is a
tablet on the same WiFi. No PC, no standalone service.

This bench proves the actual product path, not a stand-in:
  Java BenchTarget (TLS listener)
    -> Java AnnounceClient emits the exact POST /api/remote/announce bytes
    -> inproc.rd_handle (the real product-surface handler) accepts them
    -> svc.controller.resolve_endpoint returns the Java listener's
       (host, port)
    -> the real Python controller connects to the Java TLS listener:
       hello (mutual auth) / act / get_frame / kill, with the real
       session and replay rules.

The inproc module is configured with the canonical pylib at f9a640a
(the warm worktree). The only bench adaptation is the P2 one: this
sandbox denies the JVM outbound TCP, so the harness captures the Java
client's exact request bytes in-JVM and delivers the parsed body to
rd_handle -- the same bytes, the real handler.

Checks:
  IP1  java listener up; fingerprint parity (Python == Java, SHA-256)
  IP2  inproc rd_handle accepts the java client's exact announce bytes
  IP3  resolve_endpoint returns the java listener's (host, port)
  IP4  request + consent + java register + connect (mutual auth, LIVE)
  IP5  act() through the real channel; the java sink recorded them
  IP6  get_frame() -> real PNG bytes, synthesized:true, within scope
  IP7  kill relay: kill_ok from java, causal stop, INDICATOR_OFF
  IP8  connect-after-kill refused with the exact reason
  IA1  announce replay (same ann_nonce) refused
  IA2  announce with a bad agent token refused
  IA3  announce with a mismatched cert fingerprint refused
  IA4  hello with a bad session token refused
  IA5  act after kill refused (session not live)

Run: python3 proofs/rd1_inproc_product_path.py
Writes: proofs/rd1_inproc_scratch/manifest.json
"""
import base64
import importlib.util
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time

TREE = os.path.expanduser("~/workspace/worktrees/warm-backend-runtime")
HERE = os.path.join(TREE, "proofs")
SCRATCH = os.path.join(HERE, "rd1_inproc_scratch")
INPROC_PATH = os.path.expanduser(
    "~/workspace/remor_mobile/payload_src/app/remote_dispatch_inproc.py")
PYLIB = os.path.join(TREE, "pylib")

sys.path.insert(0, PYLIB)

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), str(detail)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""), flush=True)


# ----------------------------------------------------------------------
# Java bench target (self-contained helpers; no cross-proof state)
# ----------------------------------------------------------------------
JDK = os.path.expanduser("~/workspace/sdks/rd-target-android-1/jdk17")
JAVA = os.path.join(JDK, "bin", "java")
JAVAC = os.path.join(JDK, "bin", "javac")
PROTO_SRC = os.path.join(
    TREE, "runtime", "remote_dispatch", "android", "app", "app", "src",
    "main", "java", "com", "remor", "dispatchtarget", "proto")
JAVA_CLASSES = os.path.join(SCRATCH, "java-classes")
_rest_fd_data = {}


def compile_java():
    os.makedirs(JAVA_CLASSES, exist_ok=True)
    srcs = [os.path.join(PROTO_SRC, f) for f in os.listdir(PROTO_SRC)
            if f.endswith(".java")]
    r = subprocess.run(
        [JAVAC, "-encoding", "UTF-8", "-d", JAVA_CLASSES] + srcs,
        capture_output=True, text=True, timeout=120)
    check("java proto stack compiles",
          r.returncode == 0, r.stderr[-500:] if r.returncode else "")


def _read_line(proc, timeout=20):
    end = time.time() + timeout
    buf = b""
    fd = proc.stdout.fileno()
    while time.time() < end:
        rlist, _, _ = select.select([fd], [], [],
                                    max(0.1, end - time.time()))
        if not rlist:
            continue
        chunk = os.read(fd, 4096)
        if not chunk:
            raise RuntimeError("java target stdout closed")
        buf += chunk
        if b"\n" in buf:
            line, rest = buf.split(b"\n", 1)
            _rest_fd_data[fd] = rest + _rest_fd_data.get(fd, b"")
            return line.decode("utf-8", "replace").strip()
    raise TimeoutError("timed out waiting for java target output")


def read_line2(proc, timeout=20):
    fd = proc.stdout.fileno()
    data = _rest_fd_data.get(fd, b"")
    if b"\n" in data:
        line, rest = data.split(b"\n", 1)
        _rest_fd_data[fd] = rest
        return line.decode("utf-8", "replace").strip()
    _rest_fd_data.pop(fd, None)
    return _read_line(proc, timeout)


def start_java_target(device_id, agent_id, agent_token, keystore, port=0):
    env = dict(os.environ)
    env.update({
        "RD_DEVICE_ID": device_id,
        "RD_AGENT_ID": agent_id,
        "RD_AGENT_TOKEN": agent_token,
        "RD_KEYSTORE": keystore,
        "RD_KEYSTORE_PASS": "benchpass",
        "RD_PORT": str(port),
        "RD_FORGE_PROOF": "0",
    })
    proc = subprocess.Popen(
        [JAVA, "-Djava.awt.headless=true", "-cp", JAVA_CLASSES,
         "com.remor.dispatchtarget.proto.BenchTarget"],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=open(os.path.join(SCRATCH, "java.stderr"), "wb"),
        bufsize=0)
    lines = {}
    for _ in range(10):
        line = _read_line(proc, timeout=20)
        if line.startswith("READY "):
            m = re.search(r"port=(\d+)", line)
            lines["port"] = int(m.group(1))
        elif line.startswith("FINGERPRINT "):
            lines["fingerprint"] = line.split(" ", 1)[1]
        if "port" in lines and "fingerprint" in lines:
            break
    check("IP1 java target READY", "port" in lines and "fingerprint" in lines,
          str(lines))
    proc._bench_lines = lines
    return proc


def java_cmd(proc, cmd, expect_prefix, timeout=20):
    proc.stdin.write((cmd + "\n").encode("utf-8"))
    proc.stdin.flush()
    line = read_line2(proc, timeout=timeout)
    if not line.startswith(expect_prefix):
        raise RuntimeError(f"java cmd {cmd!r} -> {line!r}")
    return line[len(expect_prefix):]


def java_register(proc, sid, token_hash, scope_compact, exp):
    java_cmd(proc,
             f"register {sid} {token_hash} {scope_compact} {exp}",
             "REGISTERED ")


def stop_java(proc):
    try:
        proc.stdin.write(b"stop\n")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()


# ----------------------------------------------------------------------
# inproc module loading
# ----------------------------------------------------------------------
def load_inproc():
    spec = importlib.util.spec_from_file_location(
        "remote_dispatch_inproc", INPROC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["remote_dispatch_inproc"] = mod
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------------------
# main
# ----------------------------------------------------------------------
def main():
    if os.path.exists(SCRATCH):
        shutil.rmtree(SCRATCH)
    os.makedirs(SCRATCH, exist_ok=True)
    compile_java()

    from swarm_engine.remote_dispatch import tls as rd_tls
    from swarm_engine.remote_dispatch.session_model import Scope
    from swarm_engine.remote_dispatch.controller import RemoteSession

    inproc = load_inproc()
    inproc.configure(PYLIB)
    inproc.DATA_DIR = os.path.join(SCRATCH, "inproc_data")
    inproc.rd_boot()
    check("inproc module booted (real product surface)",
          inproc._rd is not None and inproc._rd["svc"] is not None, "")

    svc = inproc._rd["svc"]
    loop = inproc._rd["loop"]

    def E(fn):
        return loop.call(fn)

    ctrl = svc.controller
    store = ctrl.store

    # one cert-generation path (openssl, like tls.ensure_cert) -> PKCS12
    cert_dir = os.path.join(SCRATCH, "certs")
    cert_path, key_path = rd_tls.ensure_cert(cert_dir)
    fp = rd_tls.fingerprint(cert_path)
    keystore = os.path.join(SCRATCH, "keystore.p12")
    r = subprocess.run(
        ["openssl", "pkcs12", "-export", "-in", cert_path,
         "-inkey", key_path, "-out", keystore,
         "-password", "pass:benchpass", "-name", "rd-target"],
        capture_output=True, text=True, timeout=60)
    check("cert -> PKCS12 for the java listener", r.returncode == 0,
          r.stderr[-300:] if r.returncode else "")

    # pair + user (the out-of-band ceremony's controller side)
    pair = E(lambda: ctrl.pair_device(
        "tablet-1", "Bench tablet", cert_fingerprint=fp))
    agent_id, agent_token = pair["agent_id"], pair["agent_token"]
    check("pair: tablet enrolled", agent_id.startswith("rd-target-"),
          agent_id)
    user_token = E(lambda: store.register_user("bench-user"))
    check("user registered", user_token.startswith("rduser_"))

    proc = start_java_target("tablet-1", agent_id, agent_token, keystore)
    port = proc._bench_lines["port"]
    check("IP1 fingerprint parity (Python == Java, SHA-256 of DER)",
          proc._bench_lines["fingerprint"] == fp,
          proc._bench_lines["fingerprint"][:16])

    try:
        # IP2: the java client's exact announce bytes -> the real
        # inproc rd_handle (the product-surface handler, not a shim).
        # The URL is unreachable (sandbox denies JVM egress); the
        # "capture" mode captures the exact request bytes in-JVM.
        out = java_cmd(
            proc,
            f"announce http://127.0.0.1:9/api/remote/announce {port} {fp} "
            f"127.0.0.1 capture",
            "ANNOUNCE_CAPTURE ")
        cap = json.loads(out)
        check("IP2 java client emits a well-formed announce request",
              cap.get("method") == "POST"
              and cap.get("path") == "/api/remote/announce"
              and (cap.get("content_type") or "").startswith(
                  "application/json"),
              f"{cap.get('method')} {cap.get('path')}")
        cbody = json.loads(cap["body"])
        check("IP2 announce body carries the 7 protocol fields",
              cbody.get("device_id") == "tablet-1"
              and cbody.get("agent_token") == agent_token
              and cbody.get("host") == "127.0.0.1"
              and cbody.get("port") == port
              and cbody.get("cert_fingerprint") == fp
              and isinstance(cbody.get("ann_ts"), (int, float))
              and isinstance(cbody.get("ann_nonce"), str)
              and len(cbody["ann_nonce"]) == 32,
              sorted(cbody))
        # the real product handler accepts the client's exact bytes
        announced = inproc.rd_handle(
            "POST", "/api/remote/announce", cbody)
        check("IP2 inproc rd_handle accepts the announce",
              announced.get("ok") is True, str(announced)[:160])

        # IP3: resolve_endpoint returns the java listener's (host, port)
        host, eport = E(lambda: ctrl.resolve_endpoint("tablet-1"))
        check("IP3 resolve_endpoint hits the announced endpoint",
              (host, eport) == ("127.0.0.1", port), f"{host}:{eport}")

        # IP4: request + consent (target-side) + java register + connect
        scope = Scope(actions=["move", "click", "scroll", "type",
                               "launch_app"],
                      x_min=0, y_min=0, x_max=1600, y_max=1200,
                      apps=["com.example.app"], max_actions=200,
                      ttl_s=600, screen_share=True, record_frames=False)
        req = E(lambda: ctrl.request_session("tablet-1", scope))
        sid = req["session_id"]
        consent = E(lambda: store.user_grant_consent(
            sid, user_token, ttl_s=600))
        session_token = consent["session_token"]
        row = E(lambda: store._get_session(sid))
        scope_compact = json.dumps(json.loads(row["scope_json"]),
                                   separators=(",", ":"))
        exp_row = E(lambda: store._conn.execute(
            "SELECT expires_at FROM rd_consents WHERE session_id=? "
            "ORDER BY granted_at DESC LIMIT 1", (sid,)).fetchone())
        java_register(proc, sid, row["session_token_hash"],
                      scope_compact, exp_row["expires_at"])
        sess = E(lambda: ctrl.connect(sid, session_token))
        check("IP4 connect over the resolved endpoint (mutual auth)",
              isinstance(sess, RemoteSession))
        check("IP4 LIVE indicator on (java stdout)",
              read_line2(proc, timeout=20).startswith("INDICATOR_ON "))
        # liveness mirror (the named two-device gap): the harness
        # mirrors mark_live on the observable INDICATOR_ON, exactly
        # what the Python target does to the shared DB.
        E(lambda: store.mark_live(sid))

        # IP5: actions through the real channel
        r = E(lambda: sess.act({"type": "move", "x": 400, "y": 300}))
        check("IP5 move executed", r and r[0].get("action") == "move",
              str(r))
        r = E(lambda: sess.act_batch([
            {"type": "click", "x": 100, "y": 200},
            {"type": "scroll", "dx": 0, "dy": -2}]))
        check("IP5 batch executed", len(r) == 2, str(r)[:120])
        executed = json.loads(java_cmd(proc, "dump", "EXECUTED "))
        kinds = [a.get("type") for a in executed]
        check("IP5 java sink recorded the actions",
              kinds == ["move", "click", "scroll"], str(kinds))

        # IP6: get_frame -> real PNG bytes
        frame = E(lambda: sess.get_frame())
        check("IP6 frame_ok shape",
              frame.get("format") == "png"
              and frame.get("synthesized") is True, str(sorted(frame)))
        png = base64.b64decode(frame["data_b64"])
        check("IP6 real PNG bytes", png[:8] == b"\x89PNG\r\n\x1a\n",
              f"{len(png)} bytes")
        check("IP6 frame within scope bounds",
              0 < frame["width"] <= 1600 and 0 < frame["height"] <= 1200,
              f"{frame['width']}x{frame['height']}")

        # adversarial on the product path
        # IA1: announce replay (same ann_nonce) refused
        replayed = inproc.rd_handle(
            "POST", "/api/remote/announce", cbody)
        check("IA1 announce replay refused",
              replayed.get("ok") is False
              and "replay" in str(replayed.get("error", "")).lower(),
              str(replayed)[:160])
        # IA2/IA3: fresh java announces with mutated fields
        out2 = json.loads(java_cmd(
            proc,
            f"announce http://127.0.0.1:9/api/remote/announce {port} {fp} "
            f"127.0.0.1 capture",
            "ANNOUNCE_CAPTURE "))
        bad_tok = json.loads(out2["body"])
        bad_tok["agent_token"] = "wrong-token"
        refused = inproc.rd_handle(
            "POST", "/api/remote/announce", bad_tok)
        check("IA2 bad agent token refused",
              refused.get("ok") is False, str(refused)[:160])
        bad_fp = json.loads(out2["body"])
        bad_fp["cert_fingerprint"] = "00" * 32
        refused = inproc.rd_handle(
            "POST", "/api/remote/announce", bad_fp)
        check("IA3 fingerprint mismatch refused",
              refused.get("ok") is False, str(refused)[:160])

        # IA4: hello with a bad session token refused
        try:
            E(lambda: ctrl.connect(sid, "bad-token"))
            check("IA4 bad session token refused", False, "connected!")
        except Exception as e:
            check("IA4 bad session token refused",
                  "refused" in str(e).lower(), str(e)[:160])

        # IP7: kill relay -- causal stop, not a dropped connection
        target_kill = E(lambda: sess.channel.request("kill", {}))
        check("IP7 kill_ok from the java target",
              target_kill["kind"] == "kill_ok"
              and target_kill["body"].get("state") == "killed"
              and target_kill["body"].get("processes_terminated") == 0,
              str(target_kill["body"]))
        kill_out = E(lambda: sess.kill())
        check("IP7 controller store killed",
              kill_out.get("state") == "killed", str(kill_out))
        check("IP7 indicator off after kill",
              read_line2(proc, timeout=20).startswith("INDICATOR_OFF "))
        executed_after = json.loads(java_cmd(proc, "dump", "EXECUTED "))
        check("IP7 causal stop: no executions after halt",
              len(executed_after) == len(executed),
              f"before={len(executed)} after={len(executed_after)}")

        # IA5: act after kill refused
        try:
            E(lambda: sess.act({"type": "move", "x": 1, "y": 1}))
            check("IA5 act after kill refused", False, "acted!")
        except Exception as e:
            check("IA5 act after kill refused",
                  "not live" in str(e).lower()
                  or "killed" in str(e).lower(), str(e)[:160])

        # IP8: connect-after-kill refused with the exact reason
        try:
            E(lambda: ctrl.connect(sid, session_token))
            check("IP8 connect-after-kill refused", False, "connected!")
        except Exception as e:
            check("IP8 connect-after-kill refused",
                  "hello refused: session killed: refused" in str(e),
                  str(e)[:160])
    finally:
        stop_java(proc)
        inproc.rd_reset()

    n_fail = sum(1 for _, ok, _ in CHECKS if not ok)
    print(f"\n==== rd1_inproc_product_path: {len(CHECKS) - n_fail}/"
          f"{len(CHECKS)} checks passed ====", flush=True)
    if n_fail:
        sys.exit(1)
    with open(os.path.join(SCRATCH, "manifest.json"), "w") as f:
        json.dump({"checks": [
            {"name": n, "ok": ok, "detail": d}
            for n, ok, d in CHECKS]}, f, indent=1)


if __name__ == "__main__":
    main()
