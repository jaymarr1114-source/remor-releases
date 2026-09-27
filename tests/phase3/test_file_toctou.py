"""Phase 3 TOCTOU hardening battery for ScopedFileService.

Real threads, real symlinks, real races -- no simulation. The attacker is a
live thread flipping symlink <-> real components on a real filesystem while
victim threads hammer the service. Every refusal below is observed.

Semantics under test (see runtime/services/files.py docstring):
  * _resolve() canonicalizes static symlinks BEFORE the open, so static
    inside-pointing symlinks (final or intermediate) keep working, and
    static outside-pointing ones are refused at check time -- unchanged.
  * A symlink that APPEARS after the check (final or intermediate
    component) trips O_NOFOLLOW at open and is refused with
    "symlink encountered at open (possible race)" -- even when it points
    inside the root, because at open time it is indistinguishable from a
    race-planted escape.

Layout:
  TestStaticSymlinkParity   - static symlinks: inside works, outside refused
  TestIntermediateSymlink  - non-final symlink components still work
  TestOpenTimeRefusal      - deterministic window: symlink swapped in between
                             the real _resolve() and the real open is refused,
                             inside- or outside-pointing
  TestDeterministicWindow  - hook-driven window for every public op + a
                             positive control showing an UNHARDENED
                             check-then-open falls to the same primitive
  TestStochasticRaces      - threaded symlink-swap races, 7500 victim ops
  TestWriteLock            - the per-instance RLock really serializes writers
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.files import (  # noqa: E402
    ELOOP_REFUSAL,
    ScopedFileService,
    _AtomicOpenRefused,
)

INSIDE_BYTES = "INSIDE-BYTES-marker-7f3a"
OUTSIDE_SECRET = "OUTSIDE-SECRET-marker-9e1c"


def _write(path, data):
    with open(path, "w") as fh:
        fh.write(data)


def _read(path):
    with open(path) as fh:
        return fh.read()


class _Tree:
    """Real two-tree fixture: root/ (in scope) and outside/ (out of scope)."""

    def __init__(self):
        self.td = tempfile.TemporaryDirectory()
        self.od = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.td.name)
        self.outside = os.path.realpath(self.od.name)
        os.makedirs(os.path.join(self.root, "sub"))
        _write(os.path.join(self.root, "sub", "victim.txt"), INSIDE_BYTES)
        _write(os.path.join(self.outside, "victim.txt"), OUTSIDE_SECRET)
        self._outside_snapshot = self._snapshot_outside()

    def _snapshot_outside(self):
        snap = {}
        for dirpath, _dirnames, filenames in os.walk(self.outside):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                with open(p, "rb") as fh:
                    snap[os.path.relpath(p, self.outside)] = fh.read()
        return snap

    def assert_outside_untouched(self, extra_msg=""):
        snap = self._snapshot_outside()
        assert snap == self._outside_snapshot, (
            "OUTSIDE TREE MODIFIED %s:\nbefore=%r\nafter=%r"
            % (extra_msg, self._outside_snapshot, snap)
        )

    def cleanup(self):
        self.td.cleanup()
        self.od.cleanup()


class TestStaticSymlinkParity(unittest.TestCase):
    """Static symlinks behave exactly as before hardening: inside-pointing
    (final or intermediate) works; outside-pointing is refused at check."""

    def setUp(self):
        self.t = _Tree()
        _write(os.path.join(self.t.root, "inner.txt"), "inner-data")
        os.symlink("inner.txt", os.path.join(self.t.root, "final_inside"))
        os.symlink(os.path.join(self.t.outside, "victim.txt"),
                   os.path.join(self.t.root, "final_outside"))
        os.makedirs(os.path.join(self.t.root, "realdir"))
        os.symlink("realdir", os.path.join(self.t.root, "final_dir_inside"))
        os.symlink("sub", os.path.join(self.t.root, "linkdir"))
        self.svc = ScopedFileService(self.t.root, writable=True)

    def tearDown(self):
        self.t.cleanup()

    def test_static_final_inside_symlink_read_works(self):
        r = self.svc.read_text("final_inside")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["content"], "inner-data")

    def test_static_final_inside_symlink_write_works(self):
        r = self.svc.write_text("final_inside", "updated")
        self.assertTrue(r["ok"], r)
        self.assertEqual(_read(os.path.join(self.t.root, "inner.txt")),
                         "updated")
        self.t.assert_outside_untouched("static inside symlink write")

    def test_static_final_dir_inside_symlink_list_works(self):
        _write(os.path.join(self.t.root, "realdir", "f.txt"), "x")
        r = self.svc.list_dir("final_dir_inside")
        self.assertTrue(r["ok"], r)
        self.assertEqual([e["name"] for e in r["entries"]], ["f.txt"])

    def test_static_intermediate_symlink_read_write_stat(self):
        r = self.svc.read_text("linkdir/victim.txt")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["content"], INSIDE_BYTES)
        w = self.svc.write_text("linkdir/new.txt", "via link")
        self.assertTrue(w["ok"], w)
        s = self.svc.stat("linkdir/new.txt")
        self.assertTrue(s["ok"], s)
        self.assertFalse(s["is_dir"])
        self.t.assert_outside_untouched("static intermediate symlink")

    def test_static_final_outside_symlink_refused(self):
        r = self.svc.read_text("final_outside")
        self.assertFalse(r["ok"])
        self.assertNotIn(OUTSIDE_SECRET, str(r))
        w = self.svc.write_text("final_outside", "PWNED")
        self.assertFalse(w["ok"])
        self.t.assert_outside_untouched("static outside symlink write")


class TestIntermediateSymlink(unittest.TestCase):
    """Non-final symlink components still work for read/write/list/stat,
    and mkdir resolves through them (resolved components are real dirs)."""

    def setUp(self):
        self.t = _Tree()
        os.symlink("sub", os.path.join(self.t.root, "linkdir"))
        self.svc = ScopedFileService(self.t.root, writable=True)

    def tearDown(self):
        self.t.cleanup()

    def test_read_through_intermediate_symlink(self):
        r = self.svc.read_text("linkdir/victim.txt")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["content"], INSIDE_BYTES)

    def test_write_through_intermediate_symlink(self):
        w = self.svc.write_text("linkdir/new.txt", "via link")
        self.assertTrue(w["ok"], w)
        r = self.svc.read_text("sub/new.txt")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["content"], "via link")
        self.t.assert_outside_untouched("write through intermediate symlink")

    def test_stat_through_intermediate_symlink(self):
        r = self.svc.stat("linkdir/victim.txt")
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["is_dir"])

    def test_mkdir_through_intermediate_symlink(self):
        # _resolve() canonicalizes linkdir -> sub, so the walked components
        # are real dirs; the mkdir lands inside the root.
        r = self.svc.mkdir("linkdir/newdir")
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.isdir(os.path.join(self.t.root, "sub",
                                                   "newdir")))
        self.t.assert_outside_untouched("mkdir through intermediate symlink")

    def test_list_dir_root_still_lists_link(self):
        r = self.svc.list_dir("")
        self.assertTrue(r["ok"], r)
        names = [e["name"] for e in r["entries"]]
        self.assertIn("linkdir", names)


class _WindowBase(unittest.TestCase):
    """Deterministic window placement: run a public op with a real swap
    injected between the service's own _resolve() and its open."""

    def setUp(self):
        self.t = _Tree()
        self.svc = ScopedFileService(self.t.root, writable=True)

    def tearDown(self):
        self.t.cleanup()

    def _with_window(self, rel, op, swap, unswap):
        svc = self.svc
        orig_resolve = svc._resolve

        def hooked(r):
            resolved = orig_resolve(r)
            swap()  # attacker lands exactly in the check->open window
            return resolved

        svc._resolve = hooked
        try:
            return op()
        finally:
            svc._resolve = orig_resolve
            unswap()

    def _swap_link(self, link, target):
        """Return (swap, unswap) flipping `link` to a symlink -> target."""
        parked = link + ".winpark"

        def swap():
            os.rename(link, parked)
            os.symlink(target, link)

        def unswap():
            try:
                if os.path.islink(link):
                    os.unlink(link)
            finally:
                if os.path.lexists(parked):
                    os.rename(parked, link)

        return swap, unswap


class TestOpenTimeRefusal(_WindowBase):
    """A symlink appearing AFTER the check is refused at open -- even when
    it points inside the root (indistinguishable from a race-planted one)."""

    def test_final_swap_to_outside_symlink_read_refused(self):
        race = os.path.join(self.t.root, "race")
        _write(race, INSIDE_BYTES)
        swap, unswap = self._swap_link(
            race, os.path.join(self.t.outside, "victim.txt"))
        r = self._with_window("race", lambda: self.svc.read_text("race"),
                              swap, unswap)
        self.assertFalse(r["ok"])
        self.assertIn(ELOOP_REFUSAL, r["error"])
        self.assertNotIn(OUTSIDE_SECRET, str(r))

    def test_final_swap_to_inside_symlink_read_refused(self):
        # The swapped-in symlink points INSIDE the root -- still refused:
        # at open time it cannot be told apart from a race-planted escape.
        race = os.path.join(self.t.root, "race")
        _write(race, INSIDE_BYTES)
        swap, unswap = self._swap_link(
            race, os.path.join(self.t.root, "sub", "victim.txt"))
        r = self._with_window("race", lambda: self.svc.read_text("race"),
                              swap, unswap)
        self.assertFalse(r["ok"])
        self.assertIn(ELOOP_REFUSAL, r["error"])

    def test_final_swap_to_outside_symlink_write_refused(self):
        race = os.path.join(self.t.root, "race")
        _write(race, INSIDE_BYTES)
        swap, unswap = self._swap_link(
            race, os.path.join(self.t.outside, "victim.txt"))
        r = self._with_window(
            "race", lambda: self.svc.write_text("race", "PWNED"), swap, unswap)
        self.assertFalse(r["ok"])
        self.assertIn(ELOOP_REFUSAL, r["error"])
        # the outside file was NOT truncated/overwritten by the O_TRUNC open
        self.assertEqual(_read(os.path.join(self.t.outside, "victim.txt")),
                         OUTSIDE_SECRET)
        self.t.assert_outside_untouched("final-swap write window")

    def test_final_swap_stat_and_list_refused(self):
        race = os.path.join(self.t.root, "race")
        _write(race, INSIDE_BYTES)
        for op in (lambda: self.svc.stat("race"),
                   lambda: self.svc.list_dir("race")):
            swap, unswap = self._swap_link(
                race, os.path.join(self.t.outside, "victim.txt"))
            r = self._with_window("race", op, swap, unswap)
            self.assertFalse(r["ok"])
            self.assertIn(ELOOP_REFUSAL, r["error"])

    def test_intermediate_swap_mkdir_refused(self):
        # resolve() sees real sub; the walk must trip on the swapped symlink.
        swap, unswap = self._swap_link(
            os.path.join(self.t.root, "sub"), self.t.outside)
        r = self._with_window(
            "sub/a/b", lambda: self.svc.mkdir("sub/a/b"), swap, unswap)
        self.assertFalse(r["ok"])
        self.assertIn(ELOOP_REFUSAL, r["error"])
        self.t.assert_outside_untouched("mkdir window")

    def test_open_resolved_direct_window(self):
        # Drive the hardened open primitive itself, bypassing the public API.
        race = os.path.join(self.t.root, "race")
        _write(race, INSIDE_BYTES)
        resolved = self.svc._resolve("race")
        swap, unswap = self._swap_link(
            race, os.path.join(self.t.outside, "victim.txt"))
        swap()
        try:
            with self.assertRaises(_AtomicOpenRefused) as cm:
                self.svc._open_resolved(resolved, os.O_RDONLY)
            self.assertIn(ELOOP_REFUSAL, str(cm.exception))
            with self.assertRaises(_AtomicOpenRefused):
                self.svc._open_resolved(
                    resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
        finally:
            unswap()
        self.t.assert_outside_untouched("direct _open_resolved window")


class TestDeterministicWindow(_WindowBase):
    """Hook-driven window for the intermediate-component case on every op,
    plus the positive control: an UNHARDENED check-then-open falls to the
    same deterministic primitive (proving these tests are not vacuous)."""

    def _intermediate(self, rel):
        return self._swap_link(os.path.join(self.t.root, "sub"),
                               self.t.outside)

    def test_read_window_refused(self):
        swap, unswap = self._intermediate("sub/victim.txt")
        r = self._with_window(
            "sub/victim.txt", lambda: self.svc.read_text("sub/victim.txt"),
            swap, unswap)
        self.assertFalse(r["ok"])
        self.assertNotIn(OUTSIDE_SECRET, str(r))

    def test_write_window_refused(self):
        swap, unswap = self._intermediate("sub/pwned.txt")
        r = self._with_window(
            "sub/pwned.txt",
            lambda: self.svc.write_text("sub/pwned.txt", "PWNED"),
            swap, unswap)
        self.assertFalse(r["ok"])
        self.t.assert_outside_untouched("deterministic write window")

    def test_list_dir_window_refused(self):
        swap, unswap = self._intermediate("sub")
        r = self._with_window(
            "sub", lambda: self.svc.list_dir("sub"), swap, unswap)
        self.assertFalse(r["ok"])

    def test_stat_window_refused(self):
        swap, unswap = self._intermediate("sub/victim.txt")
        r = self._with_window(
            "sub/victim.txt", lambda: self.svc.stat("sub/victim.txt"),
            swap, unswap)
        self.assertFalse(r["ok"])

    def test_positive_control_naive_check_then_open_falls(self):
        """The SAME deterministic window defeats a naive check-then-open.

        This proves the attack primitive is real and the tests above are
        not vacuous: without O_NOFOLLOW + fd pinning, the swap wins.
        """
        svc = self.svc
        link = os.path.join(self.t.root, "sub")
        parked = link + ".pcpark"

        def place():
            os.rename(link, parked)
            os.symlink(self.t.outside, link)

        def remove():
            os.unlink(link)
            os.rename(parked, link)

        # naive write: check, swap, plain open
        path = svc._resolve("sub/pwned.txt")
        place()
        try:
            with open(path, "w") as fh:
                fh.write("PWNED-BY-PRIMITIVE")
        finally:
            remove()
        leaked = os.path.join(self.t.outside, "pwned.txt")
        self.assertTrue(os.path.exists(leaked),
                        "positive control failed: naive write did not escape")
        self.assertEqual(_read(leaked), "PWNED-BY-PRIMITIVE")
        os.unlink(leaked)  # clean the deliberately-planted outside file

        # naive read: check, swap, plain open
        path = svc._resolve("sub/victim.txt")
        place()
        try:
            with open(path) as fh:
                leaked_bytes = fh.read()
        finally:
            remove()
        self.assertEqual(leaked_bytes, OUTSIDE_SECRET,
                         "positive control failed: naive read did not escape")


class _Swapper:
    """Flaps `link` between a real fs object and a symlink -> outside.

    Each cycle uses a unique parked name so a victim that recreates `link`
    mid-cycle cannot wedge the attacker. Tolerant of races by construction.
    """

    def __init__(self, link, outside_target):
        self.link = link
        self.outside_target = outside_target
        self._stop = threading.Event()
        self._n = 0
        self.flaps = 0

    def _one_flap(self):
        self._n += 1
        parked = "%s.parked.%d" % (self.link, self._n)
        os.rename(self.link, parked)  # atomic: link now absent
        try:
            os.symlink(self.outside_target, self.link)  # hostile state
            self.flaps += 1
        finally:
            try:
                if os.path.islink(self.link):
                    os.unlink(self.link)
            except OSError:
                pass
            try:
                if os.path.lexists(parked) and not os.path.lexists(self.link):
                    os.rename(parked, self.link)
            except OSError:
                pass  # victim recreated link; parked dir cleaned at teardown

    def run(self):
        while not self._stop.is_set():
            try:
                self._one_flap()
            except OSError:
                pass

    def stop(self):
        self._stop.set()

    def restore(self):
        d = os.path.dirname(self.link)
        base = os.path.basename(self.link)
        try:
            for entry in os.listdir(d):
                if entry.startswith(base + ".parked."):
                    p = os.path.join(d, entry)
                    try:
                        if os.path.isdir(p) and not os.path.islink(p):
                            shutil.rmtree(p)
                        else:
                            os.unlink(p)
                    except OSError:
                        pass
        except OSError:
            pass
        try:
            if os.path.islink(self.link):
                os.unlink(self.link)
        except OSError:
            pass


class TestStochasticRaces(unittest.TestCase):
    """Live threaded races: attacker flaps symlink<->real while victims
    hammer the service. No outside byte may ever be read; no outside file
    may ever be created, truncated, or modified."""

    def setUp(self):
        self.t = _Tree()
        self.svc = ScopedFileService(self.t.root, writable=True)

    def tearDown(self):
        self.t.cleanup()

    def _run_race(self, victim_fn, link, outside_target, ops):
        swapper = _Swapper(link, outside_target)
        attacker = threading.Thread(target=swapper.run, daemon=True)
        stats = {"ok": 0, "eloop": 0, "refused_other": 0, "errors": [],
                 "reasons": {}}
        lock = threading.Lock()

        def victim():
            for i in range(ops):
                try:
                    outcome = victim_fn(i)
                except Exception as exc:  # never expected: hard failure
                    with lock:
                        stats["errors"].append(repr(exc))
                    continue
                with lock:
                    if outcome == "ok":
                        stats["ok"] += 1
                    elif outcome == "eloop":
                        stats["eloop"] += 1
                    else:
                        stats["refused_other"] += 1
                        stats["reasons"][outcome] = \
                            stats["reasons"].get(outcome, 0) + 1

        t0 = time.time()
        attacker.start()
        vt = threading.Thread(target=victim)
        vt.start()
        vt.join()
        swapper.stop()
        attacker.join(timeout=10)
        elapsed = time.time() - t0
        swapper.restore()
        if os.path.islink(link):
            os.unlink(link)
        return stats, elapsed, swapper.flaps

    def _report(self, name, stats, ops, flaps, elapsed):
        # reason buckets, most frequent first
        reasons = ", ".join(
            "%s=%d" % kv for kv in sorted(stats["reasons"].items(),
                                          key=lambda kv: -kv[1]))
        print("\n[race] %s: ops=%d ok=%d eloop=%d refused_other=%d "
              "flaps=%d elapsed=%.2fs reasons={%s}" % (
                  name, ops, stats["ok"], stats["eloop"],
                  stats["refused_other"], flaps, elapsed, reasons))

    def _classify(self, r):
        # every refusal lands in exactly one bucket; ELOOP is its own
        if ELOOP_REFUSAL in r["error"]:
            return "eloop"
        return r["error"]

    # -- intermediate-component swap: read race --------------------------
    def test_intermediate_swap_read_race(self):
        ops = 2000

        def victim_fn(i):
            r = self.svc.read_text("sub/victim.txt")
            if r["ok"]:
                # THE property: only inside bytes, ever.
                assert r["content"] == INSIDE_BYTES, (
                    "ESCAPE: read returned %r" % r["content"][:60])
                return "ok"
            return self._classify(r)

        stats, elapsed, flaps = self._run_race(
            victim_fn, os.path.join(self.t.root, "sub"), self.t.outside, ops)
        self.assertEqual(stats["errors"], [])
        self.assertEqual(stats["ok"] + stats["eloop"] + stats["refused_other"],
                         ops)
        self.t.assert_outside_untouched("read race")
        self._report("intermediate-swap READ", stats, ops, flaps, elapsed)

    # -- intermediate-component swap: write race -------------------------
    def test_intermediate_swap_write_race(self):
        ops = 2000

        def victim_fn(i):
            payload = "PAYLOAD-%d" % i
            r = self.svc.write_text("sub/out.txt", payload)
            if r["ok"]:
                return "ok"
            return self._classify(r)

        stats, elapsed, flaps = self._run_race(
            victim_fn, os.path.join(self.t.root, "sub"), self.t.outside, ops)
        self.assertEqual(stats["errors"], [])
        self.t.assert_outside_untouched("write race")
        # in-scope file holds exactly one victim payload (no outside bytes)
        p = os.path.join(self.t.root, "sub", "out.txt")
        if os.path.exists(p):
            content = _read(p)
            self.assertRegex(content, r"^PAYLOAD-\d+$")
        self._report("intermediate-swap WRITE", stats, ops, flaps, elapsed)

    # -- final-component swap: read race ---------------------------------
    def test_final_swap_read_race(self):
        ops = 1500
        race = os.path.join(self.t.root, "race")
        _write(race, INSIDE_BYTES)

        def victim_fn(i):
            r = self.svc.read_text("race")
            if r["ok"]:
                assert r["content"] == INSIDE_BYTES, (
                    "ESCAPE: read returned %r" % r["content"][:60])
                return "ok"
            return self._classify(r)

        stats, elapsed, flaps = self._run_race(
            victim_fn, race, os.path.join(self.t.outside, "victim.txt"), ops)
        self.assertEqual(stats["errors"], [])
        self.t.assert_outside_untouched("final-swap read race")
        self._report("final-swap READ", stats, ops, flaps, elapsed)

    # -- final-component swap: write race --------------------------------
    def test_final_swap_write_race(self):
        # Without O_NOFOLLOW, open(O_TRUNC) would truncate the outside file
        # the moment the swap lands in the window. The canary must survive.
        ops = 1500
        race = os.path.join(self.t.root, "race")
        _write(race, INSIDE_BYTES)

        def victim_fn(i):
            r = self.svc.write_text("race", "PAYLOAD-%d" % i)
            if r["ok"]:
                return "ok"
            return self._classify(r)

        stats, elapsed, flaps = self._run_race(
            victim_fn, race, os.path.join(self.t.outside, "victim.txt"), ops)
        self.assertEqual(stats["errors"], [])
        self.t.assert_outside_untouched("final-swap write race")
        self.assertEqual(
            _read(os.path.join(self.t.outside, "victim.txt")), OUTSIDE_SECRET)
        self._report("final-swap WRITE", stats, ops, flaps, elapsed)

    # -- intermediate-component swap: mkdir race -------------------------
    def test_intermediate_swap_mkdir_race(self):
        ops = 500

        def victim_fn(i):
            r = self.svc.mkdir("sub/mk%d" % i)
            if r["ok"]:
                return "ok"
            return self._classify(r)

        stats, elapsed, flaps = self._run_race(
            victim_fn, os.path.join(self.t.root, "sub"), self.t.outside, ops)
        self.assertEqual(stats["errors"], [])
        self.t.assert_outside_untouched("mkdir race")
        self._report("intermediate-swap MKDIR", stats, ops, flaps, elapsed)


class TestWriteLock(unittest.TestCase):
    """The per-instance RLock really serializes the mutating sequence."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.svc = ScopedFileService(self.td.name, writable=True)

    def tearDown(self):
        self.td.cleanup()

    def test_mutating_op_blocks_while_lock_held(self):
        self.svc._write_lock.acquire()
        done = threading.Event()
        errors = []

        def writer():
            try:
                r = self.svc.write_text("x.txt", "hi")
                assert r["ok"], r
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                done.set()

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        self.assertFalse(done.wait(timeout=2),
                         "write_text did not block on the held write lock")
        self.svc._write_lock.release()
        t.join(timeout=10)
        self.assertTrue(done.is_set())
        self.assertEqual(errors, [])
        self.assertEqual(_read(os.path.join(self.td.name, "x.txt")), "hi")

    def test_mkdir_blocks_while_lock_held(self):
        self.svc._write_lock.acquire()
        done = threading.Event()
        t = threading.Thread(
            target=lambda: (self.svc.mkdir("a/b"), done.set()), daemon=True)
        t.start()
        self.assertFalse(done.wait(timeout=2),
                         "mkdir did not block on the held write lock")
        self.svc._write_lock.release()
        t.join(timeout=10)
        self.assertTrue(done.is_set())
        self.assertTrue(os.path.isdir(os.path.join(self.td.name, "a", "b")))

    def test_concurrent_writers_never_interleave(self):
        # Serialized writers: the shared file always holds exactly one
        # complete payload, never a torn mix.
        errs = []

        def writer(ch, n):
            for _ in range(n):
                try:
                    r = self.svc.write_text("shared.txt", ch * 3000)
                    assert r["ok"], r
                except Exception as exc:
                    errs.append(repr(exc))

        threads = [threading.Thread(target=writer, args=(ch, 100))
                   for ch in ("A", "B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        self.assertEqual(errs, [])
        content = _read(os.path.join(self.td.name, "shared.txt"))
        self.assertIn(content, ("A" * 3000, "B" * 3000),
                      "torn write observed (%d bytes)" % len(content))


if __name__ == "__main__":
    unittest.main()
