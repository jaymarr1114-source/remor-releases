"""Backup and restore for anchored databases (Phase 3, Worker E, 2026-09-25).

A backup bundle is a directory holding:

- ``db.sqlite``      -- transactionally consistent snapshot of the database
                        (taken with the sqlite backup API, so it is safe
                        even if the source DB is live)
- ``journal``        -- verbatim copy of the anchor journal (if any)
- ``key``            -- verbatim copy of the anchor key (0600) (if any)
- ``manifest.json``  -- sha256 of each file, the journal tip digest
                        ("anchor head digest"), the tip's committed
                        scope_heads, key_id, created_at, source paths

Design rules (fail-closed):

1. create_backup REFUSES mismatched sets: when the caller supplies
   ``collect_heads`` (a zero-arg callable returning the live
   {scope: head_digest}), the live heads must equal the journal tip's
   committed heads or no bundle is written. It also refuses when the
   journal advances *during* the backup (a concurrent writer raced the
   snapshot) -- the half-written bundle is deleted.
2. verify_backup re-hashes every file against the manifest and
   cross-checks the journal tip against the manifest's recorded tip.
   Any mismatch -> (False, explicit reason). It never raises on corrupt
   input.
3. restore_backup verifies the bundle FIRST, refuses to restore over a
   live/owned target, stages the restore into a scratch dir, re-verifies
   the STAGED set (journal chain + key match + journal-vs-manifest via
   the anchor's own public verify(), then the optional anchor_factory's
   verify_all() against the staged DB), and only then commits with
   atomic os.replace per file. A bundle whose journal does not match
   its DB, or whose key does not match its journal, is REFUSED with
   nothing overwritten -- the originals are never touched before every
   check passes.
4. Never restore over a live-open DB: restore_backup refuses when the
   target DB is open in another live process (detected by scanning
   /proc/*/fd for the file's (st_dev, st_ino)), when SQLite sidecars
   (-wal / -journal) indicate un-checkpointed state, or when an
   ownership marker ``<db>.owner`` is present.

Worker-A ownership seam (file convention only -- this module never
imports or edits Worker A's files): Worker A's ownership layer
(``runtime/core/db_ownership.py``) claims a database by holding
``fcntl.flock(LOCK_EX)`` on ``<abspath(db)>.owner.lock`` for the claim's
lifetime. Before restoring, this module probes that exact lockfile: if
it exists and a non-blocking exclusive flock on it FAILS, a live owner
holds the claim and the restore is refused. If the lockfile is absent
(or the lock is free -- a stale lockfile from a dead owner), the
restore proceeds. The probe never creates the lockfile and never holds
a lock beyond the probe.

Multi-DB anchors (CrossDbAnchor over scheduler_runs + artifacts):
create_backup covers ONE database per bundle. The operational pattern
is one bundle per protected DB (all sharing the same journal/key
copies), then restore each bundle with ``anchor_factory=None``, then
run the real anchor's verify_all() over the restored SET (both DBs with
their providers rebound). restore_backup's ``anchor_factory`` hook
(journal_path, key_path, db_path) -> object with verify_all() covers
the single-DB-anchored case directly.

Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

FORMAT = "remor-backup/1"
MANIFEST_NAME = "manifest.json"
_DB_NAME = "db.sqlite"
_JOURNAL_NAME = "journal"
_KEY_NAME = "key"

#: Suffix of Worker A's owner lockfile: abspath(db) + this.
#: (Mirrors runtime/core/db_ownership._lock_path_for; file convention
#: only -- this module never imports that file.)
OWNER_LOCK_SUFFIX = ".owner.lock"

#: SQLite sidecars whose non-empty presence means "un-checkpointed or
#: hot-journal state" -- restoring the main file under them is refused.
_HOT_SIDECARS = ("-wal", "-journal")


class BackupError(Exception):
    """A backup/restore operation was refused or failed. Fail closed."""


# ---------------------------------------------------------------------------
# small helpers


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json_atomic(path: str, obj: Any) -> None:
    """Write JSON atomically (temp + fsync + os.replace)."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    # fsync the directory entry so the rename is durable
    try:
        dfd = os.open(directory, os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _snapshot_db(src_db_path: str, dest_path: str) -> None:
    """Consistent snapshot of a (possibly live) SQLite DB via backup API.

    Written atomically: backup into a temp file in the destination dir,
    fsync, os.replace.
    """
    directory = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", dir=directory)
    os.close(fd)
    try:
        src = sqlite3.connect(src_db_path, timeout=30)
        try:
            dst = sqlite3.connect(tmp, timeout=30)
            try:
                src.backup(dst)
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
        # fsync the snapshot before the rename
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, dest_path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_journal_tip(journal_path: str) -> Dict[str, Any]:
    """Read the tip record of a JSONL anchor journal. No side effects.

    Returns {"record_digest", "seq", "scope_heads", "key_id"}.
    Raises BackupError if the journal is missing/empty/corrupt.
    """
    try:
        with open(journal_path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError as exc:
        raise BackupError(f"cannot read journal {journal_path!r}: {exc}")
    if not lines:
        raise BackupError(f"journal {journal_path!r} has no records")
    try:
        tip = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise BackupError(
            f"journal {journal_path!r}: tip line is not JSON: {exc}")
    if not isinstance(tip, dict):
        raise BackupError(f"journal {journal_path!r}: tip is not an object")
    for field in ("record_digest", "seq", "scope_heads", "key_id"):
        if field not in tip:
            raise BackupError(
                f"journal {journal_path!r}: tip missing field {field!r}")
    if not isinstance(tip["scope_heads"], dict):
        raise BackupError(
            f"journal {journal_path!r}: tip scope_heads is not an object")
    return {
        "record_digest": str(tip["record_digest"]),
        "seq": tip["seq"],
        "scope_heads": {str(k): str(v)
                         for k, v in tip["scope_heads"].items()},
        "key_id": str(tip["key_id"]),
    }


def _key_id(key_bytes: bytes) -> str:
    # Mirrors swarm_engine.governance.anchor._key_id (sha256 hex, 16 chars).
    return hashlib.sha256(key_bytes).hexdigest()[:16]


# ---------------------------------------------------------------------------
# create


def create_backup(db_path: str,
                  journal_path: Optional[str] = None,
                  key_path: Optional[str] = None,
                  dest_dir: str = "",
                  collect_heads: Optional[Callable[[], Dict[str, str]]] = None
                  ) -> str:
    """Create a backup bundle of one database (+ optional anchor files).

    ``dest_dir`` is the bundle directory itself: it is created and must
    not already exist (refusing avoids mixing two backups). Returns the
    absolute bundle path.

    When ``journal_path``/``key_path`` are given, both files must exist
    and are copied into the bundle; when omitted (None), the bundle is
    DB-only and ``manifest["anchor"]`` is null.

    When ``collect_heads`` is given (and a journal is included), the
    live heads must equal the journal tip's committed heads, otherwise
    the backup is REFUSED as a mismatched set -- backing up a
    journal/DB pair that already disagree would produce a bundle that
    can never verify on restore.

    A journal that advances during the snapshot (concurrent writer)
    also refuses: the partial bundle is deleted.
    """
    if not dest_dir:
        raise BackupError("create_backup: dest_dir is required")
    db_abs = os.path.abspath(db_path)
    if not os.path.isfile(db_abs):
        raise BackupError(f"create_backup: database not found: {db_abs!r}")
    bundle = os.path.abspath(dest_dir)
    if os.path.exists(bundle):
        raise BackupError(
            f"create_backup: bundle dir already exists (refusing to mix "
            f"backups): {bundle!r}")
    os.makedirs(bundle, mode=0o700)

    journal_abs = os.path.abspath(journal_path) if journal_path else None
    key_abs = os.path.abspath(key_path) if key_path else None
    if journal_abs and not os.path.isfile(journal_abs):
        shutil.rmtree(bundle, ignore_errors=True)
        raise BackupError(
            f"create_backup: journal given but file missing: "
            f"{journal_abs!r} -- refusing to back up a mismatched set")
    if journal_abs and (not key_abs or not os.path.isfile(key_abs)):
        shutil.rmtree(bundle, ignore_errors=True)
        raise BackupError(
            f"create_backup: journal included but key missing "
            f"({key_abs!r}) -- refusing to back up a mismatched set")

    try:
        anchor_info: Optional[Dict[str, Any]] = None
        if journal_abs and key_abs:
            tip = _read_journal_tip(journal_abs)
            if collect_heads is not None:
                live = collect_heads()
                if not isinstance(live, dict):
                    raise BackupError(
                        "create_backup: collect_heads() did not return "
                        "a dict")
                live = {str(k): str(v) for k, v in live.items()}
                if live != tip["scope_heads"]:
                    diff = sorted(
                        {s for s in set(live) | set(tip["scope_heads"])
                         if live.get(s) != tip["scope_heads"].get(s)})[:8]
                    raise BackupError(
                        "create_backup refused: live heads differ from "
                        "the journal tip's committed heads "
                        f"({len(diff)} differing scope(s): "
                        f"{', '.join(diff)}); backing up a mismatched "
                        "set would produce an unrestorable bundle")
            with open(key_abs, "rb") as fh:
                key_bytes = fh.read()
            if _key_id(key_bytes) != tip["key_id"]:
                raise BackupError(
                    "create_backup refused: the key file's id "
                    f"{_key_id(key_bytes)!r} does not match the journal "
                    f"tip's key_id {tip['key_id']!r} (wrong key for this "
                    "journal)")
            anchor_info = {
                "journal_tip_digest": tip["record_digest"],
                "tip_seq": tip["seq"],
                "scope_heads": tip["scope_heads"],
                "key_id": tip["key_id"],
            }

        # Snapshot the DB (consistent even if live), then the anchor
        # files, then re-read the journal tip: if it moved, a writer
        # raced the backup and the bundle is torn.
        db_dest = os.path.join(bundle, _DB_NAME)
        _snapshot_db(db_abs, db_dest)
        if journal_abs and key_abs:
            shutil.copy2(journal_abs, os.path.join(bundle, _JOURNAL_NAME))
            key_dest = os.path.join(bundle, _KEY_NAME)
            shutil.copy2(key_abs, key_dest)
            os.chmod(key_dest, 0o600)
            tip_after = _read_journal_tip(
                os.path.join(bundle, _JOURNAL_NAME))
            # Compare against the LIVE journal: the bundle copy was taken
            # after the snapshot, so compare with a fresh live read.
            tip_live = _read_journal_tip(journal_abs)
            if (tip_live["record_digest"] != tip["record_digest"]
                    or tip_after["record_digest"] != tip["record_digest"]):
                raise BackupError(
                    "create_backup refused: the journal advanced during "
                    "the backup (concurrent writer raced the snapshot); "
                    "no bundle was written -- retry when quiescent")

        files: Dict[str, Dict[str, str]] = {
            "db": {"name": _DB_NAME,
                   "sha256": _sha256_file(db_dest)},
        }
        if journal_abs and key_abs:
            files["journal"] = {
                "name": _JOURNAL_NAME,
                "sha256": _sha256_file(os.path.join(bundle, _JOURNAL_NAME)),
            }
            files["key"] = {
                "name": _KEY_NAME,
                "sha256": _sha256_file(os.path.join(bundle, _KEY_NAME)),
            }
        manifest = {
            "format": FORMAT,
            "created_at": _utcnow(),
            "sources": {
                "db": db_abs,
                "journal": journal_abs,
                "key": key_abs,
            },
            "files": files,
            "anchor": anchor_info,
            "db_snapshot": {"method": "sqlite-backup-api"},
        }
        _write_json_atomic(os.path.join(bundle, MANIFEST_NAME), manifest)
        os.chmod(bundle, 0o700)
        return bundle
    except BaseException:
        shutil.rmtree(bundle, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# verify


def _read_manifest(bundle_dir: str) -> Dict[str, Any]:
    mpath = os.path.join(bundle_dir, MANIFEST_NAME)
    try:
        with open(mpath, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except FileNotFoundError:
        raise BackupError(f"bundle has no {MANIFEST_NAME}: {bundle_dir!r}")
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"bundle manifest unreadable: {exc}")
    if not isinstance(manifest, dict):
        raise BackupError("bundle manifest is not an object")
    if manifest.get("format") != FORMAT:
        raise BackupError(
            f"bundle format {manifest.get('format')!r} != {FORMAT!r}")
    if not isinstance(manifest.get("files"), dict):
        raise BackupError("bundle manifest has no 'files' section")
    return manifest


def verify_backup(bundle_dir: str) -> Tuple[bool, str]:
    """Verify a backup bundle. Fail-closed: never raises on corrupt input.

    Re-hashes every file listed in the manifest and compares; then, when
    the bundle carries an anchor, cross-checks the journal tip (digest,
    seq, scope_heads, key_id) against the manifest's recorded tip.
    Returns (True, msg) or (False, explicit reason).
    """
    try:
        return _verify_backup_inner(os.path.abspath(bundle_dir))
    except BackupError as exc:
        return False, str(exc)
    except Exception as exc:  # fail closed on anything unexpected
        return False, f"bundle verify failed: {type(exc).__name__}: {exc}"


def _verify_backup_inner(bundle: str) -> Tuple[bool, str]:
    if not os.path.isdir(bundle):
        return False, f"bundle dir not found: {bundle!r}"
    try:
        manifest = _read_manifest(bundle)
    except BackupError as exc:
        return False, str(exc)
    for section, spec in manifest["files"].items():
        name = spec.get("name")
        expect = spec.get("sha256")
        if not name or not expect:
            return False, (
                f"manifest files[{section!r}] missing name/sha256")
        if (os.path.basename(name) != name or name.startswith(".")
                or not name.isprintable()):
            return False, (
                f"manifest files[{section!r}] has an unsafe file name "
                f"{name!r} -- refusing")
        fpath = os.path.join(bundle, name)
        if not os.path.isfile(fpath):
            return False, (
                f"bundle file missing: {name!r} "
                f"(manifest section {section!r})")
        actual = _sha256_file(fpath)
        if actual != expect:
            return False, (
                f"bundle file {name!r} hash mismatch "
                f"(manifest section {section!r}): expected "
                f"{expect[:16]}..., got {actual[:16]}... -- refusing")
    anchor = manifest.get("anchor")
    if anchor is not None:
        if not isinstance(anchor, dict):
            return False, "manifest 'anchor' section is not an object"
        jpath = os.path.join(bundle, _JOURNAL_NAME)
        try:
            tip = _read_journal_tip(jpath)
        except BackupError as exc:
            return False, f"bundle journal unreadable: {exc}"
        if tip["record_digest"] != anchor.get("journal_tip_digest"):
            return False, (
                "bundle journal tip digest does not match the manifest "
                f"(journal {tip['record_digest'][:16]}... vs manifest "
                f"{str(anchor.get('journal_tip_digest'))[:16]}...) -- "
                "the journal was swapped or modified after the backup")
        if tip["scope_heads"] != anchor.get("scope_heads"):
            return False, (
                "bundle journal tip scope_heads do not match the "
                "manifest -- the journal was swapped or modified after "
                "the backup")
        kpath = os.path.join(bundle, _KEY_NAME)
        try:
            with open(kpath, "rb") as fh:
                kid = _key_id(fh.read())
        except OSError as exc:
            return False, f"bundle key unreadable: {exc}"
        if kid != anchor.get("key_id") or kid != tip["key_id"]:
            return False, (
                f"bundle key id {kid!r} does not match the manifest "
                f"{anchor.get('key_id')!r} / journal tip "
                f"{tip['key_id']!r} -- wrong key for this journal")
    n = len(manifest["files"])
    anchored = "anchored" if anchor is not None else "db-only"
    return True, (
        f"bundle verified: {n} file(s), {anchored}, "
        f"created {manifest.get('created_at')}")


# ---------------------------------------------------------------------------
# live-target refusal


def _live_db_holders(db_path: str) -> List[int]:
    """PIDs (other than this process) holding an open fd to db_path.

    Best-effort Linux /proc scan comparing (st_dev, st_ino). Returns []
    when /proc is unavailable.
    """
    holders: List[int] = []
    try:
        want = (os.stat(db_path).st_dev, os.stat(db_path).st_ino)
    except OSError:
        return holders
    if not os.path.isdir("/proc"):
        return holders
    me = str(os.getpid())
    try:
        pids = os.listdir("/proc")
    except OSError:
        return holders
    for pid in pids:
        if not pid.isdigit() or pid == me:
            continue
        fd_dir = os.path.join("/proc", pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            fdp = os.path.join(fd_dir, fd)
            try:
                fst = os.stat(fdp)
            except OSError:
                continue
            if (fst.st_dev, fst.st_ino) == want:
                holders.append(int(pid))
                break
    return sorted(set(holders))


def _refuse_if_owned(db_path: str) -> None:
    """Refuse when Worker A's owner lock is held by a live process.

    File-convention probe (no import of db_ownership): open the
    ``<abspath(db)>.owner.lock`` file WITHOUT creating it and try a
    non-blocking exclusive flock. BlockingIOError -> a live owner holds
    the claim -> refuse. Success -> no live owner (stale lockfile at
    most); release immediately and proceed. Never creates the lockfile,
    never holds the lock beyond the probe.
    """
    import fcntl

    lock_path = os.path.abspath(db_path) + OWNER_LOCK_SUFFIX
    if not os.path.exists(lock_path):
        return
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return  # vanished or unreadable: nothing to coordinate with
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError(
                "restore refused: target database is owned -- the "
                f"Worker-A owner lock {lock_path!r} is held by a live "
                f"process. Stop the owning service first; this module "
                f"never restores over an owned DB.")
        except OSError as exc:
            raise BackupError(
                "restore refused: cannot probe the Worker-A owner lock "
                f"{lock_path!r} ({type(exc).__name__}: {exc}); failing "
                f"closed rather than restoring over a possibly-owned DB.")
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
    finally:
        os.close(fd)


def _refuse_if_target_live(db_path: str) -> None:
    """Refuse to restore over a live/owned/hot target. Raises BackupError."""
    db_abs = os.path.abspath(db_path)
    # 1. Worker-A ownership seam: live owner lock (file convention).
    _refuse_if_owned(db_abs)
    if os.path.exists(db_abs):
        # 2. Live open file descriptors in other processes.
        holders = _live_db_holders(db_abs)
        if holders:
            raise BackupError(
                "restore refused: target database is open in live "
                f"process(es) {holders}. Stop the owning services "
                "first -- restoring under a live store would leave it "
                "reading the pre-restore inode.")
        # 3. Hot SQLite sidecars: un-checkpointed WAL or a hot rollback
        # journal. The sidecar belongs to the CURRENT main file; after an
        # os.replace it would be stale/foreign.
        for suffix in _HOT_SIDECARS:
            sidecar = db_abs + suffix
            try:
                if (os.path.isfile(sidecar)
                        and os.path.getsize(sidecar) > 0):
                    raise BackupError(
                        "restore refused: target has a non-empty SQLite "
                        f"sidecar {sidecar!r} (un-checkpointed WAL or "
                        "hot rollback journal). Resolve it (checkpoint / "
                        "clean shutdown) before restoring.")
            except OSError:
                pass


def _atomic_replace(src: str, dst: str, mode: Optional[int] = None) -> None:
    """Copy src -> dst.tmp, fsync, os.replace. Raises BackupError on OSError."""
    ddir = os.path.dirname(os.path.abspath(dst))
    os.makedirs(ddir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".restore_tmp_", dir=ddir)
    try:
        with os.fdopen(fd, "wb") as out:
            with open(src, "rb") as src_fh:
                shutil.copyfileobj(src_fh, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        try:
            os.replace(tmp, dst)
        except OSError as exc:
            raise BackupError(
                f"restore failed committing {dst!r}: {exc} "
                f"({type(exc).__name__}; the target may be immutable, "
                f"e.g. an append-only journal)")
        try:
            dfd = os.open(ddir, os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# restore


def restore_backup(bundle_dir: str,
                    db_path: str,
                    journal_path: Optional[str] = None,
                    key_path: Optional[str] = None,
                    anchor_factory: Optional[Callable[
                        [str, str, str], Any]] = None,
                    include_anchor: bool = True) -> Dict[str, Any]:
    """Restore a backup bundle over the target paths. Fail-closed.

    Steps: verify the bundle (hashes) -> refuse live/owned targets ->
    stage copies into a scratch dir -> re-verify the STAGED set with the
    anchor's own public verify() (journal chain integrity + key match +
    journal-vs-manifest tip) -> optionally run
    ``anchor_factory(staged_journal, staged_key, staged_db).verify_all()``
    against the staged DB -> commit each file atomically (temp +
    os.replace).

    ``anchor_factory(journal_path, key_path, db_path)`` must return an
    object with ``verify_all() -> (bool, str)`` (e.g. a bound
    CrossDbAnchor); it runs against the STAGED copies, so a failing
    bundle is refused with NOTHING overwritten.

    ``include_anchor=False`` restores only the DB file (used by crash
    recovery: the live journal may legitimately have advanced past the
    backup; restoring the stale journal would diverge it). When False,
    ``journal_path``/``key_path`` must be None.

    Raises BackupError with an explicit reason on any refusal. Returns a
    result dict on success.
    """
    bundle = os.path.abspath(bundle_dir)
    ok, msg = verify_backup(bundle)
    if not ok:
        raise BackupError(f"restore refused: {msg}")
    manifest = _read_manifest(bundle)
    has_anchor = manifest.get("anchor") is not None

    if not include_anchor and (journal_path or key_path):
        raise BackupError(
            "restore_backup: include_anchor=False with journal/key "
            "targets is contradictory")
    if has_anchor and include_anchor and (not journal_path or not key_path):
        raise BackupError(
            "restore refused: bundle is anchored but no journal/key "
            "targets were given")

    db_abs = os.path.abspath(db_path)
    journal_abs = os.path.abspath(journal_path) if journal_path else None
    key_abs = os.path.abspath(key_path) if key_path else None

    # Refuse live/owned targets BEFORE staging anything.
    _refuse_if_target_live(db_abs)

    stage = tempfile.mkdtemp(prefix="remor_restore_")
    restored: List[str] = []
    try:
        # Staging layout keeps the anchor files OUTSIDE the staged DB's
        # parent dir (the anchor's own placement rule).
        stage_db_dir = os.path.join(stage, "db")
        stage_anchor_dir = os.path.join(stage, "anchor")
        os.makedirs(stage_db_dir)
        os.makedirs(stage_anchor_dir)
        staged_db = os.path.join(stage_db_dir, _DB_NAME)
        shutil.copy2(os.path.join(bundle, _DB_NAME), staged_db)

        staged_journal = staged_key = None
        if has_anchor and include_anchor:
            assert journal_abs and key_abs
            staged_journal = os.path.join(stage_anchor_dir, _JOURNAL_NAME)
            staged_key = os.path.join(stage_anchor_dir, _KEY_NAME)
            shutil.copy2(os.path.join(bundle, _JOURNAL_NAME), staged_journal)
            shutil.copy2(os.path.join(bundle, _KEY_NAME), staged_key)
            os.chmod(staged_key, 0o600)

            # Staged anchor check with the anchor's OWN public verify():
            # passing the manifest's recorded scope_heads as the "live"
            # heads means (True) iff the staged journal chain is intact,
            # the staged key matches the journal, and the staged journal
            # is the one the manifest describes (swap detection).
            from swarm_engine.governance.anchor import AnchorStore
            anchor = AnchorStore(staged_journal, staged_key, staged_db)
            expect_heads = manifest["anchor"]["scope_heads"]
            aok, amsg = anchor.verify(expect_heads)
            if not aok:
                raise BackupError(
                    "restore refused: staged journal/key check failed: "
                    f"{amsg}")
            if anchor_factory is not None:
                bound = anchor_factory(staged_journal, staged_key,
                                       staged_db)
                vok, vmsg = bound.verify_all()
                if not vok:
                    raise BackupError(
                        "restore refused: staged set failed "
                        f"verify_all(): {vmsg}")

        # All checks passed: commit atomically, DB first.
        _atomic_replace(staged_db, db_abs)
        restored.append(f"db -> {db_abs}")
        if staged_journal and journal_abs:
            _atomic_replace(staged_journal, journal_abs)
            restored.append(f"journal -> {journal_abs}")
        if staged_key and key_abs:
            _atomic_replace(staged_key, key_abs, mode=0o600)
            restored.append(f"key -> {key_abs}")
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    return {
        "restored": restored,
        "bundle": bundle,
        "anchor_included": bool(has_anchor and include_anchor),
        "anchor_factory_verified": anchor_factory is not None,
    }
