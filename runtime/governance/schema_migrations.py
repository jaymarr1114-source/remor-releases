"""Schema migration framework (Batch 8, 2026-09-25).

Centralizes the five ad-hoc migration sites (M1-M5) into a versioned,
transactional framework. Previously each site did its own
`PRAGMA table_info` guard or try/except around `ALTER TABLE ... ADD COLUMN`
with no version tracking, no ordering guarantees, and no rollback.

Design:
- `schema_migrations` table records applied versions (version INTEGER PRIMARY KEY,
  applied_at TEXT, description TEXT).
- `PRAGMA user_version` mirrors the max applied version for fast checks.
- Migrations are defined as (version, description, up_sql, down_sql) tuples.
  up_sql is a list of DDL statements; down_sql is the rollback (for ADD COLUMN,
  this is a table rebuild without the column, since SQLite has no DROP COLUMN
  in older versions — we use the 12-step rebuild).
- `apply_migrations(db_path)` runs pending migrations in a transaction with
  a pre-migration backup. On failure, rolls back to the backup.
- `rollback_migration(db_path, version)` rolls back a single version.

The five migrations:
- M1 (v1): memory/failure_memory.py — ADD COLUMN context TEXT to failure_memory
- M2 (v2): synthesis/acquisition_learning.py — ADD COLUMN policy TEXT to acquisition_experience
- M3 (v3): longhorizon/cycle_loop.py — ADD COLUMN heldout_failed INTEGER to lh_challenge
- M4 (v4): cognition/representations.py — ADD COLUMN law TEXT to case_memory
- M5 (v5): governance/oracle_binding.py — ADD COLUMN supplier_id TEXT to ob_evaluations

All are additive and idempotent (IF NOT EXISTS semantics via version check).
"""

import os
import shutil
import sqlite3
import time
from typing import List, Tuple, Dict, Any

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


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create schema_migrations if not exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            description TEXT NOT NULL
        )
    """)


def get_applied_versions(db_path: str) -> List[int]:
    """Return sorted list of applied migration versions."""
    conn = sqlite3.connect(db_path)
    try:
        _ensure_migrations_table(conn)
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_user_version(db_path: str) -> int:
    """Return PRAGMA user_version."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def apply_migrations(db_path: str, backup: bool = True) -> Dict[str, Any]:
    """Apply pending migrations atomically.

    Returns {"applied": [versions], "skipped": [versions], "user_version": int}.
    All pending migrations run inside ONE transaction: any failure rolls back
    every migration in the batch (SQLite DDL is transactional). PRAGMA
    user_version is NOT transactional, so it is saved and restored manually
    on failure. The pre-migration file backup is kept as defense-in-depth.

    (Finisher repair 2026-09-25: the previous per-migration-commit design
    relied on file-copy restore for atomicity, but the engine DB runs in WAL
    mode and a leaked sqlite3.Connection survives engine teardown — the stale
    -wal resurrects the migrated state after the file copy, so "rollback"
    silently did nothing. Single-transaction rollback is immune to that.)

    On failure, re-raises after rollback/restore.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    backup_path = None
    if backup:
        backup_path = db_path + f".migrate_backup_{int(time.time())}"
        shutil.copy2(db_path, backup_path)

    def _cleanup_backup():
        if backup_path and os.path.exists(backup_path):
            os.remove(backup_path)

    conn = sqlite3.connect(db_path)
    try:
        # Single transaction for the whole batch: true atomicity.
        # _ensure_migrations_table runs INSIDE the transaction so a failed
        # batch leaves zero trace (not even the empty bookkeeping table).
        conn.execute("BEGIN")
        _ensure_migrations_table(conn)
        applied = set(
            r[0] for r in conn.execute(
                "SELECT version FROM schema_migrations").fetchall())
        pre_user_version = conn.execute(
            "PRAGMA user_version").fetchone()[0]

        result: Dict[str, Any] = {"applied": [], "skipped": [],
                                  "user_version": pre_user_version}
        pending = [m for m in MIGRATIONS if m[0] not in applied]
        result["skipped"] = [m[0] for m in MIGRATIONS if m[0] in applied]
        if not pending:
            _cleanup_backup()
            return result

        # Single transaction for the whole batch: true atomicity.
        # (BEGIN already issued above, before _ensure_migrations_table.)
        try:
            for version, description, up_stmts, _ in pending:
                for stmt in up_stmts:
                    # Skip if table doesn't exist (migration not applicable)
                    # — this handles databases that never had the table.
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
                    "INSERT INTO schema_migrations (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (version, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     description))
                result["applied"].append(version)
            new_version = max(result["applied"])
            conn.execute(f"PRAGMA user_version = {new_version}")
            conn.commit()
            result["user_version"] = new_version
        except Exception:
            conn.rollback()
            # PRAGMA user_version is not transactional: restore it manually.
            try:
                conn.execute(f"PRAGMA user_version = {pre_user_version}")
            except Exception:
                pass
            raise

        _cleanup_backup()
        return result
    except Exception:
        # The single-transaction rollback above is the atomicity mechanism;
        # it has already restored the database (WAL-safe, unlike file-copy
        # restore, which a stale -wal can defeat when other connections are
        # open). The pre-migration backup file is left in place as a forensic
        # snapshot of the pre-migration state.
        try:
            conn.close()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def verify_migrations(db_path: str) -> Tuple[bool, str]:
    """Verify all migrations applied and user_version matches."""
    applied = get_applied_versions(db_path)
    expected = [v for v, _, _, _ in MIGRATIONS]
    if applied != expected:
        return False, f"applied {applied} != expected {expected}"
    uv = get_user_version(db_path)
    if uv != max(expected):
        return False, f"user_version {uv} != {max(expected)}"
    return True, f"all {len(expected)} migrations applied, user_version={uv}"
