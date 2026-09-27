"""Tests for semantic_route.py (Worker B: intent routing + direct answers).

Frames are constructed DIRECTLY (not via parse_frame — that is Worker A's
territory and not implemented yet). The inventory tests boot a REAL
SwarmEngine on scratch sqlite DBs and assert the ANSWER_META answer reflects
live-read values (primitive count, family names), never canned text.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.synthesis.semantic_frames import Intent, IntentFrame  # noqa: E402
from swarm_engine.synthesis.semantic_route import (  # noqa: E402
    SemanticRouteResult,
    evaluate_expression,
    route_frame,
)


def _frame(intent, entities=None, alternatives=None, raw=""):
    return IntentFrame(raw=raw, intent=intent, entities=entities or {},
                       alternatives=alternatives or [], confidence=0.9)


class TestAnswerMetaLiveInventory(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls._td.name, "engine.db")
        cls.engine = SwarmEngine(db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def test_answer_mentions_live_primitive_count(self):
        frame = _frame(Intent.ANSWER_META, {"topic": "tasks"},
                       raw="What are you able to do in terms of tasks?")
        result = route_frame(frame, self.engine)
        self.assertTrue(result.handled)
        self.assertIsNone(result.refusal)
        live_count = len(self.engine.primitives)
        self.assertIn(str(live_count), result.answer_text,
                      "answer must mention the REAL primitive count read at call time")

    def test_answer_mentions_live_family_names(self):
        result = route_frame(_frame(Intent.ANSWER_META, {"topic": "capabilities"}),
                             self.engine)
        live_families = sorted(self.engine.primitives.families())
        for fam in live_families:
            self.assertIn(fam, result.answer_text,
                          f"answer must mention real family {fam!r}")

    def test_answer_reflects_inventory_change(self):
        """Removing a primitive from the live registry changes the answer."""
        before = route_frame(_frame(Intent.ANSWER_META, {"topic": "tasks"}),
                             self.engine)
        before_count = len(self.engine.primitives)
        name = self.engine.primitives.names()[0]
        removed_prim = self.engine.primitives.get(name)
        removed = self.engine.primitives.unregister(name)
        self.assertTrue(removed, "test precondition: unregister a real primitive")
        try:
            after = route_frame(_frame(Intent.ANSWER_META, {"topic": "tasks"}),
                                self.engine)
            after_count = len(self.engine.primitives)
            self.assertEqual(after_count, before_count - 1)
            self.assertIn(str(after_count), after.answer_text)
            self.assertNotEqual(before.answer_text, after.answer_text,
                                "answer must change when the underlying inventory changes")
        finally:
            # restore the shared engine's vocabulary for other tests
            self.engine.primitives.register(removed_prim, overwrite=True)

    def test_answer_reports_capability_store_state(self):
        result = route_frame(_frame(Intent.ANSWER_META, {"topic": "tasks"}),
                             self.engine)
        inv = result.detail["inventory"]
        live_records = self.engine.capabilities.list(status="active")
        self.assertEqual(inv["capability_count"], len(live_records))

    def test_missing_engine_attributes_reported_honestly(self):
        result = route_frame(_frame(Intent.ANSWER_META, {"topic": "tasks"}),
                             object())
        self.assertTrue(result.handled)
        self.assertIn("doesn't expose a capability store", result.answer_text)
        self.assertIn("doesn't expose a primitive registry", result.answer_text)
        self.assertTrue(result.detail["inventory"]["capabilities_missing"])
        self.assertTrue(result.detail["inventory"]["primitives_missing"])

    def test_no_hardcoded_inventory(self):
        """A synthetic stub engine with made-up numbers must surface verbatim."""
        class FakeStore:
            def list(self, status="active"):
                class R:  # noqa: D106
                    name = "synthetic_cap_7"
                    capability_id = "cap-007"
                return [R()]

        class FakeRegistry:
            def __len__(self):
                return 424242
            def families(self):
                return {"fictional": ["nope"]}

        class FakeEngine:  # noqa: D106
            capabilities = FakeStore()
            primitives = FakeRegistry()

        result = route_frame(_frame(Intent.ANSWER_META, {"topic": "tasks"}),
                             FakeEngine())
        self.assertIn("424242", result.answer_text)
        self.assertIn("fictional", result.answer_text)
        self.assertIn("synthetic_cap_7", result.answer_text)


class TestComputeEvaluator(unittest.TestCase):
    def _route(self, expression):
        return route_frame(_frame(Intent.COMPUTE, {"expression": expression}),
                           None)

    def test_mission_prompts(self):
        self.assertEqual(self._route("5*6").answer_text, "30")
        self.assertEqual(self._route("1-1").answer_text, "0")

    def test_division_and_floats(self):
        self.assertEqual(self._route("10/4").answer_text, "2.5")
        self.assertEqual(self._route("7/2").answer_text, "3.5")
        self.assertEqual(self._route("10//3").answer_text, "3")
        self.assertEqual(self._route("10%3").answer_text, "1")

    def test_precedence_parens_power_unary(self):
        self.assertEqual(self._route("2+3*4").answer_text, "14")
        self.assertEqual(self._route("(2+3)*4").answer_text, "20")
        self.assertEqual(self._route("2**10").answer_text, "1024")
        self.assertEqual(self._route("-5+3").answer_text, "-2")
        self.assertEqual(self._route("2.5*4").answer_text, "10")

    def test_malformed_fails_closed(self):
        for bad in ("5+*6", "abc", "", "   ", "5*", "(2+3"):
            r = self._route(bad)
            self.assertTrue(r.handled)
            self.assertEqual(r.refusal, "compute_malformed", bad)

    def test_missing_expression_entity(self):
        r = route_frame(_frame(Intent.COMPUTE, {}), None)
        self.assertTrue(r.handled)
        self.assertEqual(r.refusal, "compute_malformed")

    def test_injection_attempts_rejected(self):
        for evil in ("__import__('os').system('id')",
                     "().__class__.__bases__",
                     "[x for x in range(3)]",
                     "open('/etc/passwd')",
                     "1 if True else 2",
                     "len('abc')"):
            r = self._route(evil)
            self.assertTrue(r.handled)
            self.assertEqual(r.refusal, "compute_malformed", evil)

    def test_division_by_zero_fails_closed_not_crash(self):
        r = self._route("1/0")
        self.assertTrue(r.handled)
        self.assertEqual(r.refusal, "compute_error")
        self.assertIsInstance(r.answer_text, str)

    def test_evaluate_expression_direct(self):
        self.assertEqual(evaluate_expression("5*6"), 30)
        self.assertEqual(evaluate_expression("(1+2)**2"), 9)

    def test_bool_constants_rejected(self):
        r = self._route("True+1")
        self.assertEqual(r.refusal, "compute_malformed")


class TestFailClosed(unittest.TestCase):
    def test_ambiguous_names_competing_readings(self):
        alt1 = _frame(Intent.CREATE_IMAGE, raw="...")
        alt2 = _frame(Intent.ANSWER_META, raw="...")
        frame = IntentFrame(raw="make me something", intent=Intent.AMBIGUOUS,
                            alternatives=[alt1, alt2], confidence=0.4)
        r = route_frame(frame, None)
        self.assertTrue(r.handled)
        self.assertEqual(r.refusal, "ambiguous_intent")
        self.assertIn("create_image", r.answer_text)
        self.assertIn("answer_meta", r.answer_text)

    def test_ambiguous_without_alternatives_still_honest(self):
        r = route_frame(_frame(Intent.AMBIGUOUS, raw="huh"), None)
        self.assertTrue(r.handled)
        self.assertEqual(r.refusal, "ambiguous_intent")
        self.assertIn("not sure", r.answer_text.lower())

    def test_unknown(self):
        r = route_frame(_frame(Intent.UNKNOWN, raw="blorpt flim"), None)
        self.assertTrue(r.handled)
        self.assertEqual(r.refusal, "unknown_intent")
        self.assertIn("don't understand", r.answer_text)

    def test_non_frame_input_fails_closed(self):
        r = route_frame("just a string", None)
        self.assertTrue(r.handled)
        self.assertEqual(r.refusal, "unknown_intent")


class TestDownstreamPassthrough(unittest.TestCase):
    def test_unhandled_intents_pass_through(self):
        for intent in (Intent.CREATE_FILE, Intent.CREATE_IMAGE, Intent.CREATE_VIDEO,
                       Intent.CREATE_SONG, Intent.CREATE_VOICE,
                       Intent.ANSWER_FACTUAL, Intent.EXECUTE, Intent.ROUTE):
            r = route_frame(_frame(intent, raw="do the thing"), None)
            self.assertFalse(r.handled, intent)
            self.assertIsNone(r.answer_text, intent)
            self.assertIsNone(r.refusal, intent)
            self.assertIn("downstream", r.detail.get("reason", ""), intent)

    def test_result_shape(self):
        r = route_frame(_frame(Intent.UNKNOWN), None)
        self.assertIsInstance(r, SemanticRouteResult)
        self.assertIsInstance(r.handled, bool)
        self.assertIsInstance(r.detail, dict)


if __name__ == "__main__":
    unittest.main()
