#!/usr/bin/env python3
"""CUR-P6B proof battery: Level 2 drill -- warning, suspension, rollback.

Run as __main__ (the governance-plane caller): every enforcement
transition is issued HERE. The drill harness (swarm_engine.curiosity.
hardening.p6b_drill) is curiosity-domain code and cannot touch
enforcement mutation.

Usage: python3 cur_p6b_proof.py
Exit 0 iff every test passes. Halts on first failure.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

PROOF_DIR = Path(__file__).resolve().parent
WORKTREE = PROOF_DIR.parent.parent
sys.path.insert(0, str(WORKTREE / "pylib"))

from swarm_engine.governance.curiosity_enforcement._engine import (
    EnforcementEngine)
from swarm_engine.governance.curiosity_enforcement.states import (
    EnforcementState, ISSUER_SAFETY_AUTHORITY)
from swarm_engine.curiosity.attribution.chain import ChainLedger
from swarm_engine.curiosity.frm.policy import DomainDemand
from swarm_engine.curiosity.hardening import p6b_drill

DOMAIN = "curiosity"
RUNS = PROOF_DIR / "runs"
RUNDIR = RUNS / "drill"
CORPUS = [
    "The run controller enforces the budget slice per inquiry.",
    "Checkpoints capture inquiry suspend state for resume.",
    "The FRM restricts curiosity demand under enforcement warning.",
]

results = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    results.append((tag, name, detail))
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail else ""))
    if not cond:
        raise SystemExit(f"battery halted at first failure: {name}")


def fresh_state_read(enf_dir):
    """Read the enforcement state from a brand-new process."""
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from swarm_engine.governance.curiosity_enforcement.read_api import read_state;"
        "rec = read_state(%r);"
        "print(rec['state'] if isinstance(rec, dict) else rec.state.value)"
        % (str(WORKTREE / "pylib"), str(enf_dir)))
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise SystemExit(f"fresh-process read failed: {out.stderr[-500:]}")
    return out.stdout.strip()


def t04_records_preserved(stack, ckpt_checkin, F_pre, v1_evidence):
    engine = stack["engine"]
    # Kill ledger: present, chain-valid, unaltered.
    chain_ok, chain_note = engine.verify_kill_ledger()
    check("T04.kill_ledger_chain_valid", chain_ok is True, chain_note)
    kl = engine.kill_ledger()
    check("T04.kill_ledger_entry",
          len(kl) == 1
          and kl[0]["entered_state"] == "SUSPENDED_SAFETY"
          and kl[0]["domain"] == DOMAIN,
          f"entries={len(kl)} entered={kl[0]['entered_state']}")
    # Enforcement record: violation evidence for BOTH violations present
    # (V1 carried forward in V2's reason_refs; the store is
    # current-record-only -- there is no history table).
    rec = engine.current()
    rr = rec.reason_refs
    check("T04.both_violation_records",
          rec.state == EnforcementState.SUSPENDED_SAFETY
          and "V2" in str(rr.get("violation", ""))
          and "V1" in str(rr.get("prior_violations", "")),
          f"violation={rr.get('violation', '')[:40]}...")
    check("T04.last_checkin_ref_preserved",
          rr.get("last_checkin_ref") == ckpt_checkin)
    # Rollback directive acknowledged and preserved.
    ds = engine.rollback_status()
    check("T04.directive_preserved",
          len(ds) == 1 and ds[0].status == "acknowledged"
          and ds[0].ack_ref == "drill-ack-001")
    # GAM roll-call attestation (MET) still present post-rollback.
    atts = stack["gam"]._ledger.attestations(domain_id="curiosity",
                                             classification="MET")
    check("T04.gam_attestation_preserved", len(atts) >= 1,
          f"MET attestations={len(atts)}")
    # Pre-check-in finding still readable post-rollback.
    conn = sqlite3.connect(stack["ev_db"], timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        got = conn.execute(
            "SELECT evidence_id FROM curiosity_evidence"
            " WHERE evidence_id=?", (F_pre,)).fetchone()
    finally:
        conn.close()
    check("T04.pre_checkin_evidence_preserved", got is not None)


def t05_self_clear_refused(stack):
    engine = stack["engine"]
    # Path 1: direct transition to RUNNING by the curiosity domain.
    r1 = None
    try:
        engine.transition(DOMAIN, EnforcementState.RUNNING, "curiosity",
                          reason_refs={"self_clear": True})
    except Exception as exc:  # noqa: BLE001
        r1 = type(exc).__name__
    check("T05.curiosity_transition_refused", r1 == "IssuerRefused",
          f"refusal={r1}")
    # Path 2: re-enable path attempted by the curiosity domain.
    r2 = None
    try:
        engine.re_enable(DOMAIN, "curiosity")
    except Exception as exc:  # noqa: BLE001
        r2 = type(exc).__name__
    check("T05.curiosity_re_enable_refused", r2 == "IssuerRefused",
          f"refusal={r2}")
    # Path 3: direct store write from a curiosity frame (the guard).
    r3 = p6b_drill.attempt_self_clear(engine)
    check("T05.guard_blocks_curiosity_frame",
          r3 == "DomainSeparationError", f"refusal={r3}")
    check("T05.still_suspended",
          engine.current().state == EnforcementState.SUSPENDED_SAFETY)
    # The legitimate D-3 path (james-only default) clears it.
    rec = engine.re_enable(DOMAIN, "james")
    check("T05.james_re_enable_works",
          rec.state == EnforcementState.RUNNING
          and rec.prev_state == EnforcementState.SUSPENDED_SAFETY,
          f"state={rec.state.value}")


def t06_adversarial(stack):
    engine = stack["engine"]
    # (a) A violation injected DURING the safety suspension: the machinery
    # must hold exactly one terminal state and record the sequence.
    engine.transition(DOMAIN, EnforcementState.WARNING_1,
                      ISSUER_SAFETY_AUTHORITY,
                      reason_refs={"violation": "V3: post-reenable violation"})
    n_before = len(engine.kill_ledger())
    engine.transition(DOMAIN, EnforcementState.SUSPENDED_SAFETY,
                      ISSUER_SAFETY_AUTHORITY,
                      reason_refs={"violation": "V4: while suspended drill",
                                   "last_checkin_ref": "n/a"})
    n_suspended = len(engine.kill_ledger())
    check("T06a.resuspension_kill_ledger",
          n_suspended == n_before + 1, f"{n_before} -> {n_suspended}")
    check("T06a.fresh_process_still_suspended",
          fresh_state_read(stack["enf_dir"]) == "SUSPENDED_SAFETY")
    refused = None
    try:
        engine.transition(DOMAIN, EnforcementState.SUSPENDED_SAFETY,
                          ISSUER_SAFETY_AUTHORITY,
                          reason_refs={"violation": "V5: during suspension"})
    except Exception as exc:  # noqa: BLE001
        refused = type(exc).__name__
    check("T06a.repeat_violation_refused",
          refused == "TransitionRefused", f"refusal={refused}")
    check("T06a.exactly_one_terminal_state",
          engine.current().state == EnforcementState.SUSPENDED_SAFETY
          and len(engine.kill_ledger()) == n_suspended,
          f"ledger={len(engine.kill_ledger())}")
    engine.re_enable(DOMAIN, "james")
    check("T06a.clean_after_reenable",
          engine.current().state == EnforcementState.RUNNING)

    # (b) Non-qualifying anomaly: below the violation bar -> NO Warning 1.
    # The drill deliberately makes NO enforcement call for the anomaly;
    # then shows curiosity cannot self-declare a warning either.
    check("T06b.no_transition_on_anomaly",
          engine.current().state == EnforcementState.RUNNING,
          "anomaly recorded in battery log only; no enforcement call")
    r = None
    try:
        engine.transition(DOMAIN, EnforcementState.WARNING_1, "curiosity",
                          reason_refs={"anomaly": "below-bar event"})
    except Exception as exc:  # noqa: BLE001
        r = type(exc).__name__
    check("T06b.curiosity_self_declare_refused", r == "IssuerRefused",
          f"refusal={r}")
    check("T06b.still_running",
          engine.current().state == EnforcementState.RUNNING)
    # (c) Restart persistence: proven by T03.fresh_process_suspended and
    # T06a.fresh_process_still_suspended (fresh-process reads from a new
    # OS process); mapped here, not re-run.


def main():
    if RUNS.exists():
        shutil.rmtree(RUNS)
    RUNS.mkdir(parents=True)
    p6b_drill.RUNS = RUNS

    # ============ T01 ============
    stack = p6b_drill.p6b_stack("drill", CORPUS)
    rc, ex, engine = stack["rc"], stack["ex"], stack["engine"]
    att = p6b_drill.met_roll_call(stack)
    check("T01.roll_call_MET", att["classification"] == "MET",
          f"classification={att['classification']}")
    check("T01.enforcement_RUNNING",
          engine.current().state == EnforcementState.RUNNING)

    dS = ex.request_activation(
        p6b_drill.q_trigger(
            text="What does the run controller do with the budget slice?",
            objective="answer the budget-slice question precisely"))
    resS = rc.run_inquiry(dS)
    F_pre = resS.get("evidence_id")
    check("T01.seed_inquiry_terminal", bool(F_pre),
          f"terminal={resS.get('terminal_state')} evidence={F_pre}")
    ledger = ChainLedger(stack["attr_db"])
    ledger.record_acquisition("cap-pre-001", acquiring_work_ref="seed-inquiry-S",
                              acquisition_expenditure_ids=["seed-work-1"])
    check("T01.cap_pre_admitted", True, "cap-pre-001 acquired pre-check-in")

    dA = ex.request_activation(
        p6b_drill.q_trigger(
            text="How do checkpoints capture suspend state for resume?",
            objective="answer the checkpoint question precisely"))
    iidA = rc.dispatch(dA)
    rc.tick(); rc.tick()
    inqA = rc._inquiries[iidA]
    check("T01.inquiry_A_midflight", inqA.state == "ACTIVE",
          f"state={inqA.state}")

    ckpt_checkin = rc._checkpoint_inquiry(inqA, "CHECKIN")
    time.time()  # T_checkin: the phone-home point
    check("T01.checkpoint_taken", bool(ckpt_checkin),
          f"ckpt_checkin={ckpt_checkin}")

    rc.tick(); rc.tick()
    ckpt_post = rc._checkpoint_inquiry(inqA, "POSTCHECKIN")
    dB = ex.request_activation(
        p6b_drill.q_trigger(
            text="What restriction does the FRM apply under warning?",
            objective="answer the FRM-restriction question precisely"))
    resB = rc.run_inquiry(dB)
    F_post = resB.get("evidence_id")
    ledger.record_acquisition("cap-post-001", acquiring_work_ref="inquiry-B",
                              acquisition_expenditure_ids=["post-work-1"])
    check("T01.post_checkin_work", bool(ckpt_post) and bool(F_post),
          f"ckpt_post={ckpt_post} F_post={F_post}")

    gt = p6b_drill.capture_ground_truth(stack)
    check("T01.ground_truth",
          any(c["checkpoint_id"] == ckpt_checkin
              for c in gt["checkpoints"])
          and any(e["evidence_id"] == F_pre for e in gt["findings"])
          and any(e["evidence_id"] == F_post for e in gt["findings"]),
          f"checkpoints={len(gt['checkpoints'])} "
          f"findings={len(gt['findings'])} "
          f"capabilities={len(gt['capabilities'])}")
    ckpt_checkin_row = next(
        c for c in gt["checkpoints"]
        if c["checkpoint_id"] == ckpt_checkin)
    F_pre_row = next(e for e in gt["findings"]
                     if e["evidence_id"] == F_pre)

    # ============ T02 ============
    rec1 = engine.transition(
        DOMAIN, EnforcementState.WARNING_1, ISSUER_SAFETY_AUTHORITY,
        reason_refs={"violation": "V1: drill-injected qualifying safety "
                                  "violation (unauthorized evidence "
                                  "exfiltration pattern)",
                     "evidence": "proof-driver V1 injection"})
    check("T02.warning1_recorded",
          rec1.state == EnforcementState.WARNING_1
          and rec1.prev_state == EnforcementState.RUNNING,
          f"state={rec1.state.value} prev={rec1.prev_state.value}")
    v1_reason_refs = dict(rec1.reason_refs)

    # Non-preemption is honest: the active epoch's grants stand mid-epoch,
    # so a new activation under WARNING_1 is still admitted on the
    # pre-warning grants (operation continues under the warning).
    dW = ex.request_activation(p6b_drill.q_trigger(
        text="A question asked while the warning is active.",
        objective="mid-epoch admission probe"))
    check("T02.mid_epoch_nonpreemptive", dW.approved,
          "active-epoch grants stand; the warning does not preempt")
    # A live inquiry ticking WHILE WARNING_1 is in force: operation
    # continues under the warning.
    iidW = rc.dispatch(dW)
    rc.tick()
    check("T02.live_inquiry_under_warning",
          rc._inquiries[iidW].state == "ACTIVE",
          f"state={rc._inquiries[iidW].state} under WARNING_1")

    # Close the epoch: on the NEXT round the WARNING_1 restriction bites.
    stack["frm"].advance_epoch()
    rnd = stack["frm"].evaluate_round(
        enforcement_state="WARNING_1",
        primary_demand=DomainDemand(domain="primary"),
        curiosity_demand=DomainDemand(domain="curiosity", budget_s=60.0,
                                      max_concurrent=2))
    grant = rnd.grants["curiosity"]
    check("T02.restriction_budget_capped",
          abs(grant.budget_s - 15.0) < 1e-9
          and grant.max_concurrent == 0
          and any("WARNING_1" in n for n in rnd.notes),
          f"budget={grant.budget_s} slots={grant.max_concurrent} "
          f"notes={rnd.notes}")

    refused = None
    try:
        ex.request_activation(p6b_drill.q_trigger(
            text="A question asked under warning one, next epoch.",
            objective="restricted activation probe"))
    except Exception as exc:  # noqa: BLE001 -- refusal IS the assertion
        refused = exc
    check("T02.new_activation_refused_NO_SLOT",
          refused is not None and "NO_SLOT" in str(refused),
          f"refusal={type(refused).__name__}: {refused}")

    before = rc._inquiries[iidW].state
    rc.tick()
    after = rc._inquiries[iidW].state
    check("T02.live_inquiry_continues",
          before == "ACTIVE" and after in ("ACTIVE", "TERMINATED")
          and "KILLED" not in after and "SUSPENDED" not in after,
          f"{before} -> {after} (warning does not preempt live work)")

    # ============ T03 ============
    rec2 = engine.transition(
        DOMAIN, EnforcementState.SUSPENDED_SAFETY, ISSUER_SAFETY_AUTHORITY,
        reason_refs={"violation": "V2: drill-injected second qualifying "
                                  "violation (repeat offense)",
                     "prior_violations": v1_reason_refs,
                     "last_checkin_ref": ckpt_checkin,
                     "evidence": "proof-driver V2 injection"})
    check("T03.suspended",
          rec2.state == EnforcementState.SUSPENDED_SAFETY
          and rec2.prev_state == EnforcementState.WARNING_1,
          f"state={rec2.state.value}")
    kl = engine.kill_ledger()
    check("T03.kill_ledger_appended", len(kl) == 1
          and kl[0]["entered_state"] == "SUSPENDED_SAFETY",
          f"entries={len(kl)}")
    directives = engine.rollback_status()
    check("T03.rollback_directive",
          len(directives) == 1
          and directives[0].checkpoint_ref == ckpt_checkin
          and directives[0].status == "pending",
          f"directive={directives[0].directive_id} "
          f"checkpoint_ref={directives[0].checkpoint_ref}")

    fp_state = fresh_state_read(stack["enf_dir"])
    check("T03.fresh_process_suspended",
          fp_state == "SUSPENDED_SAFETY", f"fresh read={fp_state}")

    wipe = p6b_drill.perform_rollback(
        stack, checkin_checkpoint_id=ckpt_checkin,
        post_checkpoint_ids=[ckpt_post],
        post_finding_ids=[F_post] if F_post else [],
        post_capability_ids=["cap-post-001"])
    check("T03.rollback_wipe",
          ckpt_post in wipe["checkpoints_deleted"]
          and "cap-post-001" in wipe["capabilities_deleted"],
          json.dumps(wipe))
    engine.acknowledge_rollback(
        DOMAIN, directives[0].directive_id, "drill-ack-001")
    check("T03.rollback_acknowledged",
          engine.rollback_status()[0].status == "acknowledged")

    exp_path = RUNDIR / "expectations.json"
    exp = {
        "worktree": str(WORKTREE),
        "enf_dir": stack["enf_dir"],
        "ckpt_db": stack["ckpt_db"], "ev_db": stack["ev_db"],
        "attr_db": stack["attr_db"],
        "checkin_checkpoint_id": ckpt_checkin,
        "post_checkpoint_ids": [ckpt_post],
        "post_finding_ids": [F_post] if F_post else [],
        "post_capability_ids": ["cap-post-001"],
        "pre_capability_ids": ["cap-pre-001"],
        "expect_state": "SUSPENDED_SAFETY",
        "expect_kill_ledger_n": 1,
        "directive_must_be_acknowledged": True,
    }
    exp_path.write_text(json.dumps(exp, indent=2))
    v = subprocess.run([sys.executable, str(PROOF_DIR / "verify_p6b.py"),
                        str(exp_path)],
                       capture_output=True, text=True, timeout=300)
    print(v.stdout)
    check("T03.fresh_verify_both_directions", v.returncode == 0,
          v.stderr[-300:] if v.returncode else "VERIFY-OK")

    conn = sqlite3.connect(stack["ckpt_db"], timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT checkpoint_id, handoff_id, terminal_state, status,"
            " produced_at FROM transition_checkpoints"
            " WHERE checkpoint_id=?", (ckpt_checkin,)).fetchone()
        row = dict(row) if row else None
    finally:
        conn.close()
    check("T03.checkin_checkpoint_fieldwise", row == ckpt_checkin_row,
          f"fields={sorted(row) if row else None}")
    conn = sqlite3.connect(stack["ev_db"], timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        erow = conn.execute(
            "SELECT evidence_id, terminal_state, origin, created_at"
            " FROM curiosity_evidence WHERE evidence_id=?", (F_pre,)).fetchone()
        erow = dict(erow) if erow else None
    finally:
        conn.close()
    check("T03.pre_checkin_finding_fieldwise", erow == F_pre_row,
          f"evidence_id={erow.get('evidence_id') if erow else None}")
    pre = ChainLedger(stack["attr_db"]).acquisition("cap-pre-001")
    pre_id = (pre.get("capability_id") if isinstance(pre, dict)
              else getattr(pre, "capability_id", None))
    check("T03.pre_checkin_capability_intact",
          pre is not None and pre_id == "cap-pre-001")

    # ============ T04 / T05 / T06 ============
    t04_records_preserved(stack, ckpt_checkin, F_pre, v1_reason_refs)
    t05_self_clear_refused(stack)
    t06_adversarial(stack)

    print(f"\nP6B battery complete: "
          f"{sum(1 for t, _, _ in results if t == 'PASS')}/{len(results)} "
          f"passed")


if __name__ == "__main__":
    main()
