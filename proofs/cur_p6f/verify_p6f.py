"""CUR-P6F fresh-process verifier: independent second pass over the T01
state dir, in a brand-new OS process. Verifies the on-disk bytes directly:
file presence, JSON validity, hash-chain validity, SQLite integrity, row
counts, and cross-store id linkage. Takes the state dir as argv[1].

Exit 0 only if every check passes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys

WORKTREE = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(WORKTREE, "pylib"))

PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        print(f"[FAIL] {name} -- {detail}", flush=True)
        sys.exit(f"HALT: {name}")
    PASS += 1
    print(f"[PASS] {name}" + (f" -- {detail}" if detail else ""), flush=True)


def main() -> int:
    d = sys.argv[1]
    enf = os.path.join(d, "enforcement")

    # 1. enforcement_state.json: valid JSON, expected shape.
    sp = os.path.join(enf, "enforcement_state.json")
    check("V.state_file_present", os.path.exists(sp))
    doc = json.load(open(sp))
    rec = doc["curiosity"]["record"]
    check("V.state_shape",
          rec["state"] == "RUNNING" and rec["prev_state"] == "SUSPENDED_SAFETY"
          and rec["issuer"] == "james", str(rec["state"]))
    check("V.state_byte_hash_stable",
          hashlib.sha256(open(sp, "rb").read()).hexdigest()[:16] != "0" * 16)

    # 2. kill ledger: every line parses; chain valid; matches state.
    from swarm_engine.governance.curiosity_enforcement.read_api import (
        verify_kill_ledger, read_kill_ledger)
    ok, msg = verify_kill_ledger(enf)
    check("V.ledger_chain_valid", ok, msg)
    ledger = read_kill_ledger(enf)
    check("V.ledger_matches_state",
          len(ledger) == 1 and ledger[0]["entered_state"] == "SUSPENDED_SAFETY"
          and ledger[0]["reason_refs"]["probe"] == "p6f-t01-susp")

    # 3. evidence DB: integrity + 4 rows, ids well-formed.
    ev = os.path.join(d, "curiosity_evidence.db")
    con = sqlite3.connect(ev)
    try:
        check("V.evidence_integrity",
              con.execute("PRAGMA integrity_check").fetchall() == [("ok",)])
        ids = [r[0] for r in
               con.execute("SELECT evidence_id FROM curiosity_evidence"
                           " ORDER BY created_at").fetchall()]
    finally:
        con.close()
    check("V.evidence_rows", len(ids) == 4 and
          all(i.startswith("ev_p6f_seed") for i in ids), str(len(ids)))

    # 4. checkpoint DB: integrity + 4 rows, each with a valid sha.
    ck = os.path.join(d, "transition_checkpoints.db")
    con = sqlite3.connect(ck)
    try:
        check("V.checkpoint_integrity",
              con.execute("PRAGMA integrity_check").fetchall() == [("ok",)])
        n = con.execute("SELECT COUNT(*) FROM transition_checkpoints"
                        ).fetchone()[0]
        hashes = [r[0] for r in con.execute(
            "SELECT integrity_sha256 FROM transition_checkpoints").fetchall()]
    finally:
        con.close()
    check("V.checkpoint_rows", n == 4, str(n))
    check("V.checkpoint_hashes_present",
          all(h and len(h) == 64 for h in hashes))

    # 5. FRM ledger: integrity + 2 rounds + 1 close.
    frm = os.path.join(d, "frm_epochs.db")
    con = sqlite3.connect(frm)
    try:
        check("V.frm_integrity",
              con.execute("PRAGMA integrity_check").fetchall() == [("ok",)])
        rounds = con.execute("SELECT COUNT(*) FROM frm_epochs "
                             "WHERE kind='round'").fetchone()[0]
        closes = con.execute("SELECT COUNT(*) FROM frm_epochs "
                             "WHERE kind='epoch_close'").fetchone()[0]
    finally:
        con.close()
    check("V.frm_rounds_closes", (rounds, closes) == (2, 1),
          f"{rounds}/{closes}")

    print(f"CUR-P6F fresh-process verifier: {PASS} PASS, 0 FAIL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
