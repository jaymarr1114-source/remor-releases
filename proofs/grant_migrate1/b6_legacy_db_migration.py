"""b6: legacy DB migration — a pre-migration ledger DB (mutable-Grant era
schema: 7 columns, no domain/lending_json) must open cleanly under the new
ledger, gain the new columns, and keep its rows readable as FrmGrants."""
import json
import os
import sqlite3
import tempfile
import time
from common import check, summary

from runtime.curiosity.attribution.grants import (
    AllocationLedger, AllocationRefused)

TMP = tempfile.mkdtemp(prefix="gm_b6_")
DB = os.path.join(TMP, "legacy.db")


@check("b6_legacy_schema_migrates")
def _():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE grants (
        grant_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL,
        epoch_s INTEGER NOT NULL, dimensions TEXT NOT NULL,
        primary_minimum TEXT NOT NULL, lent INTEGER NOT NULL,
        issued_at REAL NOT NULL)""")
    conn.execute("""CREATE TABLE allocations (
        allocation_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL,
        epoch_id TEXT NOT NULL, controller_id TEXT NOT NULL,
        originating_executive TEXT NOT NULL, budget_s REAL NOT NULL,
        max_concurrent INTEGER NOT NULL, lent INTEGER NOT NULL,
        allocated_at REAL NOT NULL)""")
    conn.execute(
        "INSERT INTO grants VALUES (?,?,?,?,?,?,?)",
        ("legacy-1", "7", 600,
         json.dumps({"budget_s": 60.0, "max_concurrent": 4}),
         json.dumps({"budget_s": 1.0, "max_concurrent": 1}),
         0, time.time()))
    conn.commit()
    conn.close()

    led = AllocationLedger(DB)  # must migrate, not crash
    cols = [r[1] for r in led._conn.execute("PRAGMA table_info(grants)")]
    assert "domain" in cols and "lending_json" in cols, cols
    g = led.get_grant("legacy-1")
    assert type(g).__name__ == "FrmGrant"
    assert g.epoch_id == 7 and isinstance(g.epoch_id, int)
    assert g.budget_s == 60.0 and g.domain == ""
    led.close()
    return f"migrated; columns={cols}"


@check("b6_legacy_str_epoch_fails_closed")
def _():
    db2 = os.path.join(TMP, "legacy2.db")
    conn = sqlite3.connect(db2)
    conn.execute("""CREATE TABLE grants (
        grant_id TEXT PRIMARY KEY, epoch_id TEXT NOT NULL,
        epoch_s INTEGER NOT NULL, dimensions TEXT NOT NULL,
        primary_minimum TEXT NOT NULL, lent INTEGER NOT NULL,
        issued_at REAL NOT NULL)""")
    conn.execute(
        "INSERT INTO grants VALUES (?,?,?,?,?,?,?)",
        ("legacy-str", "epoch-1", 600,
         json.dumps({"budget_s": 60.0, "max_concurrent": 4}),
         json.dumps({"budget_s": 1.0, "max_concurrent": 1}),
         0, time.time()))
    conn.commit()
    conn.close()
    led = AllocationLedger(db2)
    try:
        led.get_grant("legacy-str")
    except AllocationRefused as e:
        led.close()
        assert "int" in str(e)
        return f"unmigratable row fails closed: {e}"
    led.close()
    raise AssertionError("str epoch silently converted")


summary("b6")
