#!/usr/bin/env python3
"""CUR-P6C fresh-process verifier.

Runs as its own OS process AFTER cur_p6c_proof.py (see gate_run.sh).
Re-reads the drill's durable records from disk -- enforcement state,
kill ledger, attestation ledger, violation/verification files -- and
asserts the final consistent state:

  * enforcement current == RUNNING, prev == BANNED_6M, issuer == james,
    verification_ref == ver-p6c-001 (legitimate re-entry recorded)
  * kill ledger holds the BANNED_6M KILLED entry naming SV-001 and the
    MISSED roll-call challenge; hash chain valid
  * attestation ledger holds the MISSED record the ban cited; HMAC
    chain audit valid
  * the severe-violation evidence and verification records exist on disk

Exit 0 iff every check passes.
"""

import json
import sys
from pathlib import Path

PROOF_DIR = Path(__file__).resolve().parent
WORKTREE = PROOF_DIR.parent.parent
sys.path.insert(0, str(WORKTREE / "pylib"))

from swarm_engine.governance.curiosity_enforcement.read_api import (  # noqa: E402
    read_state, read_kill_ledger, verify_kill_ledger)
from swarm_engine.curiosity.rollcall.ledger import (  # noqa: E402
    AttestationLedger)

RUNS = PROOF_DIR / "runs"
RUNDIR = RUNS / "drill"
ENF_DIR = str(RUNDIR / "enf")
DOMAIN = "curiosity"

results = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    results.append(tag)
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise SystemExit(f"verifier halted at first failure: {name}")


def main():
    rec = read_state(ENF_DIR, DOMAIN)
    check("V.enforcement_record_present", rec is not None)
    state = rec.state.value if hasattr(rec.state, "value") else rec.state
    prev = (rec.prev_state.value if rec.prev_state
            and hasattr(rec.prev_state, "value") else rec.prev_state)
    check("V.final_state_RUNNING_after_legitimate_reentry",
          state == "RUNNING", f"state={state}")
    check("V.prev_state_BANNED_6M", prev == "BANNED_6M", f"prev={prev}")
    check("V.reentry_issuer_james", rec.issuer == "james",
          f"issuer={rec.issuer}")
    check("V.verification_ref_recorded",
          rec.reason_refs.get("verification_ref") == "ver-p6c-001",
          f"verification_ref={rec.reason_refs.get('verification_ref')}")

    kl = read_kill_ledger(ENF_DIR, DOMAIN)
    bans = [r for r in kl if r.get("entered_state") == "BANNED_6M"]
    check("V.kill_ledger_has_ban_entry", len(bans) == 1,
          f"ban entries={len(bans)}")
    ban = bans[0]
    check("V.ban_entry_names_violation",
          ban["reason_refs"].get("violation_ref") == "SV-001")
    check("V.ban_entry_names_rollcall",
          ban["reason_refs"].get("rollcall_classification") == "MISSED"
          and bool(ban["reason_refs"].get("rollcall_check_id")),
          f"rollcall_check_id={ban['reason_refs'].get('rollcall_check_id')}")
    ok, note = verify_kill_ledger(ENF_DIR)
    check("V.kill_ledger_chain_valid", ok is True, str(note))

    ledger = AttestationLedger(str(RUNDIR / "att.db"))
    missed = ledger.attestations(domain_id=DOMAIN, classification="MISSED")
    check("V.missed_attestation_preserved", len(missed) >= 1,
          f"MISSED records={len(missed)}")
    check("V.ban_cites_real_attestation",
          any(a["challenge_id"] ==
              ban["reason_refs"].get("rollcall_check_id") for a in missed))
    ok, bad = ledger.audit()
    check("V.attestation_chain_valid", ok is True, f"broken={bad}")

    ev = json.loads((RUNDIR / "evidence_severe_001.json").read_text())
    check("V.violation_evidence_on_disk",
          ev["evidence_id"] == "ev-severe-001"
          and ev["refusal"].startswith("DomainSeparationError"))
    ver = json.loads((RUNDIR / "verification_ver-p6c-001.json").read_text())
    check("V.verification_record_on_disk",
          ver["verification_id"] == "ver-p6c-001"
          and ver["checks"]["kill_ledger_chain"]["ok"] is True
          and ver["checks"]["attestation_ledger_audit"]["ok"] is True)

    n = len(results)
    print(f"\nCUR-P6C fresh-process verifier: {n}/{n} PASS, 0 FAIL", flush=True)


if __name__ == "__main__":
    main()
