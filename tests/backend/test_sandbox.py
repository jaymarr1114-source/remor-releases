"""Worker E (S13): sandbox hardening -- causal tests.

The execute path (ArtifactStore.run) applies OS-level rlimits in the
child via preexec_fn (swarm_engine/services/sandbox.py) and, with
REMOR_SANDBOX_PRIVDROP=1 on a root server, drops to an unprivileged
uid. These tests EXCEED the limits for real and pair each breach with
a no-limit control proving the limit -- not the wall-clock timeout --
is the causal mechanism.

Private work root: /tmp (via tempfile).
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.artifacts import ArtifactStore  # noqa: E402
from swarm_engine.services import sandbox as sandbox_mod  # noqa: E402


class _Env:
    """Set env vars for a test, restoring afterwards."""

    def __init__(self, test_case, **vars):
        self.tc = test_case
        self.vars = vars
        self.saved = {}

    def __enter__(self):
        for k, v in self.vars.items():
            self.saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.vars.items():
            old = self.saved[k]
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        return False


def _store(test_case):
    td = tempfile.TemporaryDirectory(prefix="sandbox_")
    test_case.addCleanup(td.cleanup)
    db = os.path.join(td.name, "a.db")
    sb = os.path.join(td.name, "sandbox")
    return ArtifactStore(db, sb), sb


class TestRlimitCpu(unittest.TestCase):
    def test_cpu_limit_kills_infinite_loop(self):
        store, _ = _store(self)
        with _Env(self, REMOR_SANDBOX_CPU_SECONDS="2"):
            s = store.save("looper", "python", "while True: pass")
            t0 = time.time()
            r = store.run(s["artifact_id"], timeout=30)
            elapsed = time.time() - t0
        # Killed by the rlimit (signal), NOT by the 30s wall-clock
        # timeout: negative returncode, timed_out False, fast.
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["timed_out"], r)
        self.assertLess(r["exit_code"], 0,
                        f"expected signal death, got {r['exit_code']}")
        self.assertLess(elapsed, 15,
                        f"rlimit kill took too long: {elapsed:.1f}s")

    def test_no_limit_control_timeout_kills_instead(self):
        # Perturbation pair: same code WITHOUT the rlimit override is
        # killed by the wall-clock timeout instead (timed_out True).
        store, _ = _store(self)
        s = store.save("looper2", "python", "while True: pass")
        t0 = time.time()
        r = store.run(s["artifact_id"], timeout=5)
        elapsed = time.time() - t0
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["timed_out"], r)
        self.assertIsNone(r["exit_code"])
        self.assertGreaterEqual(elapsed, 5)


class TestRlimitMemory(unittest.TestCase):
    HOG = "a = bytearray(256*1024*1024)\nprint('allocated-ok')"

    def test_memory_limit_kills_hog(self):
        store, _ = _store(self)
        with _Env(self, REMOR_SANDBOX_AS_MB="96"):
            s = store.save("hog", "python", self.HOG)
            r = store.run(s["artifact_id"], timeout=30)
        self.assertTrue(r["ok"], r)
        self.assertNotEqual(r["exit_code"], 0,
                            "256MB hog survived a 96MB address-space cap")
        self.assertNotIn("allocated-ok", r["stdout"])

    def test_no_limit_control_hog_succeeds(self):
        # Perturbation pair: the same allocation succeeds when the
        # address-space cap is at its generous default.
        store, _ = _store(self)
        s = store.save("hog2", "python", self.HOG)
        r = store.run(s["artifact_id"], timeout=60)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["exit_code"], 0, r["stderr"][-300:])
        self.assertIn("allocated-ok", r["stdout"])


class TestRlimitFsize(unittest.TestCase):
    def test_file_size_limit_stops_bomb(self):
        store, sb = _store(self)
        with _Env(self, REMOR_SANDBOX_FSIZE_MB="1"):
            s = store.save("bigwriter", "python",
                           "open('big.bin','wb').write(b'x'*(5*1024*1024))\n"
                           "print('wrote-it-all')")
            r = store.run(s["artifact_id"], timeout=30)
        self.assertTrue(r["ok"], r)
        self.assertNotEqual(r["exit_code"], 0,
                            "5MB write survived a 1MB file-size cap")
        self.assertNotIn("wrote-it-all", r["stdout"])
        target = os.path.join(sb, "big.bin")
        if os.path.exists(target):
            self.assertLess(os.path.getsize(target), 5 * 1024 * 1024)


class TestNormalExecutionUnaffected(unittest.TestCase):
    def test_normal_run_green_with_limits_active(self):
        store, _ = _store(self)
        with _Env(self, REMOR_SANDBOX_CPU_SECONDS="30",
                  REMOR_SANDBOX_AS_MB="512",
                  REMOR_SANDBOX_FSIZE_MB="64"):
            s = store.save(
                "calc", "python",
                "result = sum(i*i for i in range(1000))\n"
                "open('out.txt','w').write(str(result))\n"
                "print('ANSWER:', result)")
            r = store.run(s["artifact_id"], timeout=30)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["exit_code"], 0, r["stderr"][-300:])
        self.assertIn("ANSWER: 332833500", r["stdout"])
        self.assertIn("sandbox", r)
        self.assertIn("limits", r["sandbox"])
        self.assertIn("residual", r["sandbox"])


class TestPrivdrop(unittest.TestCase):
    def _traversable_sandbox(self):
        # The drop needs a traversable ancestor chain: put the sandbox
        # directly under /tmp (1777).
        sb = tempfile.mkdtemp(prefix="pd_sb_", dir="/tmp")
        self.addCleanup(lambda: __import__("shutil").rmtree(sb,
                                                            ignore_errors=True))
        return sb

    def test_drop_user_detection(self):
        found = sandbox_mod.find_drop_user()
        if os.environ.get("REMOR_SANDBOX_PRIVDROP") == "1":
            self.skipTest("outer env already opts into privdrop")
        # Default: not opted in -> no drop user reported.
        self.assertIsNone(found)

    def test_privdrop_runs_child_as_nobody(self):
        if os.geteuid() != 0:
            self.skipTest("privdrop needs a root server")
        try:
            import pwd  # noqa
            pwd.getpwnam("nobody")
        except KeyError:
            self.skipTest("no 'nobody' account on this host")
        sb = self._traversable_sandbox()
        td = tempfile.TemporaryDirectory(prefix="sandbox_pd_")
        self.addCleanup(td.cleanup)
        store = ArtifactStore(os.path.join(td.name, "a.db"), sb)
        with _Env(self, REMOR_SANDBOX_PRIVDROP="1"):
            s = store.save("pdprobe", "python",
                           "import os\n"
                           "print('UID:', os.getuid())\n"
                           "print('CWD:', os.getcwd())\n"
                           "open('gen.txt','w').write('made-by-drop')")
            r = store.run(s["artifact_id"], timeout=30)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["exit_code"], 0, r["stderr"][-300:])
        self.assertTrue(r["sandbox"]["mode"].startswith("privdrop:"),
                        r["sandbox"])
        self.assertIn("UID: 65534", r["stdout"])
        # Merged 2026-09-26 (#25): the child runs in a FRESH per-run
        # subdir of the sandbox (C's isolation), still owned/writable
        # by the drop user; generated files are promoted to the
        # sandbox root after the run (L's collectible-output
        # contract) so execute_api's before/after snapshot finds them.
        sb_real = os.path.realpath(sb)
        cwd_line = [ln for ln in r["stdout"].splitlines()
                    if ln.startswith("CWD:")][0]
        cwd = os.path.realpath(cwd_line.split("CWD:", 1)[1].strip())
        self.assertTrue(cwd.startswith(sb_real + os.sep) and cwd != sb_real,
                        f"cwd {cwd} is not a fresh subdir of {sb_real}")
        with open(os.path.join(sb, "gen.txt")) as fh:
            self.assertEqual(fh.read(), "made-by-drop")

    def test_privdrop_blocked_chain_falls_back_honestly(self):
        # Sandbox nested under a 0700 root tmpdir: the drop user cannot
        # traverse there -> honest same-user fallback, disclosed.
        if os.geteuid() != 0:
            self.skipTest("privdrop needs a root server")
        store, _ = _store(self)  # nested under 0700 TemporaryDirectory
        with _Env(self, REMOR_SANDBOX_PRIVDROP="1"):
            s = store.save("pdprobe2", "python", "print('hi-fallback')")
            r = store.run(s["artifact_id"], timeout=30)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("hi-fallback", r["stdout"])
        self.assertEqual(r["sandbox"]["mode"], "same-user")
        self.assertIn("not traversable", r["sandbox"]["note"])
        self.assertIn("fallback", r["sandbox"]["note"])


if __name__ == "__main__":
    unittest.main()
