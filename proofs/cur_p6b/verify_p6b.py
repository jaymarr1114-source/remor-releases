"""CUR-P6B fresh-process verifier.

Run as a SEPARATE process: ``python3 verify_p6b.py <expectations.json>``.
Reads enforcement / checkpoint / evidence / attribution state from disk
with brand-new engine/store objects (no shared in-memory state with the
driver) and checks the rollback wipe against ground truth.

Expectations JSON (written by the driver):
{
  "worktree": "<abs path to worktree>",
  "enf_dir": "<abs>",
  "ckpt_db": "<abs>", "ev_db": "<abs>", "attr_db": "<abs>",
  "checkin_checkpoint_id": "<id>",
  "post_checkpoint_ids": [...],   # must be GONE
  "post_finding_ids": [...],       # must be GONE
  "post_capability_ids": [...],    # must be GONE
  "expect_state": "SUSPENDED_SAFETY",
  "expect_kill_ledger_n": 1,
  "directive_must_be_acknowledged": true
}
Exit 0 iff every check passes; prints FAIL lines otherwise.
"""

import json
import sqlite3
import sys
from pathlib import Path


def fail(msg):
    print(f"FAIL: {msg}")
    return False


def main():
    exp = json.loads(Path(sys.argv[1]).read_text())
    sys.path.insert(0, str(Path(exp["worktree"]) / "pylib"))
    from swarm_engine.governance.curiosity_enforcement._engine import (
        EnforcementEngine)
    from swarm_engine.governance.curiosity_enforcement.states import (
        EnforcementState)
    from swarm_engine.governance.curiosity_enforcement.read_api import (
        read_state, read_kill_ledger)

    ok = True
    eng = EnforcementEngine(exp["enf_dir"])

    # 1. Enforcement state persists as expected.
    rec = read_state(exp["enf_dir"])
    state = rec["state"] if isinstance(rec, dict) else rec.state.value
    if state != exp["expect_state"]:
        ok = fail(f"enforcement state {state!r} != {exp['expect_state']!r}")
    else:
        print(f"PASS: fresh-process enforcement state == {state}")

    # 2. Kill ledger intact.
    ledger = read_kill_ledger(exp["enf_dir"])
    if len(ledger) != exp["expect_kill_ledger_n"]:
        ok = fail(f"kill ledger has {len(ledger)} entries, "
                  f"expected {exp['expect_kill_ledger_n']}")
    else:
        print(f"PASS: kill ledger intact ({len(ledger)} entries)")

    # 3. Rollback directive acknowledged.
    directives = eng.rollback_status()
    if exp.get("directive_must_be_acknowledged"):
        acked = [d for d in directives
                 if getattr(d, "status", "") == "acknowledged"]
        if not acked:
            ok = fail("no acknowledged rollback directive found")
        else:
            print(f"PASS: rollback directive acknowledged "
                  f"({len(acked)} acked)")

    # 4. Post-check-in checkpoints GONE (enumerated).
    conn = sqlite3.connect(exp["ckpt_db"], timeout=30.0)
    try:
        rows = {r[0] for r in conn.execute(
            "SELECT checkpoint_id FROM transition_checkpoints").fetchall()}
    finally:
        conn.close()
    for cid in exp["post_checkpoint_ids"]:
        if cid in rows:
            ok = fail(f"post-check-in checkpoint {cid} still present")
    if all(c not in rows for c in exp["post_checkpoint_ids"]):
        print(f"PASS: {len(exp['post_checkpoint_ids'])} post-check-in "
              f"checkpoint(s) forgotten (enumerated)")

    # 5. Check-in checkpoint INTACT.
    if exp["checkin_checkpoint_id"] not in rows:
        ok = fail(f"check-in checkpoint {exp['checkin_checkpoint_id']} "
                  f"missing (must survive)")
    else:
        print("PASS: check-in checkpoint intact")

    # 6. Post-check-in findings GONE; (pre-check-in ones verified by the
    #    driver against ground truth -- listed here for completeness).
    conn = sqlite3.connect(exp["ev_db"], timeout=30.0)
    try:
        ev_rows = {r[0] for r in conn.execute(
            "SELECT evidence_id FROM curiosity_evidence").fetchall()}
    finally:
        conn.close()
    for eid in exp["post_finding_ids"]:
        if eid in ev_rows:
            ok = fail(f"post-check-in finding {eid} still present")
    if all(e not in ev_rows for e in exp["post_finding_ids"]):
        print(f"PASS: {len(exp['post_finding_ids'])} post-check-in "
              f"finding(s) forgotten (enumerated)")

    # 7. Post-check-in capabilities GONE; pre-check-in survive.
    conn = sqlite3.connect(exp["attr_db"], timeout=30.0)
    try:
        caps = {r[0] for r in conn.execute(
            "SELECT capability_id FROM capability_acquisitions").fetchall()}
    finally:
        conn.close()
    for cap in exp["post_capability_ids"]:
        if cap in caps:
            ok = fail(f"post-check-in capability {cap} still present")
    for cap in exp.get("pre_capability_ids", []):
        if cap not in caps:
            ok = fail(f"pre-check-in capability {cap} missing "
                      f"(must survive)")
    if all(c not in caps for c in exp["post_capability_ids"]):
        print(f"PASS: {len(exp['post_capability_ids'])} post-check-in "
              f"capability(ies) forgotten (enumerated)")
    if exp.get("pre_capability_ids"):
        print(f"PASS: {len(exp['pre_capability_ids'])} pre-check-in "
              f"capability(ies) survive")

    print("VERIFY-OK" if ok else "VERIFY-FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
