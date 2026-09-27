"""Track 3 driver CLI: phase1 | phase2 | http | all.

Usage:
    python driver_track3_main.py phase1 --workdir <dir>     # fresh deployment
    python driver_track3_main.py phase2 --workdir <dir>     # NEW PROCESS
    python driver_track3_main.py http --workdir <dir>       # real HTTP check
    python driver_track3_main.py all --workdir <dir>        # phase1 + http
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pylib"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def cmd_phase1(workdir):
    import shutil
    # Fresh workdir: remove any prior L and L_anchor from earlier runs
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    anchor_dir = os.path.abspath(workdir) + "_anchor"
    if os.path.exists(anchor_dir):
        shutil.rmtree(anchor_dir)
    os.makedirs(workdir, exist_ok=True)
    from driver_track3_ph1 import phase1
    res = phase1(workdir)
    print(json.dumps({"phase1_checks": len(res["checks"]),
                      "failed": [c for c in res["checks"] if not c["ok"]]},
                     indent=2))


def cmd_phase2(workdir):
    # MUST run in a genuinely new process: refuse if phase1 ran in this one.
    if os.environ.get("TRACK3_PHASE1_DONE") == "1":
        raise SystemExit("phase2 must run in a fresh process")
    from driver_track3_ph2 import phase2
    res = phase2(workdir)
    print(json.dumps({"phase2_checks": len(res["checks"]),
                      "failed": [c for c in res["checks"] if not c["ok"]]},
                     indent=2))


def cmd_http(workdir):
    """Real HTTP: serve the deployment, GET the evidence doc, verify."""
    import threading
    import time
    import traceback
    import urllib.request
    import urllib.error
    from swarm_engine.services.http_adapter import run
    db_path = os.path.join(workdir, "engine.db")
    holder = {}
    errors = {}

    def _serve():
        # run() builds the engine in the serving thread (thread-affinity).
        # Ephemeral port: a fixed port flakes when two runners (or a
        # leftover server) contend for it; the test's purpose is the
        # evidence round-trip, not the port number.
        try:
            server, _svc = run(db_path, port=0)
            holder["server"] = server
            holder["port"] = server.server_address[1]
            server.serve_forever()
        except Exception:
            errors["trace"] = traceback.format_exc()

    srv = threading.Thread(target=_serve, daemon=True)
    srv.start()
    # Wait for the bind instead of a fixed sleep: on a loaded machine
    # engine boot can exceed 1s, and a single immediate urlopen then
    # fails with connection refused. Surface the thread's real error
    # if it died instead of masking it as a refused connection.
    deadline = time.time() + 60
    while "port" not in holder and time.time() < deadline:
        if "trace" in errors:
            raise AssertionError(
                "http server thread failed:\n" + errors["trace"])
        time.sleep(0.1)
    if "port" not in holder:
        raise AssertionError(
            "http server did not bind within 60s" +
            (" :\n" + errors["trace"] if "trace" in errors else ""))
    port = holder["port"]
    try:
        with open(os.path.join(workdir, "track3_meta.json")) as fh:
            meta = json.load(fh)
        ev_id = meta["evidence_id"]
        # The adapter bearer-gates every route: read the provisioned token
        # (<workdir>/service_data/api_token) and send it. An unauthenticated
        # request is correctly 401 -- the check must authenticate, not the
        # server relax.
        token_path = os.path.join(workdir, "service_data", "api_token")
        with open(token_path) as fh:
            token = fh.read().strip()

        def _get(url):
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"})
            return urllib.request.urlopen(req, timeout=10)

        url = f"http://127.0.0.1:{port}/api/dispatch_evidence/{ev_id}"
        with _get(url) as resp:
            assert resp.status == 200, resp.status
            body = json.loads(resp.read().decode())
        ev = body["evidence"]
        assert ev["evidence_id"] == ev_id, ev
        assert ev["agent_id"] == meta["agent_a"], ev
        assert ev["dispatch_id"] == meta["dispatch_id"], ev
        assert ev["ok"] == "1", ev
        assert json.loads(ev["result_json"]) == [1, 3, 5, 8], ev
        assert json.loads(ev["evidence_json"])["ok"] is True
        # unknown evidence -> 404, never 200 with fabricated content
        try:
            _get(f"http://127.0.0.1:{port}/api/dispatch_evidence/dsp_ev_nope")
            raise SystemExit("HTTP 404 expected for unknown evidence")
        except urllib.error.HTTPError as e:
            assert e.code == 404, e.code
        print("HTTP evidence retrieval: PASS", flush=True)
        print(json.dumps({"evidence_id": ev_id, "http": "ok", "http_checks": 1}))
    finally:
        holder.get("server", None) and holder["server"].shutdown()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["phase1", "phase2", "http", "all"])
    ap.add_argument("--workdir", required=True)
    args = ap.parse_args()
    workdir = os.path.abspath(os.path.expanduser(args.workdir))
    if args.cmd == "phase1":
        cmd_phase1(workdir)
        os.environ["TRACK3_PHASE1_DONE"] = "1"
    elif args.cmd == "phase2":
        cmd_phase2(workdir)
    elif args.cmd == "http":
        cmd_http(workdir)
    elif args.cmd == "all":
        # Pure orchestrator: each phase runs in its OWN process. The engine
        # owns <workdir>/engine.db via a process-lifetime lockfile
        # (DuplicateEngineError), so phase1's engine must be fully released
        # (process exit) before http's engine claims the db. Same discipline
        # as phase2 (fresh process per phase).
        import subprocess as _sp
        for sub in ("phase1", "http"):
            r = _sp.run([sys.executable, os.path.abspath(__file__),
                         sub, "--workdir", workdir])
            if r.returncode != 0:
                raise SystemExit(r.returncode)


if __name__ == "__main__":
    main()
