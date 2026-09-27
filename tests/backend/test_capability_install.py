"""Worker E (S7): governed capability-install surface -- real tests.

POST /api/capabilities/install (runtime/services/capability_api.py):
  * fail-closed bearer gate: no token configured anywhere -> typed 501
    install_requires_auth (never installs over unauthenticated HTTP);
  * structural validation refuses malformed bundles with typed errors;
  * provenance recorded in the EXISTING AcquiredCodeStore (no parallel
    store invented);
  * installed code is NEVER executed as part of install (sentinel test).

Service-level tests run against a scratch sqlite DB; the HTTP tests run
against the real adapter (serve(), ephemeral port, token from
<base_dir>/api_token) proving 401-without-token / 200-with-token.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.auth import AuthGate  # noqa: E402
from swarm_engine.services.capability_api import (  # noqa: E402
    CapabilityAPI, routes_for_capabilities)
from swarm_engine.services.http_adapter import (  # noqa: E402
    close_services, serve)


def _api():
    td = tempfile.TemporaryDirectory(prefix="capinstall_")
    db = os.path.join(td.name, "caps.db")
    return td, CapabilityAPI(db)


def _gated():
    return AuthGate(base_dir=None, token="test-token-abc", provision=False)


def _ungated():
    return AuthGate(base_dir=None, provision=False)


_GOOD = {
    "name": "text helper",
    "code": "def run(text):\n    return text.strip()\n",
    "entrypoint": "run",
    "source": "unit-test",
    "effects": ["none"],
    "spec": {"arity": 1},
}


class TestInstallGate(unittest.TestCase):
    def test_no_auth_object_fails_closed_501(self):
        td, api = _api()
        try:
            routes = routes_for_capabilities(api)  # auth=None
            r = routes[("POST", "/api/capabilities/install")](dict(_GOOD))
            self.assertFalse(r["ok"])
            self.assertEqual(r["unavailable"]["code"],
                             "install_requires_auth")
            # nothing was stored
            self.assertTrue(api.list_capabilities("all")["empty"])
        finally:
            td.cleanup()

    def test_ungated_auth_fails_closed_501(self):
        td, api = _api()
        try:
            auth = _ungated()
            self.assertFalse(auth.is_gated())
            routes = routes_for_capabilities(api, auth=auth)
            r = routes[("POST", "/api/capabilities/install")](dict(_GOOD))
            self.assertFalse(r["ok"])
            self.assertEqual(r["unavailable"]["code"],
                             "install_requires_auth")
            self.assertTrue(api.list_capabilities("all")["empty"])
        finally:
            td.cleanup()

    def test_gated_auth_allows_install(self):
        # Perturbation pair for the two tests above: the SAME bundle
        # installs when the gate is configured.
        td, api = _api()
        try:
            auth = _gated()
            self.assertTrue(auth.is_gated())
            routes = routes_for_capabilities(api, auth=auth)
            r = routes[("POST", "/api/capabilities/install")](dict(_GOOD))
            self.assertTrue(r["ok"], r)
            self.assertTrue(r["installed"])
            self.assertFalse(r["executed"])
            self.assertFalse(r["replaced"])
            self.assertTrue(r["capability_id"].startswith("inst_"))
        finally:
            td.cleanup()


class TestInstallValidation(unittest.TestCase):
    def _routes(self, td_api):
        td, api = td_api
        return routes_for_capabilities(api, auth=_gated())

    def test_missing_name_refused(self):
        td, api = _api()
        try:
            r = self._routes((td, api))[("POST", "/api/capabilities/install")](
                {"code": "x", "entrypoint": "run"})
            self.assertFalse(r["ok"])
            self.assertEqual(r["code"], "install_invalid_bundle")
            self.assertEqual(r["field"], "name")
        finally:
            td.cleanup()

    def test_traversal_name_refused(self):
        td, api = _api()
        try:
            routes = self._routes((td, api))
            for bad in ("../../evil", "..\\evil", "/abs", "a/b", "x\0y"):
                r = routes[("POST", "/api/capabilities/install")](
                    {"name": bad, "code": "x", "entrypoint": "run"})
                self.assertFalse(r["ok"], bad)
                self.assertEqual(r["code"], "install_invalid_bundle")
                self.assertEqual(r["field"], "name")
            self.assertTrue(api.list_capabilities("all")["empty"])
        finally:
            td.cleanup()

    def test_missing_code_refused(self):
        td, api = _api()
        try:
            r = self._routes((td, api))[("POST", "/api/capabilities/install")](
                {"name": "ok-name", "entrypoint": "run"})
            self.assertFalse(r["ok"])
            self.assertEqual(r["field"], "code")
        finally:
            td.cleanup()

    def test_oversize_code_refused(self):
        td, api = _api()
        try:
            big = "x" * (1024 * 1024 + 1)
            r = self._routes((td, api))[("POST", "/api/capabilities/install")](
                {"name": "ok-name", "code": big, "entrypoint": "run"})
            self.assertFalse(r["ok"])
            self.assertEqual(r["field"], "code")
            self.assertIn("exceeds", r["error"])
        finally:
            td.cleanup()

    def test_bad_entrypoint_refused(self):
        td, api = _api()
        try:
            routes = self._routes((td, api))
            for bad in ("9lives", "has-dash", "has space", ""):
                r = routes[("POST", "/api/capabilities/install")](
                    {"name": "ok-name", "code": "x", "entrypoint": bad})
                self.assertFalse(r["ok"], bad)
                self.assertEqual(r["field"], "entrypoint")
        finally:
            td.cleanup()

    def test_non_dict_bundle_refused(self):
        td, api = _api()
        try:
            r = self._routes((td, api))[("POST", "/api/capabilities/install")](
                "just-a-string")
            self.assertFalse(r["ok"])
            self.assertEqual(r["field"], "bundle")
        finally:
            td.cleanup()


class TestInstallProvenance(unittest.TestCase):
    def test_installed_listed_get_download_roundtrip(self):
        td, api = _api()
        try:
            routes = routes_for_capabilities(api, auth=_gated())
            r = routes[("POST", "/api/capabilities/install")](dict(_GOOD))
            cid = r["capability_id"]
            ids = {c["capability_id"]
                   for c in api.list_capabilities("all")["capabilities"]}
            self.assertIn(cid, ids)
            got = api.get_capability(cid)
            self.assertTrue(got["ok"])
            self.assertEqual(got["capability"]["name"], "text helper")
            dl = api.download_capability(cid)
            self.assertTrue(dl["ok"])
            data = base64.b64decode(dl["download"]["content_base64"])
            self.assertEqual(data.decode("utf-8"), _GOOD["code"])
            self.assertTrue(dl["download"]["filename"].endswith(".py"))
        finally:
            td.cleanup()

    def test_provenance_evidence_recorded(self):
        td, api = _api()
        try:
            routes = routes_for_capabilities(api, auth=_gated())
            r = routes[("POST", "/api/capabilities/install")](dict(_GOOD))
            entry = api.code.get("text helper")
            self.assertIsNotNone(entry)
            self.assertEqual(entry["capability_id"], r["capability_id"])
            ev = entry["evidence"]
            self.assertEqual(ev["installed_via"], "http_install")
            self.assertTrue(ev["authenticated"])
            self.assertTrue(ev["never_executed"])
            self.assertEqual(
                ev["sha256"],
                hashlib.sha256(_GOOD["code"].encode("utf-8")).hexdigest())
        finally:
            td.cleanup()

    def test_install_never_executes(self):
        # The installed payload writes a sentinel file at import/run
        # time; after install the sentinel must NOT exist.
        td, api = _api()
        try:
            sentinel = os.path.join(td.name, "SENTINEL_WAS_EXECUTED")
            routes = routes_for_capabilities(api, auth=_gated())
            r = routes[("POST", "/api/capabilities/install")](
                {"name": "sneaky",
                 "code": f"open({sentinel!r}, 'w').write('pwned')\n"
                         f"def run():\n    open({sentinel!r}, 'w').write('pwned')\n",
                 "entrypoint": "run"})
            self.assertTrue(r["ok"], r)
            self.assertFalse(os.path.exists(sentinel),
                             "install executed the submitted code")
        finally:
            td.cleanup()

    def test_reinstall_same_name_replaces(self):
        td, api = _api()
        try:
            routes = routes_for_capabilities(api, auth=_gated())
            r1 = routes[("POST", "/api/capabilities/install")](dict(_GOOD))
            r2 = routes[("POST", "/api/capabilities/install")](
                {"name": "text helper", "code": "def run(t):\n    return t\n",
                 "entrypoint": "run"})
            self.assertTrue(r2["ok"])
            self.assertTrue(r2["replaced"])
            self.assertNotEqual(r1["capability_id"], r2["capability_id"])
            # latest bytes win
            dl = api.download_capability(r2["capability_id"])
            data = base64.b64decode(dl["download"]["content_base64"])
            self.assertIn("return t", data.decode("utf-8"))
        finally:
            td.cleanup()


class TestInstallHttp(unittest.TestCase):
    """Over live HTTP: the bearer gate (401) sits in front of the
    fail-closed install handler."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="remor_gapfill_e_",
                                              dir="/tmp")
        cls.base = cls.tmp.name
        cls.server, cls.ff, cls.thread, cls.url = serve(cls.base)
        with open(os.path.join(cls.base, "api_token"),
                  encoding="utf-8") as fh:
            cls.token = fh.read().strip()
        assert cls.token
        # the served gate really is gated -> install must not 501 here
        assert cls.ff["auth"].is_gated()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
        finally:
            try:
                close_services(cls.ff)
            finally:
                cls.tmp.cleanup()

    def req(self, method, path, body=None, token="default"):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token == "default":
            headers["Authorization"] = "Bearer " + self.token
        elif token is not None:
            headers["Authorization"] = "Bearer " + token
        rq = urllib.request.Request(self.url + path, data=data,
                                    method=method, headers=headers)
        try:
            with urllib.request.urlopen(rq, timeout=60) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read().decode() or "{}")
            except Exception:
                payload = {}
            return e.code, payload

    def test_install_without_token_is_401(self):
        st, body = self.req("POST", "/api/capabilities/install",
                            dict(_GOOD), token=None)
        self.assertEqual(st, 401)
        self.assertEqual(body.get("code"), "auth_required")

    def test_install_with_bad_token_is_401(self):
        st, body = self.req("POST", "/api/capabilities/install",
                            dict(_GOOD), token="wrong-token")
        self.assertEqual(st, 401)
        self.assertEqual(body.get("code"), "auth_invalid")

    def test_install_with_token_succeeds_and_lists(self):
        bundle = dict(_GOOD)
        bundle["name"] = "http installed cap"
        st, body = self.req("POST", "/api/capabilities/install", bundle)
        self.assertEqual(st, 200, body)
        self.assertTrue(body["ok"])
        self.assertFalse(body["executed"])
        cid = body["capability_id"]
        st, body = self.req("POST", "/api/capabilities/list",
                            {"status": "all"})
        self.assertEqual(st, 200)
        self.assertIn(cid, {c["capability_id"]
                            for c in body["capabilities"]})
        # and the installed bytes download back intact
        st, body = self.req("POST", "/api/capabilities/download",
                            {"capability_id": cid})
        self.assertEqual(st, 200)
        data = base64.b64decode(body["download"]["content_base64"])
        self.assertEqual(data.decode("utf-8"), bundle["code"])

    def test_install_malformed_over_http_is_400(self):
        st, body = self.req("POST", "/api/capabilities/install",
                            {"name": "../x", "code": "y",
                             "entrypoint": "run"})
        # _contract_result maps the typed refusal via _status_for -> 400
        self.assertEqual(st, 400, body)
        self.assertFalse(body["ok"])
        self.assertEqual(body.get("code"), "install_invalid_bundle")


if __name__ == "__main__":
    unittest.main()
