"""Strict contract tests for GET /api/availability/coming-soon.

The coming-soon listing is only honest if every entry is backed by a real
unavailable control. These tests enforce that, per entry:

  * route-backed entries: the entry's route is driven through the REAL
    merged contract route table with the entry's probe body, and the
    entry's code/reason/missing_substrate must EXACTLY match the live
    handler output. Any drift in the handlers fails the suite until the
    entry is updated -- the listing cannot go stale silently.
  * absent entries (evidence_doc_generation): the route
    table is scanned for any matching route (none may exist), plausible
    paths are driven to honest 404s, and the corresponding honest-absence
    evidence is asserted (fresh evidence store has no produced documents).
  * remote_dispatch is backend-present but gui-absent: the /api/remote/*
    routes exist on the engine-bound service, while the availability
    entry keeps the user-facing capability coming-soon with its bounded
    reason.
  * coverage: the listing's code set must exactly equal the authoritative
    inventory -- report MISSION_REPORT_2026-09-26_BACKEND_CONTRACTS.md
    sections 1/7/8 (11 codes) plus the design-batch kind evidence-docs
    (1 code, genuinely absent, no route). No invented entries, none
    missing.
  * no side effects: driving the probes must not create agents, tasks,
    or artifacts anywhere.

Inclusion notes (deliberate, documented):
  * EXECUTE_LANGUAGE_UNSUPPORTED is REFUSED-by-design in the report
    section 8, but section 1 names its code/reason/missing_substrate and
    section 7 maps it to the coming-soon page; the handler returns the
    typed unavailable payload, so it belongs in the data-driven listing.
  * agent_instantiation is the real SubstrateUnavailable from the agent
    factory (report section 1 row 2), surfaced as the typed payload.
  * evidence_doc_generation backs the design-batch kind "evidence-docs":
    genuinely absent -- no route produces AI-authored evidence documents.

NL intent-dispatch sibling hook: if the sibling task appends entries to
EXTRA_ENTRIES, they are verified here identically (route-backed or
absent). If the hook is still empty, that is asserted and skipped
explicitly -- never invented.
"""
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_HERE, "..", "..", "pylib")))

from swarm_engine.services.availability import (  # noqa: E402
    ENTRIES,
    EXTRA_ENTRIES,
    build_coming_soon,
)
from swarm_engine.services.contract_types import dispatch  # noqa: E402
from swarm_engine.services.http_adapter import (  # noqa: E402
    build_services,
    close_services,
)

# The 5 kinds registered in remor_gui_design/app/static/coming_soon.js.
DESIGN_KINDS = {
    "remote-dispatch",
    "evidence-docs",
    "external-bots",
    "song-synthesis",
    "project-routing",
}

# Authoritative inventory: 11 codes from the backend-contracts report
# sections 1/7/8 + 1 code for the design-batch kind "evidence-docs"
# (genuinely absent; no route produces AI-authored evidence documents).
# NOTE (canonical convergence): the report's "recurring_schedule" predates
# the merged recurrence substrate (services/recurrence.py) -- the merged
# contract table's POST /api/tasks/recurring is served by the real
# RecurrenceService, exposure-gated, whose disabled payload is code
# "recurring_disabled". The inventory tracks the live merged runtime.
REPORT_CODES = {
    "recurring_disabled",
    "purchase_permanent_agents",
    "template_token_balance",
    "agent_instantiation",
    "remote_dispatch",
    "auto_route_conversation",
    "EXECUTE_LANGUAGE_UNSUPPORTED",
    "PLUGIN_REGISTRY_ABSENT",
    "AUDIO_PIPELINE_ABSENT",
    "metering_token_balance",
    "chat_turn_metering",
}
EXPECTED_CODES = REPORT_CODES | {"evidence_doc_generation"}

# (retired by REMOTE-DISPATCH-1: /api/remote/* now exists on the
# engine-bound service; absence is no longer the honest claim.)


class ComingSoonContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp(prefix="remor_ff_comingsoon_")
        cls.services = build_services(cls._tmp)
        cls.routes = cls.services["contract_routes"]
        cls.listing = dispatch(cls.routes, "GET",
                               "/api/availability/coming-soon", {})
        cls.by_code = {e["unavailable"]["code"]: e
                       for e in cls.listing["coming_soon"]}

    @classmethod
    def tearDownClass(cls):
        try:
            close_services(cls.services)
        finally:
            shutil.rmtree(cls._tmp, ignore_errors=True)

    # -- listing shape -------------------------------------------------
    def test_listing_shape(self):
        self.assertTrue(self.listing.get("ok"), self.listing)
        entries = self.listing["coming_soon"]
        self.assertEqual(self.listing["count"], len(entries))
        self.assertEqual(len(entries), len(ENTRIES) + len(EXTRA_ENTRIES))
        for e in entries:
            un = e["unavailable"]
            self.assertTrue(e["kind"] and e["feature"])
            self.assertEqual(un["gui"], "coming_soon")
            for field in ("code", "reason", "missing_substrate"):
                self.assertTrue(un[field], f"{e['kind']}: {field} empty")
            if e["route"] is None:
                self.assertIsNone(e["http_status"],
                                  f"{e['kind']}: absent entry needs null status")
            else:
                self.assertEqual(e["http_status"], 501,
                                 f"{e['kind']}: route-backed must be 501")
                self.assertIn(e["route"]["method"], ("GET", "POST"))

    def test_design_kinds_covered(self):
        kinds = {e["kind"] for e in self.listing["coming_soon"]}
        self.assertTrue(DESIGN_KINDS <= kinds,
                        f"missing design kinds: {DESIGN_KINDS - kinds}")

    def test_report_coverage_exact(self):
        codes = set(self.by_code)
        self.assertEqual(codes, EXPECTED_CODES,
                         f"missing: {EXPECTED_CODES - codes}, "
                         f"invented: {codes - EXPECTED_CODES}")

    # -- per-entry verification: route-backed --------------------------
    def test_route_backed_entries_match_live_handlers(self):
        for e in self.listing["coming_soon"]:
            route = e["route"]
            if route is None:
                continue
            probe = dict(e.get("probe") or {})
            res = dispatch(self.routes, route["method"], route["path"], probe)
            self.assertFalse(res.get("ok"),
                             f"{e['kind']}: route unexpectedly succeeded")
            # The adapter maps exactly this key to HTTP 501.
            self.assertIn("unavailable", res,
                          f"{e['kind']}: expected typed unavailability, "
                          f"got {str(res)[:200]}")
            un = res["unavailable"]
            want = e["unavailable"]
            self.assertEqual(un["code"], want["code"], e["kind"])
            self.assertEqual(un["reason"], want["reason"],
                             f"{e['kind']}: reason drifted -- update the entry")
            self.assertEqual(un["missing_substrate"],
                             want["missing_substrate"],
                             f"{e['kind']}: missing_substrate drifted")
            self.assertEqual(un["gui"], "coming_soon", e["kind"])

    def test_llm_instantiation_probe_is_the_real_substrate_refusal(self):
        e = self.by_code["agent_instantiation"]
        # Discover the real llm template id from the live registry -- the
        # entry hard-codes it, so this also cross-checks the entry's probe.
        tpls = dispatch(self.routes, "GET", "/api/agent-templates", {})
        llm_ids = [t["template_id"] for t in tpls["templates"]
                   if t["substrate_kind"] == "llm"]
        self.assertTrue(llm_ids, "no llm template registered")
        self.assertEqual(e["probe"]["template_id"], llm_ids[0],
                         "entry probe names a template that is not the "
                         "real llm template")
        # Fresh registry: cap cannot fire first, so this must be the real
        # SubstrateUnavailable from the agent factory, not a quota refusal.
        res = dispatch(self.routes, "POST", "/api/agents",
                       {"template_id": llm_ids[0]})
        self.assertIn("unavailable", res, res)
        self.assertEqual(res["unavailable"]["code"], "agent_instantiation")
        self.assertNotIn("limit", res,
                         "cap fired before the substrate refusal -- "
                         "test isolation broken")
        # And the factory really refuses: no agent was registered.
        agents = dispatch(self.routes, "GET", "/api/agents", {})
        self.assertEqual(agents.get("agents"), [])

    def test_probes_have_no_side_effects(self):
        for e in self.listing["coming_soon"]:
            route = e["route"]
            if route is None:
                continue
            dispatch(self.routes, route["method"], route["path"],
                     dict(e.get("probe") or {}))
        self.assertEqual(
            dispatch(self.routes, "GET", "/api/agents", {}).get("agents"), [],
            "a coming-soon probe registered an agent")
        self.assertEqual(
            dispatch(self.routes, "GET", "/api/tasks", {}).get("tasks"), [],
            "a coming-soon probe scheduled a task")
        self.assertEqual(
            self.services["artifacts"].list_artifacts(), [],
            "a coming-soon probe saved an artifact")

    # -- per-entry verification: remote-dispatch backend present --------
    # REMOTE-DISPATCH-1: the backend route exists (mounted on the
    # engine-bound _Service, over the canonical AgentDirectory), but the
    # GUI Dispatch tab is not bound to it -- the user-facing capability
    # stays coming-soon with the bounded reason.
    def test_remote_dispatch_backend_present_gui_coming_soon(self):
        from swarm_engine.services.http_adapter import _Service
        tmp = tempfile.mkdtemp(prefix="remor_ff_rd1_")
        try:
            svc = _Service(
                db_path=os.path.join(tmp, "eng.db"),
                base_dir=os.path.join(tmp, "svc"),
                enable_dispatch_learning=False)
            routes = svc.ff["contract_routes"]
            remote = sorted(p for (_m, p) in routes if "/api/remote/" in p)
            self.assertTrue(
                {"/api/remote/pair", "/api/remote/sessions",
                 "/api/remote/dispatch", "/api/remote/kill",
                 "/api/remote/end"} <= set(remote),
                f"backend remote routes missing: {remote}")
            self.assertIn("remote_dispatch", svc.ff,
                          "remote_dispatch service not wired")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        entry = self.by_code["remote_dispatch"]
        un = entry["unavailable"]
        self.assertEqual(un["gui"], "coming_soon")
        self.assertIn("bench-verified", un["reason"])
        self.assertIn("not bound", un["reason"])

    def test_evidence_doc_generation_genuinely_absent(self):
        paths = [p for (_m, p) in self.routes]
        hits = [p for p in paths
                if any(s in p for s in ("soul", "theory", "inference",
                                        "generate", "produce"))]
        self.assertFalse(hits, f"generation-shaped routes exist: {hits}")
        # The backend produces nothing: on a fresh store every hosted
        # document reads back as honestly absent.
        for name in ("soul.md", "theory.md", "hypothetical inferences.md"):
            res = dispatch(self.routes, "POST", "/api/evidence/documents/get",
                           {"name": name})
            self.assertTrue(res.get("ok"), res)
            self.assertIs(res.get("exists"), False,
                          f"{name}: backend produced a document -- "
                          "entry is no longer absent")

    # -- NL sibling extension hook -------------------------------------
    def test_nl_extension_hook(self):
        if not EXTRA_ENTRIES:
            # Sibling NL intent-dispatch task has not landed yet: the hook
            # stays empty and nothing is invented.
            self.assertEqual(EXTRA_ENTRIES, [])
            return
        for e in EXTRA_ENTRIES:
            route = e["route"]
            un = e["unavailable"]
            self.assertEqual(un["gui"], "coming_soon")
            if route is not None:
                res = dispatch(self.routes, route["method"], route["path"],
                               dict(e.get("probe") or {}))
                self.assertIn("unavailable", res, e["kind"])
                self.assertEqual(res["unavailable"]["code"], un["code"],
                                 e["kind"])


if __name__ == "__main__":
    unittest.main()
