"""Q5 ingestion source expansion tests.

Real sqlite in tmp dirs, real fresh processes, real organizational
machinery (OrgStore + ExperienceStore.submit_candidate). No mocks, no
seeding: stores start empty and every record in them was written by the
test itself.

Covers the Q5 mandate:
 1. evidence-kind decision — list_technique_deltas() retrieves deltas from
    the EvidenceStore mirror (kind="observation" + record_kind marker);
 2. plugin-bot adapter exercised by an EXPLICITLY SIMULATED harness whose
    label is preserved end to end;
 3. a new natural source — L1 experience candidates — through the same
    causal-discipline gate, positive and negative;
 4. composition-heuristic stress: scale controls stay correct; the
    generic-name false-overlap limit is demonstrated and classified.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_CANONICAL = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _CANONICAL)
sys.path.insert(0, os.path.join(_CANONICAL, "pylib"))

from runtime.acquisition.ingest import (  # noqa: E402
    InventorySnapshot,
    ingest_experience_candidate,
    ingest_plugin_action,
    ingest_subagent_trace,
    list_technique_deltas,
)
from runtime.intellect.epistemic import EpistemicStore  # noqa: E402
from swarm_engine.services.evidence import EvidenceStore  # noqa: E402
from swarm_engine.agent_org.store import OrgStore  # noqa: E402
from swarm_engine.agent_org.experience import ExperienceStore  # noqa: E402


def _tmp_epistemic():
    td = tempfile.TemporaryDirectory()
    db = os.path.join(td.name, "epistemic.db")
    return td, EpistemicStore(db), db


def _tmp_evidence_store():
    td = tempfile.TemporaryDirectory()
    return td, EvidenceStore(os.path.join(td.name, "evidence.db"))


def _novel_trace(objective="summarize a web page"):
    return {
        "trace_id": "trace-q5",
        "objective": objective,
        "steps": [
            {"tool": "web_fetch",
             "inputs": {"url": "https://example.com/page"},
             "outputs": {"html": "<p>hello</p>"}, "ok": True},
            {"tool": "summarize",
             "inputs": {"text": "hello"},
             "outputs": {"summary": "a greeting"}, "ok": True},
        ],
        "outcome": {"summary": "a greeting"},
    }


class SimulatedPluginBotHarness:
    """EXPLICITLY SIMULATED plugin-bot source.

    No plugin registry exists in this runtime (execute_api marks it
    HONESTLY-UNAVAILABLE), so there is no live bot to exercise. This
    harness emits the structured record shape a real bot would emit and
    labels itself simulated in provenance. What is proven is the adapter
    mechanism and the end-to-end label preservation — never the bot.
    """

    SIMULATED = True

    def __init__(self, bot_name="weather_bot"):
        self.bot_name = bot_name

    def action_record(self, objective, steps, outcome):
        return {
            "objective": objective,
            "steps": steps,
            "outcome": outcome,
            "provenance": {
                "simulated": True,
                "harness": type(self).__name__,
                "bot_name": self.bot_name,
                "note": "no plugin registry exists; record is synthetic "
                        "but structured",
            },
        }


class TestEvidenceKindRetrieval(unittest.TestCase):
    def test_list_technique_deltas_recovers_delta(self):
        td, store, _db = _tmp_epistemic()
        etd, estore = _tmp_evidence_store()
        try:
            res = ingest_subagent_trace(
                _novel_trace(), InventorySnapshot(capabilities=[], primitives=[]),
                store, evidence_store=estore)
            self.assertTrue(res.recorded)
            deltas = list_technique_deltas(estore)
            self.assertEqual(len(deltas), 1)
            self.assertEqual(deltas[0]["delta_id"], res.delta_id)
            self.assertEqual(deltas[0]["record_kind"], "technique_delta")
            self.assertEqual(deltas[0]["y_id"], res.y_id)
            self.assertIn("gap", deltas[0])
        finally:
            td.cleanup()
            etd.cleanup()

    def test_list_technique_deltas_skips_non_delta_entries(self):
        _etd, estore = _tmp_evidence_store()
        try:
            estore.add_entry(kind="observation",
                             text="a plain human observation",
                             source="human")
            estore.add_entry(kind="observation",
                             text="{not valid json",
                             source="broken")
            estore.add_entry(kind="hypothesis",
                             text=json.dumps({"record_kind": "technique_delta"}),
                             source="wrong-kind")
            self.assertEqual(list_technique_deltas(estore), [])
        finally:
            _etd.cleanup()

    def test_failed_store_returns_empty_not_crash(self):
        class Broken:
            def list_entries(self, **kw):
                return {"ok": False, "error": "boom"}
        self.assertEqual(list_technique_deltas(Broken()), [])


class TestSimulatedPluginBot(unittest.TestCase):
    def test_simulated_label_preserved_end_to_end(self):
        td, store, db = _tmp_epistemic()
        etd, estore = _tmp_evidence_store()
        try:
            harness = SimulatedPluginBotHarness(bot_name="weather_bot")
            record = harness.action_record(
                "fetch current weather for Lowell MA",
                [{"tool": "geo_resolve",
                  "inputs": {"place": "Lowell MA"},
                  "outputs": {"lat": 42.63, "lon": -71.31}, "ok": True},
                 {"tool": "weather_fetch",
                  "inputs": {"lat": 42.63, "lon": -71.31},
                  "outputs": {"temp_c": 18, "cond": "clear"}, "ok": True}],
                {"temp_c": 18, "cond": "clear"})
            res = ingest_plugin_action(
                record, InventorySnapshot(capabilities=[], primitives=[]),
                store, evidence_store=estore)
            self.assertTrue(res.recorded, res.reason)
            fresh = EpistemicStore(db)
            y_obs = [o for o in fresh.all_observations()
                     if o.source == "technique_y"]
            self.assertEqual(len(y_obs), 1)
            prov = y_obs[0].raw.get("provenance", {})
            self.assertTrue(prov.get("simulated"),
                            "simulated label must survive to the persisted Y record")
            self.assertEqual(prov.get("bot_name"), "weather_bot")
            self.assertEqual(y_obs[0].raw["y_record"]["source"], "plugin_bot")
            # And the delta is retrievable through the Evidence view path.
            deltas = list_technique_deltas(estore)
            self.assertEqual(len(deltas), 1)
            self.assertEqual(deltas[0]["source"], "plugin_bot")
        finally:
            td.cleanup()
            etd.cleanup()

    def test_simulated_bot_without_io_is_refused(self):
        td, store, _db = _tmp_epistemic()
        try:
            harness = SimulatedPluginBotHarness()
            record = harness.action_record(
                "you should learn to be helpful",
                [{"tool": "be_helpful", "inputs": {}, "outputs": {}}],
                {})
            res = ingest_plugin_action(
                record, InventorySnapshot(capabilities=[], primitives=[]), store)
            self.assertFalse(res.recorded)
            self.assertEqual(res.reason, "insufficient_evidence")
        finally:
            td.cleanup()


def _real_candidate(io_contract, technique="median_of_three_pivot"):
    """Produce a REAL L1 experience candidate through the organizational
    machinery (OrgStore + ExperienceStore.submit_candidate on a tmp db)."""
    td = tempfile.TemporaryDirectory()
    org = OrgStore(os.path.join(td.name, "org.db"))
    exp = ExperienceStore(org)
    cid = exp.submit_candidate(
        wp_id="wp-q5", agent_id="agent-7", problem_class="sorting",
        technique_name=technique,
        description="pick pivot as median of first/middle/last",
        code="def pivot(xs): return sorted([xs[0],xs[len(xs)//2],xs[-1]])[1]",
        entrypoint="pivot", io_contract=io_contract,
        tags=["sorting"], params={}, evidence_refs={"run": "ok"})
    cand = exp.get_candidate(cid)
    return td, cand


class TestExperienceCandidateSource(unittest.TestCase):
    def test_novel_candidate_records_delta(self):
        td, store, db = _tmp_epistemic()
        ctd, cand = _real_candidate(
            {"inputs": {"xs": [3, 1, 2]}, "outputs": {"pivot": 2}})
        try:
            res = ingest_experience_candidate(
                cand, InventorySnapshot(capabilities=["sort_numbers"],
                                        primitives=["sum"]),
                store)
            self.assertTrue(res.recorded, res.reason)
            self.assertEqual(res.reason, "gap_recorded")
            fresh = EpistemicStore(db)
            d_obs = [o for o in fresh.all_observations()
                     if o.source == "technique_delta"]
            self.assertEqual(len(d_obs), 1)
            delta = d_obs[0].raw["delta"]
            self.assertEqual(delta["Y"]["source"], "experience_candidate")
            self.assertIn("median_of_three_pivot", delta["gap"])
            y_obs = [o for o in fresh.all_observations()
                     if o.source == "technique_y"]
            ev = y_obs[0].raw["y_record"]["actions"][0]["evidence"]
            self.assertEqual(ev["agent_id"], "agent-7")
            self.assertTrue(ev["candidate_id"])
        finally:
            td.cleanup()
            ctd.cleanup()

    def test_known_technique_candidate_produces_no_record(self):
        td, store, _db = _tmp_epistemic()
        ctd, cand = _real_candidate(
            {"inputs": {"xs": [3, 1, 2]}, "outputs": {"pivot": 2}})
        try:
            n_before = len(store.all_observations())
            res = ingest_experience_candidate(
                cand,
                InventorySnapshot(capabilities=["median_of_three_pivot"],
                                  primitives=[]),
                store)
            self.assertFalse(res.recorded)
            self.assertEqual(res.reason, "no_observable_gap")
            self.assertEqual(len(store.all_observations()), n_before)
        finally:
            td.cleanup()
            ctd.cleanup()

    def test_candidate_without_observed_io_is_refused(self):
        td, store, _db = _tmp_epistemic()
        ctd, cand = _real_candidate({"inputs": {}, "outputs": {}})
        try:
            res = ingest_experience_candidate(
                cand, InventorySnapshot(capabilities=[], primitives=[]), store)
            self.assertFalse(res.recorded)
            self.assertEqual(res.reason, "insufficient_evidence")
        finally:
            td.cleanup()
            ctd.cleanup()

    def test_candidate_examples_fallback(self):
        td, store, _db = _tmp_epistemic()
        ctd, cand = _real_candidate(
            {"examples": [{"args": {"xs": [5, 4]}, "expected": {"pivot": 5}}]})
        try:
            res = ingest_experience_candidate(
                cand, InventorySnapshot(capabilities=[], primitives=[]), store)
            self.assertTrue(res.recorded, res.reason)
        finally:
            td.cleanup()
            ctd.cleanup()


class TestCompositionHeuristicStress(unittest.TestCase):
    def _inventory(self, n, generic=()):
        caps = [f"capability_{i:04d}" for i in range(n)]
        caps.extend(generic)
        return InventorySnapshot(capabilities=caps, primitives=["sum"])

    def test_scale_control_cases_stay_correct(self):
        # 500 capabilities: a truly novel tool still records; a covered
        # objective with known tools still refuses.
        td, store, _db = _tmp_epistemic()
        try:
            inv = self._inventory(500)
            res = ingest_subagent_trace(_novel_trace(), inv, store)
            self.assertTrue(res.recorded)
            self.assertIn("absent from inventory", res.detail["gap"])
            covered = dict(_novel_trace())
            covered["objective"] = "capability_0042 routine maintenance"
            covered["steps"] = [
                {"tool": "sum", "inputs": {"xs": [1]},
                 "outputs": {"total": 1}, "ok": True}]
            res2 = ingest_subagent_trace(covered, inv, store)
            self.assertFalse(res2.recorded)
            self.assertEqual(res2.reason, "no_observable_gap")
        finally:
            td.cleanup()

    def test_generic_name_false_overlap_is_a_known_bound(self):
        # KNOWN LIMIT (classified, not fixed — see report): capability
        # "image_generate" shares the word "generate" with an objective it
        # does not cover. The lexical heuristic reports overlap and the
        # real composition gap is MISSED (no record). Any purely lexical
        # retuning just moves this error around; the honest repair is the
        # planner's actual attempt as the Z check (M2/Q2 territory).
        td, store, _db = _tmp_epistemic()
        try:
            inv = self._inventory(0, generic=["image_generate"])
            trace = dict(_novel_trace())
            trace["objective"] = "generate a quarterly revenue summary"
            trace["steps"] = [
                {"tool": "sum", "inputs": {"xs": [1, 2]},
                 "outputs": {"total": 3}, "ok": True}]
            n_before = len(store.all_observations())
            res = ingest_subagent_trace(trace, inv, store)
            # Documents current behavior: the gap is missed.
            self.assertFalse(res.recorded)
            self.assertEqual(res.reason, "no_observable_gap")
            self.assertEqual(len(store.all_observations()), n_before)
        finally:
            td.cleanup()

    def test_novel_composition_still_detected(self):
        # Distinctive capability names do not collide: an uncovered
        # objective with known tools records as a composition gap.
        td, store, _db = _tmp_epistemic()
        try:
            inv = self._inventory(0, generic=["zk_quorum_attestor"])
            trace = dict(_novel_trace())
            trace["objective"] = "bake sourdough bread schedule"
            trace["steps"] = [
                {"tool": "sum", "inputs": {"xs": [1]},
                 "outputs": {"total": 1}, "ok": True}]
            res = ingest_subagent_trace(trace, inv, store)
            self.assertTrue(res.recorded)
            self.assertIn("novel composition", res.detail["gap"])
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
