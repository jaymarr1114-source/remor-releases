"""Causal tests for cooperative run preemption (run_control wiring).

Every test drives the REAL machinery -- UniversalTaskInterface.handle() on a
real SwarmEngine with scratch sqlite DBs under tempfile -- never a fake
workload. Production paths are never touched.

Honest granularity bound (also documented in run_control.py): stop/pause
take effect at the NEXT checkpoint, never instantly. A subprocess sandbox
already launched runs to its own timeout (5s), so "prompt return" after
request_stop() is bounded by one sandbox batch plus checkpoint stride.

Causality strategy: the stop test is paired with a revert test that runs the
IDENTICAL scenario with the checkpoint mechanism neutralised. If stop works
with the wiring and does nothing without it, the wiring -- not luck -- is
what makes stop work.
"""
import asyncio
import os
import py_compile
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.core.task_interface import (  # noqa: E402
    Stage,
    UniversalTaskInterface,
)
from swarm_engine.services import run_control as rc  # noqa: E402
from swarm_engine.services.run_control import (  # noqa: E402
    RunControl,
    RunStopped,
    checkpoint,
)

# A goal with a genuine capability gap: nothing in the engine's vocabulary
# computes x^3 + 2^x, so handle() spins the real strategy/growth loops
# (GeneralSynthesizer search, improvement recovery, orchestrated GENERATE
# with sandboxed candidate validation). Natural completion takes ~85s and
# ends in an honest acquisition failure -- long enough to preempt, real
# enough to prove the wiring.
_GAP_GOAL = "for input x compute x cubed plus two to the power of x"
_GAP_EXAMPLES = [({"x": x}, x ** 3 + 2 ** x) for x in range(7)]

# A goal the engine closes quickly and successfully (existing vocabulary +
# example-driven growth, ~1s). Used for the no-control regression and the
# post-stop integrity check.
_SIMPLE_GOAL = "for input x compute x squared plus three"
_SIMPLE_EXAMPLES = [({"x": x}, x ** 2 + 3) for x in range(5)]


def _make_iface():
    td = tempfile.TemporaryDirectory()
    db = os.path.join(td.name, "engine.db")
    engine = SwarmEngine(db_path=db)
    iface = UniversalTaskInterface(engine)
    return td, db, engine, iface


def _run_handle_in_thread(goal, examples, control, box, payload=None,
                          disable_checkpoints=False, followup=None):
    """Drive real handle() phase(s) on a worker thread.

    The engine is created ON the worker thread: SwarmEngine is thread-affine
    (the oracle registry keeps one sqlite connection from construction;
    reusing it from another thread raises sqlite3.ProgrammingError -- a
    pre-existing engine property, unrelated to preemption). The main thread
    only signals (request_stop / request_pause / resume) and observes.

    box receives: td, db, engine, iface, events=[(stage, ran, t)], outcome,
    followup_outcome, caps_list_ok, error, saved_checkpoint.
    Wrapping iface._trace is observation instrumentation only -- the real
    _trace still runs underneath.
    """
    def worker():
        td = tempfile.TemporaryDirectory()
        db = os.path.join(td.name, "engine.db")
        box["td"], box["db"] = td, db
        engine = SwarmEngine(db_path=db)
        iface = UniversalTaskInterface(engine)
        box["engine"], box["iface"] = engine, iface
        events = box["events"]
        orig_trace = iface._trace

        def rec(outcome, stage, ran, detail="", elapsed_ms=0.0):
            events.append((stage.value, ran, time.time()))
            return orig_trace(outcome, stage, ran, detail, elapsed_ms)

        iface._trace = rec
        # Literal per the task brief; handle() installs its own control on
        # top, so the true revert is neutralising the checkpoint choke
        # point below (identical effect to "no control installed").
        rc.set_current(None)
        if disable_checkpoints:
            box["saved_checkpoint"] = rc.RunControl.checkpoint
            rc.RunControl.checkpoint = lambda self, where="": None

        async def _go(g, ex, p, c):
            md = {"run_control": c} if c is not None else None
            return await iface.handle(g, payload=p, examples=ex, metadata=md)

        try:
            box["outcome"] = asyncio.run(_go(goal, examples, payload, control))
            if followup is not None:
                g2, ex2, p2 = followup
                box["followup_outcome"] = asyncio.run(
                    _go(g2, ex2, p2, None))
                # Capability-store read on the worker thread (thread-affine).
                box["caps_list_ok"] = isinstance(
                    engine.capabilities.list(), list)
        except Exception as exc:  # surfaced, never swallowed
            box["error"] = exc
        finally:
            if disable_checkpoints:
                rc.RunControl.checkpoint = box["saved_checkpoint"]
            iface._trace = orig_trace

    t = threading.Thread(target=worker, name="remor-run-worker", daemon=True)
    t.start()
    return t


def _wait_for_events(box, timeout=60):
    deadline = time.time() + timeout
    while not box["events"] and time.time() < deadline:
        time.sleep(0.1)
    return bool(box["events"])


def _blocked_in_checkpoint(thread):
    """True iff the worker thread is currently parked inside checkpoint().

    This is the causal observation that a pause actually stalled the run,
    rather than the run merely being slow on its own.
    """
    import sys as _sys
    frames = _sys._current_frames().get(thread.ident)
    f = frames
    while f is not None:
        if (os.path.basename(f.f_code.co_filename) == "run_control.py"
                and f.f_code.co_name == "checkpoint"):
            return True
        f = f.f_back
    return False


class TestRunControlContract(unittest.TestCase):
    """The frozen contract's own semantics (no engine involved)."""

    def test_checkpoint_noop_without_control(self):
        rc.set_current(None)
        checkpoint("anywhere")  # must not raise
        checkpoint()

    def test_stop_raises_at_checkpoint(self):
        c = RunControl()
        c.request_stop()
        with self.assertRaises(RunStopped):
            c.checkpoint("x")

    def test_pause_blocks_until_resume(self):
        c = RunControl()
        c.request_pause()
        self.assertTrue(c.paused)
        done = []
        t = threading.Thread(
            target=lambda: (c.checkpoint("p"), done.append(True)))
        t.start()
        time.sleep(0.3)
        self.assertFalse(done, "checkpoint must block while paused")
        c.resume()
        t.join(timeout=5)
        self.assertTrue(done, "checkpoint must unblock after resume")

    def test_stop_overrides_pause(self):
        c = RunControl()
        c.request_pause()
        c.request_stop()
        self.assertFalse(c.paused)
        with self.assertRaises(RunStopped):
            c.checkpoint("x")

    def test_resume_never_clears_stop(self):
        c = RunControl()
        c.request_stop()
        c.resume()
        self.assertTrue(c.stop_requested)
        with self.assertRaises(RunStopped):
            c.checkpoint("x")

    def test_stopped_stage_exists(self):
        self.assertEqual(Stage.STOPPED.value, "stopped")


class TestStopPreemption(unittest.TestCase):
    """Stop a real multi-second acquisition mid-run."""

    def test_stop_cuts_short_long_acquisition(self):
        box = {"events": [], "outcome": None, "error": None}
        control = RunControl()
        t0 = time.time()
        t = _run_handle_in_thread(
            _GAP_GOAL, _GAP_EXAMPLES, control, box)
        try:
            self.assertTrue(_wait_for_events(box),
                            "run never started; cannot preempt what never ran")
            # Wait until the run is inside the long ACQUIRE phase: the first
            # five stage events (understand..plan) precede it, and one more
            # beat puts us deep in the synthesis/search loops rather than
            # in a stage-boundary checkpoint.
            deadline = time.time() + 60
            while len(box["events"]) < 5 and time.time() < deadline:
                time.sleep(0.1)
            self.assertGreaterEqual(
                len(box["events"]), 5,
                "run never reached the acquisition phase")
            time.sleep(2.0)
            self.assertTrue(t.is_alive(),
                            "sanity: the gap goal must still be running")
            control.request_stop()
            t.join(timeout=60)
            elapsed = time.time() - t0
            self.assertFalse(t.is_alive(),
                             "handle() must return promptly after stop, not "
                             "run to natural completion (~85s)")
            self.assertLess(elapsed, 60,
                            "bounded wait: stop latency is one sandbox batch "
                            "plus checkpoint stride, not the full run")
            self.assertIsNone(box["error"], f"unexpected: {box['error']!r}")
            out = box["outcome"]
            self.assertIsNotNone(out)
            self.assertFalse(out.success)
            self.assertIn("stopped", out.error.lower())
            self.assertTrue(box["events"], "trace must be non-empty")
            self.assertTrue(out.trace, "outcome trace must be intact")
            self.assertEqual(out.trace[-1].stage, Stage.STOPPED)
        finally:
            box.get("td") and box["td"].cleanup()

    def test_stop_causality_revert_to_noop(self):
        """Identical scenario with the checkpoint mechanism neutralised:
        stop must have NO effect and the run must complete naturally."""
        box = {"events": [], "outcome": None, "error": None}
        control = RunControl()
        t0 = time.time()
        t = _run_handle_in_thread(
            _GAP_GOAL, _GAP_EXAMPLES, control, box,
            disable_checkpoints=True)
        try:
            self.assertTrue(_wait_for_events(box),
                            "run never started; cannot preempt what never ran")
            # Same depth as the wired stop test: inside the ACQUIRE phase.
            deadline = time.time() + 60
            while len(box["events"]) < 5 and time.time() < deadline:
                time.sleep(0.1)
            self.assertGreaterEqual(len(box["events"]), 5)
            time.sleep(2.0)
            self.assertTrue(t.is_alive())
            control.request_stop()
            # The stop must be ignored: the run keeps going well past the
            # point where the wired run would already have returned.
            time.sleep(5)
            self.assertTrue(t.is_alive(),
                            "with checkpoints disabled, request_stop must "
                            "have no effect")
            t.join(timeout=300)
            elapsed = time.time() - t0
            self.assertFalse(t.is_alive(), "natural run must finish")
            self.assertGreater(elapsed, 20,
                               "must have run to natural completion, not an "
                               "early stop")
            self.assertIsNone(box["error"], f"unexpected: {box['error']!r}")
            out = box["outcome"]
            self.assertIsNotNone(out)
            self.assertFalse(out.success)
            self.assertNotIn("stopped", out.error.lower(),
                             "without the wiring there is no stopped outcome")
            self.assertFalse(
                any(tr.stage == Stage.STOPPED for tr in out.trace),
                "no STOPPED stage without the wiring")
        finally:
            box.get("td") and box["td"].cleanup()


class TestPauseResume(unittest.TestCase):
    """Pause must observably stall the run; resume must let it continue.

    Honest note: no goal was found on this runtime that BOTH runs long
    enough to pause mid-run AND succeeds naturally (the engine honestly
    fails these acquisitions after ~85s of real search). So "completes"
    here means the run reaches its natural terminal outcome with an
    intact trace after resume -- not a hang, and not converted into a
    stop. The pause/resume mechanism itself is proven causally: the
    worker thread is observed parked inside run_control.checkpoint (via
    stack inspection, not inference), no stage progress occurs while it
    is parked, and progress resumes after resume().
    """

    def test_pause_stalls_and_resume_completes(self):
        box = {"events": [], "outcome": None, "error": None}
        control = RunControl()
        t = _run_handle_in_thread(
            _GAP_GOAL, _GAP_EXAMPLES, control, box)
        try:
            # Wait until the run is inside the long ACQUIRE phase, so the
            # pause demonstrably stalls real search work, not just a fast
            # stage transition.
            self.assertTrue(_wait_for_events(box), "run never started")
            deadline = time.time() + 60
            while len(box["events"]) < 5 and time.time() < deadline:
                time.sleep(0.1)
            self.assertGreaterEqual(len(box["events"]), 5)
            time.sleep(1.0)
            control.request_pause()
            # Causal proof the pause took effect: the worker thread parks
            # inside run_control.checkpoint (not merely "slow somewhere").
            deadline = time.time() + 30
            parked = False
            while time.time() < deadline:
                if _blocked_in_checkpoint(t):
                    parked = True
                    break
                time.sleep(0.1)
            self.assertTrue(parked,
                            "worker never parked in checkpoint after pause")
            n_events = len(box["events"])
            time.sleep(3)
            self.assertEqual(len(box["events"]), n_events,
                             "no stage progress while paused")
            self.assertTrue(t.is_alive(), "run must still be alive, stalled")
            control.resume()
            # The run must unblock after resume: the worker leaves
            # run_control.checkpoint (or finishes). Stage events are too
            # coarse here -- the next one only fires when the whole
            # ACQUIRE phase completes -- so stack observation is again
            # the causal signal.
            deadline = time.time() + 30
            resumed = False
            while time.time() < deadline:
                if not t.is_alive() or not _blocked_in_checkpoint(t):
                    resumed = True
                    break
                time.sleep(0.1)
            self.assertTrue(resumed, "run did not unblock after resume")
            # Let it run to its natural (honest-failure) completion; a stop
            # here would conflate the resume assertion.
            t.join(timeout=300)
            self.assertFalse(t.is_alive())
            self.assertIsNone(box["error"], f"unexpected: {box['error']!r}")
            out = box["outcome"]
            self.assertIsNotNone(out)
            # The gap goal honestly fails acquisition; "completes" means it
            # reaches a terminal outcome with an intact trace, not a hang.
            self.assertTrue(out.trace)
            self.assertFalse(
                any(tr.stage == Stage.STOPPED for tr in out.trace),
                "resume must not convert into a stop")
        finally:
            box.get("td") and box["td"].cleanup()


class TestEngineIntegrityAfterStop(unittest.TestCase):
    """A stopped run must leave the engine usable, not half-written."""

    def test_same_engine_completes_normal_task_after_stop(self):
        box = {"events": [], "outcome": None, "error": None}
        control = RunControl()
        t = _run_handle_in_thread(
            _GAP_GOAL, _GAP_EXAMPLES, control, box,
            followup=(_SIMPLE_GOAL, _SIMPLE_EXAMPLES, {"x": 4}))
        try:
            self.assertTrue(_wait_for_events(box),
                            "run never started; cannot preempt what never ran")
            time.sleep(0.5)
            control.request_stop()
            t.join(timeout=120)
            self.assertFalse(t.is_alive())
            self.assertIsNone(box["error"], f"unexpected: {box['error']!r}")
            stopped_out = box["outcome"]
            self.assertIsNotNone(stopped_out)
            self.assertEqual(stopped_out.trace[-1].stage, Stage.STOPPED)

            # Same engine instance, normal task: must succeed.
            out = box.get("followup_outcome")
            self.assertIsNotNone(out, "follow-up task never ran")
            self.assertTrue(out.success,
                            f"engine broken after stop: {out.error}")
            self.assertEqual(out.value, 19.0)

            # Capability store reads work (checked on the worker thread;
            # the store's connection is thread-affine).
            self.assertTrue(box.get("caps_list_ok"),
                            "capability store list() broken after stop")

            # The sqlite DB opens cleanly from a fresh connection: no
            # half-written rows break reads.
            con = sqlite3.connect(box["db"])
            try:
                self.assertEqual(
                    con.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                n = con.execute(
                    "SELECT count(*) FROM sqlite_master").fetchone()[0]
                self.assertGreater(n, 0)
            finally:
                con.close()
        finally:
            box.get("td") and box["td"].cleanup()


class TestNoControlRegression(unittest.TestCase):
    """With no control in play, handle() behaves exactly as before."""

    def test_simple_goal_success_path_unchanged(self):
        td, _db, _eng, iface = _make_iface()
        try:
            out = asyncio.run(iface.handle(
                _SIMPLE_GOAL, payload={"x": 4}, examples=_SIMPLE_EXAMPLES))
            self.assertTrue(out.success, f"regression: {out.error}")
            self.assertEqual(out.value, 19.0)
            self.assertEqual(out.error, "")
            stages = [tr.stage for tr in out.trace]
            # All 12 pipeline stages present, in order, none skipped weirdly.
            self.assertEqual(stages, [
                Stage.UNDERSTAND, Stage.DECOMPOSE, Stage.ASSESS, Stage.GAPS,
                Stage.PLAN, Stage.ACQUIRE, Stage.EXECUTE, Stage.VERIFY,
                Stage.RECOVER, Stage.LEARN, Stage.IMPROVE, Stage.PERSIST,
            ])
            self.assertNotIn(Stage.STOPPED, stages)
        finally:
            td.cleanup()

    def test_explicit_none_control_behaves_as_no_control(self):
        box = {"events": [], "outcome": None, "error": None}
        t = _run_handle_in_thread(
            _SIMPLE_GOAL, _SIMPLE_EXAMPLES, None, box, payload={"x": 4})
        try:
            t.join(timeout=120)
            self.assertFalse(t.is_alive())
            self.assertIsNone(box["error"], f"unexpected: {box['error']!r}")
            out = box["outcome"]
            self.assertTrue(out.success)
            self.assertEqual(out.value, 19.0)
        finally:
            box.get("td") and box["td"].cleanup()


class TestTouchedFilesCompile(unittest.TestCase):
    """compileall-clean on every file this track touched."""

    _TOUCHED = [
        "core/task_interface.py",
        "core/engine.py",
        "acquisition/orchestrator.py",
        "capability/growth_engine.py",
        "synthesis/composer.py",
        "cognition/synthesis.py",
        "cognition/primitive_synthesis.py",
        "acquisition/decomposition.py",
        "project/loop.py",
        "services/run_control.py",
    ]

    def test_compileall_clean(self):
        root = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "runtime"))
        # Bytecode output goes to a temp dir, never beside canonical sources:
        # the test asserts every _TOUCHED file exists and compiles clean,
        # without polluting canonical/runtime/ with .pyc files.
        with tempfile.TemporaryDirectory() as td:
            for i, rel in enumerate(self._TOUCHED):
                path = os.path.join(root, rel)
                self.assertTrue(os.path.isfile(path), f"missing: {rel}")
                cfile = os.path.join(td, "touched_%d.pyc" % i)
                py_compile.compile(path, cfile=cfile, doraise=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
