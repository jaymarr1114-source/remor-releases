"""Causal tests for swarm_engine.services.projects.ProjectService.

Real ingestor, real lifecycle, real work loop — no simulation. Scratch
sqlite DBs and project dirs via tempfile; nothing touches production state.

Environment note: ProjectWorkLoop.run_tests() shells out to
`python -m pytest` (surveyed in project/loop.py — it does NOT use
CommandRunner). pytest is not installed in this environment and cannot be
installed (PEP 668 externally-managed interpreter), so loop test runs
genuinely fail with "No module named pytest". The tests assert the real
causal mechanics (gap detection -> plan -> file edits -> retest) and branch
the success assertion on whether pytest is actually importable, documenting
the boundary instead of faking a test runner.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "pylib")))

from swarm_engine.services.projects import ProjectService  # noqa: E402
from swarm_engine.project.lifecycle import ProjectLifecycle  # noqa: E402

HAS_PYTEST = importlib.util.find_spec("pytest") is not None

# A placeholder engine. The fixtures below never take the
# capability-acquisition path (every round produces source-edit plans or
# diagnose-driven repair plans), so the engine is never consulted; this is
# not a simulated engine, just an unexercised argument.
_DUMMY_ENGINE = object()


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def _make_service(tmpdir):
    return ProjectService(os.path.join(tmpdir, "svc.db"),
                          os.path.join(tmpdir, "projects"),
                          engine=None)


def _make_service_with_engine(tmpdir):
    return ProjectService(os.path.join(tmpdir, "svc.db"),
                          os.path.join(tmpdir, "projects"),
                          engine=_DUMMY_ENGINE)


def _build_calc_fixture(root):
    """calc project with a genuinely broken add() and missing mul dispatch."""
    _write(os.path.join(root, "calc", "__init__.py"), "")
    _write(os.path.join(root, "calc", "ops.py"),
           '"""Arithmetic operations."""\n\n'
           'def add(a, b):\n'
           '    return a - b\n\n'
           'def mul(a, b):\n'
           '    return a * b\n')
    _write(os.path.join(root, "calc", "service.py"),
           '"""Dispatch service."""\n\n'
           'from calc.ops import add\n\n'
           'def compute(op, a, b):\n'
           '    if op == "add":\n'
           '        return add(a, b)\n'
           '    raise ValueError(f"unknown op {op}")\n')
    _write(os.path.join(root, "tests", "test_calc.py"),
           'from calc.ops import add, mul\n'
           'from calc.service import compute\n\n'
           'def test_add():\n'
           '    assert add(2, 3) == 5\n\n'
           'def test_mul():\n'
           '    assert mul(2, 3) == 6\n\n'
           'def test_compute_mul():\n'
           '    assert compute("mul", 2, 3) == 6\n')
    _write(os.path.join(root, "REQUIREMENTS.md"),
           '# Calculator requirements\n\n'
           '- `add(a, b)` must return the sum of a and b.\n'
           '- `mul(a, b)` must return the product of a and b.\n'
           '- `compute(op, a, b)` must dispatch on op and support add and mul.\n')


def _build_never_passing_fixture(root):
    """Fixture whose tests can never pass; the loop runs until stopped.

    The failing test name contains 'test_add' so the loop's diagnose step
    keeps producing source-repair plans every round (never falling into the
    capability-acquisition path, which would need a real engine).
    """
    _write(os.path.join(root, "tests", "test_loop.py"),
           'def test_add_still_broken():\n'
           '    assert False, "permanent failure: this fixture never passes"\n')
    _write(os.path.join(root, "REQUIREMENTS.md"),
           '# Fixture\n\nThe loop must keep running until stopped.\n')


def _to_executing(svc, project_id):
    for state in ("analyzing", "planning", "executing"):
        r = svc.transition(project_id, state, reason="test advance")
        assert r["ok"], r


def _wait_terminal(svc, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = svc.loop_status(job_id)
        assert st["ok"], st
        if st["status"] in ("completed", "failed", "stopped"):
            return st
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} not terminal after {timeout}s")


class TestCreateListGet(unittest.TestCase):
    def test_create_blank_listed_ingested(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            r = svc.create({"kind": "blank", "project_id": "blank1"})
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["project_id"], "blank1")
            self.assertTrue(os.path.isdir(r["root"]))

            rows = svc.list_projects()
            row = next(x for x in rows if x["project_id"] == "blank1")
            self.assertEqual(row["state"], "ingested")
            # No progress ever recorded: fraction honestly 0.0, not vacuous 1.0.
            self.assertEqual(row["fraction_complete"], 0.0)
            self.assertEqual(row["cycles"], 0)

            g = svc.get_project("blank1")
            self.assertTrue(g["ok"], g)
            self.assertEqual(g["state"], "ingested")
            self.assertGreaterEqual(g["model"]["file_count"], 2)
            self.assertEqual(g["legal_next"], ["analyzing"])
            # History begins at the first transition: no faked "created" row.
            self.assertEqual(g["history"], [])
            self.assertIsNone(g["progress"])

    def test_create_from_real_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            zpath = os.path.join(tmp, "proj.zip")
            with zipfile.ZipFile(zpath, "w") as zf:
                zf.writestr("hello.py", 'def greet():\n    return "hi"\n')
                zf.writestr("util.py", 'def double(x):\n    return 2 * x\n')
                zf.writestr("REQUIREMENTS.md",
                            "# Requirements\n\n"
                            "- The tool must greet the user.\n"
                            "- Output should be friendly.\n")
            svc = _make_service(tmp)
            r = svc.create({"kind": "zip", "path": zpath,
                            "project_id": "zip1"})
            self.assertTrue(r["ok"], r)
            g = svc.get_project("zip1")
            self.assertTrue(g["ok"], g)
            self.assertEqual(g["state"], "ingested")
            self.assertGreaterEqual(g["model"]["file_count"], 3)
            paths = [f["path"] for f in g["model"]["files"]]
            self.assertIn("hello.py", paths)
            self.assertGreaterEqual(g["model"]["requirement_count"], 2)

    def test_create_honest_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            r = svc.create({"kind": "zip", "path": "/nonexistent/x.zip"})
            self.assertFalse(r["ok"])
            self.assertIn("not found", r["error"])
            r = svc.create({"kind": "dir", "path": "/nonexistent/dir"})
            self.assertFalse(r["ok"])
            r = svc.create({"kind": "carrier-pigeon"})
            self.assertFalse(r["ok"])
            self.assertIn("unknown source kind", r["error"])


class TestTransitions(unittest.TestCase):
    def test_illegal_transition_refused_naming_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc.create({"kind": "blank", "project_id": "t1"})
            r = svc.transition("t1", "complete")
            self.assertFalse(r["ok"])
            # The refusal must name the legal options (ingested -> analyzing).
            self.assertIn("analyzing", r["error"])
            self.assertIn("not legal", r["error"])
            # State unchanged.
            self.assertEqual(svc.get_project("t1")["state"], "ingested")

    def test_legal_chain_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc.create({"kind": "blank", "project_id": "t2"})
            chain = ["analyzing", "planning", "executing",
                     "verifying", "complete"]
            prev = "ingested"
            for state in chain:
                r = svc.transition("t2", state, reason=f"test->{state}")
                self.assertTrue(r["ok"], r)
                self.assertEqual(r["state"], state)
                prev = state
            g = svc.get_project("t2")
            self.assertEqual(len(g["history"]), 5)  # one row per transition
            self.assertEqual(g["history"][0]["from"], "ingested")
            self.assertEqual(g["history"][0]["to"], "analyzing")
            self.assertEqual(g["history"][-1]["to"], "complete")
            self.assertEqual(g["legal_next"], [])  # complete is terminal

    def test_transition_unknown_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc.create({"kind": "blank", "project_id": "t3"})
            r = svc.transition("t3", "vibing")
            self.assertFalse(r["ok"])
            self.assertIn("unknown state", r["error"])


class TestRunLoop(unittest.TestCase):
    def test_run_loop_requires_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)  # engine=None
            svc.create({"kind": "blank", "project_id": "e1"})
            _to_executing(svc, "e1")
            r = svc.run_loop("e1")
            self.assertFalse(r["ok"])
            self.assertIn("engine", r["error"])

    def test_run_loop_requires_executing(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service_with_engine(tmp)
            svc.create({"kind": "blank", "project_id": "e2"})
            r = svc.run_loop("e2")  # still ingested
            self.assertFalse(r["ok"])
            self.assertIn("executing", r["error"])

    def test_run_loop_repairs_fixture_causally(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixdir = os.path.join(tmp, "fix")
            _build_calc_fixture(fixdir)
            svc = _make_service_with_engine(tmp)
            r = svc.create({"kind": "dir", "path": fixdir,
                            "project_id": "repair1"})
            self.assertTrue(r["ok"], r)
            _to_executing(svc, "repair1")

            jr = svc.run_loop("repair1", max_rounds=4)
            self.assertTrue(jr["ok"], jr)
            st = _wait_terminal(svc, jr["job_id"])
            self.assertEqual(st["status"], "completed")
            report = st["report"]
            self.assertIsNotNone(report)
            self.assertGreaterEqual(report["rounds"], 1)
            self.assertGreaterEqual(len(report["test_runs"]), 2)  # baseline +

            # Causal evidence: the loop really found the broken add, both in
            # the round-1 inspect gaps and in diagnose-driven repair gaps.
            all_gaps = list(report["gaps"]) + [
                g for rep in report["repairs"] for g in rep.get("gaps", [])]
            saw_gap = any(
                g.get("kind") == "wrong_implementation"
                and g.get("symbol") == "add" for g in all_gaps)
            self.assertTrue(saw_gap, "no wrong_implementation/add gap found")
            # ...really wrote files...
            self.assertTrue(report["edits"])
            self.assertTrue(all(e.get("committed") for e in report["edits"]))
            # ...and the written file genuinely computes correctly.
            ops_path = os.path.join(fixdir, "calc", "ops.py")
            with open(ops_path) as fh:
                src = fh.read()
            self.assertIn("return a + b", src)
            spec = importlib.util.spec_from_file_location(
                "fixture_ops_repaired", ops_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(mod.add(2, 3), 5)
            self.assertEqual(mod.mul(2, 3), 6)

            if HAS_PYTEST:
                self.assertTrue(report["success"])
                self.assertEqual(svc.get_project("repair1")["state"],
                                 "verifying")
            else:
                # Honest environment boundary: the loop's test command is
                # `python -m pytest` and pytest is not installed here.
                self.assertFalse(report["success"])
                first = report["test_runs"][0]
                self.assertIn("No module named pytest",
                              (first.get("stderr") or "") +
                              (first.get("stdout") or ""))
                # The loop could not map the failure back to a source gap
                # (no test names in the output without pytest), so it honestly
                # took the capability-acquisition path and stopped after the
                # round instead of pretending to make progress.
                self.assertTrue(any("capability_acquisition" in rep
                                    for rep in report["repairs"]),
                                "expected the honest acquisition-path stop")
                # Failure maps legally: executing -> blocked.
                self.assertEqual(svc.get_project("repair1")["state"],
                                 "blocked")

            # Progress persisted from the report: cycles == rounds, gaps remain
            # outstanding (failure case) -> fraction 0.0.
            g = svc.get_project("repair1")
            self.assertEqual(g["progress"]["cycles"], report["rounds"])
            if not HAS_PYTEST:
                self.assertEqual(g["progress"]["fraction_complete"], 0.0)
                self.assertTrue(g["progress"]["outstanding_requirements"])

    def test_run_loop_stop_midrun(self):
        # Deterministic stop: emulate a slow test suite by adding latency to
        # the real run_tests (documented scaffolding — the loop logic,
        # checkpoint, RunStopped conversion, and lifecycle settle are all
        # real). Without this, rounds take ~50ms here (no pytest installed)
        # and the loop self-terminates before a stop can land mid-run.
        from unittest import mock
        from swarm_engine.project import loop as loop_mod
        orig_run_tests = loop_mod.ProjectWorkLoop.run_tests

        def _slow_run_tests(self):
            out = orig_run_tests(self)
            time.sleep(2.0)
            return out

        with tempfile.TemporaryDirectory() as tmp:
            fixdir = os.path.join(tmp, "fixstop")
            _build_never_passing_fixture(fixdir)
            svc = _make_service_with_engine(tmp)
            svc.create({"kind": "dir", "path": fixdir, "project_id": "stop1"})
            _to_executing(svc, "stop1")

            with mock.patch.object(loop_mod.ProjectWorkLoop, "run_tests",
                                   _slow_run_tests):
                jr = svc.run_loop("stop1", max_rounds=200)
                self.assertTrue(jr["ok"], jr)
                job_id = jr["job_id"]

                # Wait until the job is genuinely mid-run, then stop.
                deadline = time.time() + 30
                while True:
                    st = svc.loop_status(job_id)
                    self.assertTrue(st["ok"], st)
                    if st["status"] == "running":
                        break
                    self.assertLess(time.time(), deadline,
                                    "job never reached running")
                    time.sleep(0.05)

                sr = svc.stop_loop(job_id)
                self.assertTrue(sr["ok"], sr)

                st = _wait_terminal(svc, job_id)
                self.assertEqual(st["status"], "stopped")
                self.assertIsNone(st["report"])  # stopped, not finished
                self.assertIn("stopped", st["error"])

            # Lifecycle left in a legal state: executing -> blocked.
            g = svc.get_project("stop1")
            self.assertEqual(g["state"], "blocked")

            # No orphan thread.
            thread = svc._jobs[job_id]["thread"]
            thread.join(timeout=15)
            self.assertFalse(thread.is_alive())

            # Stopping a finished job is an honest no-op.
            sr2 = svc.stop_loop(job_id)
            self.assertTrue(sr2["ok"])
            self.assertIn("already finished", sr2["note"])

    def test_second_concurrent_run_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixdir = os.path.join(tmp, "fixconc")
            _build_never_passing_fixture(fixdir)
            svc = _make_service_with_engine(tmp)
            svc.create({"kind": "dir", "path": fixdir, "project_id": "conc1"})
            _to_executing(svc, "conc1")
            jr = svc.run_loop("conc1", max_rounds=200)
            self.assertTrue(jr["ok"], jr)
            try:
                r2 = svc.run_loop("conc1", max_rounds=4)
                self.assertFalse(r2["ok"])
                self.assertIn("already running", r2["error"])
            finally:
                svc.stop_loop(jr["job_id"])
                _wait_terminal(svc, jr["job_id"])


class TestTamperAndUnknown(unittest.TestCase):
    def test_tampered_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc.create({"kind": "blank", "project_id": "tamper1"})
            db_path = svc._db_path
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO project_state (project_id, state, updated_at)"
                " VALUES (?, ?, ?)", ("tamper1", "bogus", time.time()))
            conn.commit()
            conn.close()

            # Lifecycle level: Enum construction raises ValueError — the row
            # is refused, never returned as garbage.
            lc = ProjectLifecycle(db_path)
            with self.assertRaises(ValueError):
                lc.state_of("tamper1")

            # Service level: fail-closed honest error, not a crash, not garbage.
            g = svc.get_project("tamper1")
            self.assertFalse(g["ok"])
            self.assertIn("corrupt", g["error"])

            rows = svc.list_projects()
            row = next(x for x in rows if x["project_id"] == "tamper1")
            self.assertEqual(row["state"], "corrupt")
            self.assertIn("error", row)

    def test_unknown_project_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service_with_engine(tmp)
            g = svc.get_project("nope")
            self.assertFalse(g["ok"])
            self.assertIn("not found", g["error"])
            t = svc.transition("nope", "analyzing")
            self.assertFalse(t["ok"])
            self.assertIn("not found", t["error"])
            rl = svc.run_loop("nope")
            self.assertFalse(rl["ok"])
            self.assertIn("not found", rl["error"])
            ls = svc.loop_status("nope")
            self.assertFalse(ls["ok"])
            self.assertIn("unknown job", ls["error"])
            sl = svc.stop_loop("nope")
            self.assertFalse(sl["ok"])
            self.assertIn("unknown job", sl["error"])

    def test_delete_project_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = _make_service(tmp)
            svc.create({"kind": "blank", "project_id": "d1"})
            r = svc.delete_project("d1")
            self.assertFalse(r["ok"])
            self.assertIn("ABSENT", r["error"])
            # Project untouched by the refused delete.
            self.assertTrue(svc.get_project("d1")["ok"])


if __name__ == "__main__":
    unittest.main()
