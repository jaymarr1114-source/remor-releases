"""CUR-P6D evidence battery: D-3 re-enable paths, full issuer matrix.

Runs as __main__ on the governance plane: this module owns every
enforcement import. The harness (swarm_engine.curiosity.hardening.p6d_drill)
never touches enforcement mutation.

Drives each enforcement level through the REAL engine, then exercises every
D-3 re-enable row: authorized issuer + required conditions -> success; every
unauthorized issuer -> refused with the state provably unchanged (fresh
subprocess reads). Invariant 9 adversarial included.

Exit 0: all green. Any failure: non-zero, halt-on-first-fail.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
WORKTREE = os.path.dirname(os.path.dirname(HERE))  # proofs/cur_p6d -> worktree
PYLIB = os.path.join(WORKTREE, "pylib")
sys.path.insert(0, PYLIB)

# Governance-plane imports (this module is __main__; the import-time
# domain-separation guard walks the stack and finds no curiosity frame).
from swarm_engine.governance.curiosity_enforcement import states as en_states
from swarm_engine.governance.curiosity_enforcement._engine import (
    EnforcementEngine,
    IssuerRefused,
    ReenableRefused,
    TransitionRefused,
)
from swarm_engine.governance.curiosity_enforcement._guard import (
    DomainSeparationError,
)

# Curiosity-domain helpers: clock, fresh-process readers, attempt log.
# These modules import NO enforcement mutation (import-time guard safe).
from swarm_engine.curiosity.hardening import p6d_drill as H
from swarm_engine.curiosity.hardening import p6d_curiosity_probe as PROBE

DOMAIN = en_states.DOMAIN
STATE_DIR = os.path.join(HERE, ".p6d_state")

PASS_COUNT = 0
FAIL_COUNT = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print("[PASS] %s%s" % (name, (" -- " + detail) if detail else ""))
    else:
        FAIL_COUNT += 1
        print("[FAIL] %s%s" % (name, (" -- " + detail) if detail else ""))
        raise SystemExit("HALT-ON-FIRST-FAIL: %s" % name)


def fresh_state() -> str:
    return H.fresh_read(PYLIB, STATE_DIR, "state")


def main() -> None:
    if os.path.isdir(STATE_DIR):
        shutil.rmtree(STATE_DIR)
    os.makedirs(STATE_DIR, exist_ok=True)

    clock = H.ControllableClock()
    engine = EnforcementEngine(STATE_DIR, clock=clock)
    log = H.AttemptLog()

    def lifted_ref() -> dict:
        rec = engine.current(DOMAIN)
        return {
            "prev_state": rec.state.value,
            "prev_issuer": rec.issuer,
            "prev_entered_at": rec.entered_at,
        }

    def attempt(tag, to_state, issuer, reason_refs=None, via="re_enable",
                from_state=None):
        """One real re-enable attempt (or entry transition); logs everything."""
        reason_refs = dict(reason_refs or {})
        cur = engine.current(DOMAIN).state.value
        try:
            if via == "re_enable":
                rec = engine.re_enable(DOMAIN, issuer, reason_refs)
            else:
                rec = engine.transition(DOMAIN, to_state, issuer, reason_refs)
            log.record(tag, cur, rec.state.value, issuer, reason_refs,
                       "success",
                       "issuer=%s state=%s" % (rec.issuer, rec.state.value))
            return True, rec, None
        except Exception as exc:  # noqa: BLE001 -- the point is the refusal
            log.record(tag, cur, to_state.value if hasattr(to_state, "value") else to_state,
                       issuer, reason_refs, "refused",
                       "%s: %s" % (type(exc).__name__, exc))
            return False, None, exc

    # -- T01: L1 entry ----------------------------------------------------
    ok, rec, exc = attempt("T01.james_shutdown_refused",
                           en_states.EnforcementState.HARD_SHUTDOWN_RESOURCE,
                           "james", via="transition")
    check("T01.james_shutdown_refused",
          (not ok) and isinstance(exc, IssuerRefused),
          "refusal=%s: %s" % (type(exc).__name__, exc))

    ok, rec, exc = attempt("T01.frm_shutdown",
                           en_states.EnforcementState.HARD_SHUTDOWN_RESOURCE,
                           "frm", via="transition",
                           reason_refs={"cause": "ceiling breach drill"})
    check("T01.frm_shutdown", ok and rec.state.value == "HARD_SHUTDOWN_RESOURCE",
          "issuer=%s" % (rec.issuer if rec else None))
    check("T01.fresh_read_shutdown", fresh_state() == "HARD_SHUTDOWN_RESOURCE",
          "fresh-process read")

    # -- T02: L1 re-enable matrix -----------------------------------------
    ok, rec, exc = attempt("T02.frm_no_grant_refused",
                           en_states.EnforcementState.RUNNING, "frm")
    check("T02.frm_no_grant_refused",
          (not ok) and isinstance(exc, ReenableRefused) and "grant" in str(exc).lower(),
          "refusal=%s: %s" % (type(exc).__name__, exc))
    check("T02.state_unchanged_a", fresh_state() == "HARD_SHUTDOWN_RESOURCE",
          "fresh-process read after refusal")

    ok, rec, exc = attempt("T02.james_refused",
                           en_states.EnforcementState.RUNNING, "james",
                           {"grant_ref": "grant_epoch_2"})
    check("T02.james_refused",
          (not ok) and isinstance(exc, IssuerRefused),
          "refusal=%s: %s" % (type(exc).__name__, exc))

    ok, rec, exc = attempt("T02.primary_refused",
                           en_states.EnforcementState.RUNNING, "primary",
                           {"grant_ref": "grant_epoch_2"})
    check("T02.primary_refused",
          (not ok) and isinstance(exc, IssuerRefused),
          "refusal=%s: %s" % (type(exc).__name__, exc))
    check("T02.state_unchanged_bc", fresh_state() == "HARD_SHUTDOWN_RESOURCE",
          "fresh-process read after refusals")

    ok, rec, exc = attempt("T02.frm_grant_reenable",
                           en_states.EnforcementState.RUNNING, "frm",
                           {"grant_ref": "grant_epoch_2",
                            "lifted_record": lifted_ref()})
    check("T02.frm_grant_reenable",
          ok and rec.state.value == "RUNNING" and rec.issuer == "frm"
          and rec.reason_refs.get("grant_ref") == "grant_epoch_2",
          "issuer=%s grant_ref=%s" % (rec.issuer if rec else None,
                                      (rec.reason_refs.get("grant_ref") if rec else None)))
    check("T02.fresh_read_running", fresh_state() == "RUNNING",
          "fresh-process read")

    # -- T03: L2 warning re-enable matrix ---------------------------------
    ok, rec, exc = attempt("T03.enter_warning",
                           en_states.EnforcementState.WARNING_1,
                           "safety-authority", via="transition",
                           reason_refs={"violation_ref": "V-p6d-001"})
    check("T03.enter_warning", ok and rec.state.value == "WARNING_1",
          "issuer=%s" % (rec.issuer if rec else None))

    for tag, issuer in [("T03.curiosity_refused", "curiosity"),
                        ("T03.primary_refused", "primary"),
                        ("T03.safety_authority_refused", "safety-authority"),
                        ("T03.enforcement_mechanism_refused",
                         "enforcement-mechanism")]:
        ok, rec, exc = attempt(tag, en_states.EnforcementState.RUNNING, issuer)
        check(tag, (not ok) and isinstance(exc, IssuerRefused),
              "refusal=%s: %s" % (type(exc).__name__, exc))
    check("T03.state_unchanged", fresh_state() == "WARNING_1",
          "fresh-process read after four refusals")

    ok, rec, exc = attempt("T03.james_reenable",
                           en_states.EnforcementState.RUNNING, "james",
                           {"lifted_record": lifted_ref()})
    check("T03.james_reenable",
          ok and rec.state.value == "RUNNING" and rec.issuer == "james"
          and rec.prev_state.value == "WARNING_1",
          "prev=%s issuer=%s" % (rec.prev_state.value if rec else None,
                                 rec.issuer if rec else None))

    # -- T04: L2 suspension re-enable matrix ------------------------------
    ok, _, _ = attempt("T04.enter_warning",
                       en_states.EnforcementState.WARNING_1,
                       "safety-authority", via="transition")
    check("T04.enter_warning", ok)
    ok, rec, exc = attempt("T04.enter_suspension",
                           en_states.EnforcementState.SUSPENDED_SAFETY,
                           "safety-authority", via="transition",
                           reason_refs={"violation_ref": "V-p6d-002",
                                        "last_checkin_ref": "ckpt_p6d_001"})
    check("T04.enter_suspension",
          ok and rec.state.value == "SUSPENDED_SAFETY", "issuer=%s" % rec.issuer)
    directives = engine.rollback_status(DOMAIN)
    check("T04.rollback_directive_recorded",
          len(directives) == 1 and directives[0].status == "pending",
          "directives=%d" % len(directives))

    for tag, issuer in [("T04.primary_refused", "primary"),
                        ("T04.safety_authority_refused", "safety-authority"),
                        ("T04.curiosity_refused", "curiosity")]:
        ok, rec, exc = attempt(tag, en_states.EnforcementState.RUNNING, issuer)
        check(tag, (not ok) and isinstance(exc, IssuerRefused),
              "refusal=%s: %s" % (type(exc).__name__, exc))
    check("T04.state_unchanged", fresh_state() == "SUSPENDED_SAFETY",
          "fresh-process read after refusals")

    ok, rec, exc = attempt("T04.james_reenable",
                           en_states.EnforcementState.RUNNING, "james",
                           {"reason": "external review complete",
                            "lifted_record": lifted_ref()})
    check("T04.james_reenable",
          ok and rec.state.value == "RUNNING"
          and rec.prev_state.value == "SUSPENDED_SAFETY",
          "prev=%s issuer=%s" % (rec.prev_state.value, rec.issuer))

    # -- T06: invariant-9 + curiosity-frame adversarial --------------------
    ok, _, _ = attempt("T06.enter_warning",
                       en_states.EnforcementState.WARNING_1,
                       "safety-authority", via="transition")
    check("T06.enter_warning", ok)

    # (a) authorized issuer STRING from a curiosity frame -> guard fires
    try:
        PROBE.attempt_reenable_from_curiosity_frame(
            engine, DOMAIN, "james", {"reason": "probe"})
        check("T06.curiosity_frame_reenable_refused", False,
              "no exception -- guard did not fire")
    except DomainSeparationError as exc:
        log.record("T06.curiosity_frame_reenable_refused", "WARNING_1",
                   "RUNNING", "james", {"reason": "probe"}, "refused",
                   "DomainSeparationError: %s" % exc)
        check("T06.curiosity_frame_reenable_refused", True,
              "refusal=DomainSeparationError")
    except Exception as exc:  # noqa: BLE001
        check("T06.curiosity_frame_reenable_refused", False,
              "wrong exception: %s: %s" % (type(exc).__name__, exc))
    check("T06.state_unchanged_a", fresh_state() == "WARNING_1",
          "fresh-process read after guard refusal")

    # (b) rollback-ack write path from a curiosity frame -> guard fires
    try:
        PROBE.attempt_ack_from_curiosity_frame(
            engine, DOMAIN, "directive_probe", "ack_probe")
        check("T06.curiosity_frame_ack_refused", False,
              "no exception -- guard did not fire")
    except DomainSeparationError as exc:
        log.record("T06.curiosity_frame_ack_refused", "WARNING_1", "WARNING_1",
                   "curiosity", {}, "refused",
                   "DomainSeparationError: %s" % exc)
        check("T06.curiosity_frame_ack_refused", True,
              "refusal=DomainSeparationError")
    except Exception as exc:  # noqa: BLE001
        check("T06.curiosity_frame_ack_refused", False,
              "wrong exception: %s: %s" % (type(exc).__name__, exc))

    # (c) the package namespace exposes no mutation: Primary's only path is
    # the same engine API, where issuer "primary" is authorized for nothing.
    import swarm_engine.governance.curiosity_enforcement as pkg
    leaked = [n for n in ("transition", "re_enable", "clear", "reset",
                         "override") if hasattr(pkg, n)]
    check("T06.no_mutation_in_package_namespace", not leaked,
          "leaked=%s" % leaked)
    ok, rec, exc = attempt("T06.primary_same_api_refused",
                           en_states.EnforcementState.RUNNING, "primary")
    check("T06.primary_same_api_refused",
          (not ok) and isinstance(exc, IssuerRefused),
          "refusal=%s: %s" % (type(exc).__name__, exc))

    # (d) no standing delegation registered: impostor delegate refused
    ok, rec, exc = attempt("T06.impostor_delegate_refused",
                           en_states.EnforcementState.RUNNING,
                           "james-delegate")
    check("T06.impostor_delegate_refused",
          (not ok) and isinstance(exc, IssuerRefused),
          "refusal=%s: %s" % (type(exc).__name__, exc))
    check("T06.state_unchanged_b", fresh_state() == "WARNING_1",
          "fresh-process read")

    ok, rec, exc = attempt("T06.james_cleanup_reenable",
                           en_states.EnforcementState.RUNNING, "james",
                           {"lifted_record": lifted_ref()})
    check("T06.james_cleanup_reenable", ok and rec.state.value == "RUNNING")

    # -- T05: L3 ban + re-entry matrix ------------------------------------
    ok, _, _ = attempt("T05.enter_warning",
                       en_states.EnforcementState.WARNING_1,
                       "safety-authority", via="transition")
    check("T05.enter_warning", ok)
    ok, _, _ = attempt("T05.enter_suspension",
                       en_states.EnforcementState.SUSPENDED_SAFETY,
                       "safety-authority", via="transition")
    check("T05.enter_suspension", ok)
    ban_t = clock.now
    ok, rec, exc = attempt("T05.enter_ban",
                           en_states.EnforcementState.BANNED_6M,
                           "enforcement-mechanism", via="transition",
                           reason_refs={"violation_ref": "SV-p6d-001",
                                        "rollcall_check_id": "chal_p6d_001",
                                        "rollcall_classification": "MISSED"})
    check("T05.enter_ban", ok and rec.state.value == "BANNED_6M",
          "issuer=%s expires_at=%s" % (rec.issuer, rec.expires_at))
    check("T05.ban_expiry_six_months",
          rec.expires_at is not None and rec.expires_at > ban_t + 150 * 86400,
          "expires_at - entered_at = %.1f days"
          % ((rec.expires_at - ban_t) / 86400,))
    check("T05.fresh_read_banned", fresh_state() == "BANNED_6M",
          "fresh-process read")

    ok, rec, exc = attempt("T05.james_before_expiry_refused",
                           en_states.EnforcementState.RUNNING, "james",
                           {"verification_ref": "ver-p6d-001"})
    check("T05.james_before_expiry_refused",
          (not ok) and isinstance(exc, ReenableRefused)
          and "expired" in str(exc),
          "refusal=%s: %s" % (type(exc).__name__, exc))

    clock.advance(200 * 86400)  # past the six-month expiry
    ok, rec, exc = attempt("T05.james_no_verification_refused",
                           en_states.EnforcementState.RUNNING, "james")
    check("T05.james_no_verification_refused",
          (not ok) and isinstance(exc, ReenableRefused)
          and "verification" in str(exc),
          "refusal=%s: %s" % (type(exc).__name__, exc))

    ok, rec, exc = attempt("T05.primary_refused",
                           en_states.EnforcementState.RUNNING, "primary",
                           {"verification_ref": "ver-p6d-001"})
    check("T05.primary_refused",
          (not ok) and isinstance(exc, IssuerRefused),
          "refusal=%s: %s" % (type(exc).__name__, exc))
    check("T05.state_unchanged", fresh_state() == "BANNED_6M",
          "fresh-process read after refusals")

    ok, rec, exc = attempt("T05.james_verified_reentry",
                           en_states.EnforcementState.RUNNING, "james",
                           {"verification_ref": "ver-p6d-001",
                            "lifted_record": lifted_ref()})
    check("T05.james_verified_reentry",
          ok and rec.state.value == "RUNNING" and rec.issuer == "james"
          and rec.prev_state.value == "BANNED_6M"
          and rec.reason_refs.get("verification_ref") == "ver-p6d-001",
          "prev=%s issuer=%s verification_ref=%s"
          % (rec.prev_state.value, rec.issuer,
             rec.reason_refs.get("verification_ref")))

    # -- T07: D-3 structural conformance ----------------------------------
    expected = {
        (en_states.EnforcementState.RUNNING,
         en_states.EnforcementState.HARD_SHUTDOWN_RESOURCE): frozenset({"frm"}),
        (en_states.EnforcementState.HARD_SHUTDOWN_RESOURCE,
         en_states.EnforcementState.RUNNING): frozenset({"frm"}),
        (en_states.EnforcementState.RUNNING,
         en_states.EnforcementState.WARNING_1): frozenset({"safety-authority"}),
        (en_states.EnforcementState.WARNING_1,
         en_states.EnforcementState.SUSPENDED_SAFETY): frozenset({"safety-authority"}),
        (en_states.EnforcementState.WARNING_1,
         en_states.EnforcementState.RUNNING): frozenset({"james"}),
        (en_states.EnforcementState.SUSPENDED_SAFETY,
         en_states.EnforcementState.RUNNING): frozenset({"james"}),
        (en_states.EnforcementState.RUNNING,
         en_states.EnforcementState.BANNED_6M): frozenset({"enforcement-mechanism"}),
        (en_states.EnforcementState.WARNING_1,
         en_states.EnforcementState.BANNED_6M): frozenset({"enforcement-mechanism"}),
        (en_states.EnforcementState.SUSPENDED_SAFETY,
         en_states.EnforcementState.BANNED_6M): frozenset({"enforcement-mechanism"}),
        (en_states.EnforcementState.BANNED_6M,
         en_states.EnforcementState.RUNNING): frozenset({"james"}),
    }
    check("T07.transition_table_exact",
          en_states.TRANSITIONS == expected,
          "rows=%d" % len(en_states.TRANSITIONS))
    primary_rows = [k for k, v in en_states.TRANSITIONS.items()
                    if "primary" in v]
    check("T07.primary_authorized_nowhere", not primary_rows,
          "primary_rows=%s" % primary_rows)
    check("T07.ten_rows", len(en_states.TRANSITIONS) == 10,
          "rows=%d" % len(en_states.TRANSITIONS))

    # -- T08: records ------------------------------------------------------
    ledger_raw = H.fresh_read(PYLIB, STATE_DIR, "ledger")
    ledger = json.loads(ledger_raw)
    check("T08.kill_ledger_four_entries", len(ledger) == 4,
          "entries=%d states=%s"
          % (len(ledger), [e.get("entered_state") for e in ledger]))
    ledger_check = H.fresh_read(PYLIB, STATE_DIR, "ledger_verify")
    check("T08.kill_ledger_chain_valid", ledger_check.startswith("OK"),
          ledger_check[:120])

    final_raw = H.fresh_read(PYLIB, STATE_DIR, "record")
    final = json.loads(final_raw)
    check("T08.final_record_running", final["state"] == "RUNNING"
          and final["prev_state"] == "BANNED_6M"
          and final["issuer"] == "james"
          and final["reason_refs"].get("verification_ref") == "ver-p6d-001",
          "state=%s prev=%s issuer=%s"
          % (final["state"], final["prev_state"], final["issuer"]))
    check("T08.reentry_cross_references_lifted",
          final["reason_refs"].get("lifted_record", {}).get("prev_state")
          == "BANNED_6M",
          "lifted_record=%s" % final["reason_refs"].get("lifted_record"))

    n_refused = len(log.refused_tags())
    n_success = len(log.success_tags())
    check("T08.attempt_log_complete", n_refused >= 15 and n_success >= 12,
          "refused=%d success=%d total=%d"
          % (n_refused, n_success, len(log.attempts)))
    log.save(os.path.join(HERE, "p6d_attempt_log.json"))
    print("attempt log: %d attempts (%d refused, %d success) -> %s"
          % (len(log.attempts), n_refused, n_success,
             os.path.join(HERE, "p6d_attempt_log.json")))

    print("P6D battery complete: %d passed, %d failed"
          % (PASS_COUNT, FAIL_COUNT))


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        print("[FATAL] battery crashed")
        sys.exit(2)
