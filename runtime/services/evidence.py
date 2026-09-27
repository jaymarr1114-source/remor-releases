"""Contract 8 — Evidence store.

There is NO epistemic/hypothesis store anywhere in the runtime, so this
contract BUILDS one for real: a sqlite-backed typed store for hypotheses,
observations and inferences, plus hosted user/agent-authored documents.

Honesty rules this store lives by:
- It is empty by default. `list_entries` on an empty kind returns an empty
  list with `"empty": true`, matching the GUI's "No hypotheses recorded
  yet." / "No observations recorded yet." states — that is honest emptiness,
  never seeded "example" content. This store NEVER invents entries.
- Documents (soul.md, theory.md, hypothetical inferences.md) are HOSTED:
  saved by the user or agents through `save_document`, never written by the
  engine. Absent documents report `{"ok": true, "exists": false}`.
- All entries are stored as inert text via parameterized SQL — SQL-shaped
  text can never execute.
- Oversized input is refused with an explicit error, never silently
  truncated.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

KINDS = ("hypothesis", "observation", "inference")
DOCUMENTS = ("soul.md", "theory.md", "hypothetical inferences.md")
MAX_ENTRY_TEXT = 20_000
MAX_ENTRY_SOURCE = 500
MAX_DOCUMENT_BYTES = 200_000


class EvidenceStore:
    """Real sqlite persistence for the epistemic store."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS evidence_entries (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind       TEXT NOT NULL,
                    text       TEXT NOT NULL,
                    source     TEXT,
                    created_at REAL NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS evidence_documents (
                    name       TEXT PRIMARY KEY,
                    markdown   TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_evidence_kind "
                      "ON evidence_entries(kind)")

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # -- entries ----------------------------------------------------------
    def add_entry(self, kind: str, text: str,
                  source: Optional[str] = None) -> Dict[str, Any]:
        if kind not in KINDS:
            return {"ok": False,
                    "error": f"kind {kind!r} refused; must be one of "
                             f"{list(KINDS)}"}
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "text must be a non-empty string"}
        if len(text) > MAX_ENTRY_TEXT:
            return {"ok": False,
                    "error": f"text refused: {len(text)} chars exceeds limit "
                             f"{MAX_ENTRY_TEXT}"}
        if source is not None:
            if not isinstance(source, str):
                return {"ok": False, "error": "source must be a string"}
            if len(source) > MAX_ENTRY_SOURCE:
                return {"ok": False,
                        "error": f"source refused: {len(source)} chars exceeds "
                                 f"limit {MAX_ENTRY_SOURCE}"}
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO evidence_entries (kind, text, source, created_at) "
                "VALUES (?,?,?,?)",
                (kind, text, source, time.time()))
            entry_id = cur.lastrowid
        return {"ok": True, "id": entry_id, "kind": kind}

    def get_entry(self, entry_id: int) -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                "SELECT id, kind, text, source, created_at FROM evidence_entries "
                "WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": f"entry {entry_id!r} not found"}
        return {"ok": True, "entry": self._row(row)}

    def list_entries(self, kind: Optional[str] = None,
                     limit: int = 200) -> Dict[str, Any]:
        if kind is not None and kind not in KINDS:
            return {"ok": False,
                    "error": f"kind {kind!r} refused; must be one of "
                             f"{list(KINDS)}"}
        limit = max(1, min(int(limit or 200), 1000))
        with self._conn() as c:
            if kind is None:
                rows = c.execute(
                    "SELECT id, kind, text, source, created_at "
                    "FROM evidence_entries ORDER BY id DESC LIMIT ?",
                    (limit,)).fetchall()
            else:
                rows = c.execute(
                    "SELECT id, kind, text, source, created_at "
                    "FROM evidence_entries WHERE kind=? "
                    "ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
            counts = c.execute(
                "SELECT kind, COUNT(*) FROM evidence_entries "
                "GROUP BY kind").fetchall()
        entries = [self._row(r) for r in rows]
        by_kind = {k: 0 for k in KINDS}
        by_kind.update({k: n for k, n in counts})
        return {
            "ok": True,
            "entries": entries,
            "empty": len(entries) == 0,
            "by_kind": by_kind,
        }

    @staticmethod
    def _row(row: Tuple) -> Dict[str, Any]:
        return {"id": row[0], "kind": row[1], "text": row[2],
                "source": row[3], "created_at": row[4]}

    # -- documents --------------------------------------------------------
    def save_document(self, name: str, markdown: str) -> Dict[str, Any]:
        if name not in DOCUMENTS:
            return {"ok": False,
                    "error": f"document {name!r} refused; only "
                             f"{list(DOCUMENTS)} may be hosted"}
        if not isinstance(markdown, str):
            return {"ok": False, "error": "markdown must be a string"}
        if len(markdown.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            return {"ok": False,
                    "error": f"document refused: exceeds "
                             f"{MAX_DOCUMENT_BYTES} bytes"}
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO evidence_documents "
                      "(name, markdown, updated_at) VALUES (?,?,?)",
                      (name, markdown, time.time()))
        return {"ok": True, "name": name}

    def get_document(self, name: str) -> Dict[str, Any]:
        if name not in DOCUMENTS:
            return {"ok": False,
                    "error": f"document {name!r} refused; only "
                             f"{list(DOCUMENTS)} may be hosted"}
        with self._conn() as c:
            row = c.execute("SELECT markdown, updated_at FROM evidence_documents "
                            "WHERE name=?", (name,)).fetchone()
        if row is None:
            return {"ok": True, "name": name, "exists": False}
        return {"ok": True, "name": name, "exists": True,
                "markdown": row[0], "updated_at": row[1]}


def routes_for_evidence(store: EvidenceStore) -> Dict[Any, Any]:
    """Route table: (method, path) -> handler(body_dict) -> JSON dict.

    `store` is a constructed EvidenceStore (db_path chosen by the host).
    """
    def _add(body: Dict[str, Any]) -> Dict[str, Any]:
        body = body or {}
        return store.add_entry(str(body.get("kind", "")),
                               body.get("text", ""),
                               body.get("source"))

    def _get(body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            entry_id = int((body or {}).get("id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "id must be an integer"}
        return store.get_entry(entry_id)

    def _list(body: Dict[str, Any]) -> Dict[str, Any]:
        body = body or {}
        return store.list_entries(kind=body.get("kind"),
                                  limit=body.get("limit", 200))

    def _doc_get(body: Dict[str, Any]) -> Dict[str, Any]:
        return store.get_document(str((body or {}).get("name", "")))

    def _doc_save(body: Dict[str, Any]) -> Dict[str, Any]:
        body = body or {}
        return store.save_document(str(body.get("name", "")),
                                   body.get("markdown", ""))

    return {
        ("POST", "/api/evidence/add"): _add,
        ("POST", "/api/evidence/get"): _get,
        ("POST", "/api/evidence/list"): _list,
        ("POST", "/api/evidence/documents/get"): _doc_get,
        ("POST", "/api/evidence/documents/save"): _doc_save,
    }
