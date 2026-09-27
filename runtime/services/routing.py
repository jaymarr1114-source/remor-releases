"""
swarm_engine/services/routing.py

Contract 4 — Projects + conversation routing: explicit, operator-driven
mapping of main-chat conversations into project chats.

From the GUI spec: a main-chat conversation is routed into a project's
chats (Project A -> chat A1/A2; Project B -> chat B1/B2). There is NO
topic/intent classifier substrate in the runtime, so NOTHING here
guesses: routing is an explicit operator action recorded in sqlite, and
an unknown conversation resolves to an honest "unrouted" state.

What is real here
-----------------
* create_chat(project_id, chat_label) -> chat_id. Refused unless the
  project really exists (project_exists callback, e.g. over the real
  ProjectService store).
* route_conversation(conversation_id, chat_id) — explicit mapping row;
  refused for an unknown chat_id. conversation_id is the main chat's own
  identifier space; we store it verbatim (parameterized sqlite, so
  traversal-shaped ids are inert data, never paths).
* list_project_chats(project_id) — real rows for a real project.
* resolve_conversation(conversation_id) -> the project/chat mapping, or
  {"routed": False, "state": "unrouted"} — never guessed.

Honest boundary
---------------
Automatic "main chat reroutes depending on task or topic" is
HONESTLY-UNAVAILABLE (code "auto_route_conversation"): no topic/intent
classifier substrate exists in the runtime, so there is nothing that
could assign a conversation to a project chat automatically. Offered as
a route returning the typed coming-soon payload.

Route-table convention: handlers take a single body_dict; for GET routes
the composing adapter merges the query string into body_dict.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from swarm_engine.services.contract_types import contract_unavailable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_chats (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    label      TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_project_chats_project
    ON project_chats (project_id);
CREATE TABLE IF NOT EXISTS conversation_routes (
    conversation_id TEXT PRIMARY KEY,
    chat_id         TEXT NOT NULL REFERENCES project_chats (id),
    routed_at       REAL NOT NULL
);
"""


class ConversationRouter:
    """Explicit conversation -> project-chat routing over sqlite.

    project_exists: callable(project_id) -> bool, backed by the real
    project store (e.g. ``lambda pid: project_service._store.get(pid)
    is not None`` is NOT acceptable here — pass a public callable; in
    tests we pass a thin wrapper over ProjectService.get_project).
    """

    def __init__(self, db_path: str,
                 project_exists: Callable[[str], bool]) -> None:
        self.db_path = db_path
        self._project_exists = project_exists
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------
    # chats
    # ------------------------------------------------------------------
    def create_chat(self, project_id: str,
                    chat_label: str) -> Dict[str, Any]:
        """Create a named chat inside a project that really exists."""
        if not isinstance(project_id, str) or not project_id:
            return {"ok": False, "error": "project_id is required"}
        if not isinstance(chat_label, str) or not chat_label.strip():
            return {"ok": False,
                    "error": "chat_label must be a non-empty string"}
        try:
            exists = bool(self._project_exists(project_id))
        except Exception as exc:
            return {"ok": False,
                    "error": f"project lookup failed: {exc!r}"}
        if not exists:
            return {"ok": False,
                    "error": f"unknown project {project_id!r}: "
                             f"refusing to create a chat for a project "
                             f"that does not exist"}
        chat_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO project_chats (id, project_id, label, "
                "created_at) VALUES (?, ?, ?, ?)",
                (chat_id, project_id, chat_label.strip(), now))
            self._db.commit()
        return {"ok": True, "chat_id": chat_id, "project_id": project_id,
                "chat_label": chat_label.strip(), "created_at": now}

    def list_project_chats(self, project_id: str) -> Dict[str, Any]:
        """All chats recorded for a project that really exists."""
        if not isinstance(project_id, str) or not project_id:
            return {"ok": False, "error": "project_id is required"}
        try:
            exists = bool(self._project_exists(project_id))
        except Exception as exc:
            return {"ok": False,
                    "error": f"project lookup failed: {exc!r}"}
        if not exists:
            return {"ok": False,
                    "error": f"unknown project {project_id!r}"}
        with self._lock:
            cur = self._db.execute(
                "SELECT id, label, created_at FROM project_chats "
                "WHERE project_id=? ORDER BY created_at ASC",
                (project_id,))
            chats = [{"chat_id": cid, "project_id": project_id,
                      "chat_label": label, "created_at": ca}
                     for cid, label, ca in cur.fetchall()]
        return {"ok": True, "project_id": project_id, "chats": chats}

    # ------------------------------------------------------------------
    # routing (explicit only)
    # ------------------------------------------------------------------
    def route_conversation(self, conversation_id: str,
                           chat_id: str) -> Dict[str, Any]:
        """Record an explicit conversation -> chat mapping.

        conversation_id comes from the main chat's own id space and is
        stored verbatim; the chat must exist (and therefore belongs to a
        project that exists).
        """
        if not isinstance(conversation_id, str) or not conversation_id:
            return {"ok": False, "error": "conversation_id is required"}
        if not isinstance(chat_id, str) or not chat_id:
            return {"ok": False, "error": "chat_id is required"}
        with self._lock:
            cur = self._db.execute(
                "SELECT project_id, label FROM project_chats WHERE id=?",
                (chat_id,))
            chat = cur.fetchone()
            if chat is None:
                return {"ok": False,
                        "error": f"unknown chat {chat_id!r}: refusing to "
                                 f"route a conversation to a chat that does "
                                 f"not exist"}
            project_id, label = chat
            now = time.time()
            self._db.execute(
                "INSERT OR REPLACE INTO conversation_routes "
                "(conversation_id, chat_id, routed_at) VALUES (?, ?, ?)",
                (conversation_id, chat_id, now))
            self._db.commit()
        return {"ok": True, "conversation_id": conversation_id,
                "chat_id": chat_id, "project_id": project_id,
                "chat_label": label, "routed_at": now}

    def resolve_conversation(self,
                             conversation_id: str) -> Dict[str, Any]:
        """Resolve a conversation to its project/chat, or honest unrouted.

        Never guesses: unknown -> {"ok": True, "routed": False,
        "state": "unrouted"}.
        """
        if not isinstance(conversation_id, str) or not conversation_id:
            return {"ok": False, "error": "conversation_id is required"}
        with self._lock:
            cur = self._db.execute(
                "SELECT r.chat_id, r.routed_at, c.project_id, c.label "
                "FROM conversation_routes r "
                "JOIN project_chats c ON c.id = r.chat_id "
                "WHERE r.conversation_id=?", (conversation_id,))
            row = cur.fetchone()
        if row is None:
            return {"ok": True, "conversation_id": conversation_id,
                    "routed": False, "state": "unrouted"}
        chat_id, routed_at, project_id, label = row
        return {"ok": True, "conversation_id": conversation_id,
                "routed": True, "chat_id": chat_id,
                "project_id": project_id, "chat_label": label,
                "routed_at": routed_at}

    def auto_route(self, conversation_id: str = "") -> Dict[str, Any]:
        """Topic-driven automatic rerouting: honestly unavailable."""
        return contract_unavailable(
            "auto_route_conversation",
            "Routing is explicit-operator-only: the runtime has no "
            "topic or intent classifier substrate that could assign a "
            "conversation to a project chat automatically, so any "
            "'auto' answer would be a guess.",
            "intent/topic classifier over conversation content")

    def close(self) -> None:
        with self._lock:
            self._db.close()


# ----------------------------------------------------------------------
# Route table for the coordinator (do NOT wire into http_adapter here).
# ----------------------------------------------------------------------
def routes_for_routing(router: "ConversationRouter") -> Dict:
    def _create_chat(body):
        return router.create_chat(
            project_id=body.get("project_id"),
            chat_label=body.get("chat_label"))

    def _list_chats(body):
        return router.list_project_chats(project_id=body.get("project_id"))

    def _route(body):
        return router.route_conversation(
            conversation_id=body.get("conversation_id"),
            chat_id=body.get("chat_id"))

    def _resolve(body):
        return router.resolve_conversation(
            conversation_id=body.get("conversation_id"))

    def _auto(body):
        return router.auto_route(body.get("conversation_id", ""))

    return {
        ("POST", "/api/projects/chats"): _create_chat,
        ("GET", "/api/projects/chats"): _list_chats,
        ("POST", "/api/routing/route"): _route,
        ("GET", "/api/routing/resolve"): _resolve,
        ("POST", "/api/routing/auto"): _auto,
    }
