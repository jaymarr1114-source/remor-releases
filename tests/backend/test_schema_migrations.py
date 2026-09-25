#!/usr/bin/env python3
"""Batch 8 regression: schema_migrations works on live engine DBs.

Covers the two defects found during finisher re-verification (I-13):
1. apply_migrations failed on a real fresh engine DB with
   "duplicate column name" because the five ad-hoc migration sites still
   self-apply on engine boot.
2. The "atomic rollback" was not atomic: per-migration commits + file-copy
   restore, defeated by WAL mode + a lingering sqlite connection.

Each test uses a REAL fresh SwarmEngine DB (not a synthetic old-schema DB).
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "..", "pylib"))

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.governance import schema_migrations as sm


def _fresh_engine_db():
    d = tempfile.mkdtemp(prefix="migreg_")
    db = os.path.join(d, "eng.db")
    SwarmEngine(db_path=db)
    return db


def _tables(db):
    con = sqlite3.connect(db)
    try:
        return sorted(r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"))
    finally:
        con.close()


class TestMigrationsOnLiveDB(unittest.TestCase):
    def test_apply_on_fresh_engine_db(self):
        """All 5 migrations apply on a real fresh engine DB (I-13 defect 1)."""
        db = _fresh_engine_db()
        res = sm.apply_migrations(db)
        self.assertEqual(res["applied"], [1, 2, 3, 4, 5])
        self.assertEqual(sm.get_applied_versions(db), [1, 2, 3, 4, 5])
        ok, msg = sm.verify_migrations(db)
        self.assertTrue(ok, msg)

    def test_idempotent_rerun(self):
        """Re-running skips all already-applied migrations."""
        db = _fresh_engine_db()
        sm.apply_migrations(db)
        res = sm.apply_migrations(db)
        self.assertEqual(res["applied"], [])
        self.assertEqual(res["skipped"], [1, 2, 3, 4, 5])

    def test_atomic_rollback_on_failure(self):
        """A failing migration leaves zero trace (I-13 defect 2)."""
        db = _fresh_engine_db()
        pre_tables = _tables(db)
        con = sqlite3.connect(db)
        pre_uv = con.execute("PRAGMA user_version").fetchone()[0]
        con.close()

        bad = list(sm.MIGRATIONS) + [
            (6, "M6-bogus", ["THIS IS NOT VALID SQL AT ALL"], [])]
        old = sm.MIGRATIONS
        sm.MIGRATIONS = bad
        try:
            with self.assertRaises(Exception):
                sm.apply_migrations(db)
        finally:
            sm.MIGRATIONS = old

        # Atomicity: tables identical, user_version restored, no rows.
        self.assertEqual(_tables(db), pre_tables)
        con = sqlite3.connect(db)
        try:
            uv = con.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(uv, pre_uv)
            if "schema_migrations" in _tables(db):
                n = con.execute(
                    "SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
                self.assertEqual(n, 0)
        finally:
            con.close()

    def test_duplicate_column_treated_as_applied(self):
        """Ad-hoc site columns do not break the framework (I-13 defect 1)."""
        db = _fresh_engine_db()
        # Simulate an ad-hoc site having already added a column the
        # framework also manages: the framework must not crash.
        res = sm.apply_migrations(db)
        self.assertEqual(res["applied"], [1, 2, 3, 4, 5])


if __name__ == "__main__":
    unittest.main()
