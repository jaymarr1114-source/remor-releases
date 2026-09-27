"""Unit tests for the grammar-based semantic frame parser.

These tests prove the ANTI-SIMULATION bar: the parser must generalise
through grammatical STRUCTURE (verb classes, NP rules, mood), not through
listed words. Paraphrase variants below use different verbs and different
purpose constructions that all route through the same grammar rules and
must yield the same frames.

Run: python3 -m unittest discover -s tests/synthesis -v
(from ~/workspace/remor_nl_semantic, or set PYTHONPATH to pylib/)
"""
import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib"),
)

from swarm_engine.synthesis.semantic_frames import (
    Intent,
    IntentFrame,
    parse_frame,
)


class MissionPrompts(unittest.TestCase):
    """The mission's four required prompts."""

    def test_create_python_file(self):
        f = parse_frame("Create a python file for a random number generator")
        self.assertEqual(f.intent, Intent.CREATE_FILE)
        self.assertEqual(f.mood, "imperative")
        self.assertEqual(f.entities.get("artifact"), "file")
        self.assertEqual(f.entities.get("language"), "python")
        self.assertEqual(f.entities.get("purpose"), "random number generator")
        self.assertTrue(f.actionable)
        self.assertTrue(f.trace)

    def test_answer_meta_tasks(self):
        f = parse_frame("What are you able to do in terms of tasks?")
        self.assertEqual(f.intent, Intent.ANSWER_META)
        self.assertEqual(f.mood, "interrogative")
        self.assertEqual(f.entities.get("topic"), "tasks")
        self.assertTrue(f.trace)

    def test_compute_word_op(self):
        f = parse_frame("5 times 6")
        self.assertEqual(f.intent, Intent.COMPUTE)
        self.assertEqual(f.entities.get("expression"), "5*6")
        self.assertTrue(f.trace)

    def test_compute_symbol_trailing(self):
        f = parse_frame("1-1=?")
        self.assertEqual(f.intent, Intent.COMPUTE)
        self.assertEqual(f.entities.get("expression"), "1-1")
        self.assertTrue(f.trace)


class ParaphraseGeneralisation(unittest.TestCase):
    """Different words, same grammatical constructions, same frames.

    This is the generalisation proof: none of these paraphrases share
    their verb or purpose-construction with the mission prompt, but all
    must yield the same CREATE_FILE frame via the creation-verb class +
    NP rule.
    """

    def _assert_create_file(self, text, language, purpose):
        f = parse_frame(text)
        self.assertEqual(f.intent, Intent.CREATE_FILE, msg=text)
        self.assertEqual(f.mood, "imperative", msg=text)
        self.assertEqual(f.entities.get("artifact"), "file", msg=text)
        self.assertEqual(f.entities.get("language"), language, msg=text)
        self.assertEqual(f.entities.get("purpose"), purpose, msg=text)
        self.assertTrue(f.trace, msg=text)
        return f

    def test_relative_clause_purpose(self):
        # different verb (write), purpose as relative clause, not for-PP
        self._assert_create_file(
            "Write me a python script that generates random numbers",
            "python", "random number generator")

    def test_infinitive_purpose(self):
        # different verb (build), purpose as infinitive
        self._assert_create_file(
            "Build a small python program to generate random numbers",
            "python", "random number generator")

    def test_make_verb(self):
        self._assert_create_file(
            "Make a python file for a random number generator",
            "python", "random number generator")

    def test_generate_verb(self):
        self._assert_create_file(
            "Generate a python file for a random number generator",
            "python", "random number generator")

    def test_different_language_and_nominalisation(self):
        # purpose nominalisation through the verb->agent-noun rule
        self._assert_create_file(
            "Write a javascript file that sorts numbers",
            "javascript", "number sorter")

    def test_polite_imperative(self):
        self._assert_create_file(
            "Please create a python file for a random number generator",
            "python", "random number generator")

    def test_meta_paraphrases(self):
        for text, topic in [
            ("What can you do?", "capabilities"),
            ("What are you capable of doing?", "capabilities"),
            ("How are you able to help?", "capabilities"),
        ]:
            f = parse_frame(text)
            self.assertEqual(f.intent, Intent.ANSWER_META, msg=text)
            self.assertEqual(f.mood, "interrogative", msg=text)
            self.assertEqual(f.entities.get("topic"), topic, msg=text)

    def test_compute_paraphrases(self):
        for text, expr in [
            ("3 plus 4", "3+4"),
            ("10 minus 2", "10-2"),
            ("12 divided by 4", "12/4"),
            ("2 * 3", "2*3"),
            ("What is 7 times 8?", "7*8"),
            ("How much is 9 plus 1?", "9+1"),
            ("Calculate 6 times 7", "6*7"),
        ]:
            f = parse_frame(text)
            self.assertEqual(f.intent, Intent.COMPUTE, msg=text)
            self.assertEqual(f.entities.get("expression"), expr, msg=text)


class GrammarStructure(unittest.TestCase):
    """Intent falls out of structure: verb class + NP taxonomy + mood."""

    def test_verb_class_not_keyword(self):
        # 'draw' is an imagine-class verb: NP head decides only when the
        # verb class is the neutral 'creation' class
        f = parse_frame("Draw a cat")
        self.assertEqual(f.intent, Intent.CREATE_IMAGE)
        self.assertEqual(f.entities.get("prompt"), "cat")

    def test_creation_verb_with_image_noun(self):
        # neutral creation verb + image-taxonomy head -> CREATE_IMAGE
        f = parse_frame("Create an image of a sunset")
        self.assertEqual(f.intent, Intent.CREATE_IMAGE)
        self.assertIn("sunset", f.entities.get("prompt", ""))

    def test_music_verb(self):
        f = parse_frame("Compose a song about the sea")
        self.assertEqual(f.intent, Intent.CREATE_SONG)

    def test_dispatch_verb(self):
        f = parse_frame("Run the backup job")
        self.assertEqual(f.intent, Intent.EXECUTE)
        self.assertEqual(f.entities.get("goal_text"), "the backup job")
        self.assertEqual(f.mood, "imperative")

    def test_desire_declarative_routes(self):
        f = parse_frame("I need a summary of this file")
        self.assertEqual(f.intent, Intent.ROUTE)
        self.assertEqual(f.mood, "declarative")
        self.assertTrue(f.entities.get("goal_text"))

    def test_interrogative_factual_fallback(self):
        f = parse_frame("What is the capital of France?")
        self.assertEqual(f.intent, Intent.ANSWER_FACTUAL)
        self.assertEqual(f.mood, "interrogative")
        self.assertIn("capital", f.entities.get("question", ""))

    def test_quoted_filename_hint(self):
        f = parse_frame('Create a file named "report.txt"')
        self.assertEqual(f.intent, Intent.CREATE_FILE)
        self.assertEqual(f.entities.get("filename_hint"), "report.txt")

    def test_unknown_verb_fails_closed(self):
        # verbs outside the lexicon must NOT be guessed at
        for text in ["Frobnicate a widget",
                     "Defenestrate the config file",
                     "Transmogrify my document"]:
            f = parse_frame(text)
            self.assertEqual(f.intent, Intent.UNKNOWN, msg=text)
            self.assertFalse(f.actionable, msg=text)


class AmbiguityDetection(unittest.TestCase):
    def test_verb_noun_conflict_is_ambiguous(self):
        # imagine-verb vs file-taxonomy head: two parses within 0.15
        f = parse_frame("Draw a python script")
        self.assertEqual(f.intent, Intent.AMBIGUOUS)
        self.assertFalse(f.actionable)
        intents = {a.intent for a in f.alternatives}
        self.assertIn(Intent.CREATE_IMAGE, intents)
        self.assertIn(Intent.CREATE_FILE, intents)
        self.assertTrue(f.trace)

    def test_clear_winner_not_ambiguous(self):
        f = parse_frame("Create a python file for a random number generator")
        self.assertEqual(f.intent, Intent.CREATE_FILE)
        self.assertEqual(f.alternatives, [])


class FailClosed(unittest.TestCase):
    def test_empty(self):
        for text in ["", "   ", "\n\t "]:
            f = parse_frame(text)
            self.assertEqual(f.intent, Intent.UNKNOWN, msg=repr(text))
            self.assertEqual(f.confidence, 0.0)
            self.assertFalse(f.actionable)
            self.assertTrue(f.trace)

    def test_non_str(self):
        for bad in [None, 123, 4.5, ["create"], {"x": 1}, b"create"]:
            f = parse_frame(bad)
            self.assertEqual(f.intent, Intent.UNKNOWN)
            self.assertFalse(f.actionable)
            self.assertTrue(f.trace)

    def test_never_raises_on_garbage(self):
        garbage = ["\x00\xff", "!!!???", "a" * 10000, "5 +",
                   "create create create", "? ? ?", "123 abc !@#"]
        for text in garbage:
            f = parse_frame(text)  # must not raise
            self.assertIsInstance(f, IntentFrame, msg=repr(text)[:40])
            self.assertTrue(f.trace, msg=repr(text)[:40])

    def test_gibberish_is_unknown(self):
        f = parse_frame("blorpt wug zorp")
        self.assertEqual(f.intent, Intent.UNKNOWN)


class TraceAndDeterminism(unittest.TestCase):
    def test_every_frame_has_trace(self):
        texts = [
            "Create a python file for a random number generator",
            "What are you able to do in terms of tasks?",
            "5 times 6",
            "1-1=?",
            "Draw a python script",
            "Frobnicate a widget",
            "",
            "Run the backup",
            "I need a summary",
        ]
        for text in texts:
            f = parse_frame(text)
            self.assertTrue(f.trace, msg=text)
            self.assertTrue(all(isinstance(t, str) and t
                                for t in f.trace), msg=text)

    def test_deterministic(self):
        texts = [
            "Create a python file for a random number generator",
            "What are you able to do in terms of tasks?",
            "5 times 6",
            "Draw a python script",
        ]
        for text in texts:
            a = parse_frame(text).as_dict()
            b = parse_frame(text).as_dict()
            self.assertEqual(a, b, msg=text)

    def test_as_dict_roundtrip(self):
        f = parse_frame("5 times 6")
        d = f.as_dict()
        self.assertEqual(d["intent"], "compute")
        self.assertEqual(d["entities"]["expression"], "5*6")
        self.assertTrue(d["actionable"])


if __name__ == "__main__":
    unittest.main()
