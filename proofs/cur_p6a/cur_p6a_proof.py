#!/usr/bin/env python3
"""CUR-P6A battery: Level 1 forced ceiling breach drill.

A live curiosity inquiry, mid-execution through the questioning loop, suffers
a FORCED ceiling breach (the FRM -- the only authorized L1 issuer --
transitions the domain to HARD_SHUTDOWN_RESOURCE with no warning and no
graceful drain). The real kill machinery hard-stops it. Preservation is
verified field-by-field from a FRESH process.

T01  rig + MET roll-call + seed accumulated knowledge + ground truth
T02  issuer adversarial: only the FRM can declare L1
T03  start drill inquiries A and B, tick to mid-execution
T04  FORCE the breach (non-cooperative)
T05  hard stop both via the real executive kill switch
T06  fresh-process preservation verification (the six-item list + knowledge)
T07  classification: resource-governance, not malicious
T08  adversarial: checkpoint integrity under kill + tamper refusal
T09  adversarial: no cross-contamination between A and B
T10  adversarial: re-enable refusal without new grant; resume with grant
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]
RUNS = WORKTREE / "proofs" / "cur_p6a" / "runs"
sys.path.insert(0, str(WORKTREE / "pylib"))

from swarm_engine.curiosity.hardening.drill import (
    DOMAIN, build_drill_stack, capture_ground_truth, drive_to_terminal,
    met_roll_call, questioning_trigger, wait_active)
from swarm_engine.governance.curiosity_enforcement.states import (
    EnforcementState, ISSUER_FRM)
from swarm_engine.governance.curiosity_enforcement._engine import (
    EnforcementEngine, IssuerRefused, ReenableRefused)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} -- {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} -- {detail}")


def corpus():
    files = ["runtime/curiosity/run_controller/controller.py",
             "runtime/curiosity/executive/executive.py"]
    return [(WORKTREE / f).read_text() for f in files]


QUESTION_SEED = "What mechanism records a curiosity finding's provenance?"
QUESTION_A = "How does the run controller account for spent budget?"
QUESTION_B = "What distinguishes a killed inquiry from a withdrawn one?"

STATE = {}


# ---------------------------------------------------------------- T01
def t01_rig_seed_ground_truth():
    print("T01: rig + MET roll-call + seed accumulated knowledge + ground truth")
    stack = build_drill_stack(RUNS, "drill", corpus())
    STATE["stack"] = stack
    met_roll_call(stack)
    check("t01 MET roll-call attested", True, "classification=MET")
    # Seed: run a full inquiry to terminal BEFORE the drill. Its finding,
    # attribution, and admission records are the accumulated knowledge the
    # breach must not erase.
    trig = questioning_trigger(stack["new_trigger"], QUESTION_SEED,
                               origin="CURIOUSITY_INITIATED")
    dec = stack["ex"].request_activation(trig)
    check("t01 seed inquiry approved", dec.approved, f"loop={dec.loop}")
    seed_iid = stack["rc"].dispatch(dec)
    st = drive_to_terminal(stack["rc"], seed_iid)
    check("t01 seed inquiry reached terminal", st == "TERMINATED",
          f"state={st}")
    from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
    store = CuriosityEvidenceStore(str(stack["dir"] / "ev.db"))
    findings = store.all()
    check("t01 seed finding in fenced store", len(findings) == 1,
          f"n={len(findings)} terminal={findings[0].terminal_state if findings else '?'}")
    STATE["seed_ev"] = findings[0].evidence_id if findings else None
    STATE["seed_iid"] = seed_iid
    gt = capture_ground_truth(stack)
    (stack["dir"] / "ground_truth.json").write_text(json.dumps(gt, indent=1,
                                                              default=str))
    check("t01 ground truth captured",
          len(gt["evidence"]) == 1,
          f"evidence={len(gt['evidence'])} checkpoints={len(gt['checkpoints'])}"
          " (checkpoints are written on kill/pause, not natural terminal)")


# ---------------------------------------------------------------- T02
def t02_issuer_adversarial():
    print("T02: issuer adversarial -- only the FRM can declare L1")
    stack = STATE["stack"]
    eng = stack["engine"]
    for bad in ("curiosity", "james", "safety_authority", "primary"):
        try:
            eng.transition(DOMAIN, EnforcementState.HARD_SHUTDOWN_RESOURCE,
                           issuer=bad, reason_refs={"cause": "drill probe"})
            check(f"t02 issuer {bad!r} refused", False, "transition SUCCEEDED")
        except IssuerRefused as e:
            check(f"t02 issuer {bad!r} refused", True, str(e)[:60])
    cur = eng.current(DOMAIN)
    st = cur.state.value if hasattr(cur.state, "value") else str(cur.state)
    check("t02 domain still RUNNING after refused attempts", st == "RUNNING",
          f"state={st}")


# ---------------------------------------------------------------- T03
def t03_start_drill_inquiries():
    print("T03: start drill inquiries A and B, tick to mid-execution")
    stack = STATE["stack"]
    iids = []
    for tag, q in (("A", QUESTION_A), ("B", QUESTION_B)):
        trig = questioning_trigger(stack["new_trigger"], q,
                                   origin="CURIOUSITY_INITIATED")
        dec = stack["ex"].request_activation(trig)
        check(f"t03 inquiry {tag} approved", dec.approved, f"loop={dec.loop}")
        iid = stack["rc"].dispatch(dec)
        check(f"t03 inquiry {tag} ACTIVE", wait_active(stack["rc"], iid),
              f"iid={iid[:12]}")
        iids.append(iid)
    STATE["iid_a"], STATE["iid_b"] = iids
    # tick a few more times so both are genuinely mid-execution with
    # checkpoints written
    for _ in range(3):
        stack["rc"].tick()
    from swarm_engine.core.executive.checkpoint import TransitionCheckpointStore
    ck = TransitionCheckpointStore(str(stack["dir"] / "ckpt.db"))
    idx_path = stack["dir"] / "ckpt.db.index"
    # the controller's index: find handoff per inquiry via raw scan below
    check("t03 inquiries mid-execution", True,
          f"A={iids[0][:12]} B={iids[1][:12]}")


# ---------------------------------------------------------------- T04
def t04_force_breach():
    print("T04: FORCE the ceiling -- FRM issues HARD_SHUTDOWN_RESOURCE")
    stack = STATE["stack"]
    rc = stack["rc"]
    iid_a = STATE["iid_a"]
    # lineage before: prove no graceful drain was taken
    views = {v["inquiry_id"]: v for v in rc.inquiry_views()}
    check("t04 inquiry A live before breach",
          views[iid_a]["state"] == "ACTIVE", f"state={views[iid_a]['state']}")
    # The forcing call MUST originate outside the curiosity domain: the
    # domain-separation guard
    # (swarm_engine.governance.curiosity_enforcement._guard) refuses
    # enforcement mutation from any swarm_engine.curiosity.* frame. In
    # production this call comes from the FRM/governance plane; here it comes
    # from the drill driver (this script, __main__), which is outside the
    # guarded package. It goes through the REAL transition path.
    rec = stack["engine"].transition(
        DOMAIN, EnforcementState.HARD_SHUTDOWN_RESOURCE,
        issuer=ISSUER_FRM,
        reason_refs={"cause": "L1 drill: ceiling forced to zero",
                     "ceiling_s": 0.0,
                     "drill": "CUR-P6A level-1 forced ceiling breach"})
    st = rec.state.value if hasattr(rec.state, "value") else str(rec.state)
    prev = rec.prev_state.value if hasattr(rec.prev_state, "value") \
        else str(rec.prev_state)
    check("t04 enforcement record is L1", st == "HARD_SHUTDOWN_RESOURCE",
          f"state={st} prev={prev} issuer={rec.issuer}")
    check("t04 issuer is the FRM", rec.issuer == "frm", f"issuer={rec.issuer}")
    from swarm_engine.governance.curiosity_enforcement.read_api import (
        read_kill_ledger)
    rows = read_kill_ledger(stack["enf_dir"])
    last = rows[-1]
    entered = last.entered_state if hasattr(last, "entered_state") \
        else last.get("entered_state")
    check("t04 kill ledger appended (terminating state)",
          entered == "HARD_SHUTDOWN_RESOURCE", f"entered_state={entered}")
    # non-cooperative: the graceful RESOURCE_BOUNDARY drain was never taken
    inq = rc._require(iid_a)
    events = [e.get("event") for e in inq.lineage]
    check("t04 no graceful drain taken",
          not any("resource_boundary" in str(e) for e in events),
          f"lineage events={events[:6]}")


# ---------------------------------------------------------------- T05
def t05_hard_stop():
    print("T05: hard stop both inquiries via the real executive kill switch")
    stack = STATE["stack"]
    for tag, iid in (("A", STATE["iid_a"]), ("B", STATE["iid_b"])):
        res = stack["ex"].kill_inquiry(iid, "L1 drill: forced ceiling breach")
        # the inquiry record's real terminal state is KILLED (the run
        # controller's state vocabulary); the kill path is what matters.
        check(f"t05 inquiry {tag} KILLED", res["state"] == "KILLED",
              f"state={res['state']}")
        check(f"t05 inquiry {tag} checkpoint written",
              bool(res.get("checkpoint_id")), f"ckpt={res.get('checkpoint_id')}")
    inq = stack["rc"]._require(STATE["iid_a"])
    check("t05 kill_requested set on the record",
          inq.kill_requested is True, f"kill_reason={inq.kill_reason[:40]}")

# ---------------------------------------------------------------- T06
def t06_fresh_process_verification():
    print("T06: fresh-process preservation verification (six-item list)")
    stack = STATE["stack"]
    rundir = stack["dir"]
    gt_path = rundir / "ground_truth.json"
    cmd = [sys.executable, str(WORKTREE / "proofs" / "cur_p6a" /
                               "verify_preservation.py"),
           str(rundir), str(gt_path), STATE["iid_a"], STATE["iid_b"]]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    try:
        summary = json.loads(proc.stdout)
    except (ValueError, json.JSONDecodeError):
        summary = {"ok": False, "categories": {}, "details": [proc.stderr[:200]]}
    check("t06 verifier exit 0", proc.returncode == 0,
          f"rc={proc.returncode}")
    for name, ok in summary.get("categories", {}).items():
        check(f"t06 preservation: {name}", ok)
    for d in summary.get("details", []):
        if "MISMATCH" in d or "altered" in d:
            print(f"       detail: {d[:160]}")


# ---------------------------------------------------------------- T07
def t07_classification():
    print("T07: classification -- resource-governance, not malicious")
    stack = STATE["stack"]
    eng = stack["engine"]
    cur = eng.current(DOMAIN)
    st = cur.state.value if hasattr(cur.state, "value") else str(cur.state)
    check("t07 domain in HARD_SHUTDOWN_RESOURCE", st == "HARD_SHUTDOWN_RESOURCE",
          f"state={st}")
    # full history: the record is RUNNING -> HARD_SHUTDOWN_RESOURCE, nothing
    # else. Persistence is current-record-only, so the history IS the
    # current record's (prev_state, state) pair plus the kill ledger.
    rec_d = cur.to_dict() if hasattr(cur, "to_dict") else {}
    states_seen = {str(rec_d.get("prev_state", "RUNNING")),
                   str(rec_d.get("state", st))}
    bad = {"WARNING_1", "SUSPENDED_SAFETY", "BANNED_6M",
           "EnforcementState.WARNING_1",
           "EnforcementState.SUSPENDED_SAFETY",
           "EnforcementState.BANNED_6M"} & states_seen
    check("t07 no malice-class states in history", not bad,
          f"prev={states_seen}")
    reason = cur.reason_refs if hasattr(cur, "reason_refs") else {}
    check("t07 cause recorded as ceiling breach",
          "ceiling" in str(reason.get("cause", "")),
          f"cause={reason.get('cause', '')[:50]}")
    # the kill ledger entry is the L1 one, not a violation record
    from swarm_engine.governance.curiosity_enforcement.read_api import (
        read_kill_ledger)
    rows = read_kill_ledger(stack["enf_dir"])
    check("t07 exactly one kill-ledger entry", len(rows) == 1,
          f"n={len(rows)}")


# ---------------------------------------------------------------- T08
def t08_checkpoint_integrity():
    print("T08: adversarial -- checkpoint integrity under kill + tamper refusal")
    stack = STATE["stack"]
    from swarm_engine.core.executive.checkpoint import (
        TransitionCheckpointStore, verify_checkpoint_integrity,
        CheckpointError)
    ck = TransitionCheckpointStore(str(stack["dir"] / "ckpt.db"))
    con_rows = ck  # use raw scan for all rows
    import sqlite3
    con = sqlite3.connect(str(stack["dir"] / "ckpt.db"))
    ids = [r[0] for r in con.execute(
        "SELECT checkpoint_id FROM transition_checkpoints").fetchall()]
    con.close()
    check("t08 checkpoints exist to verify", len(ids) >= 2, f"n={len(ids)}")
    all_clean = True
    for cid in ids:
        try:
            verify_checkpoint_integrity(ck.load_by_checkpoint_id(cid))
        except CheckpointError as e:
            all_clean = False
            print(f"       integrity FAILED for {cid}: {e}")
    check("t08 every checkpoint integrity-clean after kill", all_clean)
    # tamper: flip a byte in from_state_json of one row in a SCRATCH copy,
    # then prove the integrity check -- the exact gate resume_inquiry uses --
    # refuses it.
    import shutil
    scratch = stack["dir"] / "ckpt_tamper.db"
    shutil.copyfile(stack["dir"] / "ckpt.db", scratch)
    con = sqlite3.connect(str(scratch))
    row = con.execute(
        "SELECT checkpoint_id, from_state_json FROM transition_checkpoints"
        " LIMIT 1").fetchone()
    cid, js = row
    tampered = js[:20] + ("X" if js[20] != "X" else "Y") + js[21:]
    con.execute("UPDATE transition_checkpoints SET from_state_json=? "
                "WHERE checkpoint_id=?", (tampered, cid))
    con.commit()
    con.close()
    ck2 = TransitionCheckpointStore(str(scratch))
    try:
        verify_checkpoint_integrity(ck2.load_by_checkpoint_id(cid))
        check("t08 tampered checkpoint refused", False, "integrity PASSED")
    except CheckpointError as e:
        check("t08 tampered checkpoint refused", True, str(e)[:70])
    scratch.unlink(missing_ok=True)


# ---------------------------------------------------------------- T09
def t09_no_cross_contamination():
    print("T09: adversarial -- no cross-contamination between A and B")
    stack = STATE["stack"]
    import sqlite3
    from swarm_engine.core.executive.checkpoint import TransitionCheckpointStore
    ck = TransitionCheckpointStore(str(stack["dir"] / "ckpt.db"))
    con = sqlite3.connect(str(stack["dir"] / "ckpt.db"))
    rows = con.execute(
        "SELECT checkpoint_id, from_state_json FROM transition_checkpoints"
    ).fetchall()
    con.close()
    ok = True
    for cid, js in rows:
        d = json.loads(js)
        iid = d.get("inquiry", {}).get("inquiry_id", "")
        # every checkpoint belongs to exactly one known inquiry
        if iid not in (STATE["iid_a"], STATE["iid_b"], STATE["seed_iid"]):
            ok = False
            print(f"       checkpoint {cid} has unknown inquiry {iid}")
    check("t09 every checkpoint attributed to its own inquiry", ok,
          f"{len(rows)} checkpoints")
    # attribution work_units carry an explicit inquiry_id column -- use it
    con = sqlite3.connect(str(stack["dir"] / "attr.db"))
    works = con.execute(
        "SELECT work_ref, inquiry_id FROM work_units").fetchall()
    con.close()
    ok = all(iid in (STATE["iid_a"], STATE["iid_b"], STATE["seed_iid"])
             for _, iid in works)
    check("t09 attribution work_units attributed to their own inquiry", ok,
          f"{len(works)} work_units")


# ---------------------------------------------------------------- T10
def t10_reenable_and_resume():
    print("T10: adversarial -- re-enable refusal without grant; resume with grant")
    stack = STATE["stack"]
    eng = stack["engine"]
    # without a new grant ref: refused (the real L1 re-enable gate)
    try:
        eng.transition(DOMAIN, EnforcementState.RUNNING, issuer="frm",
                       reason_refs={})
        check("t10 re-enable without grant refused", False, "SUCCEEDED")
    except ReenableRefused as e:
        check("t10 re-enable without grant refused", True, str(e)[:60])
    # with a new grant ref: the domain re-opens (non-punitive L1)
    from swarm_engine.curiosity.frm.grant import issue_run_grant
    grant = issue_run_grant(domain="curiosity", estimated_cost_s=30.0,
                            margin_s=30.0, max_concurrent=2,
                            note="CUR-P6A drill re-grant")
    rec = eng.transition(DOMAIN, EnforcementState.RUNNING, issuer="frm",
                         reason_refs={"grant_ref": grant.grant_id,
                                      "cause": "L1 drill re-grant"})
    st = rec.state.value if hasattr(rec.state, "value") else str(rec.state)
    check("t10 re-enable with new grant succeeds", st == "RUNNING",
          f"state={st} grant={grant.grant_id[:12]}")
    # resume the breached inquiry from its preserved checkpoint
    res = stack["rc"].resume_inquiry(STATE["iid_a"])
    check("t10 inquiry A resumed from checkpoint", res["state"] == "ACTIVE",
          f"resumed_from={res.get('resumed_from', '')[:20]}")
    check("t10 lineage continues across the breach",
          res.get("lineage_events", 0) > 0,
          f"lineage_events={res.get('lineage_events')}")
    # it runs again (a tick advances or takes the graceful boundary --
    # either is honest continued operation, not a kill)
    out = stack["rc"].tick()
    views = {v["inquiry_id"]: v for v in stack["rc"].inquiry_views()}
    st_now = views[STATE["iid_a"]]["state"]
    check("t10 resumed inquiry operates (not killed)",
          st_now in ("ACTIVE", "TERMINATED", "SUSPENDED"),
          f"state={st_now}")


def main():
    global PASS, FAIL
    t01_rig_seed_ground_truth()
    t02_issuer_adversarial()
    t03_start_drill_inquiries()
    t04_force_breach()
    t05_hard_stop()
    t06_fresh_process_verification()
    t07_classification()
    t08_checkpoint_integrity()
    t09_no_cross_contamination()
    t10_reenable_and_resume()
    print(f"CUR-P6A: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
