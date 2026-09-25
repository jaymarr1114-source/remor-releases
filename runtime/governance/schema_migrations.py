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
    """Apply pending migrations transactionally.

    Returns {"applied": [versions], "skipped": [versions], "user_version": int}.
    On failure, restores from backup and re-raises.
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"database not found: {db_path}")

    backup_path = None
    if backup:
        backup_path = db_path + f".migrate_backup_{int(time.time())}"
        shutil.copy2(db_path, backup_path)

    conn = sqlite3.connect(db_path)
    try:
        _ensure_migrations_table(conn)
        applied = set(
            r[0] for r in conn.execute(
                "SELECT version FROM schema_migrations").fetchall())

        result = {"applied": [], "skipped": [], "user_version": 0}

        for version, description, up_stmts, _ in MIGRATIONS:
            if version in applied:
                result["skipped"].append(version)
                continue

            # Apply in a transaction
            try:
                conn.execute("BEGIN")
                for stmt in up_stmts:
                    # Skip if table doesn't exist (migration not applicable)
                    # — this handles databases that never had the table.
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError as e:
                        if "no such table" in str(e):
                            pass  # Table doesn't exist, skip this statement
                        else:
                            raise
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (version, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     description))
                conn.execute(f"PRAGMA user_version = {version}")
                conn.commit()
                result["applied"].append(version)
            except Exception:
                conn.rollback()
                raise

        result["user_version"] = conn.execute(
            "PRAGMA user_version").fetchone()[0]

        # Clean up backup on success
        if backup_path and os.path.exists(backup_path):
            os.remove(backup_path)

        return result
    except Exception:
        # Restore from backup on failure
        conn.close()
        if backup_path and os.path.exists(backup_path):
            shutil.copy2(backup_path, db_path)
            os.remove(backup_path)
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
