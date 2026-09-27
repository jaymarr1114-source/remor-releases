"""Causal tests for swarm_engine.services.agent_api (Contract 2 — Agents).

Anti-simulation: every test runs the real AgentRegistry / TemplateRegistry /
AgentFactory / OracleRegistry on real scratch sqlite DBs (tempfile). The
free-tier guard is enforced by the service's own check-and-register
mechanism under a lock; the revert test proves causality by disabling the
guard (monkeypatch) and showing the 4th agent then registers.
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.agent_api import (  # noqa: E402
    AGENT_CAP_CODE,
    INSTANTIATION_CODE,
    PURCHASE_CODE,
    TOKEN_BALANCE_CODE,
    AgentService,
    build_agent_service,
    dispatch_agents,
    routes_for_agents,
)
from swarm_engine.services.contract_types import dispatch  # noqa: E402

SYMBOLIC = "tpl_symbolic_coder_v1"
CALLABLE = "tpl_callable_coder_v1"
LLM = "tpl_llm_coder_v1"


class AgentApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = build_agent_service(
            os.path.join(self.tmp.name, "agents"))

    def tearDown(self):
        self.tmp.cleanup()

    # -- templates: real, honest ---------------------------------------
    def test_list_templates_real_seed(self):
        r = self.svc.list_templates()
        self.assertTrue(r["ok"])
        ids = {t["template_id"] for t in r["templates"]}
        self.assertEqual(ids, {SYMBOLIC, CALLABLE, LLM})
        # substrate_kind reported honestly: blueprints, not model identities
        kinds = {t["substrate_kind"] for t in r["templates"]}
        self.assertEqual(kinds, {"llm", "symbolic", "callable"})
        # only one family genuinely exists; nothing invented
        self.assertEqual(r["families"], ["coder"])
        # llm blueprint exists but cannot be instantiated in this build
        by_id = {t["template_id"]: t for t in r["templates"]}
        self.assertFalse(by_id[LLM]["instantiable"])
        self.assertTrue(by_id[SYMBOLIC]["instantiable"])
        self.assertTrue(by_id[CALLABLE]["instantiable"])
        # genuine resource_requirements reported verbatim, never invented
        rr = by_id[SYMBOLIC]["contracts"]["resource_requirements"]
        self.assertEqual(rr, {"max_seconds": 120, "max_memory_mb": 512})

    def test_list_agents_empty(self):
        r = self.svc.list_agents()
        self.assertTrue(r["ok"])
        self.assertEqual(r["agents"], [])
        self.assertEqual(r["free_tier_max"], 3)

    # -- free-tier guard: causal ----------------------------------------
    def test_register_three_then_fourth_refused(self):
        ids = []
        for _ in range(3):
            r = self.svc.register_agent(SYMBOLIC)
            self.assertTrue(r["ok"], r)
            ids.append(r["agent"]["agent_id"])
        self.assertEqual(len(set(ids)), 3)  # distinct real agents
        fourth = self.svc.register_agent(CALLABLE)
        self.assertFalse(fourth["ok"])
        self.assertIn("limit", fourth)
        self.assertEqual(fourth["limit"]["code"], AGENT_CAP_CODE)
        self.assertEqual(fourth["limit"]["limit"], 3)
        self.assertEqual(fourth["limit"]["current"], 3)
        # registry itself holds exactly 3 live agents: the guard measured
        # real state
        self.assertEqual(self.svc.active_agent_count(), 3)

    def test_revert_guard_fourth_registers(self):
        """Causality: with the guard bypassed, the 4th agent registers --
        proving the refusal comes from the guard mechanism, not the
        registry."""
        for _ in range(3):
            self.assertTrue(self.svc.register_agent(SYMBOLIC)["ok"])
        orig = self.svc._check_free_tier_limit
        try:
            self.svc._check_free_tier_limit = lambda: None  # guard reverted
            r = self.svc.register_agent(CALLABLE)
            self.assertTrue(r["ok"], r)  # 4th registers: guard was the cause
            self.assertEqual(self.svc.active_agent_count(), 4)
        finally:
            self.svc._check_free_tier_limit = orig
        # guard re-applied: 5th refused again
        fifth = self.svc.register_agent(CALLABLE)
        self.assertFalse(fifth["ok"])
        self.assertEqual(fifth["limit"]["code"], AGENT_CAP_CODE)
        self.assertEqual(fifth["limit"]["current"], 4)

    def test_destroy_frees_slot(self):
        recs = [self.svc.register_agent(SYMBOLIC)["agent"] for _ in range(3)]
        d = self.svc.destroy_agent(recs[0]["agent_id"])
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["agent"]["state"], "DESTROYED")
        r = self.svc.register_agent(CALLABLE)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.svc.active_agent_count(), 3)

    def test_double_submit_race(self):
        """10 concurrent register attempts: exactly 3 succeed, the rest are
        refused by the real guard; the registry ends with exactly 3 live
        agents. The lock makes check-and-register atomic."""
        results = []
        lock = threading.Lock()

        def attempt():
            r = self.svc.register_agent(SYMBOLIC)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = [r for r in results if r.get("ok")]
        refused = [r for r in results
                   if not r.get("ok") and "limit" in r]
        self.assertEqual(len(ok), 3, results)
        self.assertEqual(len(refused), 7, results)
        for r in refused:
            self.assertEqual(r["limit"]["code"], AGENT_CAP_CODE)
        self.assertEqual(self.svc.active_agent_count(), 3)
        self.assertEqual(len({r["agent"]["agent_id"] for r in ok}), 3)

    # -- input validation ------------------------------------------------
    def test_garbage_template_ids_refused(self):
        before = self.svc.active_agent_count()
        for bad in ("", "   ", None, 123, -1, ["x"], "../evil",
                    "tpl_does_not_exist", "TPL_SYMBOLIC_CODER_V1"):
            r = self.svc.register_agent(bad)
            self.assertFalse(r["ok"], bad)
            self.assertIn("error", r, bad)
        self.assertEqual(self.svc.active_agent_count(), before)

    def test_llm_template_honestly_uninstantiable(self):
        """The llm blueprint is real, but AgentFactory raises
        SubstrateUnavailable for it -- surfaced as unavailable, never
        simulated."""
        r = self.svc.register_agent(LLM)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("unavailable"), r)
        self.assertEqual(r["unavailable"]["code"], INSTANTIATION_CODE)
        self.assertIn("llm", r["unavailable"]["reason"])

    def test_destroy_unknown_agent(self):
        r = self.svc.destroy_agent("agt_nope")
        self.assertFalse(r["ok"])
        self.assertIn("error", r)

    def test_list_agents_state_filter(self):
        a = self.svc.register_agent(SYMBOLIC)["agent"]
        self.svc.destroy_agent(a["agent_id"])
        self.svc.register_agent(CALLABLE)
        avail = self.svc.list_agents(state="AVAILABLE")
        self.assertTrue(all(x["state"] == "AVAILABLE"
                            for x in avail["agents"]))
        destroyed = self.svc.list_agents(state="DESTROYED")
        self.assertEqual(len(destroyed["agents"]), 1)

    # -- honest absences --------------------------------------------------
    def test_purchase_permanent_unavailable(self):
        r = self.svc.purchase_permanent_agent(SYMBOLIC)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("unavailable"), r)
        self.assertEqual(r["unavailable"]["code"], PURCHASE_CODE)
        self.assertIn("payment", r["unavailable"]["reason"].lower())
        self.assertIn("payment_provider",
                      r["unavailable"]["missing_substrate"])

    def test_template_token_balance_unavailable(self):
        r = self.svc.template_token_balance(SYMBOLIC)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("unavailable"), r)
        self.assertEqual(r["unavailable"]["code"], TOKEN_BALANCE_CODE)

    # -- route table -------------------------------------------------------
    def test_routes_dispatch(self):
        routes = routes_for_agents(self.svc)
        self.assertTrue(all(isinstance(k, tuple) and len(k) == 2
                            for k in routes))
        r = dispatch(routes, "GET", "/api/agent-templates")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["templates"]), 3)
        r = dispatch(routes, "POST", "/api/agents",
                     {"template_id": SYMBOLIC})
        self.assertTrue(r["ok"], r)
        aid = r["agent"]["agent_id"]
        r = dispatch_agents(self.svc, "GET", f"/api/agents/{aid}")
        self.assertTrue(r["ok"])
        self.assertEqual(r["agent"]["agent_id"], aid)
        r = dispatch_agents(self.svc, "GET", "/api/agents")
        self.assertEqual(r["count"], 1)
        r = dispatch_agents(self.svc, "POST", "/api/agents/purchase-permanent",
                            {"template_id": SYMBOLIC})
        self.assertTrue(r.get("unavailable"), r)
        r = dispatch_agents(self.svc, "POST", f"/api/agents/{aid}/destroy",
                            {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["agent"]["state"], "DESTROYED")
        # unknown route: honest 404-shaped payload
        r = dispatch_agents(self.svc, "GET", "/api/nope")
        self.assertFalse(r["ok"])
        self.assertEqual(r["http_status"], 404)
        # every handler returns JSON-serializable output
        import json
        json.dumps(dispatch_agents(self.svc, "GET", "/api/agents"))


if __name__ == "__main__":
    unittest.main()
