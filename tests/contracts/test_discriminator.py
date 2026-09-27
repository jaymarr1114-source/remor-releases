"""Discriminator tests (conversational-minimum mission, Worker 1).

Three parts:

1. TestDiscriminatorEval -- RUNS the real classifier over the held-out
   eval set (tests/data/discriminator_eval.jsonl), COMPUTES per-class
   precision/recall/F1 and the confusion matrix, and PRINTS the numbers.
   Nothing is canned: the classifications come from
   discriminator.classify(). Regression floors sit below the measured
   2026-09-26 values; they guard against regressions, not for quality
   claims. The dev set (discriminator_dev.jsonl) was used during
   development; the eval set was authored after the classifier froze and
   is measured once here.

2. TestMandatedCases -- the mission-mandated boundary messages with
   their required classes, asserted explicitly.

3. TestChatWiringContract -- the HTTP contract over a live serve()
   with Worker 2's REAL chat_handler: chat/ambiguous turns never reach
   the metering gate (tasks_today and the scheduler_runs row count are
   unchanged), task turns still consume a slot, a 429 quota refusal of
   a chat turn is impossible, the classifier is the causal gate
   (revert->fails/apply->holds), and a missing chat_handler fails
   LOUD, never silent.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services import discriminator  # noqa: E402
from swarm_engine.services import intent_dispatch_api  # noqa: E402
from swarm_engine.services.message_text import (  # noqa: E402
    normalize_message_text)
from swarm_engine.services.http_adapter import (  # noqa: E402
    serve, close_services)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
EVAL_PATH = os.path.join(DATA_DIR, "discriminator_eval.jsonl")
CLASSES = ("chat", "task", "ambiguous")

# The mission-mandated boundary messages and their required classes.
MANDATED = [
    ("5 times 6", "task"),
    ("make an image", "task"),
    ("what can you do", "chat"),
    ("hey", "chat"),
    ("can you tell me the time?", "chat"),
    ("can you set a timer?", "task"),
    ("the server is down", "ambiguous"),
    ("can you write a parser for me?", "task"),
    ("can you help me with this?", "ambiguous"),
]


def _load_eval():
    items = []
    with open(EVAL_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


class TestDiscriminatorEval(unittest.TestCase):
    """Measured precision/recall on the held-out eval set."""

    def test_measured_precision_recall(self):
        items = _load_eval()
        self.assertTrue(len(items) >= 40,
                        "eval set too small to mean anything")
        # confusion[true][pred]
        confusion = {t: {p: 0 for p in CLASSES} for t in CLASSES}
        for item in items:
            true = item["label"]
            pred = discriminator.classify(item["text"])["mode"]
            self.assertIn(true, CLASSES, item)
            self.assertIn(pred, CLASSES, item["text"])
            confusion[true][pred] += 1

        stats = {}
        for cls in CLASSES:
            tp = confusion[cls][cls]
            fp = sum(confusion[t][cls] for t in CLASSES if t != cls)
            fn = sum(confusion[cls][p] for p in CLASSES if p != cls)
            support = sum(confusion[cls].values())
            precision = tp / (tp + fp) if (tp + fp) else 0.0
            recall = tp / (tp + fn) if (tp + fn) else 0.0
            f1 = (2 * precision * recall / (precision + recall)
                  if (precision + recall) else 0.0)
            stats[cls] = {"precision": precision, "recall": recall,
                          "f1": f1, "support": support,
                          "tp": tp, "fp": fp, "fn": fn}
        total = len(items)
        correct = sum(confusion[c][c] for c in CLASSES)
        accuracy = correct / total
        task_chat = confusion["task"]["chat"]      # slot saved, work lost
        chat_task = confusion["chat"]["task"]      # slot burned on chatter
        macro_f1 = sum(s["f1"] for s in stats.values()) / len(CLASSES)

        # -- the measured numbers, printed in the test output -----------
        print("\n==== discriminator held-out eval (%s) ====" % EVAL_PATH)
        print("n=%d  accuracy=%.4f  macro-F1=%.4f" %
              (total, accuracy, macro_f1))
        for cls in CLASSES:
            s = stats[cls]
            print("  %-9s P=%.4f R=%.4f F1=%.4f  "
                  "(tp=%d fp=%d fn=%d support=%d)" %
                  (cls, s["precision"], s["recall"], s["f1"],
                   s["tp"], s["fp"], s["fn"], s["support"]))
        print("  confusion rows=true cols=pred:")
        print("            " + "".join("%10s" % c for c in CLASSES))
        for t in CLASSES:
            print("  true=%-5s" % t + "".join("%10d" % confusion[t][p]
                                              for p in CLASSES))
        print("  task->chat misfires (work lost): %d" % task_chat)
        print("  chat->task misfires (slot burned): %d" % chat_task)
        print("==== end discriminator eval ====\n")

        # -- regression floors (below the measured 2026-09-26 values) --
        self.assertGreaterEqual(accuracy, 0.90)
        self.assertEqual(task_chat, 0,
                         "a task turn must never be answered as chat")
        self.assertEqual(chat_task, 0,
                         "a chat turn must never burn a task slot")
        for cls in CLASSES:
            self.assertGreaterEqual(stats[cls]["recall"], 0.85, cls)


class TestMandatedCases(unittest.TestCase):
    """Each mission-mandated boundary message classifies as required."""

    def test_mandated_boundaries(self):
        for text, want in MANDATED:
            got = discriminator.classify(text)
            self.assertEqual(got["mode"], want,
                             "%r -> %r (signals=%r), want %r"
                             % (text, got["mode"], got["signals"], want))


# ---------------------------------------------------------------------
# chat-wiring contract over real HTTP
# ---------------------------------------------------------------------
# chat-wiring contract over real HTTP
# ---------------------------------------------------------------------
# Worker 2's chat_handler (runtime_work/services/chat_handler.py) is
# present, so these tests exercise the REAL integration: the real
# discriminator routes, the real handler answers from real store reads,
# and the metering gate is never reached for chat/ambiguous turns.


class _HideChatHandler:
    """Context manager that makes `import
    swarm_engine.services.chat_handler` fail as if the module were
    absent: None in sys.modules halts the import, and the parent
    package attribute (left by any earlier real import) is removed so
    the from-import cannot fall back to it."""

    def __init__(self):
        self._had_mod = False
        self._saved_mod = None
        self._had_attr = False
        self._saved_attr = None

    def __enter__(self):
        name = "swarm_engine.services.chat_handler"
        self._had_mod = name in sys.modules
        self._saved_mod = sys.modules.get(name)
        parent = sys.modules.get("swarm_engine.services")
        self._had_attr = hasattr(parent, "chat_handler")
        if self._had_attr:
            self._saved_attr = parent.chat_handler
            delattr(parent, "chat_handler")
        sys.modules[name] = None
        return self

    def __exit__(self, *exc):
        name = "swarm_engine.services.chat_handler"
        parent = sys.modules.get("swarm_engine.services")
        if self._had_mod:
            sys.modules[name] = self._saved_mod
        else:
            sys.modules.pop(name, None)
        if self._had_attr:
            parent.chat_handler = self._saved_attr
        elif hasattr(parent, "chat_handler"):
            delattr(parent, "chat_handler")
        return False


class _ChatHttpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chatwiring_")
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.server, self.services, self.thread, self.base = serve(self.tmp)
        with open(os.path.join(self.tmp, "api_token"), encoding="utf-8") as fh:
            self.token = fh.read().strip()
        with open(os.path.join(self.tmp, "operator.token"),
                  encoding="utf-8") as fh:
            self.op_token = fh.read().strip()
        self.op_id = self.server.svc.operator_id

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        close_services(self.services)

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + self.token}
        if method in ("POST", "PUT", "DELETE", "PATCH"):
            # Merged contract: mutating routes need operator credentials.
            headers["X-Agent-Id"] = self.op_id
            headers["X-Agent-Token"] = self.op_token
        r = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def tasks_today(self):
        code, usage = self.req("GET", "/api/metering")
        self.assertEqual(code, 200)
        return usage["tasks_today"]

    def run_rows(self):
        con = sqlite3.connect(self.services["scheduler"].db_path)
        try:
            return con.execute(
                "SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
        finally:
            con.close()


class TestChatWiringContract(_ChatHttpBase):
    """Chat/ambiguous turns over the REAL HTTP path never touch metering."""

    def test_chat_turn_consumes_no_slot(self):
        before_tasks = self.tasks_today()
        before_rows = self.run_rows()
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "hey", "producer": "gui:operator"})
        self.assertEqual(code, 200, obj)
        # real chat_handler shape, answered from the handler itself
        self.assertEqual(obj["mode"], "acknowledge", obj)
        self.assertIn("text", obj, obj)
        self.assertEqual(self.tasks_today(), before_tasks)
        self.assertEqual(self.run_rows(), before_rows)

    def test_capability_question_consumes_no_slot(self):
        before_tasks = self.tasks_today()
        before_rows = self.run_rows()
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "what can you do"})
        self.assertEqual(code, 200, obj)
        # answered from the REAL capability store (empty on scratch)
        self.assertEqual(obj["mode"], "answer", obj)
        self.assertEqual(obj["kind"], "capabilities", obj)
        self.assertEqual(self.tasks_today(), before_tasks)
        self.assertEqual(self.run_rows(), before_rows)

    def test_ambiguous_turn_asks_to_clarify_no_slot(self):
        before_tasks = self.tasks_today()
        before_rows = self.run_rows()
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "can you help me with this?"})
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["mode"], "clarify", obj)
        self.assertIn("rephrase", obj["text"], obj)
        self.assertEqual(self.tasks_today(), before_tasks)
        self.assertEqual(self.run_rows(), before_rows)

    def test_task_turn_still_consumes_slot(self):
        before = self.tasks_today()
        code, obj = self.req(
            "POST", "/api/intent/dispatch",
            {"text": "generate contract probe xyzzy",
             "producer": "wiring-test"})
        self.assertEqual(code, 200, obj)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["refusal"], "unknown_intent", obj)
        self.assertEqual(self.tasks_today(), before + 1)

    def test_old_probe_texts_now_route_to_chat(self):
        """Documents the converted-behavior boundary: the old verbless
        probe fragments classify as ambiguous -> the chat handler asks
        to clarify, no slot. The inherited metering tests were converted
        to imperative probes for exactly this reason."""
        before = self.tasks_today()
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "unroutable probe 0 xyzzy"})
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["mode"], "clarify", obj)
        self.assertEqual(self.tasks_today(), before)

    def test_chat_survives_exhausted_quota(self):
        """A 429 quota refusal of a CHAT turn is impossible: chat never
        reaches the gate. Exhaust the 35/day quota with real task turns
        over HTTP, then chat turns are still answered."""
        for i in range(35):
            code, obj = self.req(
                "POST", "/api/intent/dispatch",
                {"text": f"generate quota probe file {i} xyzzy",
                 "producer": "wiring-test"})
            self.assertEqual(code, 200, (i, code, obj))
            self.assertEqual(obj["refusal"], "unknown_intent", (i, obj))
        self.assertEqual(self.tasks_today(), 35)
        # 36th TASK turn: the REAL 429
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "generate one more quota probe xyzzy"})
        self.assertEqual(code, 429, obj)
        self.assertFalse(obj["ok"])
        # ...but CHAT turns are still answered, quota untouched
        for text, want_mode in (("hey", "acknowledge"),
                                ("what can you do", "answer"),
                                ("can you help me with this?", "clarify")):
            code, obj = self.req("POST", "/api/intent/dispatch",
                                 {"text": text})
            self.assertEqual(code, 200, (text, obj))
            self.assertNotEqual(code, 429)
            self.assertEqual(obj["mode"], want_mode, (text, obj))
        self.assertEqual(self.tasks_today(), 35)

    def test_classifier_is_causal_gate(self):
        """Revert->fails / apply->holds: with the classifier broken
        (forced to task), a chat turn leaks into the task pipeline and
        burns a slot; with the real classifier restored, it does not."""
        real_classify = discriminator.classify
        discriminator.classify = lambda text: {"mode": "task",
                                               "signals": ["revert-probe"],
                                               "reason": "forced"}
        self.addCleanup(setattr, discriminator, "classify", real_classify)
        before = self.tasks_today()
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "hey"})
        self.assertEqual(code, 200, obj)
        # revert case: the chat turn burned a slot (the failure mode)
        self.assertEqual(self.tasks_today(), before + 1)

        discriminator.classify = real_classify
        before = self.tasks_today()
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "hey"})
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["mode"], "acknowledge", obj)
        # apply case: the invariant holds again
        self.assertEqual(self.tasks_today(), before)

    def test_converted_probe_texts_are_task(self):
        """The converted inherited probe texts genuinely classify as
        task (documents why the conversion preserves those tests)."""
        for text in ("generate metering probe file 0 xyzzy",
                     "generate one more metering probe xyzzy",
                     "generate a probe while queued xyzzy"):
            self.assertEqual(discriminator.classify(text)["mode"],
                             "task", text)

    def test_missing_chat_handler_is_loud(self):
        """Without the chat_handler module, a chat turn raises a clear
        error naming the missing module -- never a silent re-route
        into the task pipeline."""
        with _HideChatHandler():
            code, obj = self.req("POST", "/api/intent/dispatch",
                                 {"text": "hey"})
        self.assertEqual(code, 500, obj)
        self.assertIn("chat_handler", obj.get("error", ""), obj)
        self.assertIn("conversational minimum", obj.get("error", ""), obj)


class TestMessageNormalization(_ChatHttpBase):
    """Dictation-artifact normalization (James's ", make a picture..."
    defect): junk punctuation is stripped at the very top of
    intent_dispatch(), before the discriminator and the metering gate,
    so the run's goal text -- and anything that echoes it -- is clean.
    Inner content is byte-preserved."""

    def test_normalize_unit_cases(self):
        cases = [
            # (raw, expected)
            (", make a picture of a dog with brown fur and white spots,",
             "make a picture of a dog with brown fur and white spots"),
            ("  spaced out  ", "spaced out"),
            ("...hello...", "hello"),
            ('"quoted"', "quoted"),
            ("don't stop, believin'!", "don't stop, believin"),
            # inner punctuation is byte-preserved
            ("well, hello there!", "well, hello there"),
            # Unicode punctuation deliberately untouched (ASCII-only)
            ("«hello»", "«hello»"),
            ("hello…", "hello…"),
            # degenerate inputs: never emptied by the strip loop
            ("...", "..."),
            (",", ","),
            ("", ""),
            ("   ", ""),
        ]
        for raw, want in cases:
            self.assertEqual(normalize_message_text(raw), want, repr(raw))
        # non-string input passes through for the caller's type check
        self.assertEqual(normalize_message_text(123), 123)
        self.assertIsNone(normalize_message_text(None))

    def latest_goal(self):
        con = sqlite3.connect(self.services["scheduler"].db_path)
        try:
            row = con.execute(
                "SELECT goal FROM scheduler_runs "
                "ORDER BY queued_at DESC LIMIT 1").fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def test_dictation_junk_produces_clean_goal_end_to_end(self):
        """James's exact defect: leading/trailing comma junk must not
        reach the run's goal text (or anything echoing it)."""
        junk = ", make a picture of a dog with brown fur and white spots,"
        clean = "make a picture of a dog with brown fur and white spots"
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": junk, "producer": "gui:operator"})
        self.assertEqual(code, 200, obj)
        # normalized text classifies as a task turn (imperative "make")
        self.assertEqual(obj["refusal"], "unknown_intent", obj)
        # the created run's goal is the CLEAN text, byte-exact
        self.assertEqual(self.latest_goal(), clean)
        # and the inner content survived byte-identical
        self.assertEqual(self.latest_goal(), junk.strip(" ,"))

    def test_normalization_revert_pair(self):
        """Revert: with normalization disabled (identity), the junk
        survives verbatim in the run's goal; enabled: it is clean.
        Inner text byte-identical in both cases."""
        real = intent_dispatch_api._normalize_message_text
        intent_dispatch_api._normalize_message_text = lambda t: t
        self.addCleanup(setattr, intent_dispatch_api,
                        "_normalize_message_text", real)
        junk = "generate a picture of a dog, "  # trailing junk keeps it
        # a task turn even unnormalized (imperative "generate")
        clean = "generate a picture of a dog"

        # revert: junk survives in the created run's goal
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": junk})
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["refusal"], "unknown_intent", obj)
        self.assertEqual(self.latest_goal(), junk)

        # apply: the same message normalizes to a clean goal
        intent_dispatch_api._normalize_message_text = real
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": junk})
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["refusal"], "unknown_intent", obj)
        self.assertEqual(self.latest_goal(), clean)

        # inner text byte-identical across revert/apply
        self.assertEqual(junk.strip(" ,"), clean)

    def test_leading_junk_breaks_routing_without_normalization(self):
        """Why normalization sits before the discriminator: a leading
        comma breaks the imperative match, so without normalization
        James's message misroutes to chat (clarify, no work attempted);
        with it, the message routes as the task it is."""
        junk = ", make a picture of a dog with brown fur and white spots,"
        real = intent_dispatch_api._normalize_message_text
        intent_dispatch_api._normalize_message_text = lambda t: t
        try:
            code, obj = self.req("POST", "/api/intent/dispatch",
                                 {"text": junk})
            self.assertEqual(code, 200, obj)
            self.assertEqual(obj["mode"], "clarify", obj)  # misrouted
            self.assertEqual(self.tasks_today(), 0)
        finally:
            intent_dispatch_api._normalize_message_text = real
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": junk})
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["refusal"], "unknown_intent", obj)  # task
        self.assertEqual(self.tasks_today(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
