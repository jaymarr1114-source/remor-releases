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
    case. TOCTOU between check and open (symlink swapped mid-call by a
    local attacker on a shared filesystem): PROVEN closed — every public
    operation opens through _open_resolved(), which pins the parent
    directory (O_DIRECTORY|O_NOFOLLOW + /proc/self/fd verification) and
    opens the final component with openat(parent_fd, base,
    O_NOFOLLOW|...), so no path-string decision is made after the pin. A
    final component that became a symlink after the check is refused
    with "symlink encountered at open (possible race)". Mutating ops
    additionally hold a per-instance RLock across resolve->open->verify.
    _resolve() itself is fail-closed on mid-resolution races. Linux
    dependency: /proc/self/fd introspection; unavailable → fail closed.
    Residual: an attacker able to replace the scope root's parent is out
    of scope.

Archive inspection (this module, QUEUED-2):
  * list_archive: entry names/sizes/flags as stored; `safe` preview per
    entry from the same name check extraction uses — PROVEN.
  * read_inside: byte-exact reads without touching disk; refuses unsafe
    names, symlinks, dirs, oversize/bomb claims — PROVEN (real crafted
    archives, including a binary-patched 10 GB size lie).
  * extract: skip-and-neutralize — unsafe entries are skipped, logged,
    and reported, never written; per-entry + total streaming caps catch
    lying central directories; every target re-checked for containment;
    provenance manifest written per extraction — PROVEN.
  * tar/tar.gz: ABSENT — refused as "not a zip archive", never sniffed.
  * Password-protected zips: refused, feature ABSENT.
  * Hostile-local TOCTOU (symlink swapped mid-extraction): BOUNDED, same
    as above — the archive is the threat model here, not the machine.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat as stat_module
import threading
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple


class PathEscapeError(Exception):
    """Raised internally when a request escapes the scope root."""


class _AtomicOpenRefused(Exception):
    """Internal: the atomic open/verify sequence refused the path.

    Carries the public-facing error message.
    """


#: Refusal reason when O_NOFOLLOW trips at open time.
ELOOP_REFUSAL = "symlink encountered at open (possible race)"


# -- archive inspection -------------------------------------------------
# Limits are stated in ARCHIVE_INSPECTION_DESIGN.md and enforced below.
ZIP_MAX_ENTRIES = 10_000
ZIP_MAX_TOTAL_BYTES = 100_000_000
ZIP_MAX_ENTRY_BYTES = 10_000_000
READ_INSIDE_MAX_BYTES = 5_000_000
ZIP_MAX_NAME_LEN = 1024
PROVENANCE_NAME = ".remor_extract_provenance.json"
_CHUNK = 65536


def _is_symlink_zi(zi: "zipfile.ZipInfo") -> bool:
    """True when the entry was stored as a symlink (unix mode bits)."""
    return (zi.external_attr >> 16) & 0o170000 == 0o120000


def _check_entry_name(name: Any) -> Tuple[bool, Optional[str]]:
    """Strict canonical-form safety check for a zip entry name.

    Returns (True, None) when the name is safe to materialize inside the
    sandbox, else (False, reason). Canonical form: '/'-separated, no
    empty / '.' / '..' components, no backslashes, no absolute or
    drive-letter paths, no NUL.
    """
    if not isinstance(name, str) or not name:
        return False, "empty entry name"
    if len(name) > ZIP_MAX_NAME_LEN:
        return False, "entry name too long"
    if "\x00" in name:
        return False, "entry name contains NUL byte"
    if "\\" in name:
        return False, "entry name contains backslash"
    if name.startswith("/"):
        return False, "absolute entry path"
    if len(name) >= 2 and name[1] == ":" and name[0].isalpha():
        return False, "drive-letter entry path"
    if name.startswith("\\\\"):
        return False, "UNC entry path"
    parts = name.split("/")
    # A trailing slash marks a directory entry; check the rest.
    body = parts[:-1] if parts[-1] == "" else parts
    for comp in body:
        if comp == "":
            return False, "empty path component"
        if comp == ".":
            return False, "dot path component"
        if comp == "..":
            return False, "dot-dot path component"
    return True, None


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
        self._refusals: List[Dict[str, Any]] = []
        self._refusal_lock = threading.Lock()
        # Serializes the resolve->open->verify sequence of MUTATING ops
        # against other in-process threads. Cross-process attackers are
        # handled by O_NOFOLLOW + fd pinning, not by this lock.
        self._write_lock = threading.RLock()

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
        try:
            real = os.path.realpath(joined)
        except OSError:
            # A component vanished mid-resolution (local attacker flapping
            # entries, or a concurrent rename): fail closed, never serve
            # a half-resolved path.
            raise PathEscapeError("path could not be resolved (possible race)")
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

    # -- atomic open + fd verification -----------------------------------
    def _fd_realpath(self, fd: int) -> str:
        """Canonical path of an open fd via /proc/self/fd.

        Raises _AtomicOpenRefused (fail closed) when the introspection
        is unavailable — we never proceed unverified.
        """
        try:
            target = os.readlink("/proc/self/fd/%d" % fd)
        except OSError:
            raise _AtomicOpenRefused(
                "cannot verify opened file descriptor "
                "(/proc/self/fd unavailable on this platform)"
            )
        if target.endswith(" (deleted)"):
            target = target[: -len(" (deleted)")]
        return os.path.realpath(target)

    def _fd_in_scope(self, fd: int) -> bool:
        """True iff the open fd's canonical path is inside the scope root."""
        real = self._fd_realpath(fd)  # fail-closed on introspection failure
        try:
            common = os.path.commonpath([self._root, real])
        except ValueError:
            return False
        return common == self._root

    def _symlink_refusal(self, exc: OSError, lstat) -> Optional[_AtomicOpenRefused]:
        """Map an open-time OSError to the symlink refusal when the open
        failed because the trailing component is a symlink.

        Linux reports ELOOP for O_NOFOLLOW on a symlink -- EXCEPT when
        O_DIRECTORY is also set, in which case a trailing symlink reports
        ENOTDIR. The lstat (a zero-arg callable) is message-only: the
        refusal stands regardless of what it says; it only picks the
        honest reason string. Returns an _AtomicOpenRefused, or None when
        the error is not a symlink refusal.
        """
        if exc.errno == errno.ELOOP:
            return _AtomicOpenRefused(ELOOP_REFUSAL)
        if exc.errno == errno.ENOTDIR:
            try:
                st = lstat()
            except OSError:
                st = None
            if st is not None and stat_module.S_ISLNK(st.st_mode):
                return _AtomicOpenRefused(ELOOP_REFUSAL)
        return None

    def _open_verified_parent(self, resolved: str) -> int:
        """Open dirname(resolved) with O_DIRECTORY|O_NOFOLLOW and verify
        the fd really is that canonical directory.

        Returns the fd (caller must close). Raises _AtomicOpenRefused.
        """
        parent = os.path.dirname(resolved)
        try:
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            refused = self._symlink_refusal(exc, lambda: os.lstat(parent))
            if refused is not None:
                raise refused
            raise _AtomicOpenRefused(
                "cannot open parent directory: %s" % (exc.strerror or exc)
            )
        try:
            # The parent is derived from realpath(resolved): fully canonical.
            # Any deviation means a component was swapped between _resolve()
            # and open(). (No scope-containment demand here: when
            # resolved == root, the parent is the root's own parent.)
            if self._fd_realpath(fd) != parent:
                raise _AtomicOpenRefused(
                    "opened directory is not the resolved parent "
                    "(possible race)"
                )
            return fd
        except _AtomicOpenRefused:
            os.close(fd)
            raise

    def _open_resolved(
        self, resolved: str, flags: int, mode: int = 0o666
    ) -> int:
        """Atomically open a _resolve()d path and verify the opened fd.

        The parent directory is pinned (opened + verified first); the final
        component is opened with openat(parent_fd, base, O_NOFOLLOW|flags),
        so no path-string decision happens after the pin. The opened fd's
        canonical path is re-checked for scope containment before return.

        Raises _AtomicOpenRefused with a public error message, or OSError
        for ordinary filesystem failures (ENOENT, EISDIR, ...).
        """
        open_flags = flags | os.O_NOFOLLOW
        if resolved == self._root:
            # Opening the scope root itself: open directly, then verify the
            # fd really is the root.
            try:
                fd = os.open(resolved, open_flags, mode)
            except OSError as exc:
                refused = self._symlink_refusal(exc, lambda: os.lstat(resolved))
                if refused is not None:
                    raise refused
                raise
            try:
                if self._fd_realpath(fd) != self._root:
                    raise _AtomicOpenRefused(
                        "opened path is not the scope root (possible race)"
                    )
                return fd
            except _AtomicOpenRefused:
                os.close(fd)
                raise
        base = os.path.basename(resolved)
        if not base or base in (".", "..") or "/" in base:
            raise _AtomicOpenRefused("invalid path component")
        parent_fd = self._open_verified_parent(resolved)
        try:
            try:
                fd = os.open(base, open_flags, mode, dir_fd=parent_fd)
            except OSError as exc:
                refused = self._symlink_refusal(
                    exc, lambda: os.lstat(base, dir_fd=parent_fd)
                )
                if refused is not None:
                    raise refused
                raise
            try:
                if not self._fd_in_scope(fd):
                    raise _AtomicOpenRefused(
                        "opened file left the scoped root (possible race)"
                    )
                return fd
            except _AtomicOpenRefused:
                os.close(fd)
                raise
        finally:
            os.close(parent_fd)

    # -- read ------------------------------------------------------------
    def list_dir(self, rel: str = "") -> Dict[str, Any]:
        try:
            resolved = self._resolve(rel)
        except PathEscapeError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            fd = self._open_resolved(
                resolved, os.O_RDONLY | os.O_DIRECTORY
            )
        except _AtomicOpenRefused as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return {"ok": False, "error": "not found"}
            if exc.errno == errno.ENOTDIR:
                return {"ok": False, "error": "not a directory"}
            return {"ok": False, "error": f"unreadable: {exc.strerror or exc}"}
        try:
            # fd-based scandir: entries are resolved relative to the pinned
            # dir fd, so a post-open swap cannot redirect the listing.
            # (Closing the iterator does NOT close our fd; we close it.)
            entries: List[Dict[str, Any]] = []
            it = os.scandir(fd)
            try:
                for de in it:
                    try:
                        st = de.stat(follow_symlinks=True)
                    except OSError:
                        continue
                    is_dir = de.is_dir(follow_symlinks=True)
                    entries.append(
                        {
                            "name": de.name,
                            "rel": os.path.join(self._rel_of(resolved), de.name)
                            if self._rel_of(resolved)
                            else de.name,
                            "is_dir": is_dir,
                            "size_bytes": 0 if is_dir else st.st_size,
                            "mtime": st.st_mtime,
                        }
                    )
            finally:
                it.close()
        finally:
            os.close(fd)
        entries.sort(key=lambda e: (not e["is_dir"], e["name"]))
        return {"ok": True, "entries": entries, "rel": self._rel_of(resolved)}

    def stat(self, rel: str) -> Dict[str, Any]:
        try:
            resolved = self._resolve(rel)
        except PathEscapeError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            fd = self._open_resolved(resolved, os.O_RDONLY)
        except _AtomicOpenRefused as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return {"ok": False, "error": "not found"}
            return {"ok": False, "error": f"unreadable: {exc.strerror or exc}"}
        try:
            st = os.fstat(fd)
        finally:
            os.close(fd)
        is_dir = stat_module.S_ISDIR(st.st_mode)
        return {
            "ok": True,
            "rel": self._rel_of(resolved),
            "is_dir": is_dir,
            "size_bytes": 0 if is_dir else st.st_size,
            "mtime": st.st_mtime,
        }

    def read_text(self, rel: str, max_bytes: int = 200_000) -> Dict[str, Any]:
        try:
            resolved = self._resolve(rel)
        except PathEscapeError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            fd = self._open_resolved(resolved, os.O_RDONLY)
        except _AtomicOpenRefused as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return {"ok": False, "error": "not found"}
            return {"ok": False, "error": "unreadable"}
        try:
            st = os.fstat(fd)
            if stat_module.S_ISDIR(st.st_mode):
                return {"ok": False, "error": "not a file"}
            if st.st_size > max_bytes:
                return {
                    "ok": False,
                    "error": (
                        f"file too large ({st.st_size} bytes > "
                        f"{max_bytes} byte limit)"
                    ),
                }
            buf = bytearray()
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > max_bytes:
                    # Grew between fstat and read: refuse, never serve partial.
                    return {
                        "ok": False,
                        "error": (
                            f"file too large ({len(buf)} bytes > "
                            f"{max_bytes} byte limit)"
                        ),
                    }
            data = bytes(buf)
        finally:
            os.close(fd)
        if b"\x00" in data:
            return {"ok": False, "error": "binary file (contains NUL bytes)"}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "binary file (not valid UTF-8)"}
        return {"ok": True, "rel": self._rel_of(resolved), "content": text}

    # -- write -----------------------------------------------------------
    def write_text(self, rel: str, content: str) -> Dict[str, Any]:
        if not self._writable:
            return {"ok": False, "error": "read-only scope"}
        if not isinstance(content, str):
            return {"ok": False, "error": "content must be a string"}
        # Serialize in-process resolve->open->verify against other threads.
        with self._write_lock:
            try:
                resolved = self._resolve(rel)
            except PathEscapeError as exc:
                return {"ok": False, "error": str(exc)}
            try:
                fd = self._open_resolved(
                    resolved,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o666,
                )
            except _AtomicOpenRefused as exc:
                return {"ok": False, "error": str(exc)}
            except OSError as exc:
                if exc.errno == errno.EISDIR:
                    return {"ok": False, "error": "not a file"}
                return {
                    "ok": False,
                    "error": f"write failed: {exc.strerror or exc}",
                }
            try:
                st = os.fstat(fd)
                if stat_module.S_ISDIR(st.st_mode):
                    return {"ok": False, "error": "not a file"}
                data = content.encode("utf-8")
                view = memoryview(data)
                while view:
                    n = os.write(fd, view)
                    view = view[n:]
                st = os.fstat(fd)
            except OSError as exc:
                return {
                    "ok": False,
                    "error": f"write failed: {exc.strerror or exc}",
                }
            finally:
                os.close(fd)
            return {
                "ok": True,
                "rel": self._rel_of(resolved),
                "size_bytes": st.st_size,
                "mtime": st.st_mtime,
            }

    def mkdir(self, rel: str) -> Dict[str, Any]:
        if not self._writable:
            return {"ok": False, "error": "read-only scope"}
        # Serialize in-process resolve->mutate against other threads.
        with self._write_lock:
            try:
                resolved = self._resolve(rel)
            except PathEscapeError as exc:
                return {"ok": False, "error": str(exc)}
            if resolved == self._root:
                components: List[str] = []
            else:
                sub = os.path.relpath(resolved, self._root)
                components = sub.split(os.sep)
                # realpath-derived, so this cannot trigger; enforced anyway.
                if any(c in ("", ".", "..") for c in components):
                    return {"ok": False, "error": "path escapes the scoped root"}
            try:
                fd = self._mkdir_components(components)
            except _AtomicOpenRefused as exc:
                return {"ok": False, "error": str(exc)}
            except OSError as exc:
                return {
                    "ok": False,
                    "error": f"mkdir failed: {exc.strerror or exc}",
                }
            try:
                st = os.fstat(fd)
            finally:
                os.close(fd)
            return {
                "ok": True,
                "rel": self._rel_of(resolved),
                "mtime": st.st_mtime,
            }

    def _mkdir_components(self, components: List[str]) -> int:
        """Create/walk components one level at a time, each relative to a
        pinned, verified parent dir fd (mkdirat/openat, O_DIRECTORY|
        O_NOFOLLOW). Any symlink component trips ELOOP and is refused.

        Returns the fd of the final directory (caller closes).
        Raises _AtomicOpenRefused or OSError.
        """
        try:
            cur = os.open(
                self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as exc:
            refused = self._symlink_refusal(exc, lambda: os.lstat(self._root))
            if refused is not None:
                raise refused
            raise
        try:
            if self._fd_realpath(cur) != self._root:
                raise _AtomicOpenRefused(
                    "scope root changed during mkdir (possible race)"
                )
            for comp in components:
                if not comp or comp in (".", "..") or "/" in comp:
                    raise _AtomicOpenRefused("invalid path component")
                try:
                    nxt = os.open(
                        comp,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=cur,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(comp, 0o777, dir_fd=cur)
                    except FileExistsError:
                        pass  # raced into existence; the open below decides
                    try:
                        nxt = os.open(
                            comp,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=cur,
                        )
                    except OSError as exc2:
                        refused = self._symlink_refusal(
                            exc2, lambda: os.lstat(comp, dir_fd=cur)
                        )
                        if refused is not None:
                            raise refused
                        raise
                except OSError as exc:
                    refused = self._symlink_refusal(
                        exc, lambda: os.lstat(comp, dir_fd=cur)
                    )
                    if refused is not None:
                        raise refused
                    raise
                os.close(cur)
                cur = nxt
            return cur
        except BaseException:
            os.close(cur)
            raise
    # -- refusal log ---------------------------------------------------
    def _log_refusal(
        self, op: str, archive_rel: str, entry: Optional[str], reason: str
    ) -> None:
        """Append a security-relevant refusal to the in-memory audit log.

        Logged: unsafe entry names, traversal/absolute/symlink
        neutralizations, bomb/oversize refusals, non-zip masquerade.
        NOT logged: ordinary "not found" (not security-relevant).
        In-memory by design — persistence is the caller's wiring.
        """
        rec = {
            "ts": time.time(),
            "op": op,
            "archive": archive_rel,
            "entry": entry,
            "reason": reason,
        }
        with self._refusal_lock:
            self._refusals.append(rec)

    def refusals(self) -> List[Dict[str, Any]]:
        """Copy of the in-memory security refusal log."""
        with self._refusal_lock:
            return list(self._refusals)

    # -- archive inspection --------------------------------------------
    def _open_zip(self, rel: str) -> Tuple[Optional[zipfile.ZipFile], Optional[Dict[str, Any]]]:
        """Resolve `rel` in scope and open it as a zip.

        Returns (ZipFile, None) on success, else (None, error_dict). The
        caller owns closing the ZipFile.
        """
        try:
            path = self._resolve(rel)
        except PathEscapeError as exc:
            return None, {"ok": False, "error": str(exc)}
        if not os.path.exists(path):
            return None, {"ok": False, "error": "not found"}
        if os.path.isdir(path):
            return None, {"ok": False, "error": "not a file"}
        if not zipfile.is_zipfile(path):
            self._log_refusal("open_zip", self._rel_of(path), None,
                              "not a zip archive")
            return None, {"ok": False, "error": "not a zip archive"}
        try:
            zf = zipfile.ZipFile(path, "r")
        except zipfile.BadZipFile as exc:
            self._log_refusal("open_zip", self._rel_of(path), None,
                              f"bad zip: {exc}")
            return None, {"ok": False, "error": f"bad zip file: {exc}"}
        return zf, None

    def _resolve_under(self, dest_real: str, name: str) -> str:
        """Containment re-check for an extraction target under dest_real.

        Defense in depth under _check_entry_name: even if the name check
        had a bug, a target outside dest_real is refused here.
        """
        target = os.path.realpath(os.path.join(dest_real, name))
        try:
            common = os.path.commonpath([dest_real, target])
        except ValueError:
            raise PathEscapeError("extraction target escapes destination")
        if common != dest_real:
            raise PathEscapeError("extraction target escapes destination")
        return target

    def list_archive(
        self, rel: str, max_entries: int = ZIP_MAX_ENTRIES
    ) -> Dict[str, Any]:
        """List a zip archive's entries as stored, with a safety preview.

        Each entry: {name, size_bytes, compressed_bytes, is_dir,
        is_symlink, safe, unsafe_reason}. `safe` uses the same name check
        extraction enforces, so the listing honestly previews extraction.
        """
        zf, err = self._open_zip(rel)
        if err is not None:
            return err
        try:
            infos = zf.infolist()
        except zipfile.BadZipFile as exc:
            zf.close()
            return {"ok": False, "error": f"bad zip file: {exc}"}
        if len(infos) > max_entries:
            zf.close()
            self._log_refusal("list_archive", rel, None,
                              f"too many entries ({len(infos)} > {max_entries})")
            return {
                "ok": False,
                "error": f"too many entries ({len(infos)} > {max_entries})",
            }
        entries: List[Dict[str, Any]] = []
        for zi in infos:
            safe, reason = _check_entry_name(zi.filename)
            is_link = _is_symlink_zi(zi)
            if is_link:
                safe, reason = False, "symlink entry"
            entries.append(
                {
                    "name": zi.filename,
                    "size_bytes": zi.file_size,
                    "compressed_bytes": zi.compress_size,
                    "is_dir": zi.is_dir(),
                    "is_symlink": is_link,
                    "safe": safe,
                    "unsafe_reason": reason,
                }
            )
        zf.close()
        return {"ok": True, "rel": rel, "entries": entries,
                "entry_count": len(entries)}

    def read_inside(
        self, rel: str, name: str, max_bytes: int = READ_INSIDE_MAX_BYTES
    ) -> Dict[str, Any]:
        """Read one zip entry's bytes without extracting to disk.

        Refuses unsafe names, symlinks, directories, and oversize/bomb
        claims (claimed size is checked first, then the streamed read is
        capped — a lying central directory cannot force allocation).
        """
        safe, reason = _check_entry_name(name)
        if not safe:
            self._log_refusal("read_inside", rel, name, reason or "unsafe name")
            return {"ok": False, "error": f"unsafe entry name: {reason}"}
        zf, err = self._open_zip(rel)
        if err is not None:
            return err
        try:
            try:
                zi = zf.getinfo(name)
            except KeyError:
                return {"ok": False, "error": "entry not found"}
            if zi.is_dir():
                return {"ok": False, "error": "not a file"}
            if _is_symlink_zi(zi):
                self._log_refusal("read_inside", rel, name, "symlink entry")
                return {"ok": False, "error": "symlink entries are not readable"}
            if zi.flag_bits & 0x1:
                return {"ok": False, "error": "encrypted entries not supported"}
            if zi.file_size > max_bytes:
                self._log_refusal(
                    "read_inside", rel, name,
                    f"entry too large (claimed {zi.file_size} > {max_bytes} limit)",
                )
                return {
                    "ok": False,
                    "error": f"entry too large (claimed {zi.file_size} bytes "
                             f"> {max_bytes} byte limit)",
                }
            chunks: List[bytes] = []
            total = 0
            with zf.open(zi, "r") as src:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        self._log_refusal(
                            "read_inside", rel, name,
                            f"entry exceeded {max_bytes} bytes while streaming",
                        )
                        return {
                            "ok": False,
                            "error": f"entry too large (> {max_bytes} byte limit)",
                        }
                    chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            zf.close()
        return {"ok": True, "rel": rel, "name": name,
                "size_bytes": len(data), "data": data}

    def extract(
        self,
        rel: str,
        dest_rel: str,
        max_total_bytes: int = ZIP_MAX_TOTAL_BYTES,
        max_entry_bytes: int = ZIP_MAX_ENTRY_BYTES,
        max_entries: int = ZIP_MAX_ENTRIES,
    ) -> Dict[str, Any]:
        """Extract a zip archive into the sandbox — skip-and-neutralize.

        Unsafe entries (traversal, absolute, backslash, symlink, encrypted,
        reserved provenance name) are SKIPPED, logged, and reported in
        `skipped` — never written. Whole-archive refusal on entry-count or
        claimed-total-size bombs. Per-entry and running-total streaming caps
        catch central directories that lie about sizes. Every target is
        re-checked for containment. A provenance manifest
        (`.remor_extract_provenance.json`) is written into the destination.
        """
        if not self._writable:
            return {"ok": False, "error": "read-only scope"}
        try:
            dest_real = self._resolve(dest_rel)
        except PathEscapeError as exc:
            return {"ok": False, "error": str(exc)}
        zf, err = self._open_zip(rel)
        if err is not None:
            return err
        try:
            infos = zf.infolist()
        except zipfile.BadZipFile as exc:
            zf.close()
            return {"ok": False, "error": f"bad zip file: {exc}"}
        if len(infos) > max_entries:
            zf.close()
            self._log_refusal("extract", rel, None,
                              f"too many entries ({len(infos)} > {max_entries})")
            return {
                "ok": False,
                "error": f"too many entries ({len(infos)} > {max_entries})",
            }
        claimed_total = sum(zi.file_size for zi in infos if not zi.is_dir())
        if claimed_total > max_total_bytes:
            zf.close()
            self._log_refusal(
                "extract", rel, None,
                f"archive too large (claimed {claimed_total} > "
                f"{max_total_bytes} limit)",
            )
            return {
                "ok": False,
                "error": f"archive too large (claimed {claimed_total} bytes "
                         f"> {max_total_bytes} byte limit)",
            }
        try:
            os.makedirs(dest_real, exist_ok=True)
        except OSError as exc:
            zf.close()
            return {"ok": False, "error": f"mkdir failed: {exc.strerror or exc}"}

        extracted: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        running_total = 0

        def _skip(name: str, why: str) -> None:
            skipped.append({"name": name, "reason": why})
            self._log_refusal("extract", rel, name, why)

        for zi in infos:
            name = zi.filename
            safe, reason = _check_entry_name(name)
            if not safe:
                _skip(name, reason or "unsafe name")
                continue
            if name == PROVENANCE_NAME:
                _skip(name, "reserved provenance name")
                continue
            if _is_symlink_zi(zi):
                _skip(name, "symlink entry")
                continue
            if zi.flag_bits & 0x1:
                _skip(name, "encrypted entries not supported")
                continue
            try:
                target = self._resolve_under(dest_real, name)
            except PathEscapeError as exc:
                _skip(name, str(exc))
                continue
            if zi.is_dir():
                try:
                    os.makedirs(target, exist_ok=True)
                except OSError as exc:
                    _skip(name, f"mkdir failed: {exc.strerror or exc}")
                continue
            # File entry: stream with per-entry and running-total caps.
            parent = os.path.dirname(target)
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as exc:
                _skip(name, f"mkdir failed: {exc.strerror or exc}")
                continue
            digest = hashlib.sha256()
            written = 0
            ok_entry = True
            fail_reason = ""
            try:
                with zf.open(zi, "r") as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        running_total += len(chunk)
                        if written > max_entry_bytes:
                            ok_entry = False
                            fail_reason = (
                                f"entry exceeded {max_entry_bytes} bytes "
                                "while streaming"
                            )
                            break
                        if running_total > max_total_bytes:
                            ok_entry = False
                            fail_reason = (
                                f"archive exceeded {max_total_bytes} bytes "
                                "while streaming"
                            )
                            break
                        digest.update(chunk)
                        dst.write(chunk)
            except (zipfile.BadZipFile, OSError) as exc:
                ok_entry = False
                fail_reason = f"read/write failed: {exc}"
            if not ok_entry:
                try:
                    os.unlink(target)
                except OSError:
                    pass
                _skip(name, fail_reason)
                continue
            extracted.append(
                {"name": name, "size_bytes": written,
                 "sha256": digest.hexdigest()}
            )

        zf.close()
        provenance = {
            "archive": rel,
            "extracted_at": time.time(),
            "extractor": "swarm_engine.services.files.ScopedFileService.extract",
            "dest": self._rel_of(dest_real),
            "entries": extracted,
            "skipped": skipped,
        }
        try:
            with open(os.path.join(dest_real, PROVENANCE_NAME), "w",
                      encoding="utf-8") as fh:
                json.dump(provenance, fh, indent=2, sort_keys=True)
        except OSError as exc:
            return {"ok": False, "error": f"provenance write failed: {exc}"}
        return {
            "ok": True,
            "rel": rel,
            "dest": self._rel_of(dest_real),
            "extracted": [e["name"] for e in extracted],
            "skipped": skipped,
            "provenance": provenance,
        }
