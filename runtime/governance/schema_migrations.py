"""Schema migration framework (Batch 8, 2026-09-25; crash-safe two-phase
since Phase 3, 2026-09-25, Worker E).

Centralizes the five ad-hoc migration sites (M1-M5) into a versioned,
transactional framework. Previously each site did its own
`PRAGMA table_info` guard or try/except around `ALTER TABLE ... ADD COLUMN`
with no version tracking, no ordering guarantees, and no rollback.

Crash-safe two-phase design (Phase 3):

  (a) INTENT -- before touching anything, write a migration-intent record
      (migration ids, started_at, pre-migration DB state digest) to a
      sidecar file ``<db>.migration_intent.json`` (atomic temp+rename).
      A sidecar file is used instead of a row in ``schema_migrations``
      because the intent must survive the migration transaction itself:
      a crash mid-batch rolls the transaction back, which would also
      roll back an in-table intent, destroying the evidence that a
      migration was in flight.
  (b) BACKUP -- take a real backup bundle
      (``swarm_engine.governance.backup.create_backup``) and record its
      path in the intent.
  (c) APPLY -- run the pending migrations in ONE transaction
      (``_run_migration_batch``; SQLite DDL is transactional, so a
      failure or crash can never leave a partial migration committed).
  (d) VERIFY -- run the migration assertions (same checks as
      ``verify_migrations`` minus the intent gate, which the running
      call owns).
  (e) FINALIZE -- record the post-migration state digest on the applied
      rows, delete the pre-migration backup bundle, remove the intent.

Crash between (a) and (e): the intent file dangles. The NEXT
``apply_migrations`` call REFUSES to run (``MigrationIncompleteError``)
and ``verify_migrations`` FAILS -- a partial migration can never verify
as trusted. Recovery is the explicit ``recover_migration(db_path)``
operator command, which is deliberately NOT automatic:

  * automatic rollback would silently discard legitimate writes made
    after a cleanly-rolled-back failure;
  * if the crash happened DURING the backup (b), the backup itself may
    be torn -- auto-restoring from it is unsafe;
  * this matches the codebase's fail-closed posture (LegacySchemaError,
    journal-deletion boot refusal).

``recover_migration`` is smart about the common cases: if the live DB
digest equals the intent's pre-migration digest, the migration never
committed (clean rollback / crash before commit) -- it just clears the
intent. If the migration assertions pass, the batch committed and only
finalization was lost -- it finalizes. Otherwise it restores the DB
file from the verified pre-migration backup bundle (DB file only; the
live journal may legitimately have advanced past the backup, so the
journal/key are NOT rolled back -- a diverged anchor then needs the
audited transition() path, which recover reports).

The five migrations:
- M1 (v1): memory/failure_memory.py -- ADD COLUMN context TEXT to failure_memory
- M2 (v2): synthesis/acquisition_learning.py -- ADD COLUMN policy TEXT to acquisition_experience
- M3 (v3): longhorizon/cycle_loop.py -- ADD COLUMN heldout_failed INTEGER to lh_challenge
- M4 (v4): cognition/representations.py -- ADD COLUMN law TEXT to case_memory
- M5 (v5): governance/oracle_binding.py -- ADD COLUMN supplier_id TEXT to ob_evaluations

All are additive and idempotent (IF NOT EXISTS semantics via version check).
"""

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from typing import List, Tuple, Dict, Any, Optional

# Migration definitions: (version, description, up_statements, down_statements)
# down_statements for ADD COLUMN is None (requires table rebuild, handled separately)
MIGRATIONS: List[Tuple[int, str, List[str], List[str]]] = [
    (1, "M1: failure_memory.context TEXT",
     ["ALTER TABLE failure_memory ADD COLUMN context TEXT"],
     []),
    (2, "M2: acquisition_experience.policy TEXT",
     ["ALTER TABLE acquisition_experience ADD COLUMN policy TEXT"],
     []),
    (3, "M3: lh_challenge.heldout_failed INTEGER",
     ["ALTER TABLE lh_challenge ADD COLUMN heldout_failed INTEGER DEFAULT 0"],
     []),
    (4, "M4: case_memory.law TEXT",
     ["ALTER TABLE case_memory ADD COLUMN law TEXT"],
     []),
    (5, "M5: ob_evaluations.supplier_id TEXT",
     ["ALTER TABLE ob_evaluations ADD COLUMN supplier_id TEXT"],
     []),
]

#: Sidecar intent file: <db_path> + this suffix.
INTENT_SUFFIX = ".migration_intent.json"

INTENT_FORMAT = "remor-migration-intent/1"


class MigrationError(Exception):
    """A migration operation failed. Fail closed."""


class MigrationIncompleteError(MigrationError):
    """A previous migration did not finish: dangling intent record.

    apply_migrations refuses to run until the operator resolves it with
    recover_migration(db_path).
    """


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# intent sidecar


def intent_path_for(db_path: str) -> str:
    """Absolute path of the migration-intent sidecar for a database."""
    return os.path.abspath(db_path) + INTENT_SUFFIX


def _read_intent(db_path: str) -> Optional[Dict[str, Any]]:
    """Read the intent sidecar, or None when no migration is in flight."""
    ipath = intent_path_for(db_path)
    if not os.path.exists(ipath):
        return None
    try:
        with open(ipath, "r", encoding="utf-8") as fh:
            intent = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(
            f"migration intent file {ipath!r} is corrupt ({exc}); refusing "
            f"to proceed -- resolve manually or delete it only if you can "
            f"prove no migration is in flight")
    if not isinstance(intent, dict):
        raise MigrationError(
            f"migration intent file {ipath!r} is not an object; refusing")
    return intent


def _write_intent_file(ipath: str, intent: Dict[str, Any]) -> None:
    directory = os.path.dirname(ipath)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".intent_tmp_", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(intent, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, ipath)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_intent(db_path: str, intent: Dict[str, Any]) -> None:
    _write_intent_file(intent_path_for(db_path), intent)


def _update_intent(db_path: str, updates: Dict[str, Any]) -> None:
    intent = _read_intent(db_path) or {}
    intent.update(updates)
    _write_intent(db_path, intent)


def _clear_intent(db_path: str) -> None:
    try:
        os.remove(intent_path_for(db_path))
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise MigrationError(
            f"could not remove migration intent file: {exc}")


# ---------------------------------------------------------------------------
# state digest (crash-safe: via the backup API, no checkpoint needed)


def _db_state_digest(db_path: str) -> str:
    """sha256 of a transactionally consistent snapshot of the DB.

    Uses the sqlite backup API into a temp file, so the digest is well
    defined even for WAL-mode databases and even while other
    connections exist. This is the "pre-migration head digest" recorded
    in the intent: after a crash, equality with the live digest proves
    the migration never committed.
    """
    fd, tmp = tempfile.mkstemp(prefix=".digest_tmp_")
    os.close(fd)
    try:
        src = sqlite3.connect(db_path, timeout=30)
        try:
            dst = sqlite3.connect(tmp, timeout=30)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        h = hashlib.sha256()
        with open(tmp, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# bookkeeping table


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create schema_migrations if not exists (with post_digest column).

    ``post_digest`` records the DB state digest right after the version
    was applied (step (e) of the two-phase protocol). Pre-existing
    tables (created by older code) gain the column via a guarded ALTER.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL,
            post_digest TEXT
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(schema_migrations)")]
    if "post_digest" not in cols:
        try:
            conn.execute("ALTER TABLE schema_migrations "
                         "ADD COLUMN post_digest TEXT")
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc):
                raise


def get_applied_versions(db_path: str) -> List[int]:
    """Return sorted list of applied migration versions (pure read)."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _ensure_migrations_table(conn)
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_user_version(db_path: str) -> int:
    """Return PRAGMA user_version (pure read)."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# migration batch (single transaction)


def _run_migration_batch(db_path: str, versions: List[int],
                         pre_user_version: int) -> Dict[str, Any]:
    """Apply ``versions`` inside ONE transaction. True atomicity.

    Re-checks the pending set under BEGIN IMMEDIATE so a concurrent
    migrator that committed first turns this call into a clean no-op.
    On failure, rolls back (WAL-safe, unlike file-copy restore which a
    stale -wal can defeat) and restores PRAGMA user_version manually
    (it is not transactional), then re-raises.
    """
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_migrations_table(conn)
        applied = set(
            r[0] for r in conn.execute(
                "SELECT version FROM schema_migrations").fetchall())
        pending = [m for m in MIGRATIONS
                   if m[0] in versions and m[0] not in applied]
        result: Dict[str, Any] = {"applied": [],
                                  "user_version": pre_user_version}
        try:
            for version, description, up_stmts, _ in pending:
                for stmt in up_stmts:
                    # Skip if table doesn't exist (migration not applicable)
                    # -- this handles databases that never had the table.
                    # Also skip "duplicate column name": the five ad-hoc
                    # migration sites still self-apply on engine boot, so a
                    # live DB may already carry the column. The migration's
                    # end-state holds; record the version as applied.
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        msg = str(e)
                        if "no such table" in msg:
                            pass  # Table doesn't exist, skip this statement
                        elif "duplicate column name" in msg:
                            pass  # Column already present via ad-hoc site
                        else:
                            raise
                conn.execute(
                    "INSERT INTO schema_migrations "
                    "(version, applied_at, description) VALUES (?, ?, ?)",
                    (version, _utcnow(), description))
                result["applied"].append(version)
            if result["applied"]:
                new_version = max(result["applied"])
                conn.execute(f"PRAGMA user_version = {new_version}")
                result["user_version"] = new_version
            conn.commit()
        except Exception:
            conn.rollback()
            # PRAGMA user_version is not transactional: restore it manually.
            try:
                conn.execute(f"PRAGMA user_version = {pre_user_version}")
            except Exception:
                pass
            raise
        return result
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _verify_applied_state(db_path: str) -> Tuple[bool, str]:
    """The migration assertions WITHOUT the intent gate.

    Used by apply_migrations step (d): the running call owns the
    intent, so the gate must not fire on itself. The public
    verify_migrations() adds the gate: any dangling intent fails.
    """
    applied = get_applied_versions(db_path)
    expected = [v for v, _, _, _ in MIGRATIONS]
    if applied != expected:
        return False, f"applied {applied} != expected {expected}"
    uv = get_user_version(db_path)
    if uv != max(expected):
        return False, f"user_version {uv} != {max(expected)}"
    return True, f"all {len(expected)} migrations applied, user_version={uv}"


def verify_migrations(db_path: str) -> Tuple[bool, str]:
    """Verify all migrations applied and user_version matches.

    FAILS when a migration-intent record is dangling: a partial
    migration must never verify as trusted. Never raises on a dangling
    or corrupt intent -- returns (False, reason) and the caller fails
    closed (apply_migrations raises MigrationIncompleteError instead).
    """
    try:
        intent = _read_intent(db_path)
    except MigrationError as exc:
        return False, str(exc)
    if intent is not None:
        versions = intent.get("versions", "?")
        started = intent.get("started_at", "?")
        return False, (
            f"dangling migration intent for versions {versions} "
            f"(started {started}): the migration did not finish; "
            f"run recover_migration({os.path.abspath(db_path)!r}) -- "
            f"a partial migration never verifies as trusted")
    return _verify_applied_state(db_path)


# ---------------------------------------------------------------------------
# two-phase apply


def _migration_backup_dir(db_path: str) -> str:
    stem = os.path.splitext(os.path.basename(os.path.abspath(db_path)))[0]
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rand = os.urandom(4).hex()
    return os.path.join(os.path.dirname(os.path.abspath(db_path)),
                        "migration_backups",
                        f"{stem}_{stamp}_{rand}")


def _remove_backup_bundle(bundle: Optional[str]) -> None:
    if bundle:
        shutil.rmtree(bundle, ignore_errors=True)


def _record_post_digests(db_path: str, versions: List[int],
                         post_digest: str) -> None:
    if not versions:
        return
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _ensure_migrations_table(conn)
        conn.execute(
            "UPDATE schema_migrations SET post_digest = ? "
            "WHERE version IN (%s)" % ",".join("?" * len(versions)),
            [post_digest, *versions])
        conn.commit()
    finally:
        conn.close()


def _discover_anchor_paths(db_path: str,
                            journal_path: Optional[str],
                            key_path: Optional[str]
                            ) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort anchor discovery for the migration backup.

    Explicit paths win; otherwise the conventional sibling anchor_store
    is used when a journal already exists there. No journal is ever
    created implicitly: a missing journal means a DB-only bundle.
    """
    if journal_path or key_path:
        return journal_path, key_path
    try:
        from swarm_engine.governance.anchor import default_anchor_paths
    except ImportError:
        return None, None
    djournal, dkey, _ = default_anchor_paths(db_path)
    if os.path.isfile(djournal) and os.path.isfile(dkey):
        return djournal, dkey
    return None, None


def apply_migrations(db_path: str, backup: bool = True,
                     journal_path: Optional[str] = None,
                     key_path: Optional[str] = None) -> Dict[str, Any]:
    """Apply pending migrations with the crash-safe two-phase protocol.

    (a) write the intent sidecar; (b) take a backup bundle (step 1 of the
    backup module) and record it in the intent; (c) apply the batch in
    one transaction; (d) verify; (e) record post-migration digests,
    delete the backup, remove the intent.

    A dangling intent from a previous crashed/failed run makes this
    RAISE MigrationIncompleteError -- it never proceeds over an
    unfinished migration. Resolve with recover_migration(db_path).

    On an in-process failure of the batch: the transaction has already
    rolled back; if the live DB digest still equals the intent's
    pre-migration digest, the rollback is PROVEN clean, so the intent
    and backup are removed before re-raising (nothing to recover). If
    the digest differs, the intent is kept (marked failed) and the
    backup is kept for recover_migration.

    Returns {"applied": [versions], "skipped": [versions],
    "user_version": int}. On failure, re-raises after rollback/cleanup.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    db_path = os.path.abspath(db_path)

    # Crash gate: a dangling intent means the previous run did not
    # finish. Never proceed over it.
    intent = _read_intent(db_path)
    if intent is not None:
        raise MigrationIncompleteError(
            f"dangling migration intent for versions "
            f"{intent.get('versions', '?')} (started "
            f"{intent.get('started_at', '?')}, status "
            f"{intent.get('status', '?')}): refusing to migrate. Run "
            f"recover_migration({db_path!r}) to resolve.")

    # Inspection (read-only): which versions are pending?
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("BEGIN")
        _ensure_migrations_table(conn)
        applied = set(
            r[0] for r in conn.execute(
                "SELECT version FROM schema_migrations").fetchall())
        pre_user_version = conn.execute(
            "PRAGMA user_version").fetchone()[0]
        conn.rollback()
    finally:
        conn.close()

    result: Dict[str, Any] = {"applied": [], "skipped": sorted(applied),
                              "user_version": pre_user_version}
    pending = [m for m in MIGRATIONS if m[0] not in applied]
    if not pending:
        return result
    versions = [m[0] for m in pending]

    # (a) INTENT -- atomic sidecar write, outside any DB transaction.
    pre_digest = _db_state_digest(db_path)
    _write_intent(db_path, {
        "format": INTENT_FORMAT,
        "versions": versions,
        "started_at": _utcnow(),
        "pre_db_sha256": pre_digest,
        "backup_bundle": None,
        "db_path": db_path,
        "status": "in_progress",
        "pid": os.getpid(),
    })

    # (b) BACKUP -- real bundle; recorded in the intent.
    backup_bundle: Optional[str] = None
    if backup:
        from swarm_engine.governance import backup as backup_mod
        jpath, kpath = _discover_anchor_paths(db_path, journal_path,
                                              key_path)
        backup_bundle = backup_mod.create_backup(
            db_path, jpath, kpath, _migration_backup_dir(db_path))
        _update_intent(db_path, {"backup_bundle": backup_bundle})

    # (c) APPLY -- single transaction.
    try:
        batch = _run_migration_batch(db_path, versions, pre_user_version)
    except Exception as exc:
        # The batch already rolled back. Prove it: only clear the intent
        # (and drop the backup) when the live digest still equals the
        # pre-migration digest. Otherwise keep both for recovery.
        try:
            cur_digest: Optional[str] = _db_state_digest(db_path)
        except Exception:
            cur_digest = None
        if cur_digest is not None and cur_digest == pre_digest:
            _clear_intent(db_path)
            _remove_backup_bundle(backup_bundle)
        else:
            _update_intent(db_path, {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
        raise

    # (d) VERIFY -- the running call owns the intent, so the gate is
    # bypassed here (same assertions as verify_migrations).
    ok, msg = _verify_applied_state(db_path)
    if not ok:
        _update_intent(db_path, {"status": "failed",
                                 "error": f"post-apply verify: {msg}"})
        raise MigrationError(f"post-migration verification failed: {msg}")

    # (e) FINALIZE -- post digests, drop backup, remove intent.
    post_digest = _db_state_digest(db_path)
    _record_post_digests(db_path, batch["applied"], post_digest)
    _remove_backup_bundle(backup_bundle)
    _clear_intent(db_path)

    result["applied"] = batch["applied"]
    result["user_version"] = batch["user_version"]
    return result


# ---------------------------------------------------------------------------
# explicit recovery


def recover_migration(db_path: str) -> Dict[str, Any]:
    """Resolve a dangling migration intent (explicit operator command).

    Cases, in order:
    1. No intent -> {"recovered": False, ...} (nothing to do).
    2. Live DB digest == intent's pre-migration digest -> the batch never
       committed (clean rollback or crash before commit). Clear the
       intent, drop the backup. No restore needed.
    3. The migration assertions pass -> the batch committed; only
       finalization was lost. Record post digests, clear the intent,
       drop the backup.
    4. Otherwise -> restore the DB file from the verified pre-migration
       backup bundle (DB file ONLY: the live journal may legitimately
       have advanced past the backup; a diverged anchor then needs the
       audited transition() path, which is reported). Clear the intent.

    Raises MigrationError when recovery is impossible (e.g. state is
    unrecognized AND no usable backup bundle exists).
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")
    db_path = os.path.abspath(db_path)
    intent = _read_intent(db_path)
    if intent is None:
        return {"recovered": False,
                "reason": "no dangling migration intent"}
    versions = intent.get("versions", [])
    bundle = intent.get("backup_bundle")
    pre_digest = intent.get("pre_db_sha256")

    cur_digest = _db_state_digest(db_path)
    if pre_digest and cur_digest == pre_digest:
        _clear_intent(db_path)
        _remove_backup_bundle(bundle)
        return {
            "recovered": True,
            "action": "cleared_intent_pre_state",
            "versions": versions,
            "detail": "live DB digest equals the intent's pre-migration "
                      "digest: the batch never committed (clean rollback "
                      "or crash before commit). Intent cleared; no "
                      "restore was needed. Re-run apply_migrations to "
                      "proceed.",
        }

    ok, msg = _verify_applied_state(db_path)
    if ok:
        post_digest = _db_state_digest(db_path)
        _record_post_digests(db_path, list(versions), post_digest)
        _clear_intent(db_path)
        _remove_backup_bundle(bundle)
        return {
            "recovered": True,
            "action": "finalized_post_commit",
            "versions": versions,
            "detail": "the migration batch had committed (all assertions "
                      "pass); only finalization was lost to the crash. "
                      "Post-migration digests recorded, intent cleared.",
        }

    if not bundle or not os.path.isdir(bundle):
        raise MigrationError(
            "cannot recover: database state is neither pre-migration "
            "nor fully migrated, and no usable pre-migration backup "
            f"bundle exists (intent recorded {bundle!r}). Manual "
            f"intervention required.")
    from swarm_engine.governance import backup as backup_mod
    bok, bmsg = backup_mod.verify_backup(bundle)
    if not bok:
        raise MigrationError(
            f"cannot recover: pre-migration backup bundle failed "
            f"verification: {bmsg}")
    backup_mod.restore_backup(bundle, db_path, None, None,
                              include_anchor=False)
    vok, vmsg = _verify_applied_state(db_path)
    _clear_intent(db_path)
    _remove_backup_bundle(bundle)
    return {
        "recovered": True,
        "action": "restored_from_backup",
        "versions": versions,
        "detail": "unrecognized DB state; restored the DB file from the "
                  "verified pre-migration backup bundle. Post-restore "
                  f"migration state: ok={vok} ({vmsg}). NOTE: the anchor "
                  "journal was NOT rolled back -- if it advanced past "
                  "the backup, run an audited transition() to reconcile.",
        "bundle": bundle,
    }
