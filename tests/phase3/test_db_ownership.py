"""D11 — single-owner DB architecture tests.

Real behavioral tests (no hard-coded results): every refusal below is
executed against a live engine, a live thread, or a live subprocess.
"""
import gc
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "pylib"))

from swarm_engine.core.db_ownership import (
    DuplicateEngineError,
    ThreadAffinityError,
    db_owner,
)
from swarm_engine.core.engine import SwarmEngine


def _fresh_db(name):
    d = tempfile.mkdtemp(prefix="d11_")
    return os.path.join(d, name + ".db")


class TestDbOwnership(unittest.TestCase):
    def test_duplicate_engine_in_process(self):
        """Two live engines on one file in one process: the second
        construction raises DuplicateEngineError (fail-closed at
        construction); the first owner's claim is undisturbed."""
        db = _fresh_db("dup")
        e1 = SwarmEngine(db_path=db)
        self.assertEqual(db_owner(db), "SwarmEngine")
        try:
            with self.assertRaises(DuplicateEngineError):
                SwarmEngine(db_path=db)
            # First engine still owns and still works.
            self.assertEqual(db_owner(db), "SwarmEngine")
            self.assertIsNone(e1.oracle_registry.current_trust("nope"))
        finally:
            e1.close()

    def test_duplicate_normalizes_path_spellings(self):
        """Same file reached via a relative vs absolute path is still the
        same owner (abspath normalization)."""
        d = tempfile.mkdtemp(prefix="d11rel_")
        db = os.path.join(d, "eng.db")
        old = os.getcwd()
        os.chdir(d)
        try:
            e1 = SwarmEngine(db_path=db)
            try:
                with self.assertRaises(DuplicateEngineError):
                    SwarmEngine(db_path="eng.db")
            finally:
                e1.close()
        finally:
            os.chdir(old)

    def test_distinct_files_coexist(self):
        """No false positives: engines on different files boot side by
        side in one process (the scheduler/front wiring depends on this)."""
        db1, db2 = _fresh_db("a"), _fresh_db("b")
        e1 = SwarmEngine(db_path=db1)
        e2 = SwarmEngine(db_path=db2)
        try:
            self.assertEqual(db_owner(db1), "SwarmEngine")
            self.assertEqual(db_owner(db2), "SwarmEngine")
        finally:
            e1.close()
            e2.close()

    def test_close_releases_ownership(self):
        """engine.close() deterministically releases the claim + owner
        lock so a fresh engine can boot (in-process restart simulation)."""
        db = _fresh_db("close")
        e1 = SwarmEngine(db_path=db)
        e1.close()
        self.assertIsNone(db_owner(db))
        e2 = SwarmEngine(db_path=db)
        e2.close()
        # close() is idempotent.
        e2.close()

    def test_gc_releases_ownership(self):
        """del + gc.collect() releases the claim (the pattern used by the
        inherited regression suite's restart simulation)."""
        db = _fresh_db("gc")
        e1 = SwarmEngine(db_path=db)
        del e1
        gc.collect()
        e2 = SwarmEngine(db_path=db)
        e2.close()

    def test_hatch_allows_shared_for_tests(self):
        """_test_allow_shared=True is the explicit, auditable test-only
        bypass: with REMOR_TEST_MODE=1 and a temp-dir DB, two engines on
        one file boot. Without the env var the flag is ignored."""
        db = _fresh_db("hatch")
        with mock.patch.dict(os.environ, {"REMOR_TEST_MODE": "1"}):
            e1 = SwarmEngine(db_path=db, _test_allow_shared=True)
            e2 = SwarmEngine(db_path=db, _test_allow_shared=True)
            e1.close()
            e2.close()
        # Without the hatch the same pattern still refuses.
        e3 = SwarmEngine(db_path=db)
        try:
            with self.assertRaises(DuplicateEngineError):
                SwarmEngine(db_path=db)
        finally:
            e3.close()

    def test_hatch_ignored_without_env_var(self):
        """The bypass is dead code in production: _test_allow_shared=True
        WITHOUT REMOR_TEST_MODE=1 is ignored (fail closed)."""
        db = _fresh_db("hatchnoenv")
        with mock.patch.dict(os.environ):
            os.environ.pop("REMOR_TEST_MODE", None)
            e1 = SwarmEngine(db_path=db, _test_allow_shared=True)
            try:
                with self.assertRaises(DuplicateEngineError):
                    SwarmEngine(db_path=db, _test_allow_shared=True)
            finally:
                e1.close()

    def test_hatch_ignored_for_non_temp_path(self):
        """Even with REMOR_TEST_MODE=1, the bypass is refused for a DB
        outside the system temp dir."""
        db = _fresh_db("hatchnontmp")
        with mock.patch.dict(os.environ, {"REMOR_TEST_MODE": "1"}), \
                mock.patch("tempfile.gettempdir",
                           return_value="/definitely/not/the/temp/dir"):
            e1 = SwarmEngine(db_path=db, _test_allow_shared=True)
            try:
                with self.assertRaises(DuplicateEngineError):
                    SwarmEngine(db_path=db, _test_allow_shared=True)
            finally:
                e1.close()

    def test_two_processes_one_file(self):
        """REAL subprocess: a second process booting an engine on the same
        file fails with DuplicateEngineError naming the owner lockfile."""
        db = _fresh_db("xproc")
        child_src = (
            "import sys, os, time\n"
            "sys.path.insert(0, %r)\n"
            "from swarm_engine.core.engine import SwarmEngine\n"
            "eng = SwarmEngine(db_path=%r)\n"
            "print('READY', flush=True)\n"
            "time.sleep(30)\n"
        ) % (os.path.join(HERE, "..", "..", "pylib"), db)
        proc = subprocess.Popen(
            [sys.executable, "-c", child_src],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = proc.stdout.readline().strip()
            # NOTE: never call proc.stderr.read() here -- it blocks until
            # the child exits (30s sleep), which would release the owner
            # lock before the parent's boot attempt and invalidate the
            # test. On mismatch, terminate first, then drain stderr.
            if line != "READY":
                proc.terminate()
                _, err = proc.communicate(timeout=15)
                self.fail("child engine did not boot: " + err[-2000:])
            with self.assertRaises(DuplicateEngineError) as ctx:
                SwarmEngine(db_path=db)
            self.assertIn("owner.lock", str(ctx.exception))
            self.assertIn("another process", str(ctx.exception))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_lock_released_when_owner_process_exits(self):
        """Ownership does not go stale: after the owner process exits, a
        new process can boot on the same file."""
        db = _fresh_db("handover")
        child_src = (
            "import sys\n"
            "sys.path.insert(0, %r)\n"
            "from swarm_engine.core.engine import SwarmEngine\n"
            "eng = SwarmEngine(db_path=%r)\n"
            "print('READY', flush=True)\n"
        ) % (os.path.join(HERE, "..", "..", "pylib"), db)
        out = subprocess.run([sys.executable, "-c", child_src],
                             capture_output=True, text=True, timeout=120)
        self.assertIn("READY", out.stdout, out.stderr[-2000:])
        e = SwarmEngine(db_path=db)  # must not raise: lock was released
        e.close()

    def test_cross_thread_oracle_use_raises_named_error(self):
        """Touching the thread-bound oracle connection from another thread
        raises ThreadAffinityError (not a raw sqlite3.ProgrammingError)."""
        db = _fresh_db("threads")
        eng = SwarmEngine(db_path=db)
        try:
            box = queue.Queue()

            def worker():
                try:
                    eng.oracle_registry.current_trust("cap_x")
                    box.put(("no-error", None))
                except Exception as exc:  # noqa: BLE001
                    box.put((type(exc).__name__, exc))

            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=60)
            name, exc = box.get(timeout=60)
            self.assertEqual(
                name, "ThreadAffinityError",
                "expected ThreadAffinityError, got %s (%r)" % (name, exc))

            def worker_raw():
                try:
                    eng.oracle_registry._conn.execute("SELECT 1")
                    box.put(("no-error", None))
                except Exception as exc:  # noqa: BLE001
                    box.put((type(exc).__name__, exc))

            t2 = threading.Thread(target=worker_raw)
            t2.start()
            t2.join(timeout=60)
            name2, exc2 = box.get(timeout=60)
            self.assertEqual(
                name2, "ThreadAffinityError",
                "expected ThreadAffinityError, got %s (%r)" % (name2, exc2))
        finally:
            eng.close()

    def test_same_thread_oracle_use_ok(self):
        """The constructing thread keeps full oracle access."""
        db = _fresh_db("samethread")
        eng = SwarmEngine(db_path=db)
        try:
            self.assertIsNone(eng.oracle_registry.current_trust("nope"))
            ok, msg = eng.oracle_registry.audit_all(), None
            self.assertIsInstance(ok, dict)
            eng.oracle_registry._conn.execute("SELECT 1").fetchall()
        finally:
            eng.close()

    def test_scheduler_lazy_engine_unaffected(self):
        """The scheduler's own engine (separate file) still boots lazily
        and is a singleton -- the normal wiring is unaffected."""
        from swarm_engine.services.scheduler import RunScheduler
        base = tempfile.mkdtemp(prefix="d11sched_")
        sched = RunScheduler(
            db_path=os.path.join(base, "sched.db"),
            runtime_db_path=os.path.join(base, "runtime.db"))
        try:
            e1 = sched._engine_lazy()
            e2 = sched._engine_lazy()
            self.assertIs(e1, e2)
            self.assertTrue(os.path.abspath(
                os.path.join(base, "runtime.db")) == os.path.abspath(
                    e1.db_path))
        finally:
            sched.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
