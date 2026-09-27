"""Worker D (S4/S5/S6/S10/S12): quotas, metering bypass fix, project linkage,
bearer auth, and project retirement -- all proven over REAL HTTP.

No mocks: urllib -> live HTTPServer (ephemeral port, 127.0.0.1) ->
_Handler -> the real services on scratch dirs. The bearer token is read
from <base_dir>/api_token, exactly as a real operator would.

Private work root: /tmp/remor_gapfill_d (via tempfile).
"""
import inspect
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.auth import AuthGate  # noqa: E402
from swarm_engine.services.http_adapter import (  # noqa: E402
    close_services,
    serve,
)

QUICK_GOAL = "run knowledge_stats"  # real pipeline, ~0.02s per run
SLOW_GOAL = "run compute the triple of n"
SLOW_EXAMPLES = [({"n": 4}, 12), ({"n": 7}, 21)]
TERMINAL = ("completed", "failed", "stopped", "cancelled", "error")
SYMBOLIC = "tpl_symbolic_coder_v1"
CALLABLE = "tpl_callable_coder_v1"


class _HttpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="remor_gapfill_d_",
                                               dir="/tmp")
        self.base = self.tmp.name
        self.server, self.ff, self.thread, self.url = serve(self.base)
        with open(os.path.join(self.base, "api_token"),
                  encoding="utf-8") as fh:
            self.token = fh.read().strip()
        self.assertTrue(self.token)
        # token file must be owner-only
        mode = os.stat(os.path.join(self.base, "api_token")).st_mode & 0o777
        self.assertEqual(mode, 0o600, f"api_token mode {oct(mode)}")
        # Merged contract: mutating routes additionally require the real
        # operator credential from <base_dir>/operator.token.
        with open(os.path.join(self.base, "operator.token"),
                  encoding="utf-8") as fh:
            self.op_token = fh.read().strip()
        self.assertTrue(self.op_token)
        self.op_id = self.server.svc.operator_id

    def tearDown(self):
        try:
            self.server.shutdown()
        finally:
            try:
                close_services(self.ff)
            finally:
                self.tmp.cleanup()

    # -- client ---------------------------------------------------------
    def req(self, method, path, body=None, token="default", op=False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token == "default":
            headers["Authorization"] = "Bearer " + self.token
        elif token is not None:
            headers["Authorization"] = "Bearer " + token
        if op:
            # Merged contract: mutating routes need operator credentials
            # on top of the Bearer gate.
            headers["X-Agent-Id"] = self.op_id
            headers["X-Agent-Token"] = self.op_token
        r = urllib.request.Request(self.url + path, data=data,
                                   method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode() or "{}")
            except Exception:
                payload = {}
            return e.code, payload

    def poll_run(self, run_id, want, timeout=120.0):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            st, body = self.req("GET", f"/api/runs/{run_id}")
            last = body
            if st == 200 and body.get("status") in want:
                return body
            time.sleep(0.2)
        self.fail(f"run {run_id}: wanted status in {want}, last={last}")


class AuthGateTest(_HttpBase):
    """S10: bearer-token gate on every route."""

    def test_no_header_is_401(self):
        st, body = self.req("GET", "/api/health", token=None)
        self.assertEqual(st, 401)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("code"), "auth_required")

    def test_wrong_token_is_401(self):
        st, body = self.req("GET", "/api/health", token="wrong-token")
        self.assertEqual(st, 401)
        self.assertEqual(body.get("code"), "auth_invalid")

    def test_right_token_is_200(self):
        st, body = self.req("GET", "/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(body.get("ok"))

    def test_gate_covers_post_and_delete(self):
        st, _ = self.req("POST", "/api/runs", {"goal": "x"}, token=None)
        self.assertEqual(st, 401)
        st, _ = self.req("DELETE", "/api/artifacts/1", token="bogus")
        self.assertEqual(st, 401)

    def test_gate_is_active(self):
        self.assertTrue(self.ff["auth"].is_gated())

    def test_comparison_is_timing_safe(self):
        # The mechanism, not just the behavior: hmac.compare_digest.
        src = inspect.getsource(AuthGate.check)
        self.assertIn("compare_digest", src)

    def test_permissive_when_no_token_configured(self):
        env = dict(os.environ)
        os.environ.pop("REMOR_API_TOKEN", None)
        try:
            gate = AuthGate()  # no base_dir, no env, no explicit token
            self.assertFalse(gate.is_gated())
            self.assertIsNone(gate.check("GET", "/api/health", {}))
        finally:
            os.environ.clear()
            os.environ.update(env)

    def test_env_token_wins_over_file(self):
        env = dict(os.environ)
        os.environ["REMOR_API_TOKEN"] = "env-token-value"
        try:
            gate = AuthGate(base_dir=self.base)  # file exists with other value
            self.assertTrue(gate.is_gated())
            ok = gate.check("GET", "/api/x",
                            {"Authorization": "Bearer env-token-value"})
            self.assertIsNone(ok)
            bad = gate.check("GET", "/api/x",
                             {"Authorization": "Bearer " + self.token})
            self.assertEqual(bad[0], 401)
        finally:
            os.environ.clear()
            os.environ.update(env)


class MeteringBypassTest(_HttpBase):
    """S5: POST /api/runs is guarded (35/day, then 25-queue)."""

    def test_daily_cap_enforced_over_http(self):
        # Deterministic daily-cap proof over real HTTP. The worker is
        # parked on a real slow run (real pause machinery), so no quick
        # run ever advances: each submit below adds exactly one DISTINCT
        # task to the 24h window (cancelled runs keep counting -- they
        # were submitted tasks).
        #
        # Task arithmetic (the corrected metering contract counts distinct
        # submitted runs, one task = one count, against the advertised
        # 35/day): 1 slow + 24 quick + 10 quick = 35 distinct tasks, so
        # the 36th submit trips can_submit() with the typed
        # daily_task_cap refusal. Batches stay under the 25-queue cap
        # (24, drained by real HTTP cancels, then 10).
        st, slow = self.req("POST", "/api/runs",
                            {"goal": SLOW_GOAL, "examples": SLOW_EXAMPLES}, op=True)
        self.assertEqual(st, 200, slow)
        slow_id = slow["run_id"]
        self.poll_run(slow_id, ("running",))
        st, p = self.req("POST", f"/api/runs/{slow_id}/pause", op=True)
        self.assertEqual(st, 200, p)
        self.poll_run(slow_id, ("paused",))
        try:
            ids = []
            for i in range(24):
                st, body = self.req("POST", "/api/runs",
                                    {"goal": f"{QUICK_GOAL} d{i}"}, op=True)
                self.assertEqual(st, 200, (i, st, body))
                self.assertTrue(body.get("ok"), (i, body))
                ids.append(body["run_id"])
            for rid in ids:
                st, c = self.req("POST", f"/api/runs/{rid}/cancel", op=True)
                self.assertEqual(st, 200, (rid, st, c))
            for i in range(10):
                st, body = self.req("POST", "/api/runs",
                                    {"goal": f"{QUICK_GOAL} e{i}"}, op=True)
                self.assertEqual(st, 200, (i, st, body))
                self.assertTrue(body.get("ok"), (i, body))
            # 35 distinct tasks submitted >= 35/day: the next task is
            # refused for the day.
            st, body = self.req("POST", "/api/runs", {"goal": QUICK_GOAL}, op=True)
            self.assertEqual(st, 429, body)
            self.assertFalse(body.get("ok"))
            self.assertIn("limit", body)
            self.assertEqual(body["limit"]["code"], "daily_task_cap")
            self.assertEqual(body["limit"]["limit"], 35)
            self.assertEqual(body["limit"]["current"], 35)
        finally:
            st, s = self.req("POST", f"/api/runs/{slow_id}/stop", op=True)
            self.assertEqual(st, 200, s)
            self.poll_run(slow_id, TERMINAL, timeout=120.0)

    def test_queue_full_refused_over_http(self):
        # Occupy the single worker with a real slow run, parked at a
        # checkpoint via the real pause machinery; then fill the queue.
        st, slow = self.req("POST", "/api/runs",
                            {"goal": SLOW_GOAL, "examples": SLOW_EXAMPLES}, op=True)
        self.assertEqual(st, 200, slow)
        slow_id = slow["run_id"]
        self.poll_run(slow_id, ("running",))
        st, p = self.req("POST", f"/api/runs/{slow_id}/pause", op=True)
        self.assertEqual(st, 200, p)
        self.poll_run(slow_id, ("paused",))
        # Worker is parked: nothing drains while we queue 25 real tasks.
        for i in range(25):
            st, body = self.req("POST", "/api/runs",
                                {"goal": f"{QUICK_GOAL} q{i}"}, op=True)
            self.assertEqual(st, 200, (i, st, body))
            self.assertTrue(body.get("ok"), (i, body))
        st, body = self.req("POST", "/api/runs", {"goal": QUICK_GOAL}, op=True)
        self.assertEqual(st, 429, body)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body["limit"]["code"], "queue_cap")
        self.assertEqual(body["limit"]["limit"], 25)
        self.assertEqual(body["limit"]["current"], 25)
        # Cleanup: stop overrides the pause; worker drains.
        st, s = self.req("POST", f"/api/runs/{slow_id}/stop", op=True)
        self.assertEqual(st, 200, s)
        self.poll_run(slow_id, TERMINAL, timeout=120.0)


class AgentQuotaTest(_HttpBase):
    """S4: the 3-agent free-tier cap, end to end over HTTP."""

    def test_three_agents_then_fourth_refused_over_http(self):
        ids = []
        for i in range(3):
            st, body = self.req("POST", "/api/agents",
                                {"template_id": SYMBOLIC}, op=True)
            self.assertEqual(st, 200, (i, st, body))
            self.assertTrue(body.get("ok"), (i, body))
            ids.append(body["agent"]["agent_id"])
        self.assertEqual(len(set(ids)), 3)
        st, body = self.req("POST", "/api/agents",
                            {"template_id": CALLABLE}, op=True)
        self.assertEqual(st, 429, body)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body["limit"]["code"], "free_tier_agent_cap")
        self.assertEqual(body["limit"]["limit"], 3)
        # Destroying one frees a slot: the guard is the mechanism.
        st, d = self.req("POST", f"/api/agents/{ids[0]}/destroy", {}, op=True)
        self.assertEqual(st, 200, d)
        st, body = self.req("POST", "/api/agents",
                            {"template_id": CALLABLE}, op=True)
        self.assertEqual(st, 200, (st, body))
        self.assertTrue(body.get("ok"), body)

    def test_concurrency_reported_honestly(self):
        # The substrate runs ONE worker thread; the plan's "3 concurrent"
        # is a ceiling, not a fact. Nothing fakes 3.
        st, body = self.req("GET", "/api/metering")
        self.assertEqual(st, 200, body)
        usage = body.get("usage") or body
        conc = self.ff["metering"].concurrency()
        self.assertEqual(conc["concurrent_runs"], 1)
        self.assertEqual(conc["plan_ceiling"], 3)
        self.assertIn("not implemented", conc["plan_ceiling_status"])


class ProjectLinkageTest(_HttpBase):
    """S6: scheduler.submit(project_id=...) linkage, over HTTP + reopen."""

    def test_submit_with_project_id_queryable_over_http(self):
        st, a = self.req("POST", "/api/runs",
                         {"goal": QUICK_GOAL, "project_id": "proj-link-1"}, op=True)
        self.assertEqual(st, 200, a)
        rid_a = a["run_id"]
        st, b = self.req("POST", "/api/runs",
                         {"goal": QUICK_GOAL, "project_id": "proj-link-2"}, op=True)
        self.assertEqual(st, 200, b)
        st, body = self.req("GET", "/api/runs?project_id=proj-link-1")
        self.assertEqual(st, 200, body)
        self.assertTrue(body.get("ok"))
        ids = [r["id"] for r in body["runs"]]
        self.assertIn(rid_a, ids)
        self.assertNotIn(b["run_id"], ids)
        self.assertTrue(all(r["project_id"] == "proj-link-1"
                            for r in body["runs"]))

    def test_linkage_survives_fresh_process_reopen(self):
        st, a = self.req("POST", "/api/runs",
                         {"goal": QUICK_GOAL, "project_id": "proj-reopen-1"}, op=True)
        self.assertEqual(st, 200, a)
        rid = a["run_id"]
        # Fresh process: shut everything down, reopen the scheduler on the
        # same DB files, and the linkage must still be there.
        self.server.shutdown()
        close_services(self.ff)
        from swarm_engine.services.scheduler import RunScheduler
        sched2 = RunScheduler(
            os.path.join(self.base, "scheduler.db"),
            os.path.join(self.base, "runtime.db"))
        try:
            rows = sched2.project_runs("proj-reopen-1")
            self.assertEqual([r["id"] for r in rows], [rid])
            self.assertEqual(rows[0]["project_id"], "proj-reopen-1")
        finally:
            sched2.close()
        # tearDown re-runs shutdown/close_services: both are idempotent.


class ProjectRetirementTest(_HttpBase):
    """S12: retire -> hidden from active list -> resume -> intact."""

    PID = "retire-http-1"

    def test_retire_archive_resume_run_loop(self):
        st, c = self.req("POST", "/api/projects",
                         {"kind": "blank", "project_id": self.PID}, op=True)
        self.assertEqual(st, 200, c)
        self.assertTrue(c.get("ok"), c)

        st, listed = self.req("GET", "/api/projects")
        self.assertEqual(st, 200, listed)
        self.assertIn(self.PID, [r["project_id"] for r in listed])

        # Retire: gone from the active listing...
        st, r = self.req("POST", f"/api/projects/{self.PID}/retire",
                         {"reason": "shelving for later"}, op=True)
        self.assertEqual(st, 200, r)
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r.get("state"), "retired")

        st, listed = self.req("GET", "/api/projects")
        self.assertEqual(st, 200, listed)
        self.assertNotIn(self.PID, [p["project_id"] for p in listed])

        # ...but present in the internal archive, and directly gettable.
        st, arch = self.req("GET", "/api/projects?archive=1")
        self.assertEqual(st, 200, arch)
        self.assertIn(self.PID, [p["project_id"] for p in arch["archive"]])
        st, g = self.req("GET", f"/api/projects/{self.PID}")
        self.assertEqual(st, 200, g)
        self.assertEqual(g.get("state"), "retired")
        kinds = [(h["from"], h["to"]) for h in g["history"]]
        self.assertIn(("ingested", "retired"), kinds)

        # Resume: back in the active listing with state intact.
        st, rs = self.req("POST", f"/api/projects/{self.PID}/resume", {}, op=True)
        self.assertEqual(st, 200, rs)
        self.assertTrue(rs.get("ok"), rs)
        self.assertEqual(rs.get("state"), "analyzing")
        st, listed = self.req("GET", "/api/projects")
        self.assertIn(self.PID, [p["project_id"] for p in listed])
        st, g2 = self.req("GET", f"/api/projects/{self.PID}")
        # Full history preserved: ingested -> retired -> analyzing.
        kinds2 = [(h["from"], h["to"]) for h in g2["history"]]
        self.assertIn(("ingested", "retired"), kinds2)
        self.assertIn(("retired", "analyzing"), kinds2)

        # Advance to executing: run_loop works again.
        for to_state in ("planning", "executing"):
            st, t = self.req("POST", f"/api/projects/{self.PID}/transition",
                             {"to_state": to_state}, op=True)
            self.assertEqual(st, 200, (to_state, st, t))
        st, jl = self.req("POST", f"/api/projects/{self.PID}/run_loop",
                          {"max_rounds": 1}, op=True)
        self.assertEqual(st, 200, jl)
        self.assertTrue(jl.get("ok"), jl)
        self.assertIn("job_id", jl)
        st, sl = self.req(
            "POST",
            f"/api/projects/{self.PID}/loop/{jl['job_id']}/stop", {},
            op=True)
        self.assertEqual(st, 200, sl)

    def test_delete_is_refused_never_hard_deletes(self):
        st, c = self.req("POST", "/api/projects",
                         {"kind": "blank", "project_id": "nodelete-1"}, op=True)
        self.assertEqual(st, 200, c)
        st, body = self.req("DELETE", "/api/projects/nodelete-1", op=True)
        self.assertEqual(st, 403, (st, body))
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("code"), "permanent_deletion_refused")
        self.assertIn("retire", body.get("retire_route", ""))
        # Nothing was destroyed: the project is fully intact.
        st, g = self.req("GET", "/api/projects/nodelete-1")
        self.assertEqual(st, 200, g)
        self.assertTrue(g.get("ok"))
        st, listed = self.req("GET", "/api/projects")
        self.assertIn("nodelete-1", [p["project_id"] for p in listed])


if __name__ == "__main__":
    unittest.main(verbosity=2)
