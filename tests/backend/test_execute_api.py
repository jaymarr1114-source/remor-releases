"""Causal tests for swarm_engine.services.execute_api (Contract 5 — Execute).

Real ArtifactStore on scratch sqlite, real subprocess sandbox, real
before/after directory listings. Revert-style checks neutralize the real
substrate to prove the contract actually depends on it.
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.artifacts import ArtifactStore  # noqa: E402
from swarm_engine.services.contract_types import (  # noqa: E402
    contract_unavailable, dispatch)
from swarm_engine.services.execute_api import (  # noqa: E402
    ExecuteService, routes_for_execute)


def _svc():
    td = tempfile.TemporaryDirectory()
    db = os.path.join(td.name, "artifacts.db")
    sandbox = os.path.join(td.name, "sandbox")
    store = ArtifactStore(db, sandbox)
    return td, store, ExecuteService(store, sandbox), sandbox


class TestContractTypesShape(unittest.TestCase):
    def test_unavailable_shape(self):
        u = contract_unavailable("SOME_ABSENT", "some reason", "no thing")
        self.assertFalse(u["ok"])
        self.assertEqual(u["unavailable"]["code"], "SOME_ABSENT")
        self.assertEqual(u["unavailable"]["reason"], "some reason")
        self.assertEqual(u["unavailable"]["gui"], "coming_soon")
        self.assertEqual(u["unavailable"]["missing_substrate"], "no thing")
        json.dumps(u)  # JSON-serializable


class TestExecuteCode(unittest.TestCase):
    def test_runs_real_subprocess_and_saves_revision(self):
        td, store, svc, sandbox = _svc()
        try:
            res = svc.execute_code('print("hello-execute")', name="demo")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["language"], "python")
            self.assertEqual(res["revision"], 1)
            self.assertEqual(res["returncode"], 0)
            self.assertFalse(res["timed_out"])
            self.assertIn("hello-execute", res["stdout"])
            self.assertEqual(res["generated_files"], [])
            json.dumps(res)
            # Saved as a real versioned artifact.
            got = store.get(res["artifact_id"])
            self.assertTrue(got["ok"])
            self.assertEqual(got["code"], 'print("hello-execute")')
            self.assertEqual(got["name"], "demo")
        finally:
            td.cleanup()

    def test_generated_files_is_real_listing_not_predicted(self):
        td, store, svc, sandbox = _svc()
        try:
            code = (
                "open('out.txt', 'w').write('payload-123')\n"
                "import os\n"
                "os.makedirs('sub', exist_ok=True)\n"
                "open('sub/nested.bin', 'wb').write(b'\\x00\\x01')\n"
            )
            res = svc.execute_code(code, name="writer")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["generated_files"],
                             ["out.txt", os.path.join("sub", "nested.bin")])
            # The files really exist with the bytes the code wrote.
            with open(os.path.join(sandbox, "out.txt")) as fh:
                self.assertEqual(fh.read(), "payload-123")
            with open(os.path.join(sandbox, "sub", "nested.bin"), "rb") as fh:
                self.assertEqual(fh.read(), b"\x00\x01")
            # And a run that writes nothing reports nothing (not predicted).
            res2 = svc.execute_code("x = 1", name="quiet")
            self.assertTrue(res2["ok"], res2)
            self.assertEqual(res2["generated_files"], [])
        finally:
            td.cleanup()

    def test_same_name_new_revision_rule(self):
        td, store, svc, sandbox = _svc()
        try:
            r1 = svc.execute_code("print(1)", name="loop")
            r2 = svc.execute_code("print(2)", name="loop")
            self.assertTrue(r1["ok"] and r2["ok"])
            self.assertEqual(r1["artifact_id"], r2["artifact_id"])
            self.assertEqual((r1["revision"], r2["revision"]), (1, 2))
            revs = store.revisions(r1["artifact_id"])
            self.assertEqual(len(revs["revisions"]), 2)
            self.assertEqual(revs["revisions"][0]["code"], "print(1)")
            self.assertEqual(revs["revisions"][1]["code"], "print(2)")
        finally:
            td.cleanup()

    def test_default_name_creates_new_artifact(self):
        td, store, svc, sandbox = _svc()
        try:
            r1 = svc.execute_code("print('a')")
            r2 = svc.execute_code("print('b')")
            self.assertTrue(r1["ok"] and r2["ok"])
            self.assertNotEqual(r1["artifact_id"], r2["artifact_id"])
            got = store.get(r1["artifact_id"])
            self.assertTrue(got["name"].startswith("execute-"))
        finally:
            td.cleanup()

    def test_nonzero_exit_reported_honestly(self):
        td, store, svc, sandbox = _svc()
        try:
            res = svc.execute_code("raise ValueError('boom-42')",
                                   name="fails")
            self.assertTrue(res["ok"], res)  # the run happened
            self.assertNotEqual(res["returncode"], 0)
            self.assertIn("ValueError", res["stderr"])
            self.assertIn("boom-42", res["stderr"])
        finally:
            td.cleanup()

    def test_timeout_kills_real_process(self):
        td, store, svc, sandbox = _svc()
        try:
            t0 = time.time()
            res = svc.execute_code("import time; time.sleep(60)",
                                   name="sleeper", timeout=1)
            dt = time.time() - t0
            self.assertTrue(res["ok"], res)
            self.assertTrue(res["timed_out"])
            self.assertLess(dt, 30)  # killed near the 1s timeout, not 60s
            self.assertIn("timed out after 1", res["stderr"])
        finally:
            td.cleanup()

    def test_empty_code_refused_nothing_saved(self):
        td, store, svc, sandbox = _svc()
        try:
            res = svc.execute_code("   ", name="empty")
            self.assertFalse(res["ok"])
            self.assertEqual(store.list_artifacts(), [])
        finally:
            td.cleanup()

    def test_bad_timeout_refused(self):
        td, store, svc, sandbox = _svc()
        try:
            for bad in (0, -5, "abc", None):
                res = svc.execute_code("print(1)", name="t", timeout=bad)
                self.assertFalse(res["ok"], bad)
            self.assertEqual(store.list_artifacts(), [])
        finally:
            td.cleanup()

    def test_javascript_refused_not_faked(self):
        td, store, svc, sandbox = _svc()
        try:
            res = svc.execute_code("console.log('hi')", name="js",
                                   language="javascript")
            self.assertFalse(res["ok"])
            un = res["unavailable"]
            self.assertEqual(un["code"], "EXECUTE_LANGUAGE_UNSUPPORTED")
            self.assertEqual(un["gui"], "coming_soon")
            self.assertIn("python", un["reason"])
            json.dumps(res)
            # Refused BEFORE saving: no artifact leaked into the store.
            self.assertEqual(store.list_artifacts(), [])
        finally:
            td.cleanup()

    def test_language_case_insensitive_python_ok(self):
        td, store, svc, sandbox = _svc()
        try:
            res = svc.execute_code("print('ok')", name="ci",
                                   language="Python")
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["language"], "python")
        finally:
            td.cleanup()

    def test_name_traversal_is_metadata_only(self):
        # Artifact names are sqlite TEXT, never path components: a hostile
        # name must not create files outside the sandbox.
        td, store, svc, sandbox = _svc()
        try:
            res = svc.execute_code("print('x')", name="../../evil")
            self.assertTrue(res["ok"], res)
            self.assertFalse(
                os.path.exists(os.path.join(td.name, "evil")))
            self.assertFalse(
                os.path.exists(os.path.join(os.path.dirname(td.name),
                                            "evil")))
        finally:
            td.cleanup()


class TestRevertCausality(unittest.TestCase):
    def test_neutralized_run_no_run_happens(self):
        # Revert: replace the real substrate's run with a stub that records
        # and refuses. The contract must propagate the failure, proving the
        # run path is the real ArtifactStore.run and not canned output.
        td, store, svc, sandbox = _svc()
        try:
            calls = []

            def fake_run(artifact_id, revision=None, timeout=30):
                calls.append((artifact_id, revision, timeout))
                return {"ok": False, "error": "neutralized"}

            store.run = fake_run
            res = svc.execute_code("print('never')", name="rev")
            self.assertFalse(res["ok"])
            self.assertEqual(res["error"], "neutralized")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2], 30)  # default 30s timeout passed
            # The revision is still saved per the documented rule.
            self.assertEqual(res["artifact_id"], calls[0][0])
            got = store.get(res["artifact_id"])
            self.assertTrue(got["ok"])
        finally:
            td.cleanup()

    def test_stdout_is_real_not_canned(self):
        # A value only knowable at runtime must appear in stdout.
        td, store, svc, sandbox = _svc()
        try:
            marker = f"marker-{time.time_ns()}"
            res = svc.execute_code(f"print({marker!r})", name="dyn")
            self.assertTrue(res["ok"], res)
            self.assertIn(marker, res["stdout"])
        finally:
            td.cleanup()


class TestPluginUnavailable(unittest.TestCase):
    def test_execute_plugin_honestly_unavailable(self):
        td, store, svc, sandbox = _svc()
        try:
            res = svc.execute_plugin("some-bot")
            self.assertFalse(res["ok"])
            un = res["unavailable"]
            self.assertEqual(un["code"], "PLUGIN_REGISTRY_ABSENT")
            self.assertEqual(un["gui"], "coming_soon")
            self.assertIn("plugin registry", un["missing_substrate"])
            json.dumps(res)
        finally:
            td.cleanup()


class TestRoutes(unittest.TestCase):
    def test_route_table_operational(self):
        td, store, svc_unused, sandbox = _svc()
        try:
            routes = routes_for_execute(store, sandbox)
            self.assertIn(("POST", "/api/execute"), routes)
            self.assertIn(("POST", "/api/execute/plugin"), routes)
            res = dispatch(routes, "POST", "/api/execute",
                           {"code": "print('via-route')", "name": "rt"})
            self.assertTrue(res["ok"], res)
            self.assertIn("via-route", res["stdout"])
            self.assertEqual(res["revision"], 1)
            json.dumps(res)
            # plugin route returns the typed unavailability over the wire
            res2 = dispatch(routes, "POST", "/api/execute/plugin",
                            {"plugin": "x"})
            self.assertFalse(res2["ok"])
            self.assertEqual(res2["unavailable"]["code"],
                             "PLUGIN_REGISTRY_ABSENT")
            # unknown route is an honest 404 shape
            res3 = dispatch(routes, "GET", "/api/execute", {})
            self.assertFalse(res3["ok"])
        finally:
            td.cleanup()

    def test_route_bad_timeout_json(self):
        td, store, _svc2, sandbox = _svc()
        try:
            routes = routes_for_execute(store, sandbox)
            res = dispatch(routes, "POST", "/api/execute",
                           {"code": "print(1)", "timeout": "soon"})
            self.assertFalse(res["ok"])
            json.dumps(res)
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
