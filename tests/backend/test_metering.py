"""Causal tests for swarm_engine.services.metering (Contract 9 — Metering).

Anti-simulation: every count comes from the real RunScheduler's sqlite DB
(scratch tempfile); the daily cap is enforced by counting real
scheduler_runs rows; the queue cap is enforced against the scheduler's
real queue_depth() with the worker thread genuinely blocked so 25 runs
sit queued.
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.metering import (  # noqa: E402
    CHAT_TURNS_CODE,
    DAILY_CAP_CODE,
    QUEUE_CAP_CODE,
    TOKEN_BALANCE_CODE,
    MeteringService,
    build_metering_service,
    dispatch_metering,
    routes_for_metering,
)
from swarm_engine.services.scheduler import RunScheduler  # noqa: E402
from swarm_engine.services import scheduler as _sched_mod  # noqa: E402

QUICK_GOAL = "knowledge_stats"  # real pipeline, ~0.02s per run


def _backdate_run(sched, hours_ago):
    """Insert a run record directly into the scheduler's sqlite DB with a
    backdated queued_at (simulates a submission from hours_ago).

    Canonical's scheduler is hash-chained (Batch-11): every row carries
    prev_digest/row_digest and the chain is audited on open, so a bare
    INSERT without chain columns is refused. This helper builds the row
    with the scheduler module's own chain machinery -- prev_digest chains
    to the live tip, row_digest is computed over the row fields -- so the
    row stays audit-valid; only queued_at is set in the past. The whole
    read-tip+insert runs under the scheduler's lock so no worker append
    can interleave and fork the chain.
    """
    queued = time.time() - hours_ago * 3600
    run_id = f"backdated_{hours_ago}"
    data = {
        "goal": "old goal",
        "status": "completed",
        "conversation_id": None,
        "queued_at": queued,
        "started_at": queued,
        "ended_at": queued + 0.02,
        "outcome_json": None,
        "error": None,
    }
    with sched._lock:
        conn = sqlite3.connect(sched.db_path)
        try:
            tip = conn.execute(
                "SELECT row_digest FROM scheduler_runs "
                "ORDER BY seq DESC LIMIT 1").fetchone()
            prev = tip[0] if tip else _sched_mod._GENESIS_DIGEST
            ordered, digest = _sched_mod._chain_row(run_id, data, prev)
            conn.execute(
                "INSERT INTO scheduler_runs (id, prev_digest, row_digest, "
                "goal, status, conversation_id, queued_at, started_at, "
                "ended_at, outcome_json, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, prev, digest,
                 ordered["goal"], ordered["status"],
                 ordered["conversation_id"], ordered["queued_at"],
                 ordered["started_at"], ordered["ended_at"],
                 ordered["outcome_json"], ordered["error"]))
            conn.commit()
        finally:
            conn.close()


class MeteringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sched = RunScheduler(
            os.path.join(self.tmp.name, "sched.db"),
            os.path.join(self.tmp.name, "rt.db"))
        self.met = build_metering_service(self.sched)

    def tearDown(self):
        try:
            self.sched.close()
        finally:
            self.tmp.cleanup()

    # -- real counting ----------------------------------------------------
    def test_tasks_today_counts_real_submits(self):
        self.assertEqual(self.met.tasks_today()["tasks_today"], 0)
        for _ in range(3):
            r = self.sched.submit(QUICK_GOAL)
            self.assertTrue(r["ok"], r)
        # worker may drain concurrently; the count is of real rows
        self.assertEqual(self.met.tasks_today()["tasks_today"], 3)
        self.assertEqual(self.met.tasks_today()["remaining"], 32)

    def test_time_window_excludes_old_runs(self):
        """A run submitted 30h ago is outside the rolling 24h window and
        must not count; one from 1h ago must."""
        for _ in range(2):
            self.assertTrue(self.sched.submit(QUICK_GOAL)["ok"])
        _backdate_run(self.sched, hours_ago=30)
        _backdate_run(self.sched, hours_ago=1)
        n = self.met.tasks_since(time.time() - 24 * 3600)
        self.assertEqual(n, 3)  # 2 fresh + 1h-old; 30h-old excluded
        wide = self.met.tasks_since(time.time() - 48 * 3600)
        self.assertEqual(wide, 4)  # 48h window includes the 30h-old row

    def test_queue_depth_is_real(self):
        q = self.met.queue_depth()
        self.assertTrue(q["ok"])
        self.assertEqual(q["queue_depth"], 0)
        self.assertEqual(q["limit"], 25)

    # -- daily cap: causal -------------------------------------------------
    def test_35_ok_36th_refused(self):
        for i in range(35):
            r = self.sched.submit(f"{QUICK_GOAL} {i}")
            self.assertTrue(r["ok"], (i, r))
        can = self.met.can_submit()
        self.assertFalse(can["ok"])
        self.assertIn("limit", can)
        self.assertEqual(can["limit"]["code"], DAILY_CAP_CODE)
        self.assertEqual(can["limit"]["limit"], 35)
        self.assertEqual(can["limit"]["current"], 35)
        # guarded_submit (the GUI path) refuses the 36th too
        g = self.met.guarded_submit(QUICK_GOAL)
        self.assertFalse(g["ok"])
        self.assertEqual(g["limit"]["code"], DAILY_CAP_CODE)

    def test_guarded_submit_passes_through_when_under_cap(self):
        g = self.met.guarded_submit(QUICK_GOAL)
        self.assertTrue(g["ok"], g)
        self.assertIn("run_id", g)

    def test_guarded_submit_rejects_bad_goal(self):
        for bad in ("", "   ", None, 42):
            g = self.met.guarded_submit(bad)
            self.assertFalse(g["ok"], bad)

    # -- queue cap: causal --------------------------------------------------
    def test_25_queued_26th_refused(self):
        """Hold the scheduler's own lock so the worker thread cannot drain
        the queue; 25 real submits sit queued; the 26th guarded submit is
        refused by the real queue_depth()."""
        with self.sched._lock:  # block the worker (RLock: same thread OK)
            for i in range(25):
                r = self.sched.submit(f"{QUICK_GOAL} q{i}")
                self.assertTrue(r["ok"], (i, r))
            self.assertEqual(self.sched.queue_depth(), 25)
            q = self.met.queue_depth()
            self.assertEqual(q["queue_depth"], 25)
            self.assertEqual(q["remaining"], 0)
            g = self.met.guarded_submit(QUICK_GOAL)
            self.assertFalse(g["ok"])
            self.assertIn("limit", g)
            self.assertEqual(g["limit"]["code"], QUEUE_CAP_CODE)
            self.assertEqual(g["limit"]["limit"], 25)
            self.assertEqual(g["limit"]["current"], 25)
        # lock released: worker drains; queue cap no longer trips
        deadline = time.time() + 30
        while self.sched.queue_depth() and time.time() < deadline:
            time.sleep(0.05)

    # -- concurrency honesty -------------------------------------------------
    def test_concurrency_reports_one_not_three(self):
        c = self.met.concurrency()
        self.assertTrue(c["ok"])
        self.assertEqual(c["concurrent_runs"], 1)
        self.assertEqual(c["plan_ceiling"], 3)
        self.assertIn("not implemented", c["plan_ceiling_status"])

    # -- honest absences ------------------------------------------------------
    def test_token_balances_unavailable(self):
        r = self.met.token_balances()
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("unavailable"), r)
        self.assertEqual(r["unavailable"]["code"], TOKEN_BALANCE_CODE)
        self.assertIn("token_meter", r["unavailable"]["missing_substrate"])

    def test_chat_turns_unavailable(self):
        r = self.met.chat_turns()
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("unavailable"), r)
        self.assertEqual(r["unavailable"]["code"], CHAT_TURNS_CODE)

    def test_tier_payload(self):
        t = self.met.tier()
        self.assertTrue(t["ok"])
        self.assertEqual(t["tier"], "free")
        self.assertEqual(t["constants"]["tasks_per_day"], 35)
        self.assertEqual(t["constants"]["max_queue"], 25)
        self.assertEqual(t["constants"]["max_agents"], 3)
        codes = {e["code"] for e in t["enforced"]}
        self.assertIn(DAILY_CAP_CODE, codes)
        self.assertIn(QUEUE_CAP_CODE, codes)
        missing = {u["code"] for u in t["unavailable"]}
        self.assertIn(TOKEN_BALANCE_CODE, missing)
        self.assertIn(CHAT_TURNS_CODE, missing)

    def test_usage_summary(self):
        u = self.met.usage()
        self.assertTrue(u["ok"])
        self.assertEqual(u["tasks_today"], 0)
        self.assertEqual(u["tasks_per_day"], 35)
        self.assertEqual(u["concurrent_runs"], 1)

    # -- route table -----------------------------------------------------------
    def test_routes_dispatch(self):
        routes = routes_for_metering(self.met)
        self.assertTrue(all(isinstance(k, tuple) and len(k) == 2
                            for k in routes))
        r = dispatch_metering(self.met, "GET", "/api/metering")
        self.assertTrue(r["ok"])
        self.assertEqual(r["tasks_per_day"], 35)
        r = dispatch_metering(self.met, "GET", "/api/metering/tier")
        self.assertTrue(r["ok"])
        self.assertEqual(r["tier"], "free")
        r = dispatch_metering(self.met, "GET", "/api/metering/can-submit")
        self.assertTrue(r["ok"])
        r = dispatch_metering(self.met, "POST", "/api/metering/submit",
                              {"goal": QUICK_GOAL})
        self.assertTrue(r["ok"], r)
        self.assertIn("run_id", r)
        r = dispatch_metering(self.met, "GET",
                              "/api/metering/token-balances")
        self.assertTrue(r.get("unavailable"), r)
        r = dispatch_metering(self.met, "GET", "/api/metering/chat-turns")
        self.assertTrue(r.get("unavailable"), r)
        r = dispatch_metering(self.met, "POST", "/api/metering/submit",
                              {"goal": ""})
        self.assertFalse(r["ok"])
        import json
        json.dumps(dispatch_metering(self.met, "GET", "/api/metering"))


if __name__ == "__main__":
    unittest.main()
