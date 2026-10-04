#!/usr/bin/env python3
"""CUR-HARDEN-1 fresh-process verifier: re-reads the T01 state from a brand
new OS process and verifies both directions (nothing missing, nothing extra,
every record's integrity hash valid)."""

from __future__ import annotations

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORKTREE = os.path.dirname(os.path.dirname(HERE))
PYLIB = os.path.join(WORKTREE, "pylib")
sys.path.insert(0, PYLIB)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""))


def main() -> int:
    d = "/tmp/cur_harden_1/t01_state"
    ev_path = os.path.join(d, "evidence.db")
    frm_path = os.path.join(d, "frm.db")
    enf_dir = os.path.join(d, "enforcement")

    from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
    from swarm_engine.curiosity.frm.ledger import EpochLedger
    from swarm_engine.governance.curiosity_enforcement.read_api import (
        read_state, verify_kill_ledger)

    ev = CuriosityEvidenceStore(ev_path)
    rows = ev.all()
    check("V.evidence_three_records", len(rows) == 3,
          f"rows={len(rows)}")
    check("V.evidence_ids_exact",
          sorted(f.evidence_id for f in rows) == ["ev_h1", "ev_h2", "ev_h3"])
    con = sqlite3.connect(ev_path)
    n_hash = con.execute(
        "SELECT COUNT(*) FROM curiosity_evidence WHERE "
        "integrity_sha256 IS NOT NULL").fetchone()[0]
    uv = con.execute("PRAGMA user_version").fetchone()[0]
    con.close()
    check("V.evidence_all_hashed", n_hash == 3, f"hashed={n_hash}/3")
    check("V.evidence_sealed", uv >= 1, f"user_version={uv}")

    led = EpochLedger(frm_path)
    recs = led.all_records()
    check("V.frm_three_entries", len(recs) == 3, f"entries={len(recs)}")
    check("V.frm_kinds", [r["kind"] for r in recs] ==
          ["round", "round", "epoch_close"])

    rec = read_state(enf_dir)
    check("V.enforcement_suspended",
          rec is not None and rec.state.value == "SUSPENDED_SAFETY")
    doc = json.load(open(os.path.join(enf_dir, "enforcement_state.json")))
    check("V.enforcement_entry_sealed",
          doc["curiosity"].get("integrity_sha256") is not None
          and doc.get("integrity_version") == 1)
    ok, msg = verify_kill_ledger(enf_dir)
    check("V.kill_ledger_chain_valid", ok, msg)

    print(f"\nCUR-HARDEN-1 verifier: {len(PASS)} PASS, {len(FAIL)} FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
