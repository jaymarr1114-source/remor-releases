"""QUEUED-1 battery: atomic multi-file creation (2026-09-25).

Causally demonstrates that ArtifactStore.save_many() creates N files
as one atomic job: all files created with per-file provenance, or --
on forced mid-job failure -- zero rows and zero files on disk.

Anti-simulation notes:
- The kill test uses a REAL subprocess + SIGKILL (no cleanup code can
  run); the parent verifies by inspection.
- The injected-failure test raises a REAL exception in the REAL write
  path (fault injection, not a mock).
- Recovery tests craft real on-disk/DB states and open a FRESH store.
- No hardcoded digests; all heads/audits recomputed live.
- No absolute paths hardcoded: pylib is derived from __file__ and
  passed to the child via env.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.artifacts import ArtifactStore  # noqa: E402
from swarm_engine.governance.anchor import (  # noqa: E402
    AnchorVerifyError,
    CrossDbAnchor,
)

PYLIB = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib"))

_KILL_DRIVER = r'''
import os, sys, time
sys.path.insert(0, os.environ["REMOR_PYLIB"])
from swarm_engine.services.artifacts import ArtifactStore
db_path, sandbox, prefix, delay = (sys.argv[1], sys.argv[2],
                                   sys.argv[3], float(sys.argv[4]))
store = ArtifactStore(db_path, sandbox)
files = [{"name": f"{prefix}{i}.txt", "language": "text",
          "code": f"payload-{i}-" + "x" * 100}
         for i in range(10)]
store.save_many(files, job={"producer": "kill-driver"},
                _file_delay=delay)
'''


def _store():
    td = tempfile.TemporaryDirectory()
    db = os.path.join(td.name, "artifacts.db")
    sandbox = os.path.join(td.name, "sandbox")
    return td, ArtifactStore(db, sandbox)


def _row_count(store):
    with store._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM artifacts").fetchone()[0]


def _six_files(prefix="f"):
    return [
        {"name": f"{prefix}_main.py", "language": "python",
         "code": 'print("job-output")\n'},
        {"name": f"{prefix}_notes.txt", "language": "text",
         "code": "hello world\n"},
        {"name": f"{prefix}_doc.md", "language": "markdown",
         "code": "# Title\n\nbody\n"},
        {"name": f"{prefix}_data.json", "language": "json",
         "code": '{"a": 1, "b": [1, 2]}\n'},
        {"name": f"{prefix}_data.csv", "language": "csv",
         "code": "a,b\n1,2\n"},
        {"name": f"{prefix}_util.py", "language": "python",
         "code": "VALUE = 42\n"},
    ]


class TestHappyPath(unittest.TestCase):
    def test_six_mixed_types_byte_exact(self):
        td, store = _store()
        try:
            files = _six_files()
            r = store.save_many(
                files, job={"producer": "battery", "purpose": "t1"})
            self.assertTrue(r["ok"], r)
            self.assertEqual(len(r["artifacts"]), 6)
            job_id = r["job_id"]
            # Files materialized on disk, byte-exact.
            jobdir = os.path.join(td.name, "sandbox", "jobs", job_id)
            self.assertTrue(os.path.isdir(jobdir))
            for f in files:
                with open(os.path.join(jobdir, f["name"]),
                          encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), f["code"])
            # Chain intact.
            ok, msg = store.audit()
            self.assertTrue(ok, msg)
            # Fresh-process re-read: new store instance, same DB.
            store2 = ArtifactStore(
                os.path.join(td.name, "artifacts.db"),
                os.path.join(td.name, "sandbox"))
            for art, f in zip(r["artifacts"], files):
                g = store2.get(art["artifact_id"])
                self.assertTrue(g["ok"], g)
                self.assertEqual(g["code"], f["code"])
                self.assertEqual(g["metadata"]["job"]["job_id"], job_id)
            # Operational: the python file really runs.
            run = store2.run(r["artifacts"][0]["artifact_id"])
            self.assertTrue(run["ok"], run)
            self.assertIn("job-output", run["stdout"])
        finally:
            td.cleanup()

    def test_provenance_query(self):
        td, store = _store()
        try:
            files = _six_files()
            r = store.save_many(files, job={"producer": "prov-agent"})
            self.assertTrue(r["ok"], r)
            job_id = r["job_id"]
            listed = store.artifacts_for_job(job_id)
            self.assertEqual(len(listed), 6)
            for i, (art, f) in enumerate(zip(r["artifacts"], files)):
                g = store.get(art["artifact_id"])
                job = g["metadata"]["job"]
                self.assertEqual(job["job_id"], job_id)
                self.assertEqual(job["job_seq"], i)
                self.assertEqual(job["producer"], "prov-agent")
                self.assertIn("job_created_at", job)
            seqs = sorted(e["job_seq"] for e in listed)
            self.assertEqual(seqs, [0, 1, 2, 3, 4, 5])
            names = sorted(e["name"] for e in listed)
            self.assertEqual(names, sorted(f["name"] for f in files))
            # Unknown job -> empty, not an error.
            self.assertEqual(store.artifacts_for_job("nope"), [])
        finally:
            td.cleanup()

    def test_revision_history_intact(self):
        td, store = _store()
        try:
            s1 = store.save("ev.py", "python", 'print("v1")')
            self.assertTrue(s1["ok"], s1)
            r = store.save_many(
                [{"name": "ev.py", "language": "python",
                  "code": 'print("v2")'},
                 {"name": "new.py", "language": "python",
                  "code": 'print("new")'}])
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["artifacts"][0]["revision"], 2)
            self.assertEqual(r["artifacts"][1]["revision"], 1)
            revs = store.revisions(s1["artifact_id"])
            self.assertTrue(revs["ok"], revs)
            codes = [x["code"] for x in revs["revisions"]]
            self.assertEqual(codes, ['print("v1")', 'print("v2")'])
            ok, msg = store.audit()
            self.assertTrue(ok, msg)
        finally:
            td.cleanup()


class TestFailFast(unittest.TestCase):
    def test_empty_job_refused(self):
        td, store = _store()
        try:
            r = store.save_many([])
            self.assertFalse(r["ok"])
            self.assertEqual(_row_count(store), 0)
        finally:
            td.cleanup()

    def test_validation_failure_writes_nothing(self):
        td, store = _store()
        try:
            before = _row_count(store)
            r = store.save_many([
                {"name": "good.py", "language": "python",
                 "code": "x = 1"},
                {"name": "bad.py", "language": "python",
                 "code": "   "},  # empty code -> invalid
            ])
            self.assertFalse(r["ok"], r)
            self.assertIn("empty code", r["error"])
            self.assertEqual(_row_count(store), before)
            sandbox = os.path.join(td.name, "sandbox")
            staging = os.path.join(sandbox, ".staging")
            jobs = os.path.join(sandbox, "jobs")
            self.assertFalse(os.path.exists(staging))
            self.assertFalse(os.path.exists(jobs))
            ok, msg = store.audit()
            self.assertTrue(ok, msg)
        finally:
            td.cleanup()

    def test_unsafe_names_refused(self):
        td, store = _store()
        try:
            bad_names = ["../../evil", "/abs/path", "a/b", "..", "",
                         "CON", "nul.txt", "a\0b",
                         "x" * 256]
            for name in bad_names:
                r = store.save_many([{"name": name, "code": "x"}])
                self.assertFalse(r["ok"],
                                 f"name {name!r} was not refused: {r}")
            # Duplicate names inside one job refused.
            r = store.save_many([
                {"name": "dup.py", "code": "a"},
                {"name": "dup.py", "code": "b"},
            ])
            self.assertFalse(r["ok"], r)
            self.assertIn("duplicate", r["error"])
            self.assertEqual(_row_count(store), 0)
            ok, msg = store.audit()
            self.assertTrue(ok, msg)
        finally:
            td.cleanup()


class TestRollback(unittest.TestCase):
    def test_injected_failure_rolls_back(self):
        td, store = _store()
        try:
            files = _six_files()
            before = _row_count(store)
            with self.assertRaises(RuntimeError) as ctx:
                store.save_many(files, _fail_after=3)
            self.assertIn("injected failure", str(ctx.exception))
            # Zero rows: the 3 appended rows were uncommitted.
            self.assertEqual(_row_count(store), before)
            names = [f["name"] for f in files]
            with store._connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE name IN "
                    f"({','.join('?' * len(names))})",
                    names).fetchone()[0]
            self.assertEqual(n, 0)
            # Zero files on disk: staging removed, no job published.
            sandbox = os.path.join(td.name, "sandbox")
            staging = os.path.join(sandbox, ".staging")
            self.assertEqual(os.listdir(staging), [])
            jobs = os.path.join(sandbox, "jobs")
            self.assertFalse(os.path.exists(jobs))
            ok, msg = store.audit()
            self.assertTrue(ok, msg)
        finally:
            td.cleanup()

    def test_kill_minus9_mid_job(self):
        td = tempfile.TemporaryDirectory()
        try:
            db = os.path.join(td.name, "artifacts.db")
            sandbox = os.path.join(td.name, "sandbox")
            prefix = "k" + uuid.uuid4().hex[:8]
            driver = os.path.join(td.name, "driver.py")
            with open(driver, "w", encoding="utf-8") as fh:
                fh.write(_KILL_DRIVER)
            env = dict(os.environ, REMOR_PYLIB=PYLIB)
            proc = subprocess.Popen(
                [sys.executable, driver, db, sandbox, prefix, "0.3"],
                env=env)
            try:
                # Wait until 4 files are staged, then SIGKILL: no
                # cleanup code can run in the child.
                deadline = time.time() + 30
                staged = []
                while time.time() < deadline:
                    root = os.path.join(sandbox, ".staging")
                    if os.path.isdir(root):
                        for jid in os.listdir(root):
                            d = os.path.join(root, jid)
                            if os.path.isdir(d):
                                staged = os.listdir(d)
                    if len(staged) >= 4:
                        break
                    time.sleep(0.05)
                self.assertGreaterEqual(
                    len(staged), 4,
                    "child never reached 4 staged files")
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            # Fresh open: must not raise (journal rolled back by
            # sqlite itself), zero rows for the job's names.
            store = ArtifactStore(db, sandbox)
            with store._connect() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) FROM artifacts WHERE name LIKE ?",
                    (prefix + "%",)).fetchone()[0]
            self.assertEqual(n, 0)
            ok, msg = store.audit()
            self.assertTrue(ok, msg)
            # No job was published: jobs/ is absent or empty
            # (recovery makedirs it on open -- that is expected).
            jobs = os.path.join(sandbox, "jobs")
            if os.path.exists(jobs):
                self.assertEqual(os.listdir(jobs), [])
        finally:
            td.cleanup()

    def test_recovery_cleans_orphan_staging(self):
        td, store = _store()
        try:
            # Craft the post-kill state: staging dir, no DB rows.
            staging_root = os.path.join(td.name, "sandbox", ".staging")
            orphan = os.path.join(staging_root, "orphan-job")
            os.makedirs(orphan)
            with open(os.path.join(orphan, "x.txt"), "w") as fh:
                fh.write("x")
            # Age it past the grace period (fast-forward, no waiting).
            old = time.time() - 3600
            os.utime(orphan, (old, old))
            # Fresh open triggers recovery.
            store2 = ArtifactStore(
                os.path.join(td.name, "artifacts.db"),
                os.path.join(td.name, "sandbox"))
            self.assertFalse(os.path.exists(orphan))
            ok, msg = store2.audit()
            self.assertTrue(ok, msg)
        finally:
            td.cleanup()

    def test_recovery_completes_committed_job(self):
        td, store = _store()
        try:
            # Craft the crash-between-commit-and-rename state using
            # the REAL append path: committed rows + staging dir.
            job_id = "committed-job"
            conn = store._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for i, nm in enumerate(["c1.txt", "c2.txt"]):
                    meta = {"job": {"job_id": job_id, "job_seq": i,
                                    "job_created_at": 1.0,
                                    "producer": None, "purpose": None}}
                    store._append_row(conn, {
                        "artifact_key": uuid.uuid4().hex, "name": nm,
                        "language": "text", "rev": 1,
                        "code": f"content-{i}",
                        "metadata_json": json.dumps(meta),
                        "created_at": 1.0})
                conn.commit()
            finally:
                conn.close()
            staging = os.path.join(td.name, "sandbox", ".staging",
                                   job_id)
            os.makedirs(staging)
            for i in range(2):
                with open(os.path.join(staging, f"c{i + 1}.txt"),
                          "w") as fh:
                    fh.write(f"content-{i}")
            # Fresh open: recovery must publish the staged files.
            store2 = ArtifactStore(
                os.path.join(td.name, "artifacts.db"),
                os.path.join(td.name, "sandbox"))
            jobdir = os.path.join(td.name, "sandbox", "jobs", job_id)
            self.assertTrue(os.path.isdir(jobdir))
            for i in range(2):
                with open(os.path.join(jobdir, f"c{i + 1}.txt")) as fh:
                    self.assertEqual(fh.read(), f"content-{i}")
            self.assertFalse(os.path.exists(staging))
            listed = store2.artifacts_for_job(job_id)
            self.assertEqual(len(listed), 2)
            ok, msg = store2.audit()
            self.assertTrue(ok, msg)
        finally:
            td.cleanup()


class TestConcurrency(unittest.TestCase):
    def test_two_concurrent_jobs_no_interleaving(self):
        td, store = _store()
        try:
            results = {}
            errors = []

            def run_job(tag):
                try:
                    files = _six_files(prefix=tag)
                    results[tag] = store.save_many(
                        files, job={"producer": tag})
                except Exception as e:  # noqa: BLE001
                    errors.append(e)

            t1 = threading.Thread(target=run_job, args=("t1",))
            t2 = threading.Thread(target=run_job, args=("t2",))
            t1.start()
            t2.start()
            t1.join(timeout=60)
            t2.join(timeout=60)
            self.assertFalse(t1.is_alive())
            self.assertFalse(t2.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(results["t1"]["ok"], results["t1"])
            self.assertTrue(results["t2"]["ok"], results["t2"])
            j1, j2 = (results["t1"]["job_id"], results["t2"]["job_id"])
            self.assertNotEqual(j1, j2)
            # All 12 files byte-exact, correct job attribution.
            self.assertEqual(_row_count(store), 12)
            for tag, job_id in (("t1", j1), ("t2", j2)):
                for f in _six_files(prefix=tag):
                    p = os.path.join(td.name, "sandbox", "jobs",
                                     job_id, f["name"])
                    with open(p, encoding="utf-8") as fh:
                        self.assertEqual(fh.read(), f["code"])
                self.assertEqual(
                    len(store.artifacts_for_job(job_id)), 6)
            ok, msg = store.audit()
            self.assertTrue(ok, msg)
        finally:
            td.cleanup()


class TestAnchorDiscipline(unittest.TestCase):
    """save_many() must ride the Batch 11 anchor machinery, not bypass it."""

    def _anchored(self):
        td = tempfile.TemporaryDirectory()
        # Layout mirrors test_cross_db_trust: the journal/key must live
        # OUTSIDE the parent dir of every protected DB.
        db_dir = os.path.join(td.name, "dbs")
        os.makedirs(db_dir)
        art_db = os.path.join(db_dir, "artifacts.db")
        sandbox = os.path.join(td.name, "sandbox")
        anchor_dir = os.path.join(td.name, "anchor_store")
        os.makedirs(anchor_dir)
        journal = os.path.join(anchor_dir, "a.journal")
        key_path = os.path.join(anchor_dir, "a.key")
        store = ArtifactStore(art_db, sandbox)
        anchor = CrossDbAnchor(journal, key_path,
                               [("artifact", art_db)])
        anchor.bind([("artifact", store)])
        store.attach_anchor(anchor, authority="battery")
        anchor.initialize("battery-operator")
        return td, store, anchor

    def test_anchored_happy_path(self):
        td, store, anchor = self._anchored()
        try:
            r = store.save_many(_six_files(),
                                job={"producer": "anchored"})
            self.assertTrue(r["ok"], r)
            self.assertTrue(anchor.verify_all()[0])
            ok, msg = store.audit()
            self.assertTrue(ok, msg)
        finally:
            td.cleanup()

    def test_failed_job_never_attests(self):
        td, store, anchor = self._anchored()
        try:
            head_before = store.head_digest()
            with self.assertRaises(RuntimeError):
                store.save_many(_six_files(), _fail_after=2)
            # Journal still consistent with the rolled-back live DB:
            # the failed job attested nothing.
            self.assertTrue(anchor.verify_all()[0])
            self.assertEqual(store.head_digest(), head_before)
            ok, msg = store.audit()
            self.assertTrue(ok, msg)
        finally:
            td.cleanup()

    def test_tamper_fail_closed(self):
        td, store, anchor = self._anchored()
        try:
            r = store.save_many(_six_files())
            self.assertTrue(r["ok"], r)
            # Out-of-band tamper through a separate handle.
            raw = sqlite3.connect(
                os.path.join(td.name, "dbs", "artifacts.db"))
            try:
                raw.execute(
                    "UPDATE artifacts SET code = 'evil' WHERE id = 1")
                raw.commit()
            finally:
                raw.close()
            before = _row_count(store)
            with self.assertRaises(AnchorVerifyError):
                store.save_many(_six_files(prefix="after"))
            # Fail closed: no new rows from the refused job.
            self.assertEqual(_row_count(store), before)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
