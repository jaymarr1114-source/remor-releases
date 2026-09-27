"""M1 external technique ingestion tests.

Real sqlite in tmp dirs, real fresh processes. No mocks, no seeding: stores
start empty and every record in them was written by the test itself.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

_CANONICAL = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _CANONICAL)
sys.path.insert(0, os.path.join(_CANONICAL, "pylib"))

from runtime.acquisition.ingest import (  # noqa: E402
    DeltaRecord,
    ExternalDemonstration,
    IngestResult,
    InventorySnapshot,
    YRecord,
    build_inventory,
    ingest_chat_turn,
    ingest_plugin_action,
    ingest_subagent_trace,
)
from runtime.intellect.epistemic import EpistemicStore  # noqa: E402
from swarm_engine.services.evidence import EvidenceStore  # noqa: E402


def _tmp_epistemic():
    td = tempfile.TemporaryDirectory()
    db = os.path.join(td.name, "epistemic.db")
    return td, EpistemicStore(db), db


def _tmp_evidence_store():
    td = tempfile.TemporaryDirectory()
    return td, EvidenceStore(os.path.join(td.name, "evidence.db"))


def _base_inventory():
    return InventorySnapshot(capabilities=["sort_numbers"], primitives=["sum"])


def _novel_trace():
    return {
        "trace_id": "trace-1",
        "objective": "summarize a web page",
        "steps": [
            {"tool": "web_fetch",
             "inputs": {"url": "https://example.com/page"},
             "outputs": {"html": "<p>hello</p>"}, "ok": True},
            {"tool": "parse_html",
             "inputs": {"html": "<p>hello</p>"},
             "outputs": {"text": "hello"}, "ok": True},
            {"tool": "summarize",
             "inputs": {"text": "hello"},
             "outputs": {"summary": "a greeting"}, "ok": True},
        ],
        "outcome": {"summary": "a greeting"},
    }


class TestIngest(unittest.TestCase):
    def test_positive_subagent_trace_records_delta(self):
        td, store, db = _tmp_epistemic()
        etd, estore = _tmp_evidence_store()
        try:
            res = ingest_subagent_trace(_novel_trace(), _base_inventory(),
                                        store, evidence_store=estore)
            self.assertTrue(res.recorded)
            self.assertEqual(res.reason, "gap_recorded")
            self.assertIsNotNone(res.y_id)
            self.assertIsNotNone(res.delta_id)

            # Fresh EpistemicStore instance on the same db path.
            fresh = EpistemicStore(db)
            y_obs = [o for o in fresh.all_observations()
                     if o.source == "technique_y"]
            self.assertEqual(len(y_obs), 1)
            self.assertEqual(y_obs[0].raw["y_record"]["y_id"], res.y_id)
            self.assertEqual(y_obs[0].raw["y_record"]["source"],
                             "subagent_trace")
            self.assertEqual(len(y_obs[0].raw["y_record"]["actions"]), 3)

            d_obs = [o for o in fresh.all_observations()
                     if o.source == "technique_delta"]
            self.assertEqual(len(d_obs), 1)
            delta = d_obs[0].raw["delta"]
            self.assertEqual(delta["delta_id"], res.delta_id)
            self.assertEqual(delta["X"], "summarize a web page")
            self.assertTrue(delta["E"], "delta.E must be non-empty")
            self.assertEqual(delta["V"], {"status": "unverified",
                                          "method": None})
            self.assertIsNone(delta["C"])
            self.assertIn("absent from inventory", delta["gap"])

            # Demonstration evidence retrievable via the frozen API.
            ev = fresh.evidence_for(res.delta_id)
            self.assertEqual(len(ev), 1)
            self.assertTrue(ev[0].supports)
            self.assertEqual(ev[0].content["kind"], "demonstration_evidence")

            # Real EvidenceStore mirror surfaces the delta summary.
            listed = estore.list_entries(kind="observation")
            self.assertTrue(listed["ok"])
            hits = [e for e in listed["entries"]
                    if "technique_delta" in e["text"]
                    and res.delta_id in e["text"]]
            self.assertEqual(len(hits), 1)

            # Subprocess-based persistence check: a different process sees it.
            probe = subprocess.run(
                [sys.executable, "-c",
                 "import sys, json, os; "
                 f"sys.path.insert(0, {_CANONICAL!r}); "
                 f"sys.path.insert(0, os.path.join({_CANONICAL!r}, 'pylib')); "
                 "from swarm_engine.intellect.epistemic import EpistemicStore; "
                 f"s = EpistemicStore({db!r}); "
                 "n = len([o for o in s.all_observations() "
                 "if o.source == 'technique_delta']); "
                 "print(json.dumps({'delta_observations': n}))"],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(
                json.loads(probe.stdout)["delta_observations"], 1)
        finally:
            td.cleanup()
            etd.cleanup()

    def test_chat_turn_no_gap_no_record(self):
        td, store, db = _tmp_epistemic()
        try:
            before = len(store.all_observations())
            res = ingest_chat_turn({"text": "what time is it?"},
                                   _base_inventory(), store)
            self.assertFalse(res.recorded)
            self.assertEqual(res.reason, "insufficient_evidence")
            self.assertEqual(len(store.all_observations()), before)
        finally:
            td.cleanup()

    def test_instruction_without_technique_no_record(self):
        td, store, _ = _tmp_epistemic()
        try:
            before = len(store.all_observations())
            res = ingest_chat_turn(
                {"text": "you should learn to sort numbers",
                 "procedure_steps": []},
                _base_inventory(), store)
            self.assertFalse(res.recorded)
            self.assertEqual(res.reason, "insufficient_evidence")
            self.assertEqual(len(store.all_observations()), before)
        finally:
            td.cleanup()

    def test_known_technique_no_gap(self):
        td, store, _ = _tmp_epistemic()
        try:
            trace = {
                "trace_id": "trace-known",
                "objective": "sort these numbers",
                "steps": [
                    {"tool": "sort_numbers",
                     "inputs": {"items": [3, 1, 2]},
                     "outputs": {"sorted": [1, 2, 3]}, "ok": True},
                    {"tool": "sum",
                     "inputs": {"items": [1, 2, 3]},
                     "outputs": {"total": 6}, "ok": True},
                ],
                "outcome": {"total": 6},
            }
            before = len(store.all_observations())
            res = ingest_subagent_trace(trace, _base_inventory(), store)
            self.assertFalse(res.recorded)
            self.assertEqual(res.reason, "no_observable_gap")
            self.assertEqual(len(store.all_observations()), before)
        finally:
            td.cleanup()

    def test_novel_composition_records_gap(self):
        td, store, _ = _tmp_epistemic()
        try:
            trace = {
                "trace_id": "trace-comp",
                "objective": "plan a weekend trip",
                "steps": [
                    {"tool": "sum",
                     "inputs": {"items": [120, 80]},
                     "outputs": {"total": 200}, "ok": True},
                ],
                "outcome": {"budget": 200},
            }
            res = ingest_subagent_trace(trace, _base_inventory(), store)
            self.assertTrue(res.recorded)
            self.assertEqual(res.reason, "gap_recorded")
            self.assertIn("novel composition", res.detail["gap"])
        finally:
            td.cleanup()

    def test_fresh_process_persistence(self):
        td = tempfile.TemporaryDirectory()
        try:
            db = os.path.join(td.name, "epistemic.db")
            script_path = os.path.join(td.name, "ingest_child.py")
            trace = {
                "trace_id": "trace-child",
                "objective": "summarize a web page",
                "steps": [
                    {"tool": "web_fetch",
                     "inputs": {"url": "https://example.com"},
                     "outputs": {"html": "<p>x</p>"}, "ok": True},
                ],
                "outcome": {"html": "<p>x</p>"},
            }
            with open(script_path, "w") as f:
                f.write(
                    "import sys, json, os\n"
                    f"sys.path.insert(0, {_CANONICAL!r})\n"
                    f"sys.path.insert(0, os.path.join({_CANONICAL!r}, 'pylib'))\n"
                    "from swarm_engine.acquisition.ingest import "
                    "ingest_subagent_trace, InventorySnapshot\n"
                    "from swarm_engine.intellect.epistemic import EpistemicStore\n"
                    f"store = EpistemicStore({db!r})\n"
                    "inv = InventorySnapshot(capabilities=['sort_numbers'],\n"
                    "                          primitives=['sum'])\n"
                    f"trace = {trace!r}\n"
                    "res = ingest_subagent_trace(trace, inv, store)\n"
                    "print(json.dumps({'recorded': res.recorded,\n"
                    "                  'reason': res.reason,\n"
                    "                  'delta_id': res.delta_id}))\n"
                )
            proc = subprocess.run([sys.executable, script_path],
                                  capture_output=True, text=True, timeout=180)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            child = json.loads(proc.stdout)
            self.assertTrue(child["recorded"])
            self.assertIsNotNone(child["delta_id"])

            # Parent process reads back the delta observation by source filter.
            parent = EpistemicStore(db)
            found = [o for o in parent.all_observations()
                     if o.source == "technique_delta"
                     and o.raw["delta"]["delta_id"] == child["delta_id"]]
            self.assertEqual(len(found), 1)
        finally:
            td.cleanup()

    def test_plugin_adapter_same_path(self):
        td, store, db = _tmp_epistemic()
        try:
            record = {
                "trace_id": "bot-1",
                "objective": "check server health",
                "steps": [
                    {"tool": "ping_host",
                     "inputs": {"host": "db-1"},
                     "outputs": {"latency_ms": 12}, "ok": True},
                ],
                "outcome": {"latency_ms": 12},
            }
            res = ingest_plugin_action(record, _base_inventory(), store)
            self.assertTrue(res.recorded)
            self.assertEqual(res.reason, "gap_recorded")
            fresh = EpistemicStore(db)
            y_obs = [o for o in fresh.all_observations()
                     if o.source == "technique_y"]
            self.assertEqual(len(y_obs), 1)
            self.assertEqual(y_obs[0].raw["y_record"]["source"], "plugin_bot")
        finally:
            td.cleanup()

    def test_empty_inventory_everything_is_gap(self):
        td, store, _ = _tmp_epistemic()
        try:
            inv = build_inventory()
            self.assertEqual(inv.vocabulary(), set())
            res = ingest_subagent_trace(_novel_trace(), inv, store)
            self.assertTrue(res.recorded)
            self.assertEqual(res.reason, "gap_recorded")
        finally:
            td.cleanup()

    def test_epistemic_record_helpers(self):
        """The additive record_observation / record_hypothesis helpers:
        unique ids, persistence, frozen API underneath."""
        td, store, db = _tmp_epistemic()
        try:
            o1 = store.record_observation("saw x", "probe",
                                          raw={"k": 1})
            o2 = store.record_observation("saw y", "probe")
            self.assertTrue(o1.observation_id.startswith("obs_"))
            self.assertNotEqual(o1.observation_id, o2.observation_id)
            self.assertEqual(o2.raw, {})

            h = store.record_hypothesis("q1", "x causes y",
                                        specification={"n": 3},
                                        provenance={"by": "test"})
            self.assertTrue(h.hypothesis_id.startswith("hyp_"))
            self.assertEqual(h.state.value, "proposed")

            fresh = EpistemicStore(db)
            self.assertEqual(len(fresh.all_observations()), 2)
            self.assertEqual(fresh.get_hypothesis(h.hypothesis_id).statement,
                             "x causes y")
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
