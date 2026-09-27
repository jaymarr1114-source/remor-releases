"""Causal tests for swarm_engine.services.routing.ConversationRouter.

Real sqlite record store; project existence is checked against the real
ProjectService store (blank projects ingested for real). Scratch DBs
under tempfile; nothing touches production state.

Adversarial coverage:
  * create_chat for a nonexistent project -> refused (never fabricated).
  * route_conversation to a nonexistent chat -> refused.
  * traversal / SQL-injection-shaped conversation ids are inert data:
    stored verbatim, tables intact.
  * a failing project_exists callable -> honest error, not a crash.
  * resolve_conversation never guesses: unknown -> "unrouted".
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(
    os.path.join(_HERE, "..", "..", "pylib")))

from swarm_engine.services.projects import ProjectService  # noqa: E402
from swarm_engine.services.routing import (  # noqa: E402
    ConversationRouter,
    routes_for_routing,
)


def _make_projects(tmpdir):
    svc = ProjectService(os.path.join(tmpdir, "svc.db"),
                         os.path.join(tmpdir, "projects"),
                         engine=None)
    pa = svc.create({"kind": "blank", "project_id": "projA"})
    pb = svc.create({"kind": "blank", "project_id": "projB"})
    assert pa["ok"] and pb["ok"], (pa, pb)
    return svc


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = _make_projects(self.tmp.name)
        exists = lambda pid: self.svc.get_project(pid)["ok"]  # noqa: E731
        self.router = ConversationRouter(
            os.path.join(self.tmp.name, "routing.db"), exists)

    def tearDown(self):
        try:
            self.router.close()
        finally:
            self.tmp.cleanup()

    # -- chats belong to real projects -----------------------------------
    def test_create_chat_real_project(self):
        r = self.router.create_chat("projA", "chat A1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["project_id"], "projA")
        self.assertEqual(r["chat_label"], "chat A1")
        self.assertTrue(r["chat_id"])

        listed = self.router.list_project_chats("projA")
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(len(listed["chats"]), 1)
        self.assertEqual(listed["chats"][0]["chat_id"], r["chat_id"])

        # Project B has no chats yet — per-project isolation of listing.
        self.assertEqual(
            self.router.list_project_chats("projB")["chats"], [])

    def test_create_chat_unknown_project_refused(self):
        r = self.router.create_chat("projNOPE", "ghost chat")
        self.assertFalse(r["ok"])
        self.assertIn("unknown project", r["error"])
        # Nothing was recorded anywhere.
        self.assertFalse(self.router.list_project_chats("projNOPE")["ok"])

    def test_create_chat_bad_label_refused(self):
        self.assertFalse(self.router.create_chat("projA", "")["ok"])
        self.assertFalse(self.router.create_chat("projA", "   ")["ok"])

    def test_list_chats_unknown_project_refused(self):
        r = self.router.list_project_chats("projNOPE")
        self.assertFalse(r["ok"])
        self.assertIn("unknown project", r["error"])

    # -- explicit routing -------------------------------------------------
    def test_route_and_resolve(self):
        a1 = self.router.create_chat("projA", "chat A1")["chat_id"]
        b1 = self.router.create_chat("projB", "chat B1")["chat_id"]

        # Unknown conversation: honest unrouted, never guessed.
        un = self.router.resolve_conversation("conv-main-1")
        self.assertTrue(un["ok"])
        self.assertFalse(un["routed"])
        self.assertEqual(un["state"], "unrouted")

        r = self.router.route_conversation("conv-main-1", a1)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["project_id"], "projA")
        self.assertEqual(r["chat_label"], "chat A1")

        got = self.router.resolve_conversation("conv-main-1")
        self.assertTrue(got["routed"])
        self.assertEqual(got["project_id"], "projA")
        self.assertEqual(got["chat_id"], a1)

        # Explicit re-route to another project's chat: allowed, and the
        # resolution reflects the latest explicit mapping.
        r2 = self.router.route_conversation("conv-main-1", b1)
        self.assertTrue(r2["ok"], r2)
        got2 = self.router.resolve_conversation("conv-main-1")
        self.assertEqual(got2["project_id"], "projB")
        self.assertEqual(got2["chat_id"], b1)

    def test_route_to_unknown_chat_refused(self):
        r = self.router.route_conversation("conv-x", "no-such-chat")
        self.assertFalse(r["ok"])
        self.assertIn("unknown chat", r["error"])
        # And it stayed unrouted.
        self.assertFalse(
            self.router.resolve_conversation("conv-x")["routed"])

    def test_route_bad_ids_refused(self):
        self.assertFalse(
            self.router.route_conversation("", "whatever")["ok"])
        self.assertFalse(
            self.router.resolve_conversation("")["ok"])

    # -- adversarial ids are inert data -----------------------------------
    def test_hostile_conversation_ids_are_inert(self):
        a1 = self.router.create_chat("projA", "chat A1")["chat_id"]
        hostile = [
            "../../../etc/passwd",
            "'; DROP TABLE project_chats;--",
            "conv\nwith\nnewlines",
            "x" * 500,
        ]
        for cid in hostile:
            r = self.router.route_conversation(cid, a1)
            self.assertTrue(r["ok"], (cid, r))
            got = self.router.resolve_conversation(cid)
            self.assertTrue(got["routed"], cid)
            self.assertEqual(got["conversation_id"], cid)
            self.assertEqual(got["chat_id"], a1)

        # Tables intact: chats still listed, routes all present.
        listed = self.router.list_project_chats("projA")
        self.assertTrue(listed["ok"])
        self.assertEqual(len(listed["chats"]), 1)
        n = sqlite3.connect(
            self.router.db_path).execute(
                "SELECT COUNT(*) FROM conversation_routes").fetchone()[0]
        self.assertEqual(n, len(hostile))

    def test_project_exists_failure_is_honest(self):
        def boom(pid):
            raise RuntimeError("store on fire")

        router = ConversationRouter(
            os.path.join(self.tmp.name, "routing2.db"), boom)
        try:
            r = router.create_chat("projA", "chat")
            self.assertFalse(r["ok"])
            self.assertIn("project lookup failed", r["error"])
        finally:
            router.close()

    # -- auto-routing: honestly unavailable --------------------------------
    def test_auto_route_is_typed_unavailable(self):
        r = self.router.auto_route("conv-main-1")
        self.assertFalse(r["ok"])
        un = r["unavailable"]
        self.assertEqual(un["code"], "auto_route_conversation")
        self.assertEqual(un["gui"], "coming_soon")
        self.assertIn("classifier", un["missing_substrate"])
        self.assertTrue(un["reason"])

    # -- route table: real handlers, JSON in/out ---------------------------
    def test_routes_for_routing(self):
        routes = routes_for_routing(self.router)
        self.assertEqual(len(routes), 5)

        created = routes[("POST", "/api/projects/chats")](
            {"project_id": "projA", "chat_label": "chat A1"})
        self.assertTrue(created["ok"], created)
        chat_id = created["chat_id"]

        listed = routes[("GET", "/api/projects/chats")](
            {"project_id": "projA"})
        self.assertTrue(listed["ok"])
        self.assertEqual(len(listed["chats"]), 1)

        routed = routes[("POST", "/api/routing/route")](
            {"conversation_id": "conv-9", "chat_id": chat_id})
        self.assertTrue(routed["ok"], routed)

        resolved = routes[("GET", "/api/routing/resolve")](
            {"conversation_id": "conv-9"})
        self.assertTrue(resolved["routed"], resolved)
        self.assertEqual(resolved["project_id"], "projA")

        unrouted = routes[("GET", "/api/routing/resolve")](
            {"conversation_id": "never-seen"})
        self.assertFalse(unrouted["routed"])
        self.assertEqual(unrouted["state"], "unrouted")

        refused = routes[("POST", "/api/projects/chats")](
            {"project_id": "projNOPE", "chat_label": "x"})
        self.assertFalse(refused["ok"])

        auto = routes[("POST", "/api/routing/auto")](
            {"conversation_id": "conv-9"})
        self.assertFalse(auto["ok"])
        self.assertEqual(auto["unavailable"]["code"],
                         "auto_route_conversation")

        json.dumps(created); json.dumps(listed)
        json.dumps(routed); json.dumps(resolved); json.dumps(auto)


if __name__ == "__main__":
    unittest.main()
