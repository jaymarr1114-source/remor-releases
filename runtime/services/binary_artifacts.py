"""
swarm_engine/services/binary_artifacts.py

Contract 6 — Artifacts (binary).

Wraps (does NOT fork) ArtifactStore: code artifacts keep living in the
existing `artifacts` table; binary blobs get their own `binary_artifacts`
table in the SAME sqlite database plus content-addressed blob files on
disk. ArtifactStore is never subclassed or copied.

Model:
  * sqlite table binary_artifacts(id PK, name, content_type, sha256,
    size_bytes, created_at).
  * blob path: <blob_dir>/<sha256[0:2]>/<sha256> — content-addressed, so
    identical bytes stored under two names share one blob file on disk
    (refcounted by row count at delete time).
  * Integrity on read: get_file() re-reads the blob and recomputes sha256;
    a mismatch (tamper) is REFUSED, never silently served. A blob missing
    from disk is reported as corruption, not as empty bytes.
  * Size guard: MAX_BINARY_BYTES = 200 MiB, enforced BEFORE any disk write.
  * Names are metadata only: the blob path is hash-derived, so a hostile
    name can never escape the blob dir. Names containing path separators,
    "..", or NUL bytes are still refused for listing hygiene.
  * No extraction is performed. Zips, tarballs, blender files, and anything
    else are stored and returned as opaque bytes. Consequence, stated not
    guarded: there is no zip-bomb decompression surface in this service;
    the 200 MiB cap bounds what can be stored.

Audio pipeline (GUI vision: 1 instrumental + 2 voice -> full song) is
HONESTLY-UNAVAILABLE at this legacy route: the media-substrate modules
(music/song/voice) exist in this runtime, but voice synthesis is honestly
unavailable without piper, and this legacy mix route was never wired to
the machinery. mix_audio() returns the typed unavailability; it is not
stubbed.

HTTP surface: routes_for_binary_artifacts(binary_store) returns
{(method, path): handler} with handler(body_dict) -> JSON-serializable dict.
The download handler returns
  {"ok": true, "download": {"artifact_id", "name", "content_type",
   "sha256", "size_bytes", "bytes": <base64>}}
base64 so it stays JSON; the coordinator's HTTP layer decodes it and sets
the real Content-Type / Content-Length from content_type and size_bytes.
http_adapter.py is NOT edited; the coordinator's HTTP layer merges these.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from swarm_engine.services.contract_types import contract_unavailable, dispatch

# Sane oversize guard: 200 MiB, enforced before any disk write.
MAX_BINARY_BYTES = 200 * 1024 * 1024


def _valid_name(name: Any) -> Optional[str]:
    """Return the cleaned name, or None if it must be refused."""
    if not isinstance(name, str):
        return None
    cleaned = name.strip()
    if not cleaned:
        return None
    if "\x00" in cleaned:
        return None
    if "/" in cleaned or "\\" in cleaned:
        return None
    if cleaned in (".", "..") or ".." in cleaned.split():
        return None
    if len(cleaned) > 255:
        return None
    return cleaned


class BinaryArtifactStore:
    """Binary (any-file-type) artifacts layered over an ArtifactStore's db."""

    def __init__(self, artifact_store, blob_dir: Optional[str] = None) -> None:
        self._store = artifact_store
        db_path = artifact_store._db_path  # same-database table, not a fork
        self._blob_dir = blob_dir or os.path.join(
            os.path.dirname(os.path.abspath(db_path)), "binary_blobs")
        os.makedirs(self._blob_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    # -- persistence -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._store._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS binary_artifacts (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_binary_sha "
                "ON binary_artifacts(sha256)"
            )

    def _blob_path(self, sha256: str) -> str:
        return os.path.join(self._blob_dir, sha256[:2], sha256)

    # -- write -----------------------------------------------------------
    def store_file(
        self,
        name: str,
        data_bytes: bytes,
        content_type: str = "application/octet-stream",
    ) -> Dict[str, Any]:
        cleaned = _valid_name(name)
        if cleaned is None:
            return {"ok": False, "error": "invalid artifact name"}
        if not isinstance(data_bytes, (bytes, bytearray)):
            return {"ok": False, "error": "data_bytes must be bytes"}
        data = bytes(data_bytes)
        if len(data) > MAX_BINARY_BYTES:
            return {
                "ok": False,
                "error": f"oversize: {len(data)} bytes exceeds "
                f"{MAX_BINARY_BYTES} byte limit",
            }
        sha = hashlib.sha256(data).hexdigest()
        ctype = (content_type or "").strip() or "application/octet-stream"
        bpath = self._blob_path(sha)
        with self._lock:
            os.makedirs(os.path.dirname(bpath), exist_ok=True)
            if not os.path.exists(bpath):
                # Content-addressed: identical bytes dedupe to one blob.
                tmp = bpath + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, bpath)
            with self._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO binary_artifacts "
                    "(name, content_type, sha256, size_bytes, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (cleaned, ctype, sha, len(data), time.time()),
                )
                artifact_id = cur.lastrowid
                conn.commit()
        return {
            "ok": True,
            "kind": "binary",
            "artifact_id": artifact_id,
            "name": cleaned,
            "content_type": ctype,
            "sha256": sha,
            "size_bytes": len(data),
        }

    # -- read (integrity-checked) ----------------------------------------
    def get_file(self, artifact_id: int) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM binary_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return {"ok": False, "error": "not found"}
        bpath = self._blob_path(row["sha256"])
        try:
            with open(bpath, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            return {
                "ok": False,
                "error": "blob missing on disk: stored artifact is corrupt",
            }
        actual = hashlib.sha256(data).hexdigest()
        if actual != row["sha256"]:
            return {
                "ok": False,
                "error": "integrity check failed: blob sha256 does not "
                "match stored digest — refused",
            }
        return {
            "ok": True,
            "kind": "binary",
            "artifact_id": artifact_id,
            "name": row["name"],
            "content_type": row["content_type"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "created_at": row["created_at"],
            "data": data,
        }

    def list_binaries(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, content_type, sha256, size_bytes, created_at"
                " FROM binary_artifacts ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "kind": "binary",
                "artifact_id": r["id"],
                "name": r["name"],
                "content_type": r["content_type"],
                "sha256": r["sha256"],
                "size_bytes": r["size_bytes"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def list_all(self) -> List[Dict[str, Any]]:
        """The GUI's ALL ARTIFACTS list: code + binary artifacts together."""
        items: List[Dict[str, Any]] = []
        for entry in self._store.list_artifacts():
            entry = dict(entry)
            entry["kind"] = "code"
            items.append(entry)
        items.extend(self.list_binaries())

        def _ts(item: Dict[str, Any]) -> float:
            return float(item.get("updated_at") or item.get("created_at") or 0)

        items.sort(key=_ts, reverse=True)
        return items

    def delete_binary(self, artifact_id: int) -> Dict[str, Any]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT sha256 FROM binary_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                return {"ok": False, "error": "not found"}
            sha = row["sha256"]
            conn.execute(
                "DELETE FROM binary_artifacts WHERE id = ?", (artifact_id,))
            remaining = conn.execute(
                "SELECT COUNT(*) AS n FROM binary_artifacts WHERE sha256 = ?",
                (sha,),
            ).fetchone()["n"]
            conn.commit()
        if remaining == 0:
            # No other row references these bytes: safe to drop the blob.
            try:
                os.remove(self._blob_path(sha))
            except FileNotFoundError:
                pass
        return {"ok": True, "artifact_id": artifact_id}

    # -- honestly unavailable --------------------------------------------
    def mix_audio(self, instrumental_id: Any = None,
                  voice_ids: Any = None) -> Dict[str, Any]:
        """Audio pipeline (instrumental + voice -> full song): NOT BUILT HERE.

        The media-substrate modules (music/song/voice) exist in this
        runtime, but voice synthesis is honestly unavailable without
        piper, and this legacy mix route was never wired to the
        machinery. Returns the typed unavailability instead of a stub.
        """
        return contract_unavailable(
            "AUDIO_PIPELINE_ABSENT",
            "the audio pipeline (instrumental + voice audio -> full song) "
            "cannot run here: voice synthesis is honestly unavailable "
            "(no piper TTS engine), and the legacy mix route is not wired "
            "to the media machinery -- no full song can be assembled",
            missing_substrate="voice synthesis engine (piper TTS); legacy "
            "audio-mix route wiring",
        )


def routes_for_binary_artifacts(binary_store: BinaryArtifactStore):
    """Route table for Contract 6. Handlers take body_dict -> JSON dict."""
    svc = binary_store

    def _post_binary(body: Dict[str, Any]) -> Dict[str, Any]:
        raw = body.get("bytes_b64", "")
        if not isinstance(raw, str) or not raw:
            return {"ok": False, "error": "bytes_b64 (base64) is required"}
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            return {"ok": False, "error": "bytes_b64 is not valid base64"}
        return svc.store_file(
            body.get("name", ""),
            data,
            body.get("content_type", "application/octet-stream"),
        )

    def _get_binary(body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            aid = int(body.get("artifact_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "artifact_id is required"}
        res = svc.get_file(aid)
        if not res.get("ok"):
            return res
        payload = {
            "artifact_id": res["artifact_id"],
            "name": res["name"],
            "content_type": res["content_type"],
            "sha256": res["sha256"],
            "size_bytes": res["size_bytes"],
            "bytes": base64.b64encode(res["data"]).decode("ascii"),
        }
        return {"ok": True, "download": payload}

    def _list_all(body: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": True, "artifacts": svc.list_all()}

    def _delete_binary(body: Dict[str, Any]) -> Dict[str, Any]:
        try:
            aid = int(body.get("artifact_id"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "artifact_id is required"}
        return svc.delete_binary(aid)

    def _post_audio_mix(body: Dict[str, Any]) -> Dict[str, Any]:
        return svc.mix_audio(body.get("instrumental_id"),
                             body.get("voice_ids"))

    return {
        ("POST", "/api/artifacts/binary"): _post_binary,
        ("GET", "/api/artifacts/binary/{artifact_id}"): _get_binary,
        ("DELETE", "/api/artifacts/binary/{artifact_id}"): _delete_binary,
        ("GET", "/api/artifacts/all"): _list_all,
        ("POST", "/api/artifacts/audio/mix"): _post_audio_mix,
    }


__all__ = [
    "BinaryArtifactStore",
    "routes_for_binary_artifacts",
    "MAX_BINARY_BYTES",
    "dispatch",
    "contract_unavailable",
]
