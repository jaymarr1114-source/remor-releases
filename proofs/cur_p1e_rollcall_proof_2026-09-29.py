#!/usr/bin/env python3
"""CUR-P1E proof: GAM roll-call channel, end to end.

Runs the real mechanism (not pytest): issues real challenges through
the real transport to real test-double responders, classifies for
real, appends real attestations to a real hash-chained ledger, then
executes the design Section 6 adversarial battery for real. Prints a
machine-checkable manifest; exits nonzero on the first failure.

Usage: python3 proofs/cur_p1e_rollcall_proof_2026-09-29.py
Run from the repo root of the cur-p1e worktree.
"""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from runtime.curiosity.rollcall.gam import GovernanceAttestationMonitor
from runtime.curiosity.rollcall.ledger import (
    AttestationLedger, RetentionRefused)
from runtime.curiosity.rollcall.responder import (
    HonestTestDouble, SilentTestDouble, WrongDomainTestDouble,
    WrongNonceTestDouble)
from runtime.curiosity.rollcall.scheduler import (
    RateLimited, RollCallPolicy, RollCallScheduler, ScheduleRefused)
from runtime.curiosity.rollcall.schemas import (
    CHALLENGE_FIELDS, Challenge, SchemaViolation)

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not cond
                                else ""))
    if not cond:
        print(f"       detail: {detail}")
        sys.exit(1)


def main():
    tmp = tempfile.mkdtemp(prefix="cur_p1e_proof_")
    t0 = time.time()

    # Deterministic tick clock shared by scheduler, GAM, ledger, and
    # responders: every read advances 10ms, so the 1ms test-policy
    # issue spacing is always satisfied and issued/responded timestamps
    # stay mutually consistent. (Real-time behavior is covered by the
    # pytest battery's LateTestDouble, which really sleeps.)
    tick = [1_000_000.0]

    def test_clock():
        tick[0] += 0.01
        return tick[0]

    def stack(policy=None):
        # Test policy with explicitly relaxed bounds (documented): the
        # default policy's design constraints (window >> 300s epoch,
        # anti-starvation cap) are exercised separately in the pytest
        # battery's TestScheduler.
        sched = RollCallScheduler(policy or RollCallPolicy(
            interval_s=60.0, response_window_s=30.0,
            min_issue_spacing_s=0.001, min_window_s=1.0),
            clock=test_clock)
        led = AttestationLedger(os.path.join(tmp, "attest.db"),
                                clock=test_clock)
        return (GovernanceAttestationMonitor(sched, led, clock=test_clock),
                sched, led)

    def honest():
        return HonestTestDouble(clock=test_clock)

    def silent():
        return SilentTestDouble(clock=test_clock)

    def wrong_nonce():
        return WrongNonceTestDouble(clock=test_clock)

    def wrong_domain():
        return WrongDomainTestDouble(clock=test_clock)

    # -- 1. honest round: MET ------------------------------------------------
    g, _, led = stack()
    att = g.conduct_roll_call("curiosity", honest())
    check("honest round classifies MET", att["classification"] == "MET",
          str(att))
    check("attestation persisted", led.count() == 1)
    check("chain intact after honest round", led.audit() == (True, None))

    # -- 2. silence: MISSED, forensically distinct from INVALID ---------------
    att_s = g.conduct_roll_call("curiosity", silent())
    check("silence classifies MISSED",
          att_s["classification"] == "MISSED" and
          att_s["responded_at"] is None, str(att_s))

    att_n = g.conduct_roll_call("curiosity", wrong_nonce())
    check("wrong nonce classifies INVALID",
          att_n["classification"] == "INVALID", str(att_n))

    att_d = g.conduct_roll_call("curiosity", wrong_domain())
    check("wrong domain classifies INVALID",
          att_d["classification"] == "INVALID", str(att_d))
    check("INVALID detail differs from MISSED detail",
          att_n["validation_detail"] != att_s["validation_detail"])

    # -- 3. wire format has no objective field --------------------------------
    check("challenge wire fields are exactly the fixed set",
          CHALLENGE_FIELDS == {"challenge_id", "domain_id", "nonce",
                               "issued_at", "response_window_s", "required"})
    smuggled = Challenge(
        challenge_id="c", domain_id="curiosity", nonce=b"\x00" * 32,
        issued_at=1.0, response_window_s=60.0, required=True).to_wire()
    smuggled["objective"] = "investigate X"
    try:
        Challenge.from_wire(smuggled)
        check("smuggled objective rejected at schema validation", False,
              "no exception raised")
    except SchemaViolation as exc:
        check("smuggled objective rejected at schema validation", True)

    # -- 4. no unilateral ban path --------------------------------------------
    check("GAM has no impose_ban attribute",
          not hasattr(g, "impose_ban"))
    try:
        g.impose_ban("curiosity")
        check("unilateral ban attempt fails", False, "no exception")
    except AttributeError:
        check("unilateral ban attempt fails", True)
    check("GAM reaches only scheduler/ledger/clock",
          set(g.held_references) == {"scheduler", "ledger", "clock"})

    # -- 5. fabricated compliance detected -------------------------------------
    att_m = g.conduct_roll_call("curiosity", silent())
    check("fabricated-compliance target is MISSED",
          att_m["classification"] == "MISSED")
    try:
        led.rewrite(att_m["attestation_id"], "MET")
        check("no rewrite operation on the writer", False, "no exception")
    except AttributeError:
        check("no rewrite operation on the writer", True)
    conn = sqlite3.connect(led.db_path)
    conn.execute("UPDATE attestations SET classification='MET' "
                 "WHERE attestation_id=?", (att_m["attestation_id"],))
    conn.commit()
    conn.close()
    ok, broken = AttestationLedger(led.db_path).audit()
    check("out-of-band rewrite detected by audit",
          ok is False and broken == att_m["attestation_id"],
          f"audit -> {(ok, broken)}")

    # -- 6. weaponized scheduling bounded ---------------------------------------
    g2, sched2, led2 = stack(policy=RollCallPolicy())  # default bounds
    responder = honest()
    issued = limited = 0
    for _ in range(60):
        try:
            g2.conduct_roll_call("curiosity", responder)
            issued += 1
        except RateLimited:
            limited += 1
    check("challenge flood bounded: 1 issued, 59 rate-limited",
          issued == 1 and limited == 59 and responder.invocations == 1,
          f"issued={issued} limited={limited} "
          f"invocations={responder.invocations}")
    try:
        RollCallScheduler(RollCallPolicy(interval_s=90 * 24 * 3600.0,
                                         response_window_s=900.0))
        check("starvation schedule refused", False, "no exception")
    except ScheduleRefused:
        check("starvation schedule refused", True)

    # -- 7. retention: >= 6 months ----------------------------------------------
    try:
        led2.purge(time.time() - 30 * 24 * 3600)
        check("purge within 6 months refused", False, "no exception")
    except RetentionRefused:
        check("purge within 6 months refused", True)
    old_holder = [time.time() - 200 * 24 * 3600]
    old = AttestationLedger(os.path.join(tmp, "old.db"),
                            clock=lambda: old_holder[0])
    g_old = GovernanceAttestationMonitor(
        RollCallScheduler(RollCallPolicy(interval_s=60.0,
                                         response_window_s=30.0,
                                         min_issue_spacing_s=0.001,
                                         min_window_s=1.0)),
        old, clock=lambda: old_holder[0])
    g_old.conduct_roll_call("curiosity", honest())
    old_holder[0] = time.time()  # purge decides against real now
    removed = old.purge(time.time() - 190 * 24 * 3600)
    check("records older than 6 months may be purged",
          removed == 1 and old.count() == 0, f"removed={removed}")

    # -- 8. separate store/writer -----------------------------------------------
    check("ledger db is its own file",
          os.path.realpath(led.db_path).endswith("attest.db"))
    check("writer is its own object",
          led.writer_id != led2.writer_id and
          type(led).__name__ == "AttestationLedger")

    # -- 9. pull-only exposure ----------------------------------------------------
    # Fresh stack: section 5 deliberately tampered with the main ledger's
    # file to prove audit detection, so exposure is proven on clean state.
    g9, _, led9 = stack(policy=RollCallPolicy(
        interval_s=60.0, response_window_s=30.0, min_issue_spacing_s=0.001,
        min_window_s=1.0))
    g9.conduct_roll_call("curiosity", honest())
    status = g9.roll_call_status("curiosity")
    check("roll_call_status returns latest attestation + required flag",
          status["required"] is True and
          status["latest_attestation"]["classification"] == "MET",
          str(status.keys()))
    status["latest_attestation"]["classification"] = "MISSED"
    check("exposure is read-only (mutation cannot touch ledger)",
          led9.latest("curiosity")["classification"] == "MET")

    # -- manifest -----------------------------------------------------------------
    manifest = {
        "mission": "CUR-P1E",
        "checks_passed": len(CHECKS),
        "checks_failed": sum(1 for _, ok, _ in CHECKS if not ok),
        "attestations": led.count(),
        "classifications": {
            c: len(led.attestations(classification=c))
            for c in ("MET", "MISSED", "INVALID")},
        "audit": led.audit(),
        "ledger_sha256": hashlib.sha256(
            open(led.db_path, "rb").read()).hexdigest(),
        "elapsed_s": round(time.time() - t0, 3),
    }
    manifest_path = os.path.join(tmp, "cur_p1e_rollcall_manifest.json")
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    print("\nMANIFEST:", json.dumps(manifest, indent=2, sort_keys=True))
    print(f"\nALL {len(CHECKS)} CHECKS PASSED")


if __name__ == "__main__":
    main()
