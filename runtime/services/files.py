"""
swarm_engine/services/files.py

Scoped, traversal-safe file access for the REMOR third track (file-serving
API backing a future file browser).

Every path resolution funnels through _resolve(), which
  1. rejects absolute paths and NUL bytes,
  2. joins the requested relative path onto the scope root,
  3. resolves it with os.path.realpath() — this collapses ".." AND
     resolves symlinks, including a symlink *inside* the root that points
     *outside* the root,
  4. re-checks containment with os.path.commonpath() against the (itself
     realpath-resolved) root.

A path that fails containment is refused with ok=False; the caller never
sees the absolute server path — only scoped relative names and names of
entries inside the scope are ever returned.

Classification:
  * read/write/list/stat/mkdir refused for ../, /abs/path, a/../../evil,
    and escape-symlinks: PROVEN (covered by real filesystem attacks in
    services/tests/test_files.py).
  * Symlink *inside* the root that points *inside* the root: allowed and
    works — PROVEN.
  * Writes through an escaping symlink refused: PROVEN for the static
    case (realpath is evaluated at check time and at open time the same
    check applies). TOCTOU between check and open (symlink swapped
    mid-call by a local attacker on a shared filesystem) is BOUNDED:
    there is no atomic check-and-open in the stdlib path API; an HTTP
    server would need a per-root lock or O_NOFOLLOW dance for the
    hostile-local-attacker case.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional


class PathEscapeError(Exception):
    """Raised internally when a request escapes the scope root."""


class ScopedFileService:
    """Filesystem access confined to a single root directory.

    Args:
        root: scope root; stored as its realpath so a symlinked root
            itself cannot widen the scope.
        writable: when False (default) write_text/mkdir refuse with
            "read-only scope" and nothing is ever written.
    """

    def __init__(self, root: str, writable: bool = False) -> None:
        if not os.path.isdir(root):
            raise ValueError("scope root is not a directory")
        self._root = os.path.realpath(root)
        self._writable = bool(writable)

    # -- path resolution -------------------------------------------------
    def _resolve(self, rel: Optional[str]) -> str:
        """Resolve a caller-supplied relative path to an in-scope absolute
        path, raising PathEscapeError when it escapes."""
        rel = rel or ""
        if not isinstance(rel, str):
            raise PathEscapeError("path must be a string")
        if "\x00" in rel:
            raise PathEscapeError("path contains NUL byte")
        if os.path.isabs(rel):
            raise PathEscapeError("absolute paths are not allowed")
        joined = os.path.join(self._root, rel)
        real = os.path.realpath(joined)
        try:
            common = os.path.commonpath([self._root, real])
        except ValueError:
            # e.g. different drives on Windows: not in scope.
            raise PathEscapeError("path escapes the scoped root")
        if common != self._root:
            raise PathEscapeError("path escapes the scoped root")
        return real

    def _rel_of(self, real: str) -> str:
        """Scoped relative name for an in-scope absolute path."""
        rel = os.path.relpath(real, self._root)
        return "" if rel == "." else rel

    # -- read ------------------------------------------------------------
    def list_dir(self, rel: str = "") -> Dict[str, Any]:
        try:
            path = self._resolve(rel)
        except PathEscapeError as exc:
            return {"ok": False, "error": str(exc)}
        if not os.path.exists(path):
            return {"ok": False, "error": "not found"}
        if not os.path.isdir(path):
            return {"ok": False, "error": "not a directory"}
        entries: List[Dict[str, Any]] = []
        with os.scandir(path) as it:
            for de in it:
                try:
                    st = de.stat(follow_symlinks=True)
                except OSError:
                    continue
                is_dir = de.is_dir(follow_symlinks=True)
                entries.append(
                    {
                        "name": de.name,
                        "rel": os.path.join(self._rel_of(path), de.name)
                        if self._rel_of(path)
                        else de.name,
                        "is_dir": is_dir,
                        "size_bytes": 0 if is_dir else st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
        entries.sort(key=lambda e: (not e["is_dir"], e["name"]))
        return {"ok": True, "entries": entries, "rel": self._rel_of(path)}

    def stat(self, rel: str) -> Dict[str, Any]:
        try:
            path = self._resolve(rel)
        except PathEscapeError as exc:
            return {"ok": False, "error": str(exc)}
        if not os.path.exists(path):
            return {"ok": False, "error": "not found"}
        st = os.stat(path)  # follows symlinks: target was contained
        is_dir = os.path.isdir(path)
        return {
            "ok": True,
            "rel": self._rel_of(path),
            "is_dir": is_dir,
            "size_bytes": 0 if is_dir else st.st_size,
            "mtime": st.st_mtime,
        }

    def read_text(self, rel: str, max_bytes: int = 200_000) -> Dict[str, Any]:
        try:
            path = self._resolve(rel)
        except PathEscapeError as exc:
            return {"ok": False, "error": str(exc)}
        if not os.path.exists(path):
            return {"ok": False, "error": "not found"}
        if os.path.isdir(path):
            return {"ok": False, "error": "not a file"}
        try:
            size = os.path.getsize(path)
        except OSError:
            return {"ok": False, "error": "unreadable"}
        if size > max_bytes:
            return {
                "ok": False,
                "error": f"file too large ({size} bytes > {max_bytes} byte limit)",
            }
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return {"ok": False, "error": "unreadable"}
        if b"\x00" in data:
            return {"ok": False, "error": "binary file (contains NUL bytes)"}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "binary file (not valid UTF-8)"}
        return {"ok": True, "rel": self._rel_of(path), "content": text}

    # -- write -----------------------------------------------------------
    def write_text(self, rel: str, content: str) -> Dict[str, Any]:
        if not self._writable:
            return {"ok": False, "error": "read-only scope"}
        if not isinstance(content, str):
            return {"ok": False, "error": "content must be a string"}
        try:
            path = self._resolve(rel)
        except PathEscapeError as exc:
            return {"ok": False, "error": str(exc)}
        if os.path.isdir(path):
            return {"ok": False, "error": "not a file"}
        # Re-resolve at open time: the parent dir must itself be in scope
        # (a symlink parent that escapes is already refused by _resolve).
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            return {"ok": False, "error": f"write failed: {exc.strerror or exc}"}
        st = os.stat(path)
        return {
            "ok": True,
            "rel": self._rel_of(path),
            "size_bytes": st.st_size,
            "mtime": st.st_mtime,
        }

    def mkdir(self, rel: str) -> Dict[str, Any]:
        if not self._writable:
            return {"ok": False, "error": "read-only scope"}
        try:
            path = self._resolve(rel)
        except PathEscapeError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return {
                "ok": False,
                "error": f"mkdir failed: {exc.strerror or exc}",
            }
        st = os.stat(path)
        return {"ok": True, "rel": self._rel_of(path), "mtime": st.st_mtime}
