"""Causal tests for Contract 7: services/capability_api.py (developer-only).

Real sqlite DBs on scratch tempfiles, the REAL CapabilityStore, a real
quarantined capability proving list-filter honesty, base64 download
round-trips, traversal-safe filenames, and the developer-gating BOUNDED
disclosure on every response.
"""
import base64
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.capability_api import (  # noqa: E402
    CapabilityAPI, FILTERS, routes_for_capabilities)
from swarm_engine.synthesis.capability_store import (  # noqa: E402
    CapabilityRecord, CapabilityStore, plan_fingerprint)
from swarm_engine.governance.provenance import AcquiredCodeStore  # noqa: E402


def _plan(name, goal):
    return {
        "name": name,
        "goal": goal,
        "steps": [{"id": "s1", "op": "identity", "args": {"x": {"$param": "x"}}}],
        "output": {"$step": "s1"},
        "params": {"x": None},
    }


# Fixture ids are fingerprint-bound (2026-09-27, Worker 2):
# CapabilityStore.store() now enforces capability_id ==
# plan_fingerprint(plan); synthetic ids are refused.
ALPHA_ID = plan_fingerprint(_plan("alpha", "do alpha"))
BETA_ID = plan_fingerprint(_plan("beta", "do beta"))
GAMMA_ID = plan_fingerprint(_plan("gamma", "do gamma"))


def _api():
    td = tempfile.TemporaryDirectory()
    db = os.path.join(td.name, "caps.db")
    store = CapabilityStore(db)
    r1 = store.store(CapabilityRecord(
        capability_id=ALPHA_ID, name="alpha",
        goal="do alpha", plan=_plan("alpha", "do alpha"),
        ops=["identity"], effects=["none"]))
    r2 = store.store(CapabilityRecord(
        capability_id=BETA_ID, name="beta",
        goal="do beta", plan=_plan("beta", "do beta"),
        ops=["identity"], effects=["none"], version=2,
        parent_id=r1.capability_id))
    store.set_status(r2.capability_id, "superseded")
    r3 = store.store(CapabilityRecord(
        capability_id=GAMMA_ID, name="gamma",
        goal="do gamma", plan=_plan("gamma", "do gamma"),
        ops=["identity"], effects=["none"]))
    store.set_status(r3.capability_id, "quarantined")
    store.bind_goal("do alpha", r1.capability_id)
    code = AcquiredCodeStore(db)
    code.save("helper", "acq_helper_0001", "def run():\n    return 1\n",
              "run", "synthesis", ["none"], {}, {})
    return td, CapabilityAPI(db), (r1, r2, r3)


class TestListCapabilities(unittest.TestCase):
    def test_filters_map_to_store_statuses(self):
        td, api, recs = _api()
        try:
            self.assertEqual(set(FILTERS),
                             {"all", "active", "superseded", "quarantined"})
            all_r = api.list_capabilities("all")
            self.assertTrue(all_r["ok"])
            self.assertEqual(all_r["count"], 4)
            active = api.list_capabilities("active")
            self.assertTrue(active["ok"])
            ids = {c["capability_id"] for c in active["capabilities"]}
            self.assertIn(ALPHA_ID, ids)
            self.assertIn("acq_helper_0001", ids)
            # quarantined capability honestly appears in its own filter
            quar = api.list_capabilities("quarantined")
            ids = {c["capability_id"] for c in quar["capabilities"]}
            self.assertIn(GAMMA_ID, ids)
            self.assertNotIn(ALPHA_ID, ids)
            sup = api.list_capabilities("superseded")
            ids = {c["capability_id"] for c in sup["capabilities"]}
            self.assertEqual(ids, {BETA_ID})
        finally:
            td.cleanup()

    def test_unknown_filter_refused(self):
        td, api, _ = _api()
        try:
            r = api.list_capabilities("frozen")
            self.assertFalse(r["ok"])
            self.assertIn("frozen", r["error"])
        finally:
            td.cleanup()

    def test_scope_and_gating_on_every_response(self):
        td, api, _ = _api()
        try:
            for r in (api.list_capabilities("all"),
                      api.get_capability(ALPHA_ID)):
                self.assertEqual(r["scope"], "local-device-owner")
                self.assertIn("developer_gating", r)
                self.assertIn("BOUNDED", r["developer_gating"])
        finally:
            td.cleanup()

    def test_empty_store_is_honest(self):
        td = tempfile.TemporaryDirectory()
        try:
            api = CapabilityAPI(os.path.join(td.name, "empty.db"))
            r = api.list_capabilities("all")
            self.assertTrue(r["ok"])
            self.assertTrue(r["empty"])
            self.assertEqual(r["capabilities"], [])
        finally:
            td.cleanup()


class TestGetCapability(unittest.TestCase):
    def test_get_detail_goal_bindings_events(self):
        td, api, recs = _api()
        try:
            r = api.get_capability(ALPHA_ID)
            self.assertTrue(r["ok"])
            cap = r["capability"]
            self.assertEqual(cap["name"], "alpha")
            self.assertEqual(cap["version"], 1)
            self.assertIn("do alpha", cap["goal_bindings"])
            self.assertIn("task_creation", cap)
            self.assertTrue(len(cap["events"]) >= 1)
            self.assertEqual(cap["history"],
                             [ALPHA_ID])
        finally:
            td.cleanup()

    def test_get_unknown_is_not_found(self):
        td, api, _ = _api()
        try:
            r = api.get_capability("cap_nope")
            self.assertFalse(r["ok"])
            self.assertIn("not found", r["error"])
        finally:
            td.cleanup()


class TestDownloadCapability(unittest.TestCase):
    def test_plan_download_roundtrip(self):
        td, api, recs = _api()
        try:
            r = api.download_capability(ALPHA_ID)
            self.assertTrue(r["ok"])
            dl = r["download"]
            data = base64.b64decode(dl["content_base64"])
            self.assertEqual(dl["bytes"], len(data))
            text = data.decode("utf-8")
            self.assertIn(ALPHA_ID, text)
            self.assertIn("alpha", text)
            self.assertIn("s1", text)  # rendered plan steps present
            self.assertNotIn("..", dl["filename"])
            self.assertTrue(dl["filename"].endswith(".txt"))
        finally:
            td.cleanup()

    def test_acquired_code_download(self):
        td, api, _ = _api()
        try:
            r = api.download_capability("acq_helper_0001")
            self.assertTrue(r["ok"])
            data = base64.b64decode(r["download"]["content_base64"])
            self.assertEqual(data.decode("utf-8"),
                             "def run():\n    return 1\n")
            self.assertTrue(r["download"]["filename"].endswith(".py"))
        finally:
            td.cleanup()

    def test_download_unknown_is_not_found(self):
        td, api, _ = _api()
        try:
            r = api.download_capability("cap_nope")
            self.assertFalse(r["ok"])
        finally:
            td.cleanup()


class TestRoutes(unittest.TestCase):
    def test_route_table_handlers(self):
        td, api, _ = _api()
        try:
            routes = routes_for_capabilities(api)
            # Synced 2026-09-26 (phase 3b): the applied service lineage is
            # L's; S7 added the install route (4 total).
            self.assertEqual(len(routes), 4)
            r = routes[("POST", "/api/capabilities/list")]({"status": "active"})
            self.assertTrue(r["ok"])
            self.assertGreaterEqual(r["count"], 1)
            r = routes[("POST", "/api/capabilities/get")](
                {"capability_id": ALPHA_ID})
            self.assertTrue(r["ok"])
            r = routes[("POST", "/api/capabilities/download")](
                {"capability_id": ALPHA_ID})
            self.assertTrue(r["ok"])
            # everything JSON-serializable, exactly what the GUI receives
            json.dumps(r)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
