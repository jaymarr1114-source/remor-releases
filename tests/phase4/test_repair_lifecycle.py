"""Phase 4 battery: repair versioning / conflict detection / rollback / quarantine.

Item 5. Every check below executes the REAL mechanism: real
ReviewBoard.verify_repair (IndependentValidator in a subprocess,
oracle-bound), real ReviewBoard.admit_repair trust derivation, real
ProjectModificationGuard file writes, real caller authorization, real
hash-chained + externally-anchored journaling. Nothing is stubbed
except the duck-typed RepairService (worker 1's file, not created
here), which delegates to the real ReviewBoard.

Usage: python3 test_repair_lifecycle.py
Exits nonzero on any failure; prints real pass/fail counts.
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common_phase4 import (  # noqa: E402
    WORK, check, failfast, deploy_org, provision_caller, digest,
    verify_one_repair, RepairServiceStub, FuncSpec, instance_cases,
    D1_SRC, V1_POST, V2_POST, V3_POST, EXAMPLES,
)
from swarm_engine.agent_org.repair_lifecycle import (  # noqa: E402
    RepairLifecycle, RepairLifecycleError, defect_root_of,
    STATUS_ADMITTED, STATUS_SUPERSEDED, STATUS_REVOKED,
    STATUS_QUARANTINED,
)
from swarm_engine.governance.oracle_binding import (  # noqa: E402
    DECISION_REPAIR_SUBMIT, DECISION_REPAIR_APPLY)
from swarm_engine.governance.caller_authorization import (  # noqa: E402
    AgentDirectory, AuthorizationError)
from swarm_engine.agent_org.exceptions import LifecycleError  # noqa: E402
from swarm_engine.project.modification import (  # noqa: E402
    ProjectModificationGuard)

BASE = os.path.join(WORK, "repair_lifecycle")
shutil.rmtree(BASE, ignore_errors=True)
ORGDIR = os.path.join(BASE, "org")

print("== Phase 4 battery: repair lifecycle ==", flush=True)
org = deploy_org(ORGDIR)
lc = RepairLifecycle(org)
service = RepairServiceStub(org)

A_ID = "agent_phase4_lc_A"
A_TOKEN = provision_caller(org, A_ID,
                           DECISION_REPAIR_SUBMIT, DECISION_REPAIR_APPLY)
ACALLER = (A_ID, A_TOKEN)
check("A provisioned with repair_submit+repair_apply", bool(A_TOKEN))

# Unauthorized caller B (no decision classes at all).
B_ID = "agent_phase4_lc_B"
B_TOKEN = provision_caller(org, B_ID)
BCALLER = (B_ID, B_TOKEN)

# Workspace + governed guard (bound to the caller-authorization
# directory; apply_repair re-authorizes every call).
WS = os.path.join(BASE, "ws")
os.makedirs(WS, exist_ok=True)
guard = ProjectModificationGuard(WS).bind_authorization(
    AgentDirectory(org.oregistry))
with open(os.path.join(WS, "calc.py"), "w") as fh:
    fh.write(D1_SRC)

_rid = [0]


def new_rid(prefix):
    _rid[0] += 1
    return f"{prefix}_{digest(str(_rid[0]))[:8]}"


def verify_and_stage(rid, post_code, family="binary-operator"):
    """Apply bytes to disk through the governed guard, verify them
    independently, register the bytes with the stub service."""
    res = guard.apply_repair("calc.py", post_code, caller=ACALLER,
                             verify=None, repair_id=rid)
    assert res.committed, f"apply_repair did not commit for {rid}"
    verify_one_repair(org, rid, ACALLER, post_code, family=family)
    service.register_bytes(rid, post_code)
    return rid


# ---------------------------------------------------------------- unit
sig_a = {"family": "binary-operator", "pre_digest": digest(D1_SRC)}
sig_b = {"family": "binary-operator", "pre_digest": digest(D1_SRC)}
sig_c = {"family": "other-family", "pre_digest": digest(D1_SRC)}
check("defect_root deterministic",
      defect_root_of(sig_a) == defect_root_of(sig_b))
check("defect_root distinguishes family",
      defect_root_of(sig_a) != defect_root_of(sig_c))
check("defect_root stable without pre_digest",
      defect_root_of({"family": "f"}) == defect_root_of({"family": "f"}))

# ---------------------------------------------------------------- 1. first admission -> version 1
R1 = verify_and_stage(new_rid("rep_lc_v1"), V1_POST)
dec1 = lc.admit_versioned(R1, ACALLER, service)
check("first admission returns decision", dec1.startswith("dec_"), dec1)
row1 = lc.current_admitted(defect_root_of(sig_a))
check("current admitted is R1", row1 is not None
      and row1["repair_id"] == R1)
check("first admission version is 1", row1["version"] == "1")
check("first admission status admitted",
      row1["status"] == STATUS_ADMITTED)
check("lineage persists exact post bytes", row1["post_code"] == V1_POST)
check("lineage persists target path", row1["target_path"] == "calc.py")
check("supersedes empty on first admission",
      row1["supersedes_repair_id"] == "")
rec1 = org.review.get_repair_record(R1)
check("record carries admission decision",
      rec1["admission_decision_id"] == dec1 or
      bool(rec1["admission_decision_id"]))

# ---------------------------------------------------------------- 2. conflict: no supersedes -> refused, competitor named
R2 = verify_and_stage(new_rid("rep_lc_v2"), V2_POST)
try:
    lc.admit_versioned(R2, ACALLER, service)
    check("conflict refused without supersedes", False,
          "admitted a competing repair with no supersedes claim")
except RepairLifecycleError as exc:
    msg = str(exc)
    check("conflict refused without supersedes", "conflict" in msg.lower(),
          msg[:160])
    check("refusal names the competing admitted repair", R1 in msg,
          msg[:200])
check("R2 not admitted after refused conflict",
      lc.latest_status(R2) is None)

# ---------------------------------------------------------------- 3. supersede with the correct claim -> version 2
dec2 = lc.admit_versioned(R2, ACALLER, service, supersedes=R1)
check("supersede admission returns decision", dec2.startswith("dec_"))
check("R1 now superseded", lc.latest_status(R1) == STATUS_SUPERSEDED)
row2 = lc.current_admitted(defect_root_of(sig_a))
check("R2 is current", row2 is not None and row2["repair_id"] == R2)
check("supersede bumps version to 2", row2["version"] == "2")
check("supersede records the claim",
      row2["supersedes_repair_id"] == R1)
r1_hist = lc.repair_history(R1)
check("superseded row names its successor",
      r1_hist[-1].get("reason", "").find(R2) >= 0,
      r1_hist[-1].get("reason", ""))

# ---------------------------------------------------------------- 4. stale lineage claim refused
R3 = verify_and_stage(new_rid("rep_lc_v3"), V3_POST)
try:
    lc.admit_versioned(R3, ACALLER, service, supersedes=R1)
    check("stale supersedes claim refused", False,
          "admitted with a superseded (non-current) supersedes claim")
except RepairLifecycleError as exc:
    msg = str(exc).lower()
    check("stale supersedes claim refused",
          "stale" in msg or "currently governed" in msg,
          str(exc)[:160])

# ---------------------------------------------------------------- 5. double admission refused (service-level)
try:
    lc.admit_versioned(R2, ACALLER, service, supersedes=R2)
    check("double admission refused", False, "re-admitted the current head")
except (RepairLifecycleError, LifecycleError) as exc:
    check("double admission refused", True, str(exc)[:120])
try:
    lc.admit_versioned(R1, ACALLER, service, supersedes=R2)
    check("superseded repair re-admission refused", False,
          "re-admitted a superseded repair")
except (RepairLifecycleError, LifecycleError) as exc:
    check("superseded repair re-admission refused", True, str(exc)[:120])

# ---------------------------------------------------------------- 6. out-of-lifecycle admission is a conflict
FAM_OOL = "binary-operator-ool"
RX = new_rid("rep_lc_ool_x")
verify_and_stage(RX, V1_POST, family=FAM_OOL)
# Admit RX directly through the ReviewBoard, bypassing the lifecycle.
org.review.admit_repair(RX, caller=ACALLER)
check("RX admitted outside the lifecycle",
      bool(org.review.get_repair_record(RX)["admission_decision_id"]))
RY = new_rid("rep_lc_ool_y")
verify_and_stage(RY, V2_POST, family=FAM_OOL)
try:
    lc.admit_versioned(RY, ACALLER, service)
    check("out-of-lifecycle admission treated as conflict", False,
          "absorbed an admission that bypassed versioning")
except RepairLifecycleError as exc:
    check("out-of-lifecycle admission treated as conflict",
          RX in str(exc), str(exc)[:200])

# ---------------------------------------------------------------- 7. revocation
dec_rev = lc.revoke_repair(R2, ACALLER, reason="v2 misbehaves in prod")
check("revoke returns decision", dec_rev.startswith("dec_"))
check("R2 revoked", lc.latest_status(R2) == STATUS_REVOKED)
check("revoked repair stops governing",
      lc.current_admitted(defect_root_of(sig_a)) is None)
try:
    lc.admit_versioned(R2, ACALLER, service)
    check("revoked repair can never be re-admitted", False,
          "re-admitted a revoked repair")
except RepairLifecycleError as exc:
    check("revoked repair can never be re-admitted",
          "revok" in str(exc).lower(), str(exc)[:160])
try:
    lc.revoke_repair(R2, BCALLER, reason="attacker")
    check("unauthorized revoke refused", False, "B revoked a repair")
except AuthorizationError as exc:
    check("unauthorized revoke refused", True, str(exc)[:120])
try:
    lc.revoke_repair("rep_nonexistent", ACALLER)
    check("revoke of unknown repair refused", False, "revoked a phantom")
except RepairLifecycleError as exc:
    check("revoke of unknown repair refused", True, str(exc)[:120])
# Revocation journaled as a chained decision.
dec_rows = [r for r in org.store.rows("ao_manager_decisions")
            if r.get("decision_id") == dec_rev]
check("revocation writes a chained decision",
      len(dec_rows) == 1 and
      dec_rows[0]["kind"] == "repair_lifecycle:revocation")
check("revocation decision names the authorized revoker",
      dec_rows[0]["reasons_json"].find(A_ID) >= 0)

# ---------------------------------------------------------------- 8. rollback (fresh defect lineage)
FAM_RB = "binary-operator-rollback"
RA = verify_and_stage(new_rid("rep_lc_rb_a"), V1_POST, family=FAM_RB)
lc.admit_versioned(RA, ACALLER, service)
RB = verify_and_stage(new_rid("rep_lc_rb_b"), V2_POST, family=FAM_RB)
lc.admit_versioned(RB, ACALLER, service, supersedes=RA)
with open(os.path.join(WS, "calc.py")) as fh:
    check("disk holds the bad version before rollback",
          fh.read() == V2_POST)
dec_rb = lc.rollback(RB, ACALLER, guard)
check("rollback returns decision", dec_rb.startswith("dec_"))
with open(os.path.join(WS, "calc.py")) as fh:
    disk_after = fh.read()
check("rollback restores prior bytes on disk", disk_after == V1_POST)
check("bad repair revoked by rollback",
      lc.latest_status(RB) == STATUS_REVOKED)
cur_rb = lc.current_admitted(defect_root_of(
    {"family": FAM_RB, "pre_digest": digest(D1_SRC)}))
check("prior version restored as current",
      cur_rb is not None and cur_rb["repair_id"] == RA)
check("restored version is version+1", cur_rb["version"] == "3",
      cur_rb["version"])
check("restore reason cites the bad repair",
      cur_rb.get("reason", "").find(RB) >= 0, cur_rb.get("reason", ""))
# The prior verdict still binds the restored bytes.
vrow = org.review.require_admitted_verdict(digest(V1_POST), "repair")
check("prior verdict still binds after rollback",
      vrow.get("code_digest") == digest(V1_POST))
# Rollback with nothing to roll back to refuses.
try:
    lc.rollback(RA, ACALLER, guard)
    check("rollback with no prior version refused", False,
          "rolled back a first admission")
except RepairLifecycleError as exc:
    check("rollback with no prior version refused",
          "no prior" in str(exc).lower(), str(exc)[:160])
# Unauthorized rollback touches nothing.
with open(os.path.join(WS, "calc.py"), "w") as fh:
    fh.write(V2_POST)
before = open(os.path.join(WS, "calc.py")).read()
try:
    lc.rollback(RA, BCALLER, guard)
    check("unauthorized rollback refused", False, "B rolled back")
except AuthorizationError as exc:
    check("unauthorized rollback refused", True, str(exc)[:120])
check("unauthorized rollback left disk untouched",
      open(os.path.join(WS, "calc.py")).read() == before)

# ---- 8b. fail-closed ordering: verdict for the prior bytes no longer binds
# Forge a LATER rejected verdict for the v1 bytes (legit chained +
# anchored insert, so the refusal must come from latest-governs, not
# from chain/anchor breakage). Latest-governs: a later rejected row
# supersedes the earlier admitted one.
FAM_FC = "binary-operator-failclosed"
RC = verify_and_stage(new_rid("rep_lc_fc_c"), V1_POST, family=FAM_FC)
lc.admit_versioned(RC, ACALLER, service)
RD = verify_and_stage(new_rid("rep_lc_fc_d"), V2_POST, family=FAM_FC)
lc.admit_versioned(RD, ACALLER, service, supersedes=RC)
org.store.insert("ao_review_verdicts", {
    "wp_id": "", "admitted": "0",
    "reasons_json": "[]", "bindings": "{}",
    "code_digest": digest(V1_POST), "artifact_kind": "repair",
    "artifact_ref": "forge_reject", "verifier": "independent-validator:subprocess:bound-oracles",
    "execution_id": "vex_forge_reject", "spec_digest": digest("forge"),
    "created_at": "2026-09-25T00:00:00"})
n_dec_before = len(org.store.rows("ao_manager_decisions"))
try:
    lc.rollback(RD, ACALLER, guard)
    check("rollback refuses when prior verdict no longer binds", False,
          "rolled back to bytes with a rejected latest verdict")
except Exception as exc:
    check("rollback refuses when prior verdict no longer binds",
          "verdict binding refused" in str(exc), str(exc)[:160])
check("failed rollback left disk on the bad version",
      open(os.path.join(WS, "calc.py")).read() == V2_POST)
check("failed rollback wrote no revocation",
      lc.latest_status(RD) == STATUS_ADMITTED)
check("failed rollback wrote no decisions",
      len(org.store.rows("ao_manager_decisions")) == n_dec_before)

# ---------------------------------------------------------------- 9. quarantine
FAM_Q = "binary-operator-quarantine"
RQ = verify_and_stage(new_rid("rep_lc_q"), V1_POST, family=FAM_Q)
lc.admit_versioned(RQ, ACALLER, service)
dec_q = lc.quarantine_repair(RQ, ACALLER, reason="suspect regression")
check("quarantine returns decision", dec_q.startswith("dec_"))
check("RQ quarantined", lc.latest_status(RQ) == STATUS_QUARANTINED)
check("quarantined repair stops governing",
      lc.current_admitted(defect_root_of(
          {"family": FAM_Q, "pre_digest": digest(D1_SRC)})) is None)
RQ2 = verify_and_stage(new_rid("rep_lc_q2"), V2_POST, family=FAM_Q)
try:
    lc.admit_versioned(RQ2, ACALLER, service, supersedes=RQ)
    check("quarantined repair cannot be superseded-from", False,
          "superseded from a quarantined repair")
except RepairLifecycleError as exc:
    check("quarantined repair cannot be superseded-from",
          "quarantin" in str(exc).lower(), str(exc)[:160])
# A fresh admission with no supersedes claim cannot route around the hold.
try:
    lc.admit_versioned(RQ2, ACALLER, service)
    check("fresh admission during quarantine refused", False,
          "admitted a new version while the lineage was frozen")
except RepairLifecycleError as exc:
    check("fresh admission during quarantine refused",
          "quarantin" in str(exc).lower() or "frozen" in str(exc).lower(),
          str(exc)[:160])
try:
    lc.rollback(RQ, ACALLER, guard)
    check("quarantined repair cannot be rolled back to", False,
          "rolled back to a quarantined repair")
except RepairLifecycleError as exc:
    check("quarantined repair cannot be rolled back to", True,
          str(exc)[:160])
try:
    lc.quarantine_repair(RQ, BCALLER)
    check("unauthorized quarantine refused", False, "B quarantined")
except AuthorizationError as exc:
    check("unauthorized quarantine refused", True, str(exc)[:120])
dec_uq = lc.unquarantine_repair(RQ, ACALLER, reason="cleared by re-review")
check("unquarantine returns decision", dec_uq.startswith("dec_"))
uq_row = lc.current_admitted(defect_root_of(
    {"family": FAM_Q, "pre_digest": digest(D1_SRC)}))
check("unquarantined repair governs again",
      uq_row is not None and uq_row["repair_id"] == RQ)
check("unquarantine bumps the version", uq_row["version"] == "2",
      uq_row["version"])
# Now supersession from the restored head works.
dec_q2 = lc.admit_versioned(RQ2, ACALLER, service, supersedes=RQ)
check("supersede after unquarantine works", dec_q2.startswith("dec_"))
check("RQ2 current at version 3",
      lc.current_admitted(defect_root_of(
          {"family": FAM_Q, "pre_digest": digest(D1_SRC)}))["version"] == "3")

# ---------------------------------------------------------------- 10. byte-binding at admission
FAM_WB = "binary-operator-wrongbytes"
RW = new_rid("rep_lc_wb")
verify_and_stage(RW, V1_POST, family=FAM_WB)
service.register_bytes(RW, V2_POST)  # attacker-supplied wrong bytes
try:
    lc.admit_versioned(RW, ACALLER, service)
    check("wrong post bytes refused at admission", False,
          "admitted with bytes that do not match the verified digest")
except RepairLifecycleError as exc:
    check("wrong post bytes refused at admission",
          "post_digest" in str(exc), str(exc)[:160])
# Missing bytes also refused (service supplies none).
service2 = RepairServiceStub(org)
RW2 = new_rid("rep_lc_wb2")
verify_and_stage(RW2, V1_POST, family=FAM_WB)
try:
    lc.admit_versioned(RW2, ACALLER, service2)
    check("missing post bytes refused at admission", False,
          "admitted with no verifiable post bytes")
except RepairLifecycleError as exc:
    check("missing post bytes refused at admission",
          "unavailable" in str(exc).lower(), str(exc)[:160])

# ---------------------------------------------------------------- 11. journal integrity
ok, msg = org.store.audit("ao_repair_lineage")
check("ao_repair_lineage chain intact", ok, msg)
ok, msg = org.store.audit("ao_manager_decisions")
check("ao_manager_decisions chain intact", ok, msg)
ok, msg = org.store.audit("ao_repair_records")
check("ao_repair_records chain intact", ok, msg)
# Every lineage row is anchored: the journal tip matches live heads
# (the auto-anchor wrapper re-anchors after every legitimate insert).
from swarm_engine.governance.anchor import (  # noqa: E402
    collect_anchor_heads)
aok, amsg = org.anchor.verify(collect_anchor_heads(org.store,
                                                  org.oregistry))
check("external anchor covers lineage writes", aok, amsg[:160])
# Lineage kinds are all lifecycle decisions (no foreign kinds leak in).
kinds = {r["kind"] for r in org.store.rows("ao_manager_decisions")
         if r["kind"].startswith("repair_lifecycle:")}
check("lifecycle decision kinds well-formed",
      kinds <= {"repair_lifecycle:admit_versioned",
                "repair_lifecycle:revocation",
                "repair_lifecycle:rollback",
                "repair_lifecycle:quarantine",
                "repair_lifecycle:unquarantine"}, str(sorted(kinds)))

failfast("repair-lifecycle battery")
