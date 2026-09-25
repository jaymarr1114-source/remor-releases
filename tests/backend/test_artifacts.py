"""Causal tests for swarm_engine.services.artifacts.ArtifactStore.

Real sqlite, real subprocess sandbox, real timeout kills, real tamper.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.artifacts import ArtifactStore  # noqa: E402


def _store():
    td = tempfile.TemporaryDirectory()
    db = os.path.join(td.name, "artifacts.db")
    sandbox = os.path.join(td.name, "sandbox")
    return td, ArtifactStore(db, sandbox)


class TestRevisions(unittest.TestCase):
    def test_save_get_roundtrip(self):
        td, store = _store()
        try:
            s = store.save("hello", "python", 'print("v1")')
            self.assertTrue(s["ok"])
            self.assertEqual(s["revision"], 1)
            g = store.get(s["artifact_id"])
            self.assertTrue(g["ok"])
            self.assertEqual(g["code"], 'print("v1")')
            self.assertEqual(g["revision"], 1)
            self.assertEqual(g["name"], "hello")
        finally:
            td.cleanup()

    def test_same_name_new_revision_history_preserved(self):
        td, store = _store()
        try:
            s1 = store.save("hello", "python", 'print("v1")')
            s2 = store.save("hello", "python", 'print("v2")')
            self.assertTrue(s2["ok"])
            self.assertEqual(s2["revision"], 2)
            self.assertEqual(s2["artifact_id"], s1["artifact_id"])
            latest = store.get(s1["artifact_id"])
            self.assertEqual(latest["revision"], 2)
            self.assertEqual(latest["code"], 'print("v2")')
            old = store.get(s1["artifact_id"], revision=1)
            self.assertTrue(old["ok"])
            self.assertEqual(old["code"], 'print("v1")')
        finally:
            td.cleanup()

    def test_list_and_revisions(self):
        td, store = _store()
        try:
            s = store.save("a", "python", "x=1")
            store.save("a", "python", "x=2")
            store.save("b", "python", "y=1", metadata={"k": "v"})
            lst = store.list_artifacts()
            self.assertEqual(len(lst), 2)
            by_name = {e["name"]: e for e in lst}
            self.assertEqual(by_name["a"]["latest_revision"], 2)
            self.assertEqual(by_name["b"]["artifact_id"], s["artifact_id"] + 2)
            revs = store.revisions(s["artifact_id"])
            self.assertTrue(revs["ok"])
            self.assertEqual([r["revision"] for r in revs["revisions"]], [1, 2])
        finally:
            td.cleanup()

    def test_delete_removes_all(self):
        td, store = _store()
        try:
            s = store.save("doomed", "python", "x=1")
            store.save("doomed", "python", "x=2")
            d = store.delete(s["artifact_id"])
            self.assertTrue(d["ok"])
            g = store.get(s["artifact_id"])
            self.assertFalse(g["ok"])
            self.assertEqual(g["error"], "not found")
            self.assertEqual(store.list_artifacts(), [])
        finally:
            td.cleanup()

    def test_get_missing(self):
        td, store = _store()
        try:
            g = store.get(999999)
            self.assertFalse(g["ok"])
            self.assertEqual(g["error"], "not found")
        finally:
            td.cleanup()

    def test_empty_code_refused(self):
        td, store = _store()
        try:
            s = store.save("empty", "python", "   \n")
            self.assertFalse(s["ok"])
            self.assertIn("empty code", s["error"])
        finally:
            td.cleanup()


class TestExecution(unittest.TestCase):
    def test_run_executes_real_python(self):
        td, store = _store()
        try:
            code = "result = sum(i*i for i in range(100))\nprint('ANSWER:', result)"
            s = store.save("calc", "python", code)
            r = store.run(s["artifact_id"])
            self.assertTrue(r["ok"], r)
            self.assertFalse(r["timed_out"])
            self.assertEqual(r["exit_code"], 0)
            self.assertIn("ANSWER: 328350", r["stdout"])
        finally:
            td.cleanup()

    def test_edit_then_rerun_reflects_new_revision(self):
        td, store = _store()
        try:
            s = store.save("calc", "python", "print('ANSWER: old')")
            r1 = store.run(s["artifact_id"])
            self.assertIn("old", r1["stdout"])
            store.save("calc", "python", "print('ANSWER: new')")
            r2 = store.run(s["artifact_id"])
            self.assertIn("new", r2["stdout"])
            self.assertNotIn("old", r2["stdout"])
            # pinning the old revision still runs old code
            r3 = store.run(s["artifact_id"], revision=1)
            self.assertIn("old", r3["stdout"])
        finally:
            td.cleanup()

    def test_exit_code_and_stderr(self):
        td, store = _store()
        try:
            s = store.save("fail", "python",
                           "import sys\nprint('oops', file=sys.stderr)\nsys.exit(3)")
            r = store.run(s["artifact_id"])
            self.assertTrue(r["ok"])
            self.assertEqual(r["exit_code"], 3)
            self.assertIn("oops", r["stderr"])
        finally:
            td.cleanup()

    def test_output_capped(self):
        td, store = _store()
        try:
            s = store.save("noisy", "python", "print('y' * 10000)")
            r = store.run(s["artifact_id"])
            self.assertTrue(r["ok"])
            self.assertLessEqual(len(r["stdout"]), 4000)
        finally:
            td.cleanup()

    def test_timeout_kills_no_orphan(self):
        td, store = _store()
        try:
            marker = f"sleepmarker_{os.getpid()}"
            s = store.save("sleeper", "python",
                           f"import time\ntime.sleep(60)  # {marker}")
            t0 = time.time()
            r = store.run(s["artifact_id"], timeout=2)
            elapsed = time.time() - t0
            self.assertTrue(r["ok"])
            self.assertTrue(r["timed_out"], r)
            self.assertIsNone(r["exit_code"])
            self.assertLess(elapsed, 20, "timeout took too long")
            # no orphan left running: poll the process table
            time.sleep(0.5)
            ps = subprocess.run(
                ["ps", "-eo", "args"], capture_output=True, text=True)
            self.assertNotIn(marker, ps.stdout,
                             "orphan python process still running after timeout")
        finally:
            td.cleanup()

    def test_non_python_refused(self):
        td, store = _store()
        try:
            s = store.save("js", "javascript", "console.log('hi')")
            self.assertTrue(s["ok"])
            r = store.run(s["artifact_id"])
            self.assertFalse(r["ok"])
            self.assertEqual(
                r["error"], "execution for javascript is not supported")
        finally:
            td.cleanup()

    def test_run_missing_artifact(self):
        td, store = _store()
        try:
            r = store.run(424242)
            self.assertFalse(r["ok"])
            self.assertEqual(r["error"], "not found")
        finally:
            td.cleanup()

    def test_run_cwd_is_sandbox(self):
        td, store = _store()
        try:
            s = store.save("cwdprobe", "python",
                           "import os\nprint('CWD-IS:', os.getcwd())")
            r = store.run(s["artifact_id"])
            self.assertTrue(r["ok"])
            self.assertIn("CWD-IS: " + os.path.realpath(
                os.path.join(td.name, "sandbox")), r["stdout"])
        finally:
            td.cleanup()


class TestTamperHonesty(unittest.TestCase):
    def test_tampered_blob_returned_verbatim_no_integrity_illusion(self):
        """Flip a code blob directly in sqlite. get() must return exactly
        the tampered bytes -- the store never pretends an unread blob is
        intact. Classification as of 2026-09-25 (Batch 11 cross-DB trust
        scope): DETECTED -- the chained rows make the tamper visible to
        audit()/verify, which MUST fail. get() stays a verbatim read;
        the detection surface is the chain + the anchor journal, mirroring
        the org store (latest() reads rows; audit() verifies)."""
        td, store = _store()
        try:
            s = store.save("victim", "python", "print('original')")
            db = os.path.join(td.name, "artifacts.db")
            conn = sqlite3.connect(db)
            conn.execute(
                "UPDATE artifacts SET code = ? WHERE artifact_key IN"
                " (SELECT artifact_key FROM artifacts WHERE id = ?)",
                ("print('TAMPERED-BY-DB-WRITE')", s["artifact_id"]),
            )
            conn.commit()
            conn.close()
            g = store.get(s["artifact_id"])
            self.assertTrue(g["ok"])
            self.assertEqual(g["code"], "print('TAMPERED-BY-DB-WRITE')",
                             "store must not pretend the blob is intact")
            ok, msg = store.audit()
            self.assertFalse(
                ok,
                f"chain audit must detect the out-of-band blob flip: {msg}")
            self.assertIn("row_digest mismatch", msg)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
