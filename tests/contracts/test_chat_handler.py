"""Causal tests for the honest chat handler (Worker 2, conversational minimum).

Bar: real stores under scratch dirs, real engine where capability reads are
involved, real scheduler runs, real metering counts. Every behavior claimed
is revert-paired: the "without" case is constructed by removing the real
cause (no admission, deleted capability, emptied document, no runs), never
by stubbing the machinery.

The anti-fake battery (a)-(d) each carries its paired control:
  (a) empty store must not name any capability / positive: admitted
      capability IS listed by name
  (b) unknown run_id -> honest "no such run", never a completion claim /
      positive: real completed run -> real facts (id, goal, status)
  (c) "did you delete my file?" -> honest "No" / positive: sha256 of every
      DB is unchanged after a full answer() battery (read-only proof)
  (d) "what is my name?" -> "I don't know" / positive: hosted soul.md IS
      reflected for "who are you"

HTTP: Worker 1 wires the discriminator into the dispatch path and covers
the HTTP layer. No /api/chat route exists in this tree yet, so there is no
HTTP path to drive here. The composed-service-graph test below drives
answer() through the real build_services() graph (the same services dict
Worker 1's wiring will pass), which is the operational end-to-end proof
available to this worker.
"""
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.chat_handler import answer  # noqa: E402
from swarm_engine.services.http_adapter import (  # noqa: E402
    build_services, close_services)
from swarm_engine.services.evidence import EvidenceStore  # noqa: E402
from swarm_engine.synthesis.capability_store import (  # noqa: E402
    CapabilityStore)

TERMINAL = ("completed", "failed", "stopped", "cancelled", "error")
QUICK_GOAL = "knowledge_stats"

WRITE_PLAN = {
    "name": "write_note",
    "params": {"path": "str", "content": "str"},
    "steps": [{"id": "w", "op": "write_text",
               "args": {"path": {"$param": "path"},
                        "content": {"$param": "content"}}}],
    "result": {"$step": "w"},
}
WRITE_GOAL = "write a short note to a file"


def _wait_terminal(sched, run_id, timeout=60.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = sched.get_run(run_id)
        if rec is not None and rec["status"] in TERMINAL:
            return rec
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached terminal state")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _db_content(db_path):
    """Logical content of a sqlite DB: schema + rows per table. The
    genuine no-mutation comparison (immune to WAL checkpoint byte moves)."""
    if not os.path.exists(db_path):
        return None
    con = sqlite3.connect(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "ORDER BY name")]
        out = {}
        for t in tables:
            cols = [c[1] for c in con.execute(f'PRAGMA table_info("{t}")')]
            rows = con.execute(f'SELECT * FROM "{t}"').fetchall()
            out[t] = {"columns": cols, "rows": sorted(map(repr, rows))}
        return out
    finally:
        con.close()


def _admit_real_capability(base_dir, name="write_note", goal=WRITE_GOAL):
    """Admit a capability through the REAL engine admission machinery into
    the SAME capabilities.db that the service graph's CapabilityAPI
    reads. Real Governor grant first (mirrors the machinery tests)."""
    from swarm_engine.core.engine import SwarmEngine
    from swarm_engine.primitives.core import Effect
    db = os.path.join(base_dir, "capabilities.db")
    eng = SwarmEngine(db_path=db)
    try:
        eng.governor.grant(Effect("write_fs"), base_dir + "/*", note="test grant")
        plan = dict(WRITE_PLAN)
        plan["name"] = name
        res = eng.admit_as_engine(goal, plan, name=name)
        assert res.ok, res.reasons
        return res.capability_id
    finally:
        # D11: release DB ownership; the service graph boots its own
        # engine on the same file.
        eng.close()


class ChatHandlerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="chat_handler_test_")
        self.svc = build_services(self.tmp)

    def tearDown(self):
        try:
            close_services(self.svc)
        finally:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -- (a) must not invent capabilities ---------------------------------
    def test_capabilities_empty_store_names_nothing(self):
        out = answer("what can you do", self.svc)
        self.assertEqual(out["mode"], "answer")
        self.assertEqual(out["kind"], "capabilities")
        self.assertIn("don't know", out["text"].lower())
        self.assertEqual(out["grounded"]["count"], 0)
        # The anti-fake core: with an empty store, NO capability name may
        # appear in the answer.
        for probe in ("write_note", "map_values", "compute", "triple",
                      "capability_"):
            self.assertNotIn(probe, out["text"])

    def test_capabilities_causal_add_remove(self):
        base = answer("what can you do", self.svc)
        self.assertIn("don't know", base["text"].lower())

        # APPLY: real admission through the real engine.
        cap_id = _admit_real_capability(self.tmp)
        applied = answer("what can you do", self.svc)
        self.assertIn("write_note", applied["text"])
        self.assertEqual(applied["grounded"]["count"], 1)
        self.assertIn("write_note", applied["grounded"]["names"])

        # REMOVE: real deletion through the real CapabilityStore API.
        db = os.path.join(self.tmp, "capabilities.db")
        self.assertTrue(CapabilityStore(db).delete_capability(cap_id))
        removed = answer("what can you do", self.svc)
        self.assertIn("don't know", removed["text"].lower())
        self.assertNotIn("write_note", removed["text"])
        self.assertEqual(removed["grounded"]["count"], 0)

        # REVERT->HOLDS: re-admit (second plan variant) -> listed again.
        _admit_real_capability(self.tmp, name="write_note_2",
                               goal=WRITE_GOAL + " (second)")
        reapplied = answer("what can you do", self.svc)
        self.assertIn("write_note_2", reapplied["text"])
        self.assertEqual(reapplied["grounded"]["count"], 1)

    # -- documents: causal fact add/remove --------------------------------
    def test_document_causal_add_remove(self):
        ev = self.svc["evidence"]
        base = answer("what does theory.md say", self.svc)
        self.assertIn("don't know", base["text"].lower())

        ev.save_document("theory.md", "THEORY FACT: the florp constant is 42.")
        applied = answer("what does theory.md say", self.svc)
        self.assertIn("florp constant is 42", applied["text"])
        self.assertTrue(applied["grounded"]["exists"])

        # REMOVE: the fact leaves the store through the real save API
        # (empty document) -> the answer must no longer state it.
        ev.save_document("theory.md", "")
        removed = answer("what does theory.md say", self.svc)
        self.assertIn("don't know", removed["text"].lower())
        self.assertNotIn("florp", removed["text"])

        # REVERT->HOLDS.
        ev.save_document("theory.md", "THEORY FACT: the florp constant is 42.")
        reapplied = answer("what does theory.md say", self.svc)
        self.assertIn("florp constant is 42", reapplied["text"])

    def test_unknown_document_honest(self):
        out = answer("what does notes.md say", self.svc)
        self.assertEqual(out["mode"], "answer")
        self.assertIn("don't know", out["text"].lower())
        self.assertIn("theory.md", out["text"])  # lists what CAN be hosted

    # -- identity via soul.md ----------------------------------------------
    def test_identity_no_soul_then_hosted(self):
        base = answer("who are you", self.svc)
        self.assertIn("don't know", base["text"].lower())
        self.assertFalse(base["grounded"]["exists"])

        self.svc["evidence"].save_document(
            "soul.md", "I am the REMOR backend chat handler.")
        hosted = answer("who are you", self.svc)
        self.assertIn("REMOR backend chat handler", hosted["text"])
        self.assertTrue(hosted["grounded"]["exists"])

    # -- runs: causal history ------------------------------------------------
    def test_runs_empty_no_invention(self):
        out = answer("what have you been doing", self.svc)
        self.assertEqual(out["mode"], "answer")
        self.assertIn("don't know", out["text"].lower())
        self.assertEqual(out["grounded"]["count"], 0)

    def test_runs_causal_real_history(self):
        sched = self.svc["scheduler"]
        r1 = sched.submit(QUICK_GOAL)
        r2 = sched.submit("knowledge_stats second probe")
        _wait_terminal(sched, r1["run_id"])
        _wait_terminal(sched, r2["run_id"])

        out = answer("what have you been doing", self.svc)
        self.assertEqual(out["mode"], "answer")
        self.assertIn(r1["run_id"][:8], out["text"])
        self.assertIn(r2["run_id"][:8], out["text"])
        self.assertIn(QUICK_GOAL, out["text"])
        self.assertGreaterEqual(out["grounded"]["count"], 2)

    # -- (b) must not claim a task completed --------------------------------
    def test_unknown_run_id_no_completion_claim(self):
        out = answer("what is the status of run deadbeef1234nope", self.svc)
        self.assertEqual(out["mode"], "answer")
        self.assertEqual(out["kind"], "run_status")
        self.assertIn("no record", out["text"].lower())
        self.assertFalse(out["grounded"]["found"])
        # No completion claim anywhere: the word "complete" must not appear
        # as a status assertion.
        self.assertIsNone(
            re.search(r"status ['\"]?complete", out["text"], re.IGNORECASE))

    def test_real_run_reports_real_facts(self):
        """Paired control for (b): a real run's real facts are reported."""
        sched = self.svc["scheduler"]
        r = sched.submit(QUICK_GOAL)
        rec = _wait_terminal(sched, r["run_id"])
        out = answer(f"did run {r['run_id']} complete", self.svc)
        self.assertTrue(out["grounded"]["found"])
        self.assertIn(r["run_id"], out["text"])
        self.assertIn(QUICK_GOAL, out["text"])
        self.assertIn(rec["status"], out["text"])

    # -- (c) must not claim actions it didn't take ---------------------------
    def test_no_action_claims(self):
        out = answer("did you delete my file", self.svc)
        self.assertEqual(out["mode"], "answer")
        self.assertTrue(out["text"].startswith("No."))
        self.assertIn("no actions at all", out["text"])
        # The handler must never claim it performed an action itself.
        for verb in ("I deleted", "I have deleted", "I removed",
                     "I modified", "I created", "I wrote"):
            self.assertNotIn(verb, out["text"])

    def test_answer_performs_no_writes(self):
        """Paired control for (c): the logical content (schema + rows) of
        every readable DB is identical before and after a full battery of
        answer() calls -> read-only. (Raw file bytes are NOT compared:
        sqlite's WAL checkpointing can move bytes between the -wal file and
        the main file on connection close even for pure reads; the data
        comparison below is the genuine no-mutation proof.)"""
        dbs = [os.path.join(self.tmp, n)
               for n in ("capabilities.db", "evidence.db", "scheduler.db")]
        before = {p: _db_content(p) for p in dbs}
        for msg in ("what can you do", "what have you been doing",
                    "who are you", "what does theory.md say",
                    "what is my tier", "what have you learned",
                    "what is the status of run abc123", "hey",
                    "what is the capital of France",
                    "did you delete my file", "what is my name"):
            answer(msg, self.svc)
            answer(msg, self.svc, ambiguous=True)
        after = {p: _db_content(p) for p in dbs}
        self.assertEqual(before, after)

    # -- (d) must not answer from nowhere ------------------------------------
    def test_name_unknown(self):
        out = answer("what is my name", self.svc)
        self.assertEqual(out["mode"], "answer")
        self.assertIn("don't know", out["text"].lower())
        # No name is invented.
        self.assertNotIn("James", out["text"])

    # -- status: causal metering ----------------------------------------------
    def test_status_reflects_real_metering(self):
        met = self.svc["metering"]
        before = met.tasks_today()["tasks_today"]
        out = answer("what is my tier and how many tasks today", self.svc)
        self.assertEqual(out["kind"], "status")
        self.assertIn("free", out["text"])
        self.assertIn(str(before), out["text"])
        self.assertEqual(out["grounded"]["tasks_today"], before)

        # CAUSAL: a real guarded submission moves the real count, and the
        # next answer reflects the new number.
        gate = met.guarded_submit(QUICK_GOAL,
                                  metadata={"kind": "chat_probe"})
        self.assertTrue(gate["ok"], gate)
        _wait_terminal(self.svc["scheduler"], gate["run_id"])
        after = met.tasks_today()["tasks_today"]
        self.assertEqual(after, before + 1)
        out2 = answer("tasks today", self.svc)
        self.assertIn(str(after), out2["text"])
        self.assertEqual(out2["grounded"]["tasks_today"], after)

    # -- evidence --------------------------------------------------------------
    def test_evidence_empty_then_recorded(self):
        base = answer("what have you learned", self.svc)
        self.assertIn("don't know", base["text"].lower())
        self.svc["evidence"].add_entry(
            "observation", "the probe observed X", source="test")
        out = answer("what hypotheses do you have", self.svc)
        self.assertIn("observations 1", out["text"])
        self.assertIn("the probe observed X", out["text"])

    # -- ambiguity: clarifying question, never executing ----------------------
    def test_ambiguous_returns_clarifying_question(self):
        runs_before = self.svc["scheduler"].list_runs(limit=50)
        out = answer("bank", self.svc, ambiguous=True)
        self.assertEqual(out["mode"], "clarify")
        self.assertIn("?", out["text"])
        self.assertIsNone(out["grounded"]["store"])
        # Nothing executed: no runs appeared, no store reads happened.
        self.assertEqual(self.svc["scheduler"].list_runs(limit=50),
                         runs_before)

    def test_ambiguous_restates_readings(self):
        readings = ["your account balance", "a river bank"]
        out = answer("bank", self.svc, ambiguous=True, readings=readings)
        self.assertEqual(out["mode"], "clarify")
        for r in readings:
            self.assertIn(r, out["text"])
        self.assertIn("?", out["text"])
        # Never guesses: the question defers to the user.
        self.assertIn("clarify", out["text"].lower())

    # -- chit-chat: minimal, never invents facts --------------------------------
    def test_chitchat_no_invented_facts(self):
        for msg in ("hey", "thanks", "how's it going", "good morning"):
            out = answer(msg, self.svc)
            self.assertEqual(out["mode"], "acknowledge", msg)
            self.assertIsNone(out["grounded"]["store"])
            for invented in ("I'm doing well", "I'm feeling", "great,",
                             "I just", "I deleted", "I created"):
                self.assertNotIn(invented, out["text"], msg)

    # -- input hygiene -----------------------------------------------------------
    def test_empty_and_nontext_refused(self):
        for bad in ("", "   ", None, 123):
            out = answer(bad, self.svc)
            self.assertEqual(out["mode"], "refuse", repr(bad))

    # -- fallback: honest refusal, no confabulation -------------------------------
    def test_fallback_no_confabulation(self):
        out = answer("what is the capital of France", self.svc)
        self.assertEqual(out["mode"], "answer")
        self.assertEqual(out["kind"], "unknown")
        self.assertIn("don't know", out["text"].lower())
        self.assertNotIn("Paris", out["text"])
        self.assertIsNone(out["grounded"])

    # -- determinism (no language substrate => same input, same output) ------------
    def test_deterministic(self):
        self.svc["evidence"].save_document("theory.md", "stable fact")
        for msg in ("what can you do", "what does theory.md say",
                    "what is my tier", "hey", "what is my name"):
            self.assertEqual(answer(msg, self.svc), answer(msg, self.svc))

    # -- composed service graph (operational end-to-end) ---------------------------
    def test_composed_graph_end_to_end(self):
        """answer() works through the real build_services() graph: the same
        services dict Worker 1's wiring will pass. HTTP-layer wiring is
        Worker 1's (no /api/chat route exists in this tree yet)."""
        for msg, kind in (("what can you do", "capabilities"),
                          ("what have you been doing", "runs"),
                          ("who are you", "identity"),
                          ("what is my tier", "status"),
                          ("what have you learned", "evidence")):
            out = answer(msg, self.svc)
            self.assertEqual(out["mode"], "answer", msg)
            self.assertEqual(out["kind"], kind, msg)
            self.assertIsNotNone(out["grounded"], msg)
            self.assertIsNotNone(out["grounded"]["store"], msg)

    def test_missing_services_degrade_honestly(self):
        out = answer("what can you do", {})
        self.assertIn("don't know", out["text"].lower())
        out = answer("what have you been doing", {})
        self.assertIn("don't know", out["text"].lower())


if __name__ == "__main__":
    unittest.main()
