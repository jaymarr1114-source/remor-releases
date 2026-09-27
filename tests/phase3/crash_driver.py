"""Real crash driver for the migration crash tests (Phase 3, Worker E).

Kills ITSELF with SIGKILL mid-migration -- a genuine crash, not a
simulated one. The parent test then observes the dangling intent.

Usage:
    crash_driver.py <db_path> <mode>

Modes:
    before_commit  -- intent + pre-migration backup are written for real,
                      then the process dies with the migration transaction
                      OPEN and uncommitted (real DDL executed, never
                      committed).
    after_commit   -- the migration batch really commits, then the process
                      dies before apply_migrations can finalize (no post
                      digests, intent not removed, backup not deleted).

Exit status is always SIGKILL (-9) on the crash path; any other exit
means the driver itself failed and the test must treat that as an
error, not as a crash.
"""
import os
import signal
import sqlite3
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.governance import schema_migrations as sm  # noqa: E402


def _die():
    sys.stdout.flush()
    sys.stderr.flush()
    time.sleep(0.2)
    os.kill(os.getpid(), signal.SIGKILL)


def _before_commit(db_path, versions, pre_uv):
    """Real partial migration, then SIGKILL with the txn open."""
    pending = [m for m in sm.MIGRATIONS if m[0] in versions]
    if not pending:
        print("driver: nothing pending, cannot crash mid-migration",
              file=sys.stderr)
        sys.exit(2)
    _version, _desc, up_stmts, _down = pending[0]
    conn = sqlite3.connect(db_path, timeout=30)
    # Deliberately NOT closed/committed: the SIGKILL below leaves a hot
    # rollback journal, exactly like a real crash mid-apply.
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(up_stmts[0])  # real DDL, uncommitted
    _die()
    return {"applied": [], "user_version": pre_uv}  # unreachable


def _after_commit(db_path, versions, pre_uv):
    """Real commit, then SIGKILL before finalization."""
    result = _ORIG_BATCH(db_path, versions, pre_uv)
    _die()
    return result  # unreachable


def main():
    db_path, mode = sys.argv[1], sys.argv[2]
    if mode == "before_commit":
        sm._run_migration_batch = _before_commit
    elif mode == "after_commit":
        sm._run_migration_batch = _after_commit
    else:
        print(f"driver: unknown mode {mode!r}", file=sys.stderr)
        sys.exit(2)
    sm.apply_migrations(db_path)
    # Reaching here means the crash did not happen -- a driver bug.
    print("driver: survived migration without crashing (BUG)",
          file=sys.stderr)
    sys.exit(3)


_ORIG_BATCH = sm._run_migration_batch

if __name__ == "__main__":
    main()
