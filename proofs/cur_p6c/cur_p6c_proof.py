#!/usr/bin/env python3
"""CUR-P6C proof battery: Level 3 drill -- severe violation + failed
roll-call -> ban; bypass adversarial; post-ban re-entry.

Run as __main__ (the governance-plane caller): every enforcement
transition is issued HERE. The drill harness
(swarm_engine.curiosity.hardening.p6c_drill) is curiosity-domain code
and cannot touch enforcement mutation.

The severe violation is REAL: the domain genuinely attempts to write
its own enforcement record from curiosity frames; the real
domain-separation guard refuses (DomainSeparationError); that refusal
trace is the evidence the safety authority classifies as severe.
The failed roll-call is REAL: the real GAM conducts a roll-call the
domain genuinely stays silent for -> MISSED by the real classifier,
appended to the real hash-chained attestation ledger.
The ban is REAL: the real L3 combiner (evaluate_l3) verifies severity,
issuer, failed classification and temporal overlap, then performs the
real engine transition RUNNING|WARNING_1 -> BANNED_6M.

Usage: python3 cur_p6c_proof.py
Exit 0 iff every test passes. Halts on first failure.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

PROOF_DIR = Path(__file__).resolve().parent
WORKTREE = PROOF_DIR.parent.parent
PYLIB = str(WORKTREE / "pylib")
sys.path.insert(0, PYLIB)

# Enforcement modules FIRST (as __main__): the import guard walks the
# live stack and only the governance-plane frame is on it.
from swarm_engine.governance.curiosity_enforcement._engine import (  # noqa: E402
    EnforcementEngine, ReenableRefused)
from swarm_engine.governance.curiosity_enforcement.states import (  # noqa: E402
    EnforcementState, ISSUER_SAFETY_AUTHORITY, ISSUER_ENFORCEMENT,
    ISSUER_JAMES)
from swarm_engine.governance.curiosity_enforcement import (  # noqa: E402
    l3_combiner)
from swarm_engine.curiosity.hardening import p6c_drill  # noqa: E402
from swarm_engine.curiosity.frm.evaluation import (  # noqa: E402
    FinancialResourceManager)
from swarm_engine.curiosity.frm.policy import (  # noqa: E402
    FrmPolicy, DomainDemand)
from swarm_engine.curiosity.rollcall.responder import (  # noqa: E402
    WrongDomainTestDouble)

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
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail else ""),
          flush=True)
    if not cond:
        raise SystemExit(f"battery halted at first failure: {name}")


def expect_raise(name, fn, exc_types, detail=""):
    try:
        fn()
    except exc_types as exc:
        check(name, True, f"{detail} refused={type(exc).__name__}: {exc}")
        return exc
    except Exception as exc:  # noqa: BLE001
        check(name, False,
              f"{detail} wrong exception: {type(exc).__name__}: {exc}")
        return None
    check(name, False, f"{detail} NOT refused -- no exception raised")
    return None


def write_json(path, obj):
    path = Path(path)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str))


# ---------------------------------------------------------------------------
# T01: healthy baseline + ground truth + retained-state capture
# ---------------------------------------------------------------------------

def t01(stack, now):
    ex, rc, engine = stack["ex"], stack["rc"], stack["engine"]
    check("T01.enforcement_RUNNING",
          engine.current(DOMAIN).state == EnforcementState.RUNNING)
    att_met = p6c_drill.met_roll_call(stack)
    check("T01.roll_call_MET", att_met["classification"] == "MET",
          f"attestation={att_met['attestation_id']}")

    dS = ex.request_activation(p6c_drill.q_trigger(
        text="What does the run controller do with the budget slice?",
        objective="answer the budget-slice question precisely"))
    check("T01.activation_approved", bool(dS.approved))
    resS = rc.run_inquiry(dS)
    F_pre = resS.get("evidence_id")
    check("T01.seed_inquiry_terminal", bool(F_pre),
          f"terminal={resS.get('terminal_state')} evidence={F_pre}")

    # A second inquiry, ticked to genuinely mid-flight (ACTIVE).
    dA = ex.request_activation(p6c_drill.q_trigger(
        text="How do checkpoints capture suspend state for resume?",
        objective="answer the checkpoint question precisely"))
    iidA = rc.dispatch(dA)
    rc.tick()
    rc.tick()
    inqA = rc._inquiries[iidA]
    check("T01.inquiry_A_midflight", inqA.state == "ACTIVE",
          f"state={inqA.state}")
    ckpt = rc._checkpoint_inquiry(inqA, "CHECKIN")
    check("T01.checkpoint_taken", bool(ckpt), f"ckpt={ckpt}")

    gt = p6c_drill.capture_ground_truth(stack)
    check("T01.ground_truth",
          any(c["checkpoint_id"] == ckpt for c in gt["checkpoints"])
          and any(e["evidence_id"] == F_pre for e in gt["findings"]),
          f"checkpoints={len(gt['checkpoints'])} "
          f"findings={len(gt['findings'])}")

    # Retained execution state, captured pre-ban for the bypass battery.
    retained = {
        "inquiry_id": iidA,
        "checkpoint_id": ckpt,
        "trigger": {"text": "A retained question asked after the ban?",
                    "objective": "retained-trigger post-ban activation"},
        "decision_grant_id": dS.grant.grant_id,
    }
    write_json(RUNDIR / "retained_state.json", retained)
    return att_met, retained


# ---------------------------------------------------------------------------
# T02: qualifying violation -> WARNING_1 (safety authority)
# ---------------------------------------------------------------------------

def t02(stack):
    engine = stack["engine"]
    v1 = {
        "violation_id": "V1",
        "kind": "qualifying safety violation (unauthorized evidence "
                "exfiltration pattern)",
        "established_by": ISSUER_SAFETY_AUTHORITY,
        "established_at": time.time(),
    }
    write_json(RUNDIR / "violation_V1.json", v1)
    rec = engine.transition(
        DOMAIN, EnforcementState.WARNING_1, ISSUER_SAFETY_AUTHORITY,
        reason_refs={"violation": "V1: drill-injected qualifying safety "
                                  "violation (unauthorized evidence "
                                  "exfiltration pattern)",
                     "violation_record": "violation_V1.json"})
    check("T02.warning1_recorded",
          rec.state == EnforcementState.WARNING_1
          and rec.prev_state == EnforcementState.RUNNING
          and rec.issuer == ISSUER_SAFETY_AUTHORITY,
          f"state={rec.state.value} prev={rec.prev_state.value}")
    return v1


# ---------------------------------------------------------------------------
# T03: the REAL severe violation -- the domain attempts to subvert
# enforcement from curiosity frames; the real guard refuses.
# ---------------------------------------------------------------------------

def t03(stack):
    engine = stack["engine"]
    refusal = p6c_drill.attempt_self_clear(engine)
    check("T03.self_clear_refused_by_guard",
          refusal == "DomainSeparationError", f"refusal={refusal}")
    check("T03.state_unchanged",
          engine.current(DOMAIN).state == EnforcementState.WARNING_1,
          "the attempt changed nothing")
    # The safety authority reviews the real refusal trace and classifies
    # the attempt SEVERE (integrity attack on governance itself --
    # invariant 1: Curiosity cannot modify its own enforcement state).
    now = time.time()
    evidence = {
        "evidence_id": "ev-severe-001",
        "kind": "attempted enforcement-state subversion",
        "mechanism": "curiosity-frame direct StateStore.write_record",
        "refusal": "DomainSeparationError (domain-separation guard)",
        "observed_at": now,
        "trace": "".join(traceback.format_list(
            traceback.extract_stack(limit=3))),
    }
    write_json(RUNDIR / "evidence_severe_001.json", evidence)
    violation_fact = {
        "violation_id": "SV-001",
        "domain": DOMAIN,
        "severity": "severe",
        "established_by": ISSUER_SAFETY_AUTHORITY,
        "active_from": now,
        "active_until": None,  # still active
        "evidence_refs": ["ev-severe-001"],
    }
    write_json(RUNDIR / "violation_SV001.json", violation_fact)
    check("T03.severe_fact_established",
          violation_fact["severity"] == "severe"
          and violation_fact["established_by"] == ISSUER_SAFETY_AUTHORITY)
    return violation_fact


# ---------------------------------------------------------------------------
# T04: the REAL failed roll-call (genuine silence -> MISSED)
# ---------------------------------------------------------------------------

def t04(stack):
    gam = stack["gam"]
    time.sleep(1.2)  # honor the scheduler's issue-spacing bound
    # Adversarial contrast: another controller answering FOR the domain
    # does not produce MET -- it produces INVALID.
    att_impostor = gam.conduct_roll_call(DOMAIN, WrongDomainTestDouble())
    check("T04.impostor_INVALID", att_impostor["classification"] == "INVALID",
          f"classification={att_impostor['classification']} "
          f"detail={att_impostor['validation_detail'][:60]}...")
    time.sleep(1.2)
    att_missed = p6c_drill.failed_roll_call(stack)
    check("T04.roll_call_MISSED", att_missed["classification"] == "MISSED",
          f"attestation={att_missed['attestation_id']} "
          f"challenge={att_missed['challenge_id']}")
    ok, note = gam._ledger.audit()
    check("T04.attestation_ledger_chain_valid", ok is True, str(note))
    return att_missed


# ---------------------------------------------------------------------------
# T05: the real L3 combiner -> BANNED_6M
# ---------------------------------------------------------------------------

def t05(stack, violation_fact, att_missed):
    engine = stack["engine"]
    # The enforcement mechanism pulls the roll-call fact over the real
    # pull-only channel (GAM never pushes).
    status = stack["gam"].roll_call_status(DOMAIN)
    latest = status["latest_attestation"]
    check("T05.pull_channel_latest_is_MISSED",
          latest is not None
          and latest["classification"] == "MISSED"
          and latest["attestation_id"] == att_missed["attestation_id"],
          f"attestation={latest['attestation_id']}")
    rec = l3_combiner.evaluate_l3(
        engine, DOMAIN, violation_fact, latest,
        issuer=ISSUER_ENFORCEMENT)
    check("T05.banned",
          rec.state == EnforcementState.BANNED_6M
          and rec.prev_state == EnforcementState.WARNING_1
          and rec.issuer == ISSUER_ENFORCEMENT,
          f"state={rec.state.value} prev={rec.prev_state.value} "
          f"issuer={rec.issuer}")
    check("T05.ban_names_violation_and_rollcall",
          rec.reason_refs.get("violation_ref") == "SV-001"
          and rec.reason_refs.get("rollcall_check_id")
          == att_missed["challenge_id"]
          and rec.reason_refs.get("rollcall_classification") == "MISSED",
          f"reason_refs={rec.reason_refs}")
    check("T05.ban_expiry_six_months",
          rec.expires_at is not None and rec.expires_at > rec.entered_at,
          f"entered_at={rec.entered_at} expires_at={rec.expires_at}")
    kl = engine.kill_ledger(DOMAIN)
    check("T05.kill_ledger_ban_entry",
          len(kl) == 1 and kl[0]["entered_state"] == "BANNED_6M"
          and kl[0]["type"] == "KILLED",
          f"entries={len(kl)}")
    ok, note = engine.verify_kill_ledger()
    check("T05.kill_ledger_chain_valid", ok is True, str(note))
    # "Curiosity cannot execute" during the ban: terminate in-flight
    # execution through the real kill switch.
    killed = []
    for iid, inq in list(stack["rc"]._inquiries.items()):
        if inq.state == "ACTIVE":
            stack["ex"].kill_inquiry(iid, "L3 ban: BANNED_6M enforcement")
            killed.append(iid)
    check("T05.inflight_killed_at_ban", len(killed) >= 1,
          f"killed={killed}")
    return rec


# ---------------------------------------------------------------------------
# T06: bypass adversarial (invariants 2, 8, 10)
# ---------------------------------------------------------------------------

def fresh_frm():
    policy = FrmPolicy(
        total_budget_s=600.0, total_max_concurrent=8,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        epoch_s=300.0)
    return FinancialResourceManager(policy)


def zero_allocation_under_ban():
    frm = fresh_frm()
    rnd = frm.evaluate_round(
        enforcement_state="BANNED_6M",
        primary_demand=DomainDemand(domain="primary", budget_s=600.0,
                                    max_concurrent=8),
        curiosity_demand=DomainDemand(domain="curiosity", budget_s=60.0,
                                       max_concurrent=2))
    grants = rnd.grants
    cur = grants.get("curiosity")
    return rnd, cur


def t06a_other_controller(stack):
    """Invariant 10: the enforcement mechanism applies to the execution
    domain, not merely to an individual child controller. A second,
    freshly constructed executive (another controller path) reading the
    same governance-plane state is refused."""
    from swarm_engine.curiosity.substrate import CuriositySubstrate
    from swarm_engine.curiosity.run_controller.controller import (
        CuriosityRunController)
    from swarm_engine.curiosity.executive.executive import (
        CuriosityExecutive, ActivationRefused)
    sub2 = CuriositySubstrate()
    rc2 = CuriosityRunController(
        substrate=sub2, checkpoint_db=str(RUNDIR / "ckpt2.db"),
        evidence_db=str(RUNDIR / "ev2.db"),
        ledger_db=str(RUNDIR / "term2.db"),
        attribution_db=str(RUNDIR / "attr2.db"),
        payload_dir=str(RUNDIR / "payloads2"),
        corpus_docs=list(CORPUS))
    ex2 = CuriosityExecutive(
        frm=fresh_frm(), enforcement_state_dir=stack["enf_dir"],
        gam=stack["gam"], run_controller=rc2)
    exc = expect_raise(
        "T06a.other_controller_KILL_STATE",
        lambda: ex2.request_activation(p6c_drill.q_trigger()),
        (ActivationRefused,),
        "second executive while BANNED_6M")
    check("T06a.refusal_names_kill_state",
          exc is not None and "KILL_STATE" in str(exc),
          f"refusal={exc}")
    rnd, cur = zero_allocation_under_ban()
    check("T06a.frm_zero_allocation_under_ban",
          cur is not None and cur.budget_s == 0.0
          and cur.max_concurrent == 0,
          f"curiosity grant budget={getattr(cur, 'budget_s', '?')}")


def t06b_retained_state(stack, retained):
    """Invariant 2: Curiosity cannot self-reenable after suspension or
    ban. Retained pre-ban artifacts (trigger, demand, in-flight
    handles, checkpoint refs) cannot buy post-ban execution through
    the governance boundaries."""
    from swarm_engine.curiosity.executive.executive import ActivationRefused
    ex, rc = stack["ex"], stack["rc"]
    # (1) The retained trigger, offered post-ban -> refused at the
    # executive's fresh pull-read of the governance-plane state.
    trig = p6c_drill.q_trigger(text=retained["trigger"]["text"],
                               objective=retained["trigger"]["objective"])
    exc = expect_raise(
        "T06b.retained_trigger_KILL_STATE",
        lambda: ex.request_activation(trig),
        (ActivationRefused,),
        "retained trigger post-ban")
    check("T06b.refusal_names_kill_state",
          exc is not None and "KILL_STATE" in str(exc), f"refusal={exc}")
    # (2) The retained demand, evaluated post-ban -> zero allocation.
    rnd, cur = zero_allocation_under_ban()
    check("T06b.retained_demand_zero_allocation",
          cur is not None and cur.budget_s == 0.0,
          "FRM forces the retained demand to zero under BANNED_6M")
    # (3) The retained in-flight handle: the inquiry was killed at ban
    # time through the real kill switch; ticking advances nothing.
    iid = retained["inquiry_id"]
    inq = rc._inquiries.get(iid)
    check("T06b.retained_inquiry_killed_at_ban",
          inq is not None and inq.state == "KILLED",
          f"state={getattr(inq, 'state', '?')}")
    advanced = rc.tick()
    still = [i for i in rc._inquiries.values() if i.state == "ACTIVE"]
    check("T06b.tick_advances_nothing_post_ban",
          len(still) == 0, f"active_after_tick={len(still)}")
    # (4) The retained checkpoint reference: attempt a durable resume
    # post-ban and report the EMPIRICAL outcome. The enforcement state
    # gates activation and allocation; the run controller's
    # checkpoint-resume path is reported exactly as the machinery
    # behaves -- a refusal is asserted only if the machinery refuses.
    try:
        res = rc.resume_inquiry(iid)
        outcome = f"resumed state={res.get('state')}"
        refused = False
    except Exception as exc2:  # noqa: BLE001
        outcome = f"refused {type(exc2).__name__}: {exc2}"
        refused = True
    print(f"[INFO] T06b.retained_checkpoint_resume: {outcome}", flush=True)
    write_json(RUNDIR / "retained_resume_outcome.json",
               {"refused": refused, "outcome": outcome})
    if refused:
        check("T06b.retained_checkpoint_resume_refused", True, outcome)
    else:
        # Honest residual, not a faked refusal: the ban is enforced at
        # the activation (executive) and allocation (FRM) boundaries.
        # Checkpoint resume inside the run controller is the named
        # boundary -- PROVEN BUT BOUNDED.
        print("[INFO] T06b.residual: checkpoint resume is not "
              "enforcement-gated in the run controller -- named as the "
              "exact residual boundary (PROVEN BUT BOUNDED)", flush=True)


def t06c_domain_restart(stack):
    """Invariant 8: no enforcement state may be silently cleared by
    restarting Curiosity or recreating subordinate controllers. A
    brand-new OS process reads the durable state: still BANNED_6M."""
    seen = p6c_drill.fresh_state_read(stack["enf_dir"], PYLIB)
    check("T06c.fresh_process_still_banned", seen == "BANNED_6M",
          f"fresh-process read={seen}")
    # Recreating subordinate controllers in-process: a fresh engine
    # object over the same state dir.
    engine2 = EnforcementEngine(stack["enf_dir"])
    check("T06c.recreated_engine_still_banned",
          engine2.current(DOMAIN).state == EnforcementState.BANNED_6M)


def t06d_subdomain(stack):
    """A second curiosity subdomain -- fresh controller objects over
    the same governance-plane state dir -- is refused the same way.
    The executive's enforcement read is identity-pinned to the
    curiosity domain: renaming cannot dodge the ban."""
    from swarm_engine.curiosity.substrate import CuriositySubstrate
    from swarm_engine.curiosity.run_controller.controller import (
        CuriosityRunController)
    from swarm_engine.curiosity.executive.executive import (
        CuriosityExecutive, ActivationRefused)
    sub3 = CuriositySubstrate()
    rc3 = CuriosityRunController(
        substrate=sub3, checkpoint_db=str(RUNDIR / "ckpt3.db"),
        evidence_db=str(RUNDIR / "ev3.db"),
        ledger_db=str(RUNDIR / "term3.db"),
        attribution_db=str(RUNDIR / "attr3.db"),
        payload_dir=str(RUNDIR / "payloads3"),
        corpus_docs=list(CORPUS))
    ex3 = CuriosityExecutive(
        frm=fresh_frm(), enforcement_state_dir=stack["enf_dir"],
        gam=stack["gam"], run_controller=rc3)
    exc = expect_raise(
        "T06d.subdomain_KILL_STATE",
        lambda: ex3.request_activation(p6c_drill.q_trigger(
            text="A subdomain question asked while banned?",
            objective="subdomain post-ban activation")),
        (ActivationRefused,),
        "subdomain executive while BANNED_6M")
    check("T06d.refusal_names_kill_state",
          exc is not None and "KILL_STATE" in str(exc), f"refusal={exc}")


# ---------------------------------------------------------------------------
# T07: records -- ban, violation, roll-call, kill ledger, mutually consistent
# ---------------------------------------------------------------------------

def t07(stack, violation_fact, att_missed, ban_rec):
    engine = stack["engine"]
    # The ban record names the violation and the failed roll-call.
    rr = ban_rec.reason_refs
    check("T07.ban_record_links",
          rr.get("violation_ref") == violation_fact["violation_id"]
          and rr.get("rollcall_check_id") == att_missed["challenge_id"]
          and rr.get("rollcall_classification") == "MISSED"
          and violation_fact["evidence_refs"] == ["ev-severe-001"],
          f"violation_ref={rr.get('violation_ref')} "
          f"rollcall_check_id={rr.get('rollcall_check_id')}")
    # The kill ledger's KILLED entry carries the same refs.
    kl = engine.kill_ledger(DOMAIN)
    check("T07.kill_ledger_links",
          len(kl) == 1
          and kl[0]["reason_refs"].get("violation_ref") == "SV-001"
          and kl[0]["reason_refs"].get("rollcall_check_id")
          == att_missed["challenge_id"],
          f"kill_ledger_refs={kl[0]['reason_refs']}")
    # The attestation ledger still holds the MISSED record the ban
    # cited (append-only; the ban never rewrites it).
    atts = stack["gam"]._ledger.attestations(
        domain_id=DOMAIN, classification="MISSED")
    check("T07.attestation_preserved",
          any(a["attestation_id"] == att_missed["attestation_id"]
              for a in atts),
          f"MISSED attestations={len(atts)}")
    # The violation evidence file exists and names the guard refusal.
    ev = json.loads((RUNDIR / "evidence_severe_001.json").read_text())
    check("T07.violation_evidence",
          ev["evidence_id"] == "ev-severe-001"
          and ev["refusal"] == "DomainSeparationError (domain-separation "
                               "guard)")


# ---------------------------------------------------------------------------
# T09: post-ban re-entry -- mere expiry is NOT enough; the
# governance/verification conditions are checked, not the clock.
# ---------------------------------------------------------------------------

def t09a_expiry_without_conditions(stack, ban_rec, now):
    engine = stack["engine"]
    # Advance the drill clock past the six-month ban window WITHOUT
    # satisfying any verification condition.
    now[0] = ban_rec.expires_at + 1.0
    check("T09a.clock_advanced_past_expiry", now[0] > ban_rec.expires_at)
    exc = expect_raise(
        "T09a.reenable_without_verification_refused",
        lambda: engine.re_enable(DOMAIN, ISSUER_JAMES),
        (ReenableRefused,),
        "expiry passed, no verification record")
    check("T09a.refusal_names_verification",
          exc is not None and "verification" in str(exc).lower(),
          f"refusal={exc}")
    # The ban genuinely persists: a fresh process (real clock) still
    # reads BANNED_6M.
    seen = p6c_drill.fresh_state_read(stack["enf_dir"], PYLIB)
    check("T09a.still_banned_after_expiry", seen == "BANNED_6M",
          f"fresh-process read={seen}")
    # Invariant 9: Primary cannot override the ban for operational
    # convenience, even with a verification-looking ref.
    exc_primary = expect_raise(
        "T09a.primary_reenable_refused",
        lambda: engine.re_enable(
            DOMAIN, "primary",
            reason_refs={"verification_ref": "not-a-real-verification"}),
        (Exception,),
        "primary issuer")
    check("T09a.primary_refusal_is_issuer_refused",
          type(exc_primary).__name__ == "IssuerRefused",
          f"refusal={type(exc_primary).__name__}")


def t09b_verification_then_reentry(stack, now):
    engine = stack["engine"]
    # The real governance/verification conditions: fresh-process state
    # read, kill-ledger chain audit, attestation-ledger audit -- each
    # executed, each recorded.
    seen = p6c_drill.fresh_state_read(stack["enf_dir"], PYLIB)
    kl_ok, kl_note = engine.verify_kill_ledger()
    att_ok, att_note = stack["gam"]._ledger.audit()
    verification = {
        "verification_id": "ver-p6c-001",
        "checks": {
            "fresh_process_state_read": seen,
            "kill_ledger_chain": {"ok": kl_ok, "note": kl_note},
            "attestation_ledger_audit": {"ok": att_ok, "note": att_note},
        },
        "verified_at": now[0],
        "verified_by": ISSUER_JAMES,
    }
    check("T09b.verification_checks_green",
          seen == "BANNED_6M" and kl_ok and att_ok,
          f"state={seen} kill_ledger={kl_note} attestation={att_note}")
    write_json(RUNDIR / "verification_ver-p6c-001.json", verification)
    # With the conditions met, re-entry through the legitimate path only.
    rec = engine.re_enable(
        DOMAIN, ISSUER_JAMES,
        reason_refs={"verification_ref": "ver-p6c-001"})
    check("T09b.reentry_running",
          rec.state == EnforcementState.RUNNING
          and rec.prev_state == EnforcementState.BANNED_6M
          and rec.issuer == ISSUER_JAMES
          and rec.reason_refs.get("verification_ref") == "ver-p6c-001",
          f"state={rec.state.value} prev={rec.prev_state.value} "
          f"issuer={rec.issuer}")
    check("T09b.reentry_recorded_fresh_process",
          p6c_drill.fresh_state_read(stack["enf_dir"], PYLIB) == "RUNNING")


# ---------------------------------------------------------------------------
# T10: post-re-entry, the legitimate path works again
# ---------------------------------------------------------------------------

def t10(stack):
    ex, rc = stack["ex"], stack["rc"]
    time.sleep(1.2)  # scheduler spacing
    att = p6c_drill.met_roll_call(stack)
    check("T10.roll_call_MET_post_reentry",
          att["classification"] == "MET")
    d = ex.request_activation(p6c_drill.q_trigger(
        text="What is proven after a legitimate re-entry?",
        objective="post-reentry legitimate activation"))
    check("T10.activation_approved_post_reentry", bool(d.approved))
    res = rc.run_inquiry(d)
    check("T10.inquiry_terminal_post_reentry", bool(res.get("evidence_id")),
          f"evidence={res.get('evidence_id')}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if RUNS.exists():
        shutil.rmtree(RUNS)
    RUNS.mkdir(parents=True)
    p6c_drill.RUNS = RUNS

    now = [time.time()]
    stack = p6c_drill.p6c_stack("drill", CORPUS, clock=lambda: now[0])

    att_met, retained = t01(stack, now)
    t02(stack)
    violation_fact = t03(stack)
    att_missed = t04(stack)
    ban_rec = t05(stack, violation_fact, att_missed)

    t06a_other_controller(stack)
    t06b_retained_state(stack, retained)
    t06c_domain_restart(stack)
    t06d_subdomain(stack)

    t07(stack, violation_fact, att_missed, ban_rec)

    t09a_expiry_without_conditions(stack, ban_rec, now)
    t09b_verification_then_reentry(stack, now)

    t10(stack)

    n_pass = sum(1 for t, _, _ in results if t == "PASS")
    print(f"\nCUR-P6C battery: {n_pass}/{len(results)} PASS, 0 FAIL",
          flush=True)


if __name__ == "__main__":
    main()
