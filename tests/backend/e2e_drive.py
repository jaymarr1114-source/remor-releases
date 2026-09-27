#!/usr/bin/env python3
"""REMOR third-track OPERATIONAL check (James's bar 3).

Drives every feature end-to-end over real HTTP against the real services,
exactly as the REMOR GUI frontend will call them. No mocks, no canned data:
a live ThreadingHTTPServer in-process, urllib client, real SwarmEngine,
real sqlite, real subprocess sandboxes, scratch dirs only.

Usage: python3 e2e_drive.py
Exit 0 = every operational step passed; nonzero = first failure details.
"""
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pylib")))

from swarm_engine.services.http_adapter import serve, close_services  # noqa: E402

GAP_GOAL = "for input x compute x cubed plus two to the power of x"
GAP_EXAMPLES = [[{"x": x}, x ** 3 + 2 ** x] for x in range(7)]
SIMPLE_GOAL = "for input x compute x squared plus three"
SIMPLE_EXAMPLES = [[{"x": x}, x ** 2 + 3] for x in range(5)]

BASE = None
TOKEN = None
OP_ID = None
OP_TOKEN = None
STEPS = []


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    if method in ("POST", "PUT", "DELETE", "PATCH") and OP_ID and OP_TOKEN:
        # Merged contract: mutating routes need operator credentials.
        headers["X-Agent-Id"] = OP_ID
        headers["X-Agent-Token"] = OP_TOKEN
    r = urllib.request.Request(
        BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload


def step(name):
    print(f"  -- {name}", flush=True)


def check(cond, msg):
    STEPS.append((msg, bool(cond)))
    print(f"     [{'PASS' if cond else 'FAIL'}] {msg}", flush=True)
    if not cond:
        raise AssertionError(msg)


def poll(path, want, timeout, field="status"):
    """Poll GET path until field in want (or timeout). Returns the body."""
    end = time.time() + timeout
    last = None
    while time.time() < end:
        st, body = req("GET", path)
        last = body
        if st == 200 and body.get(field) in want:
            return body
        time.sleep(0.5)
    raise AssertionError(
        f"poll {path}: wanted {field} in {want}, last={str(last)[:300]}")


def main():
    global BASE, TOKEN, OP_ID, OP_TOKEN
    tmp = tempfile.mkdtemp(prefix="remor_ff_e2e_")
    server, services, thread, BASE = serve(tmp)
    with open(os.path.join(tmp, "api_token"), encoding="utf-8") as fh:
        TOKEN = fh.read().strip()
    with open(os.path.join(tmp, "operator.token"), encoding="utf-8") as fh:
        OP_TOKEN = fh.read().strip()
    OP_ID = server.svc.operator_id
    print(f"adapter on {BASE} (data: {tmp})", flush=True)
    try:
        # ---- 1. run queue: preemption over HTTP -------------------------
        step("submit slow run")
        st, r1 = req("POST", "/api/runs",
                     {"goal": GAP_GOAL, "examples": GAP_EXAMPLES})
        check(st == 200 and r1.get("ok") and r1.get("run_id"),
              f"slow run submitted (status={st})")
        rid1 = r1["run_id"]

        step("run becomes running")
        poll(f"/api/runs/{rid1}", {"running"}, 60)
        check(True, "slow run reached 'running'")

        step("pause over HTTP")
        st, pr = req("POST", f"/api/runs/{rid1}/pause")
        check(st == 200 and pr.get("ok"), f"pause accepted ({pr})")
        poll(f"/api/runs/{rid1}", {"paused"}, 30)
        check(True, "run reached 'paused'")

        step("resume over HTTP")
        st, rr = req("POST", f"/api/runs/{rid1}/resume")
        check(st == 200 and rr.get("ok"), f"resume accepted ({rr})")
        poll(f"/api/runs/{rid1}", {"running"}, 30)
        check(True, "run back to 'running' after resume")

        step("stop over HTTP")
        st, sr = req("POST", f"/api/runs/{rid1}/stop")
        check(st == 200 and sr.get("ok"), f"stop accepted ({sr})")
        done1 = poll(f"/api/runs/{rid1}", {"stopped"}, 60)
        check(True, "run reached 'stopped'")
        st, evs = req("GET", f"/api/runs/{rid1}/events")
        stages = [e.get("stage") for e in evs if e.get("type") == "stage"]
        check(st == 200 and "stopped" in stages,
              f"STOPPED marker in real event trace {stages}")

        # ---- 2. queue advances: FIFO ------------------------------------
        step("queue: second run completes after stop")
        st, r2 = req("POST", "/api/runs",
                     {"goal": SIMPLE_GOAL, "examples": SIMPLE_EXAMPLES})
        check(st == 200 and r2.get("ok"), "quick run submitted")
        rid2 = r2["run_id"]
        done2 = poll(f"/api/runs/{rid2}", {"completed", "failed"}, 180)
        out = done2.get("outcome") or {}
        check(done2["status"] == "completed" and out.get("success") is True,
              f"quick run completed honestly: {str(done2)[:200]}")
        check(done2["started_at"] >= done1["ended_at"],
              "FIFO: second run started after first ended (no overlap)")
        st, runs = req("GET", "/api/runs")
        by_id = {r["id"]: r["status"] for r in runs}
        check(by_id.get(rid1) == "stopped" and by_id.get(rid2) == "completed",
              f"run list honest: {by_id.get(rid1)}, {by_id.get(rid2)}")

        # ---- 3. projects -------------------------------------------------
        step("project create/list/get")
        st, pc = req("POST", "/api/projects",
                     {"kind": "blank", "project_id": "e2e-proj"})
        check(st == 200 and pc.get("ok"), f"project created ({pc})")
        st, pl = req("GET", "/api/projects")
        check(any(p["project_id"] == "e2e-proj" for p in pl),
              "project listed")
        st, pg = req("GET", "/api/projects/e2e-proj")
        check(st == 200 and pg.get("ok") and pg["state"] == "ingested"
              and "analyzing" in pg.get("legal_next", []),
              f"project state honest: {pg.get('state')}")

        step("illegal transition refused over HTTP")
        st, bad = req("POST", "/api/projects/e2e-proj/transition",
                      {"to_state": "complete"})
        check(st == 409 and not bad.get("ok"),
              f"ingested->complete refused with 409 ({bad.get('error','')[:80]})")

        step("legal chain to executing")
        for nxt in ("analyzing", "planning", "executing"):
            st, tr = req("POST", "/api/projects/e2e-proj/transition",
                         {"to_state": nxt, "reason": "e2e"})
            check(st == 200 and tr.get("ok"), f"transition -> {nxt}")

        step("run_loop monitored over HTTP")
        st, jl = req("POST", "/api/projects/e2e-proj/run_loop",
                     {"max_rounds": 2})
        check(st == 200 and jl.get("ok") and jl.get("job_id"),
              f"loop job started ({jl})")
        job = jl["job_id"]
        end = time.time() + 300
        final = None
        while time.time() < end:
            st, js = req("GET", f"/api/projects/e2e-proj/loop/{job}")
            final = js
            if st == 200 and js.get("status") in (
                    "completed", "failed", "stopped"):
                break
            time.sleep(1.0)
        check(final is not None and final.get("status") in (
            "completed", "failed", "stopped"),
            f"loop reached terminal status: {(final or {}).get('status')}")
        st, pg2 = req("GET", "/api/projects/e2e-proj")
        check(st == 200 and len(pg2.get("history", [])) >= 3,
              f"lifecycle history real: {len(pg2.get('history', []))} rows")

        # ---- 4. files ----------------------------------------------------
        step("file write/list/read over HTTP")
        st, fw = req("POST", "/api/files/write",
                     {"path": "hello.txt", "content": "hello remor"})
        check(st == 200 and fw.get("ok"), "file written")
        st, fl = req("GET", "/api/files?path=" + urllib.parse.quote(""))
        check(st == 200 and any(e["name"] == "hello.txt" for e in fl["entries"]),
              "file listed")
        st, fc = req("GET", "/api/files/content?path=hello.txt")
        check(st == 200 and fc.get("content") == "hello remor",
              "file content read back")

        step("traversal attack refused over HTTP")
        st, atk = req("GET", "/api/files/content?path=" +
                      urllib.parse.quote("../../etc/passwd"))
        check(st == 400 and not atk.get("ok"),
              f"traversal refused ({atk.get('error','')[:60]})")

        # ---- 5. artifacts ------------------------------------------------
        step("artifact save/run/edit/run over HTTP")
        st, a1 = req("POST", "/api/artifacts",
                     {"name": "e2e-calc", "language": "python",
                      "code": "print(sum(i*i for i in range(100)))"})
        check(st == 200 and a1.get("ok") and a1.get("revision") == 1,
              f"artifact saved rev 1 ({a1})")
        aid = a1["artifact_id"]
        st, ar = req("POST", f"/api/artifacts/{aid}/run", {})
        check(st == 200 and ar.get("ok") and "328350" in ar.get("stdout", ""),
              f"artifact ran for real: {ar.get('stdout','')[:40]!r}")
        st, a2 = req("POST", "/api/artifacts",
                     {"name": "e2e-calc", "language": "python",
                      "code": "print(sum(i*i for i in range(10)))"})
        check(st == 200 and a2.get("revision") == 2, "edit saved as rev 2")
        st, ar2 = req("POST", f"/api/artifacts/{aid}/run", {})
        check("285" in ar2.get("stdout", ""),
              "latest revision runs edited code")
        st, ar1 = req("POST", f"/api/artifacts/{aid}/run", {"revision": 1})
        check("328350" in ar1.get("stdout", ""),
              "pinned revision 1 still runs old code")
        st, al = req("GET", "/api/artifacts")
        check(any(a["artifact_id"] == aid and a["latest_revision"] == 2
                  for a in al), "artifact listed with latest rev 2")

        print(f"\nE2E OPERATIONAL: {len(STEPS)}/{len(STEPS)} steps passed",
              flush=True)
    finally:
        close_services(services)
        server.shutdown()


if __name__ == "__main__":
    main()
