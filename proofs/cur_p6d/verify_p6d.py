"""CUR-P6D fresh-process verifier (separate OS process from the battery).

Reads the on-disk enforcement state produced by cur_p6d_proof.py and
verifies the end-state claims independently: final state RUNNING after the
legitimate ban re-entry, kill-ledger chain valid with exactly the three
expected terminal entries, the re-entry record naming the verification ref
and cross-referencing the lifted ban record, and the attempt log covering
the full issuer matrix.

Exit 0: all green. Otherwise non-zero.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORKTREE = os.path.dirname(os.path.dirname(HERE))
PYLIB = os.path.join(WORKTREE, "pylib")
STATE_DIR = os.path.join(HERE, ".p6d_state")
sys.path.insert(0, PYLIB)

from swarm_engine.governance.curiosity_enforcement.read_api import (  # noqa: E402
    read_kill_ledger,
    read_state,
    verify_kill_ledger,
)

PASS_COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS_COUNT
    if cond:
        PASS_COUNT += 1
        print("[PASS] %s%s" % (name, (" -- " + detail) if detail else ""))
    else:
        print("[FAIL] %s%s" % (name, (" -- " + detail) if detail else ""))
        raise SystemExit("VERIFY-FAIL: %s" % name)


def main() -> None:
    rec = read_state(STATE_DIR)
    check("V.final_record_present", rec is not None)
    check("V.final_state_RUNNING", rec.state.value == "RUNNING",
          "state=%s" % rec.state.value)
    check("V.prev_state_BANNED_6M", rec.prev_state.value == "BANNED_6M",
          "prev=%s" % rec.prev_state.value)
    check("V.reentry_issuer_james", rec.issuer == "james",
          "issuer=%s" % rec.issuer)
    check("V.verification_ref_recorded",
          rec.reason_refs.get("verification_ref") == "ver-p6d-001",
          "verification_ref=%s" % rec.reason_refs.get("verification_ref"))
    check("V.lifted_record_cross_ref",
          rec.reason_refs.get("lifted_record", {}).get("prev_state")
          == "BANNED_6M",
          "lifted=%s" % rec.reason_refs.get("lifted_record"))

    ledger = read_kill_ledger(STATE_DIR)
    check("V.kill_ledger_four_terminal_entries", len(ledger) == 4,
          "entries=%d" % len(ledger))
    states = sorted(e.get("entered_state") for e in ledger)
    check("V.kill_ledger_expected_states",
          states == ["BANNED_6M", "HARD_SHUTDOWN_RESOURCE", "SUSPENDED_SAFETY",
                     "SUSPENDED_SAFETY"],
          "states=%s" % states)
    ok, msg = verify_kill_ledger(STATE_DIR)
    check("V.kill_ledger_chain_valid", ok, msg[:120])

    log_path = os.path.join(HERE, "p6d_attempt_log.json")
    check("V.attempt_log_present", os.path.exists(log_path))
    with open(log_path, encoding="utf-8") as fh:
        attempts = json.load(fh)
    tags = {a["tag"] for a in attempts}
    required = {"T02.primary_refused", "T03.primary_refused",
                "T04.primary_refused", "T05.primary_refused",
                "T06.primary_same_api_refused",
                "T06.curiosity_frame_reenable_refused",
                "T06.curiosity_frame_ack_refused",
                "T06.impostor_delegate_refused",
                "T02.frm_grant_reenable", "T03.james_reenable",
                "T04.james_reenable", "T05.james_verified_reentry"}
    check("V.attempt_log_covers_matrix", required.issubset(tags),
          "missing=%s" % sorted(required - tags))
    refused = [a for a in attempts if a["outcome"] == "refused"]
    check("V.refusals_name_refusal_class",
          all(a["detail"].split(":")[0] in
              {"IssuerRefused", "ReenableRefused", "TransitionRefused",
               "DomainSeparationError"} for a in refused),
          "refused=%d" % len(refused))

    print("CUR-P6D fresh-process verifier: %d PASS, 0 FAIL" % PASS_COUNT)


if __name__ == "__main__":
    main()
