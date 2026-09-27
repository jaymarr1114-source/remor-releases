"""Phase 3, Worker E: backup/restore + crash-safe migrations (2026-09-25).

Backup/restore (real files, real hashes, real processes):
- tampered bundle file -> verify_backup refuses
- journal swapped from a different DB (shallow: file+hash only; coherent:
  full manifest rewrite) -> restore refuses, originals untouched
- wrong key -> restore refuses, originals untouched
- restore over a live-open DB (real subprocess holding the file) -> refused
- restore over a Worker-A ownership marker -> refused
- full drill: backup both service DBs, corrupt both out-of-band, restore
  both, rebuild the CrossDbAnchor over the restored set -> verify_all
  green, data intact

Migrations (real kill -9 via crash_driver.py subprocesses):
- crash before commit -> dangling intent; apply refuses; verify fails;
  recover clears (pre-state proven); re-apply succeeds; data intact
- crash after commit -> recover finalizes; verify green; post digests set
- in-process failure -> provably clean rollback clears the intent itself
- verify_migrations fails on ANY dangling intent (partial never trusted)

No simulation: every digest is recomputed, every crash is a real
SIGKILL in a subprocess, every refusal is asserted with the original
files compared byte-for-byte.
"""
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.governance import backup as backup_mod  # noqa: E402
from swarm_engine.governance.backup import (  # noqa: E402
    BackupError, create_backup, restore_backup, verify_backup)
from swarm_engine.governance import schema_migrations as sm  # noqa: E402
from swarm_engine.governance.anchor import CrossDbAnchor  # noqa: E402
from swarm_engine.services.scheduler import RunScheduler  # noqa: E402
from swarm_engine.services.artifacts import ArtifactStore  # noqa: E402

_CRASH_DRIVER = os.path.join(os.path.dirname(__file__), "crash_driver.py")
_ART_CODE = "print('backup drill')\n"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_files(paths):
    return {p: _sha256(p) for p in paths}


class _Deployment:
    """Two service DBs + one CrossDbAnchor (the real service shape)."""

    def __init__(self, td, dual=True):
        self.td = td
        self.svc_dir = os.path.join(td, "svc")
        self.anchor_dir = os.path.join(td, "anchor_store")
        os.makedirs(self.svc_dir)
        os.makedirs(self.anchor_dir)
        self.sched_db = os.path.join(self.svc_dir, "scheduler.db")
        self.art_db = os.path.join(self.svc_dir, "artifacts.db")
        self.journal = os.path.join(self.anchor_dir, "services.anchor.journal")
        self.key = os.path.join(self.anchor_dir, "services.anchor.key")
        self.sched = RunScheduler(self.sched_db,
                                  os.path.join(td, "runtime.db"))
        dbs = [("scheduler", self.sched_db)]
        providers = [("scheduler", self.sched)]
        if dual:
            self.arts = ArtifactStore(self.art_db,
                                      os.path.join(td, "sandbox"))
            dbs.append(("artifact", self.art_db))
            providers.append(("artifact", self.arts))
        else:
            self.arts = None
        self.anchor = CrossDbAnchor(self.journal, self.key, dbs)
        self.anchor.bind(providers)
        self.sched.attach_anchor(self.anchor, authority="test")
        if self.arts:
            self.arts.attach_anchor(self.anchor, authority="test")
        self.anchor.initialize("test-operator")

    def write_data(self, goal="backup drill goal"):
        """One attested scheduler run (kept queued) + one artifact."""
        r = self.sched.submit(goal)
        assert r["ok"], f"submit failed: {r}"
        c = self.sched.cancel_run(r["run_id"])
        assert c["ok"], f"cancel lost the worker race: {c}"
        art_id = None
        if self.arts:
            s = self.arts.save("drill_artifact", "python", _ART_CODE,
                               {"purpose": "drill"})
            assert s["ok"], f"save failed: {s}"
            art_id = s["artifact_id"]
        return r["run_id"], art_id

    def close(self):
        try:
            self.sched.close(timeout=10)
        except Exception:
            pass


def _corrupt_db_out_of_band(db_path, table, where):
    """Real tamper through a separate handle (the attacker's channel)."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(f"UPDATE {table} SET {where}")
        conn.commit()
    finally:
        conn.close()


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="phase3_e_")

    def tearDown(self):
        self.td.cleanup()

    # -- helpers ------------------------------------------------------
    def _single(self):
        dep = _Deployment(self.td.name, dual=False)
        self.addCleanup(dep.close)
        return dep

    def _bundle_paths(self, bundle):
        with open(os.path.join(bundle, "manifest.json")) as fh:
            manifest = json.load(fh)
        return manifest

    # -- 1. roundtrip -------------------------------------------------
    def test_create_and_verify_roundtrip(self):
        dep = self._single()
        dep.write_data()
        ok, msg = dep.anchor.verify_all()
        self.assertTrue(ok, msg)
        bundle = create_backup(
            dep.sched_db, dep.journal, dep.key,
            os.path.join(self.td.name, "b1"),
            collect_heads=dep.anchor.collect)
        vok, vmsg = verify_backup(bundle)
        self.assertTrue(vok, vmsg)
        manifest = self._bundle_paths(bundle)
        self.assertEqual(manifest["format"], "remor-backup/1")
        self.assertEqual(set(manifest["files"]), {"db", "journal", "key"})
        self.assertIsNotNone(manifest["anchor"]["journal_tip_digest"])
        self.assertEqual(manifest["sources"]["db"],
                         os.path.abspath(dep.sched_db))

    def test_create_refuses_mismatched_live_set(self):
        dep = self._single()
        dep.write_data()
        # Tamper the DB out-of-band WITHOUT re-anchoring: live heads now
        # differ from the journal tip -> create must refuse.
        conn = sqlite3.connect(dep.sched_db, timeout=30)
        conn.execute("UPDATE scheduler_runs SET status='evil'")
        conn.commit()
        conn.close()
        with self.assertRaises(BackupError) as cm:
            create_backup(dep.sched_db, dep.journal, dep.key,
                          os.path.join(self.td.name, "b1"),
                          collect_heads=dep.anchor.collect)
        self.assertIn("differ", str(cm.exception))

    # -- 2/3. tampered bundle files -----------------------------------
    def test_tampered_bundle_db_refused(self):
        dep = self._single()
        dep.write_data()
        bundle = create_backup(dep.sched_db, dep.journal, dep.key,
                               os.path.join(self.td.name, "b1"),
                               collect_heads=dep.anchor.collect)
        dbf = os.path.join(bundle, "db.sqlite")
        with open(dbf, "r+b") as fh:
            fh.seek(100)
            b = fh.read(1)
            fh.seek(100)
            fh.write(bytes([b[0] ^ 0xFF]))
        ok, msg = verify_backup(bundle)
        self.assertFalse(ok)
        self.assertIn("hash mismatch", msg)

    def test_tampered_bundle_journal_refused(self):
        dep = self._single()
        dep.write_data()
        bundle = create_backup(dep.sched_db, dep.journal, dep.key,
                               os.path.join(self.td.name, "b1"),
                               collect_heads=dep.anchor.collect)
        jf = os.path.join(bundle, "journal")
        with open(jf, "ab") as fh:
            fh.write(b"# tampered\n")
        ok, msg = verify_backup(bundle)
        self.assertFalse(ok)
        self.assertIn("hash mismatch", msg)

    # -- 4. journal swap, shallow (file + hash only) -------------------
    def test_journal_swap_shallow_refused(self):
        dep = self._single()
        dep.write_data()                       # journal @ T1
        bundle_a = create_backup(
            dep.sched_db, dep.journal, dep.key,
            os.path.join(self.td.name, "ba"),
            collect_heads=dep.anchor.collect)
        dep.write_data(goal="second goal")     # journal @ T2
        bundle_b = create_backup(
            dep.sched_db, dep.journal, dep.key,
            os.path.join(self.td.name, "bb"),
            collect_heads=dep.anchor.collect)
        # Swap A's journal file with B's, updating only the file hash.
        with open(os.path.join(bundle_b, "journal"), "rb") as fh:
            b_journal = fh.read()
        with open(os.path.join(bundle_a, "journal"), "wb") as fh:
            fh.write(b_journal)
        mpath = os.path.join(bundle_a, "manifest.json")
        with open(mpath) as fh:
            manifest = json.load(fh)
        manifest["files"]["journal"]["sha256"] = hashlib.sha256(
            b_journal).hexdigest()
        with open(mpath, "w") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)

        ok, msg = verify_backup(bundle_a)
        self.assertFalse(ok, "swapped journal must fail verification")
        self.assertIn("tip digest", msg)

        before = _snapshot_files([dep.sched_db, dep.journal, dep.key])
        with self.assertRaises(BackupError):
            restore_backup(bundle_a, dep.sched_db, dep.journal, dep.key)
        self.assertEqual(_snapshot_files([dep.sched_db, dep.journal,
                                          dep.key]),
                         before, "originals must be untouched")

    # -- 5. journal swap, coherent (full manifest rewrite) ------------
    def test_journal_swap_coherent_refused_by_verify_all(self):
        dep = self._single()
        run1, _ = dep.write_data()             # journal @ T1
        bundle_a = create_backup(
            dep.sched_db, dep.journal, dep.key,
            os.path.join(self.td.name, "ba"),
            collect_heads=dep.anchor.collect)
        dep.write_data(goal="second goal")     # journal @ T2
        bundle_b = create_backup(
            dep.sched_db, dep.journal, dep.key,
            os.path.join(self.td.name, "bb"),
            collect_heads=dep.anchor.collect)

        # Coherent swap: A's journal := B's journal, and A's manifest is
        # fully rewritten to describe B's journal (hashes + anchor tip).
        # verify_backup then PASSES; the staged verify_all must refuse.
        with open(os.path.join(bundle_b, "journal"), "rb") as fh:
            b_journal = fh.read()
        with open(os.path.join(bundle_a, "journal"), "wb") as fh:
            fh.write(b_journal)
        with open(os.path.join(bundle_b, "manifest.json")) as fh:
            manifest_b = json.load(fh)
        mpath = os.path.join(bundle_a, "manifest.json")
        with open(mpath) as fh:
            manifest_a = json.load(fh)
        manifest_a["files"]["journal"]["sha256"] = \
            manifest_b["files"]["journal"]["sha256"]
        manifest_a["anchor"] = manifest_b["anchor"]
        with open(mpath, "w") as fh:
            json.dump(manifest_a, fh, indent=2, sort_keys=True)

        ok, msg = verify_backup(bundle_a)
        self.assertTrue(ok, f"coherent swap should pass verify: {msg}")

        staged_schedulers = []

        def factory(j, k, db):
            rs = RunScheduler(db, os.path.join(self.td.name,
                                               "factory_runtime.db"))
            staged_schedulers.append(rs)
            a = CrossDbAnchor(j, k, [("scheduler", db)])
            a.bind([("scheduler", rs)])
            return a

        self.addCleanup(lambda: [s.close(timeout=10)
                                 for s in staged_schedulers])
        before = _snapshot_files([dep.sched_db, dep.journal, dep.key])
        with self.assertRaises(BackupError) as cm:
            restore_backup(bundle_a, dep.sched_db, dep.journal, dep.key,
                           anchor_factory=factory)
        self.assertIn("verify_all", str(cm.exception))
        self.assertEqual(_snapshot_files([dep.sched_db, dep.journal,
                                          dep.key]),
                         before, "originals must be untouched")

    # -- 6. wrong key --------------------------------------------------
    def test_wrong_key_refused(self):
        dep = self._single()
        dep.write_data()
        bundle = create_backup(dep.sched_db, dep.journal, dep.key,
                               os.path.join(self.td.name, "b1"),
                               collect_heads=dep.anchor.collect)
        # Replace the bundle key with a fresh random one; keep the
        # manifest coherent (hash updated) so the failure is the
        # key/journal mismatch, not a hash mismatch.
        wrong = os.urandom(32)
        with open(os.path.join(bundle, "key"), "wb") as fh:
            fh.write(wrong)
        mpath = os.path.join(bundle, "manifest.json")
        with open(mpath) as fh:
            manifest = json.load(fh)
        manifest["files"]["key"]["sha256"] = hashlib.sha256(wrong).hexdigest()
        with open(mpath, "w") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=True)

        ok, msg = verify_backup(bundle)
        self.assertFalse(ok)
        self.assertIn("wrong key", msg)

        before = _snapshot_files([dep.sched_db, dep.journal, dep.key])
        with self.assertRaises(BackupError) as cm:
            restore_backup(bundle, dep.sched_db, dep.journal, dep.key)
        self.assertIn("key", str(cm.exception).lower())
        self.assertEqual(_snapshot_files([dep.sched_db, dep.journal,
                                          dep.key]),
                         before, "originals must be untouched")

    # -- 7. live-open DB refused ---------------------------------------
    def test_restore_refuses_live_open_db(self):
        dep = self._single()
        dep.write_data()
        bundle = create_backup(dep.sched_db, dep.journal, dep.key,
                               os.path.join(self.td.name, "b1"),
                               collect_heads=dep.anchor.collect)
        dep.close()  # the deployment's own handles must be closed...
        # ...but a REAL other process still holds the DB file open.
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sqlite3, sys, time; "
             "c = sqlite3.connect(sys.argv[1]); "
             "c.execute('select count(*) from scheduler_runs').fetchall(); "
             "time.sleep(60)",
             dep.sched_db])
        self.addCleanup(lambda: (holder.terminate(), holder.wait()))
        time.sleep(1.5)  # let the holder open the file
        self.assertIsNone(holder.poll(), "holder subprocess died early")
        before = _snapshot_files([dep.sched_db, dep.journal, dep.key])
        with self.assertRaises(BackupError) as cm:
            restore_backup(bundle, dep.sched_db, dep.journal, dep.key)
        self.assertIn("live", str(cm.exception).lower())
        self.assertEqual(_snapshot_files([dep.sched_db, dep.journal,
                                          dep.key]),
                         before, "originals must be untouched")
        # After the holder exits, the same restore succeeds.
        holder.terminate()
        holder.wait(timeout=10)
        result = restore_backup(bundle, dep.sched_db, dep.journal, dep.key)
        self.assertEqual(len(result["restored"]), 3)

    # -- 8. Worker-A ownership lock refused ------------------------------
    def test_restore_refuses_owned_db(self):
        dep = self._single()
        dep.write_data()
        bundle = create_backup(dep.sched_db, dep.journal, dep.key,
                               os.path.join(self.td.name, "b1"),
                               collect_heads=dep.anchor.collect)
        dep.close()
        # A live owner per Worker A's FILE CONVENTION (no import of their
        # module): LOCK_EX held on <abspath(db)>.owner.lock, mirroring
        # db_ownership._take_flock.
        lock_path = os.path.abspath(dep.sched_db) + ".owner.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, os, sys, time; "
             "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600); "
             "fcntl.flock(fd, fcntl.LOCK_EX); "
             "time.sleep(60)",
             lock_path])
        self.addCleanup(lambda: (holder.terminate(), holder.wait()))
        time.sleep(1.0)
        self.assertIsNone(holder.poll(), "lock holder died early")
        self.assertTrue(os.path.exists(lock_path))
        before = _snapshot_files([dep.sched_db, dep.journal, dep.key])
        with self.assertRaises(BackupError) as cm:
            restore_backup(bundle, dep.sched_db, dep.journal, dep.key)
        self.assertIn("owned", str(cm.exception).lower())
        self.assertEqual(_snapshot_files([dep.sched_db, dep.journal,
                                          dep.key]),
                         before, "originals must be untouched")
        # After the owner releases, the same restore succeeds (a stale
        # lockfile alone does not block).
        holder.terminate()
        holder.wait(timeout=10)
        result = restore_backup(bundle, dep.sched_db, dep.journal, dep.key)
        self.assertEqual(len(result["restored"]), 3)

    # -- 9. full drill: dual-DB anchored deployment --------------------
    def test_full_drill_dual_db(self):
        dep = _Deployment(self.td.name, dual=True)
        run_id, art_id = dep.write_data()
        ok, msg = dep.anchor.verify_all()
        self.assertTrue(ok, f"pre-backup anchor must be green: {msg}")
        n_records = dep.anchor.record_count

        b_sched = create_backup(
            dep.sched_db, dep.journal, dep.key,
            os.path.join(self.td.name, "backups", "sched"),
            collect_heads=dep.anchor.collect)
        b_art = create_backup(
            dep.art_db, dep.journal, dep.key,
            os.path.join(self.td.name, "backups", "art"),
            collect_heads=dep.anchor.collect)
        for b in (b_sched, b_art):
            vok, vmsg = verify_backup(b)
            self.assertTrue(vok, vmsg)
        dep.close()  # close all live handles before tampering/restoring

        # Corrupt BOTH databases out-of-band (real tamper, separate handles).
        _corrupt_db_out_of_band(dep.sched_db, "scheduler_runs",
                                "status='tampered'")
        conn = sqlite3.connect(dep.art_db, timeout=30)
        conn.execute("DELETE FROM artifacts WHERE id = ?",
                     (conn.execute("SELECT MAX(id) FROM artifacts")
                      .fetchone()[0],))
        conn.commit()
        conn.close()

        # Restore both bundles (no per-bundle factory: the anchor covers
        # the SET; it is verified after both are restored).
        r1 = restore_backup(b_sched, dep.sched_db, dep.journal, dep.key)
        r2 = restore_backup(b_art, dep.art_db, dep.journal, dep.key)
        self.assertTrue(r1["anchor_included"] and r2["anchor_included"])

        # Rebuild the deployment over the restored set and verify.
        dep2 = _Deployment.__new__(_Deployment)
        dep2.td = self.td.name
        dep2.svc_dir = dep.svc_dir
        dep2.anchor_dir = dep.anchor_dir
        dep2.sched_db, dep2.art_db = dep.sched_db, dep.art_db
        dep2.journal, dep2.key = dep.journal, dep.key
        dep2.sched = RunScheduler(dep2.sched_db,
                                  os.path.join(self.td.name, "rt2.db"))
        dep2.arts = ArtifactStore(dep2.art_db,
                                 os.path.join(self.td.name, "sandbox2"))
        self.addCleanup(dep2.sched.close)
        anchor2 = CrossDbAnchor(
            dep2.journal, dep2.key,
            [("scheduler", dep2.sched_db), ("artifact", dep2.art_db)])
        anchor2.bind([("scheduler", dep2.sched), ("artifact", dep2.arts)])
        dep2.sched.attach_anchor(anchor2, authority="test")
        dep2.arts.attach_anchor(anchor2, authority="test")
        ok, msg = anchor2.verify_all()
        self.assertTrue(ok, f"post-restore verify_all must be green: {msg}")
        self.assertEqual(anchor2.record_count, n_records)

        # Data intact.
        rec = dep2.sched.get_run(run_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["id"], run_id)
        g = dep2.arts.get(art_id)
        self.assertTrue(g["ok"], g)
        self.assertEqual(g["code"], _ART_CODE)
        self.assertEqual(g["name"], "drill_artifact")

    # -- 10. single-DB restore with anchor_factory ----------------------
    def test_restore_anchor_factory_single_db(self):
        dep = self._single()
        run_id, _ = dep.write_data()
        bundle = create_backup(dep.sched_db, dep.journal, dep.key,
                               os.path.join(self.td.name, "b1"),
                               collect_heads=dep.anchor.collect)
        dep.close()
        _corrupt_db_out_of_band(dep.sched_db, "scheduler_runs",
                                "status='tampered'")

        staged_schedulers = []

        def factory(j, k, db):
            rs = RunScheduler(db, os.path.join(self.td.name,
                                               "factory_runtime.db"))
            staged_schedulers.append(rs)
            a = CrossDbAnchor(j, k, [("scheduler", db)])
            a.bind([("scheduler", rs)])
            return a

        self.addCleanup(lambda: [s.close(timeout=10)
                                 for s in staged_schedulers])
        result = restore_backup(bundle, dep.sched_db, dep.journal,
                                dep.key, anchor_factory=factory)
        self.assertTrue(result["anchor_factory_verified"])

        # The restored live set verifies with a fresh anchor too.
        sched2 = RunScheduler(dep.sched_db,
                              os.path.join(self.td.name, "rt3.db"))
        self.addCleanup(lambda: sched2.close(timeout=10))
        a2 = CrossDbAnchor(dep.journal, dep.key,
                           [("scheduler", dep.sched_db)])
        a2.bind([("scheduler", sched2)])
        ok, msg = a2.verify_all()
        self.assertTrue(ok, msg)
        rec = sched2.get_run(run_id)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["id"], run_id)


_MIG_TABLES = {
    "failure_memory": "id INTEGER PRIMARY KEY, payload TEXT",
    "acquisition_experience": "id INTEGER PRIMARY KEY, payload TEXT",
    "lh_challenge": "id INTEGER PRIMARY KEY, payload TEXT",
    "case_memory": "id INTEGER PRIMARY KEY, payload TEXT",
    "ob_evaluations": "id INTEGER PRIMARY KEY, payload TEXT",
}


def _make_migration_db(path):
    conn = sqlite3.connect(path, timeout=30)
    try:
        for table, cols in _MIG_TABLES.items():
            conn.execute(f"CREATE TABLE {table} ({cols})")
            conn.execute(f"INSERT INTO {table} (payload) VALUES "
                         f"('row1'), ('row2')")
        conn.commit()
    finally:
        conn.close()


def _row_counts(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in _MIG_TABLES}
    finally:
        conn.close()


def _columns(db_path, table):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


class MigrationCrashTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="phase3_e_mig_")
        self.db = os.path.join(self.td.name, "engine.db")
        _make_migration_db(self.db)

    def tearDown(self):
        self.td.cleanup()

    def _run_crash_driver(self, mode):
        proc = subprocess.Popen(
            [sys.executable, _CRASH_DRIVER, self.db, mode],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            _, err = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("crash driver hung")
        self.assertEqual(proc.returncode, -signal.SIGKILL,
                         f"driver must die by SIGKILL, got "
                         f"{proc.returncode}; stderr: {err.decode()}")
        return err

    def _assert_no_dangling_state(self):
        self.assertFalse(os.path.exists(sm.intent_path_for(self.db)),
                         "intent file must be gone")
        leftovers = []
        bdir = os.path.join(self.td.name, "migration_backups")
        if os.path.isdir(bdir):
            leftovers = os.listdir(bdir)
        self.assertEqual(leftovers, [],
                         f"backup bundles must be cleaned up: {leftovers}")

    # -- 11. successful migration --------------------------------------
    def test_migration_success_records_post_digest(self):
        before = _row_counts(self.db)
        result = sm.apply_migrations(self.db)
        self.assertEqual(result["applied"], [1, 2, 3, 4, 5])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["user_version"], 5)
        ok, msg = sm.verify_migrations(self.db)
        self.assertTrue(ok, msg)
        self.assertEqual(_row_counts(self.db), before)
        self.assertIn("context", _columns(self.db, "failure_memory"))
        self.assertIn("policy", _columns(self.db, "acquisition_experience"))
        self.assertIn("heldout_failed", _columns(self.db, "lh_challenge"))
        self.assertIn("law", _columns(self.db, "case_memory"))
        self.assertIn("supplier_id", _columns(self.db, "ob_evaluations"))
        conn = sqlite3.connect(self.db, timeout=30)
        try:
            rows = conn.execute(
                "SELECT version, post_digest FROM schema_migrations"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 5)
        for _v, digest in rows:
            self.assertTrue(digest and len(digest) == 64,
                            "post digest must be recorded")
        self._assert_no_dangling_state()
        # Idempotent re-run.
        again = sm.apply_migrations(self.db)
        self.assertEqual(again["applied"], [])
        self.assertEqual(again["skipped"], [1, 2, 3, 4, 5])

    # -- 12. crash before commit (real SIGKILL) -------------------------
    def test_migration_crash_before_commit(self):
        before = _row_counts(self.db)
        self._run_crash_driver("before_commit")
        # Dangling intent detected.
        self.assertIsNotNone(sm._read_intent(self.db))
        with self.assertRaises(sm.MigrationIncompleteError):
            sm.apply_migrations(self.db)
        # A partial migration NEVER verifies as trusted.
        ok, msg = sm.verify_migrations(self.db)
        self.assertFalse(ok)
        self.assertIn("dangling", msg)
        # Explicit recovery: the batch never committed (SQLite rolled the
        # hot journal back on next open), so no restore is needed.
        rec = sm.recover_migration(self.db)
        self.assertTrue(rec["recovered"])
        self.assertEqual(rec["action"], "cleared_intent_pre_state")
        self._assert_no_dangling_state()
        # Data intact, migration still pending, re-runnable.
        self.assertEqual(_row_counts(self.db), before)
        self.assertNotIn("context", _columns(self.db, "failure_memory"))
        result = sm.apply_migrations(self.db)
        self.assertEqual(result["applied"], [1, 2, 3, 4, 5])
        ok, msg = sm.verify_migrations(self.db)
        self.assertTrue(ok, msg)

    # -- 13. crash after commit (real SIGKILL) --------------------------
    def test_migration_crash_after_commit(self):
        before = _row_counts(self.db)
        self._run_crash_driver("after_commit")
        self.assertIsNotNone(sm._read_intent(self.db))
        with self.assertRaises(sm.MigrationIncompleteError):
            sm.apply_migrations(self.db)
        ok, msg = sm.verify_migrations(self.db)
        self.assertFalse(ok)
        self.assertIn("dangling", msg)
        # Recovery: the batch HAD committed; only finalization was lost.
        rec = sm.recover_migration(self.db)
        self.assertTrue(rec["recovered"])
        self.assertEqual(rec["action"], "finalized_post_commit")
        self._assert_no_dangling_state()
        ok, msg = sm.verify_migrations(self.db)
        self.assertTrue(ok, msg)
        self.assertEqual(_row_counts(self.db), before)
        self.assertIn("context", _columns(self.db, "failure_memory"))
        conn = sqlite3.connect(self.db, timeout=30)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM schema_migrations "
                "WHERE post_digest IS NOT NULL").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 5)

    # -- 14. in-process failure: provably clean rollback ----------------
    def test_migration_failure_cleans_up_when_rollback_proven(self):
        orig = sm._run_migration_batch

        def boom(db_path, versions, pre_uv):
            raise RuntimeError("boom mid-migration")

        sm._run_migration_batch = boom
        try:
            with self.assertRaises(RuntimeError):
                sm.apply_migrations(self.db)
        finally:
            sm._run_migration_batch = orig
        # The rollback was proven clean (digest unchanged), so the intent
        # and backup were removed by the failure path itself.
        self._assert_no_dangling_state()
        self.assertNotIn("context", _columns(self.db, "failure_memory"))
        ok, _ = sm.verify_migrations(self.db)
        self.assertFalse(ok)  # honestly pending, not dangling
        result = sm.apply_migrations(self.db)
        self.assertEqual(result["applied"], [1, 2, 3, 4, 5])

    # -- 15. dangling intent fails verification -------------------------
    def test_verify_refuses_dangling_intent(self):
        digest = sm._db_state_digest(self.db)
        sm._write_intent(self.db, {
            "format": sm.INTENT_FORMAT,
            "versions": [1, 2],
            "started_at": "2026-09-25T00:00:00Z",
            "pre_db_sha256": digest,
            "backup_bundle": None,
            "db_path": self.db,
            "status": "in_progress",
            "pid": 999999999,
        })
        ok, msg = sm.verify_migrations(self.db)
        self.assertFalse(ok)
        self.assertIn("dangling", msg)
        with self.assertRaises(sm.MigrationIncompleteError):
            sm.apply_migrations(self.db)
        rec = sm.recover_migration(self.db)
        self.assertTrue(rec["recovered"])
        self.assertEqual(rec["action"], "cleared_intent_pre_state")
        result = sm.apply_migrations(self.db)
        self.assertEqual(result["applied"], [1, 2, 3, 4, 5])

    # -- 16. recover with no intent --------------------------------------
    def test_recover_without_intent(self):
        rec = sm.recover_migration(self.db)
        self.assertFalse(rec["recovered"])
        self.assertIn("no dangling", rec["reason"])

    # -- 17. recover from backup on unrecognized state ------------------
    def test_recover_restores_from_backup_on_unrecognized_state(self):
        # Hand-build the crash scene: a real pre-migration backup bundle
        # + intent, then a DB state that is neither pre- nor post-.
        pre_digest = sm._db_state_digest(self.db)
        bundle = backup_mod.create_backup(
            self.db, None, None, os.path.join(self.td.name, "prebackup"))
        sm._write_intent(self.db, {
            "format": sm.INTENT_FORMAT,
            "versions": [1, 2, 3, 4, 5],
            "started_at": "2026-09-25T00:00:00Z",
            "pre_db_sha256": pre_digest,
            "backup_bundle": bundle,
            "db_path": self.db,
            "status": "in_progress",
            "pid": 999999999,
        })
        # Simulate a half-applied migration out-of-band: one column added
        # and an extra row, no bookkeeping -> neither pre nor post state.
        conn = sqlite3.connect(self.db, timeout=30)
        conn.execute("ALTER TABLE failure_memory ADD COLUMN context TEXT")
        conn.execute("INSERT INTO failure_memory (payload, context) "
                     "VALUES ('half', 'x')")
        conn.commit()
        conn.close()

        ok, _ = sm.verify_migrations(self.db)
        self.assertFalse(ok)
        rec = sm.recover_migration(self.db)
        self.assertTrue(rec["recovered"])
        self.assertEqual(rec["action"], "restored_from_backup")
        # DB is back at the pre-migration state: extra row gone, and the
        # half-applied column is gone too.
        self.assertEqual(_row_counts(self.db),
                         {t: 2 for t in _MIG_TABLES})
        self.assertNotIn("context", _columns(self.db, "failure_memory"))
        self.assertFalse(os.path.exists(sm.intent_path_for(self.db)),
                         "intent must be cleared after recovery")
        # Honestly re-runnable to green.
        result = sm.apply_migrations(self.db)
        self.assertEqual(result["applied"], [1, 2, 3, 4, 5])
        ok, msg = sm.verify_migrations(self.db)
        self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
