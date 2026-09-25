"""
swarm_engine/services/artifacts.py

Artifact store for the REMOR third track: persisted, editable artifacts
with sandboxed execution. Replaces the GUI's current one-shot throwaway
execution with saved revisions that can be edited and re-run.

Model:
  * sqlite table artifacts(id INTEGER PK, artifact_key TEXT, name, language,
    rev INTEGER, code TEXT, metadata_json TEXT, created_at REAL,
    UNIQUE(artifact_key, rev)).
  * artifact_key is a stable opaque uuid per artifact name; every row
    sharing a key is a revision of the same artifact. The public
    artifact_id is the row id of revision 1 (stable across revisions).
  * Editing = a new revision row; history is never overwritten.
  * The sqlite record is the source of truth for code. There is NO
    integrity/tamper-evidence on code blobs: if sqlite is edited out of
    band, get() returns exactly the stored bytes. This is an honest
    ABSENT, unlike the oracle-bound capability store.

Execution:
  * python only, in a real subprocess with cwd=sandbox_dir, output
    captured and capped at 4000 trailing chars, timeout kills the child.
  * Non-python languages get the GUI's existing honest refusal:
    "execution for <lang> is not supported".
  * Empty code is refused ("empty code"), matching the current GUI.

Honest bounds:
  * The sandbox is "same user, separate process, working-directory
    scoped, wall-clock killed". No UID isolation, no CPU/RAM cgroup
    limits, no network/syscall filtering. Grandchild processes spawned
    by the artifact survive a timeout kill of the direct child. If the
    HTTP layer ever exposes this, it needs a real sandbox.
  * Timeout kills only the direct child process; zombies are reaped by
    subprocess.run (wait after kill), but orphaned grandchildren are
    possible — BOUNDED, see module docstring of the timeout path.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

_OUTPUT_CAP = 4000


class ArtifactStore:
    """Persisted editable artifacts + sandboxed execution."""

    def __init__(self, db_path: str, sandbox_dir: str) -> None:
        self._db_path = db_path
        self._sandbox_dir = sandbox_dir
        os.makedirs(sandbox_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    # -- persistence -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY,
                    artifact_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    language TEXT NOT NULL,
                    rev INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    UNIQUE(artifact_key, rev)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_key_rev "
                "ON artifacts(artifact_key, rev)"
            )

    def _key_for_name(self, conn: sqlite3.Connection, name: str) -> Optional[str]:
        row = conn.execute(
            "SELECT artifact_key FROM artifacts WHERE name = ? LIMIT 1",
            (name,),
        ).fetchone()
        return row["artifact_key"] if row else None

    def save(
        self,
        name: str,
        language: str,
        code: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Save a new revision. Same name -> new revision, history kept."""
        if not name or not str(name).strip():
            return {"ok": False, "error": "name is required"}
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "error": "empty code"}
        language = (language or "").strip().lower() or "python"
        name = str(name).strip()
        meta_json = json.dumps(metadata or {})
        now = time.time()
        with self._lock, self._connect() as conn:
            key = self._key_for_name(conn, name)
            if key is None:
                key = uuid.uuid4().hex
                rev = 1
            else:
                row = conn.execute(
                    "SELECT MAX(rev) AS m FROM artifacts WHERE artifact_key = ?",
                    (key,),
                ).fetchone()
                rev = int(row["m"]) + 1
            cur = conn.execute(
                "INSERT INTO artifacts (artifact_key, name, language, rev,"
                " code, metadata_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, name, language, rev, code, meta_json, now),
            )
            if rev == 1:
                artifact_id = cur.lastrowid
            else:
                artifact_id = conn.execute(
                    "SELECT id FROM artifacts WHERE artifact_key = ? AND rev = 1",
                    (key,),
                ).fetchone()["id"]
            conn.commit()
        return {"ok": True, "artifact_id": artifact_id, "revision": rev}

    def _resolve(self, artifact_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return dict(row) if row else None

    def _latest_row(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_key = ?"
                " ORDER BY rev DESC LIMIT 1",
                (key,),
            ).fetchone()
        return dict(row) if row else None

    def get(
        self, artifact_id: int, revision: Optional[int] = None
    ) -> Dict[str, Any]:
        base = self._resolve(artifact_id)
        if base is None:
            return {"ok": False, "error": "not found"}
        key = base["artifact_key"]
        if revision is None:
            row = self._latest_row(key)
        else:
            with self._lock, self._connect() as conn:
                dbrow = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_key = ? AND rev = ?",
                    (key, revision),
                ).fetchone()
            row = dict(dbrow) if dbrow else None
        if row is None:
            return {"ok": False, "error": "not found"}
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "artifact_key": row["artifact_key"],
            "name": row["name"],
            "language": row["language"],
            "revision": row["rev"],
            "code": row["code"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    def list_artifacts(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.artifact_key,
                       (SELECT id FROM artifacts a2
                        WHERE a2.artifact_key = a.artifact_key AND a2.rev = 1) AS artifact_id,
                       a.name, a.language, a.rev AS latest_revision,
                       MAX(a.created_at) AS updated_at
                FROM artifacts a
                GROUP BY a.artifact_key
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [
            {
                "artifact_id": r["artifact_id"],
                "name": r["name"],
                "language": r["language"],
                "latest_revision": r["latest_revision"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def revisions(self, artifact_id: int) -> Dict[str, Any]:
        base = self._resolve(artifact_id)
        if base is None:
            return {"ok": False, "error": "not found"}
        key = base["artifact_key"]
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT rev, code, created_at FROM artifacts"
                " WHERE artifact_key = ? ORDER BY rev ASC",
                (key,),
            ).fetchall()
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "revisions": [
                {"revision": r["rev"], "code": r["code"], "created_at": r["created_at"]}
                for r in rows
            ],
        }

    def delete(self, artifact_id: int) -> Dict[str, Any]:
        base = self._resolve(artifact_id)
        if base is None:
            return {"ok": False, "error": "not found"}
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM artifacts WHERE artifact_key = ?",
                (base["artifact_key"],),
            )
            conn.commit()
        return {"ok": True, "artifact_id": artifact_id}

    # -- execution -------------------------------------------------------
    def run(
        self,
        artifact_id: int,
        revision: Optional[int] = None,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        got = self.get(artifact_id, revision)
        if not got["ok"]:
            return got
        language = got["language"]
        code = got["code"]
        if language != "python":
            return {
                "ok": False,
                "error": f"execution for {language} is not supported",
            }
        if not code.strip():
            return {"ok": False, "error": "empty code"}
        fd, fpath = tempfile.mkstemp(
            prefix=f"artifact_{artifact_id}_r{got['revision']}_",
            suffix=".py",
            dir=self._sandbox_dir,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(code)
            try:
                p = subprocess.run(
                    [sys.executable, fpath],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=self._sandbox_dir,
                )
                return {
                    "ok": True,
                    "stdout": p.stdout[-_OUTPUT_CAP:],
                    "stderr": p.stderr[-_OUTPUT_CAP:],
                    "exit_code": p.returncode,
                    "timed_out": False,
                }
            except subprocess.TimeoutExpired as te:
                # subprocess.run kills the direct child before raising.
                return {
                    "ok": True,
                    "stdout": (te.stdout or "")[-_OUTPUT_CAP:],
                    "stderr": (
                        ((te.stderr or "") + f"\ntimed out after {timeout}s")
                        if te.stderr
                        else f"timed out after {timeout}s"
                    )[-_OUTPUT_CAP:],
                    "exit_code": None,
                    "timed_out": True,
                }
        finally:
            try:
                os.remove(fpath)
            except OSError:
                pass
