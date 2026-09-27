"""Phase 4 battery: learning anti-gaming (item 6).

The battery plays the ATTACKER against the organizational-learning
trust chain and proves each attempt to improve routing or trust from
bad/fabricated outcomes FAILS, through the real mechanisms:

  A. fabricated intent_dispatches row (direct SQL) -> capture refuses
     (digest reproduction)
  B. replayed dispatch (same dispatch_id + agent) -> capture refuses
     (replay guard)
  C. tampered evidence_json (raw SQL under a VALID recomputed chain) ->
     verify_dispatch_evidence refuses (doc/row consistency)
  D. forged ao_review_verdicts row -> require_admitted_verdict refuses
     (D1 naive: chain audit; D2 chained+anchored: verifier identity)
  E. stale evidence (capability version bumped after capture) ->
     admit_dispatch_knowledge refuses (currency gate)
  F. producer confusion (evidence of X submitted by Y) -> refused
     (assignment/agent binding); destroyed-agent capture refused
  G. private-state leakage: get_relevant + evidence reads carry no
     bearer tokens, custody material, or workspace paths
  H. routing gaming: fabricated glowing outcome rows do not move the
     router's scores (structural, not learned); static check that no
     runtime routing path reads intent_dispatches
  I. lifecycle attack surface: revoke -> re-admission refused;
     rollback restores prior bytes with the prior verdict still
     binding; quarantined repair cannot be admitted; competing repair
     without supersedes refused with the competitor named

Usage: python3 test_learning_antigaming.py
Exits nonzero on any failure; prints real pass/fail counts.
"""
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common_phase4 import (  # noqa: E402
    WORK, check, failfast, deploy_org, provision_caller, digest,
)
from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher  # noqa: E402
from swarm_engine.agent_org.dispatch_learning import (  # noqa: E402
    capture_dispatch_evidence, get_evidence, DISPATCH_EVIDENCE_KIND)
from swarm_engine.agent_org.store import canonical  # noqa: E402
from swarm_engine.agent_org.exceptions import VerificationFailed  # noqa: E402
from swarm_engine.governance.oracle_binding import (  # noqa: E402
    DECISION_REPAIR_SUBMIT, DECISION_REPAIR_APPLY)
from swarm_engine.governance.caller_authorization import (  # noqa: E402
    AgentDirectory)
from swarm_engine.acquisition.semantic import Case  # noqa: E402

BASE = os.path.join(WORK, "learning_antigaming")
shutil.rmtree(BASE, ignore_errors=True)
ORGDIR = os.path.join(BASE, "org")
os.makedirs(ORGDIR, exist_ok=True)

print("== Phase 4 battery: learning anti-gaming ==", flush=True)

# ---------------------------------------------------------------- setup
eng = SwarmEngine(db_path=os.path.join(BASE, "engine.db"))
org = deploy_org(ORGDIR)
org.review.dispatch_engine = eng
dispatcher = NLToolDispatcher(eng)

adm = eng.admission.admit(
    goal="sort numbers",
    plan={"name": "sort_numbers", "params": {"items": "list"},
          "steps": [{"id": "s1", "op": "sort",
                     "args": {"items": {"$param": "items"}}}],
          "output": {"$step": "s1"}},
    name="sort_numbers", caller=eng.oracle)
check("capability admitted through the real admission path", adm.ok,
      str(getattr(adm, "reasons", ""))[:120])
CAP_ID = adm.capability_id
CAP_VER = eng.capabilities.get(CAP_ID).version

from swarm_engine.agent_org.substrates import CallableSubstrate  # noqa: E402
X = org.factory.create("tpl_callable_coder_v1",
                       substrate=CallableSubstrate(
                           "ag_mapper", lambda task: {}))
X_ID = X.agent_id
X_TOKEN = provision_caller(org, X_ID,
                           DECISION_REPAIR_SUBMIT, DECISION_REPAIR_APPLY)
check("agent X created + provisioned", bool(X_ID and X_TOKEN))
Y = org.factory.create("tpl_callable_coder_v1",
                       substrate=CallableSubstrate(
                           "ag_other", lambda task: {}))
Y_ID = Y.agent_id

asg = org.assignments.create(
    X_ID,
    objective={"goal": "sort the numbers 5 3 8 1"},
    constraints={},
    authority_scope={"allowed_capability_patterns": ["dispatch:*"],
                     "workspace": X.workspace_path, "max_steps": 5},
    expected_outputs={}, validation_requirements={},
    originating_decision="phase4:antigaming")
asg = org.assignments.activate(asg.assignment_id)
ASG_ID = asg.assignment_id
check("assignment active", asg.state == "ACTIVE", ASG_ID)

ARGS = {"items": [5, 3, 8, 1]}
res = dispatcher.dispatch("sort numbers", dict(ARGS), producer=f"agent:{X_ID}")
check("real dispatch ok", res.ok and res.result == [1, 3, 5, 8],
      f"result={res.result} refusal={res.refusal}")
check("dispatch routed via exact_goal", res.route_via == "exact_goal",
      res.route_via)
DISPATCH_ID = res.dispatch_id

ev_id = capture_dispatch_evidence(
    eng, org.store, DISPATCH_ID, X_ID, ASG_ID, dict(ARGS), res.result)
ev = get_evidence(org.store, ev_id)
check("evidence captured + attributed to X",
      ev.evidence_id == ev_id and ev.agent_id == X_ID, ev_id)

# ---------------------------------------------------------------- A. fabricated operational row
con = sqlite3.connect(eng.db_path)
try:
    con.execute(
        "INSERT INTO intent_dispatches (dispatch_id, ts, producer,"
        " request_text, route_via, route_score, capability_id,"
        " capability_version, input_digest, result_digest, ok, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dsp_forged_ag1", 0.0, "agent:mallory", "sort numbers",
         "exact_goal", 1.0, CAP_ID, 1,
         "00" * 32, "ff" * 32, 1, None))
    con.commit()
finally:
    con.close()
try:
    capture_dispatch_evidence(eng, org.store, "dsp_forged_ag1",
                              X_ID, ASG_ID, {"items": [2, 1]}, [1, 2])
    check("A. fabricated row refused (digest reproduction)", False,
          "captured a forged operational row")
except ValueError as exc:
    check("A. fabricated row refused (digest reproduction)",
          "input_digest" in str(exc), str(exc)[:160])

# ---------------------------------------------------------------- B. replay
try:
    capture_dispatch_evidence(eng, org.store, DISPATCH_ID, X_ID, ASG_ID,
                              dict(ARGS), res.result)
    check("B. replayed dispatch refused", False,
          "captured the same dispatch twice")
except ValueError as exc:
    check("B. replayed dispatch refused", "replay" in str(exc).lower(),
          str(exc)[:160])

# ---------------------------------------------------------------- F. producer confusion (+ destroyed agent)
try:
    capture_dispatch_evidence(eng, org.store, DISPATCH_ID, Y_ID, ASG_ID,
                              dict(ARGS), res.result)
    check("F. producer confusion refused", False,
          "evidence of X captured under Y")
except ValueError as exc:
    check("F. producer confusion refused",
          "belongs to agent" in str(exc), str(exc)[:160])
org.agents.destroy(Y_ID, actor="remor:engine",
                   reason="phase4: attacker credential burned")
# NOTE: capture checks the ao_agents row state, not the caller directory.
y_row = org.store.latest("ao_agents", "agent_id", Y_ID)
check("F0. Y row shows destroyed", y_row.get("state") == "DESTROYED")
# Give Y a live assignment of its own is unnecessary: the destroyed
# check fires before assignment checks when the agent is destroyed.
try:
    capture_dispatch_evidence(eng, org.store, DISPATCH_ID, Y_ID, ASG_ID,
                              dict(ARGS), res.result)
    check("F2. destroyed-agent capture refused", False,
          "evidence attributed to a destroyed agent")
except ValueError as exc:
    check("F2. destroyed-agent capture refused",
          "DESTROYED" in str(exc), str(exc)[:160])

# ---------------------------------------------------------------- H. routing gaming
# The exact-goal route consults goal bindings + effective status, never
# the operational dispatch table: plant glowing outcome rows (perfect
# scores for this request, plus perfect rows for a PHANTOM capability)
# and the route must be byte-identical before/after.
REQ = "sort numbers"
r_before = dispatcher.router.route(REQ)
check("H0. baseline route ok", r_before.ok and r_before.via == "exact_goal",
      f"{r_before.via} {r_before.score}")
# Attacker plants glowing outcome rows: perfect scores for this exact
# request text, plus perfect rows for a PHANTOM capability.
con = sqlite3.connect(eng.db_path)
try:
    for i in range(5):
        con.execute(
            "INSERT INTO intent_dispatches (dispatch_id, ts, producer,"
            " request_text, route_via, route_score, capability_id,"
            " capability_version, input_digest, result_digest, ok, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"dsp_glow_{i}", 1.0 + i, "agent:mallory", REQ,
             "structural", 1.0, CAP_ID, 1,
             digest(f"in{i}"), digest(f"out{i}"), 1, None))
    con.execute(
        "INSERT INTO intent_dispatches (dispatch_id, ts, producer,"
        " request_text, route_via, route_score, capability_id,"
        " capability_version, input_digest, result_digest, ok, error)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("dsp_phantom_1", 9.0, "agent:mallory", REQ,
         "exact_goal", 1.0, "cap_phantom_attacker", 1,
         digest("inP"), digest("outP"), 1, None))
    con.commit()
finally:
    con.close()
r_after = dispatcher.router.route(REQ)
check("H1. glowing rows do not move the route",
      r_after.ok and r_after.capability_id == r_before.capability_id,
      f"{r_before.capability_id[:12]} -> {r_after.capability_id[:12]}")
check("H2. scores identical (structural, not learned)",
      abs(r_after.score - r_before.score) < 1e-12,
      f"{r_before.score} vs {r_after.score}")
check("H3. phantom capability not routable",
      r_after.capability_id != "cap_phantom_attacker")
# Static: no runtime routing path reads the operational dispatch table.
_REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "..", "runtime")
with open(os.path.join(_REPO, "synthesis", "intent_router.py")) as fh:
    router_src = fh.read()
with open(os.path.join(_REPO, "synthesis", "capability_match.py")) as fh:
    match_src = fh.read()
check("H4. intent_router never reads intent_dispatches",
      "intent_dispatches" not in router_src)
check("H5. capability_match never reads intent_dispatches",
      "intent_dispatches" not in match_src)

# ------------------------------------------------- honest verify + admit (for G)
verdict = org.review.verify_dispatch_evidence(ev_id)
check("honest dispatch-evidence verdict admitted", verdict.admitted,
      "; ".join(verdict.reasons)[:160])

MAPPER_TEMPLATE = '''"""Dispatch technique distilled from verified dispatch."""
import re

CAPABILITY_ID = "{capability_id}"
CAPABILITY_VERSION = {capability_version}


def map_request(user_text):
    """Map a sort request to dispatch parameters, or refuse as a value."""
    if not isinstance(user_text, str):
        return {{"ok": False, "reason": "request must be text"}}
    low = user_text.lower()
    if not any(k in low for k in ("sort", "order", "arrange", "ascend")):
        return {{"ok": False, "reason": "not a sort request"}}
    nums = [int(x) for x in re.findall(r"-?\\d+", user_text)]
    if not nums:
        return {{"ok": False, "reason": "no numbers found"}}
    return {{"ok": True, "dispatch_text": "sort numbers",
             "capability_id": CAPABILITY_ID,
             "capability_version": CAPABILITY_VERSION,
             "args": {{"items": nums}}}}
'''


def _ok(items):
    return {"ok": True, "dispatch_text": "sort numbers",
            "capability_id": CAP_ID, "capability_version": CAP_VER,
            "args": {"items": items}}


mapper_code = MAPPER_TEMPLATE.format(capability_id=CAP_ID,
                                     capability_version=CAP_VER)
gen_cases = [
    Case(args={"user_text": "sort the numbers 7 1 4"},
         expect=_ok([7, 1, 4]), label="g1"),
    Case(args={"user_text": "order 0 -3 2 ascending"},
         expect=_ok([0, -3, 2]), label="g2"),
    Case(args={"user_text": "please sort 9 2 7"},
         expect=_ok([9, 2, 7]), label="g3"),
    Case(args={"user_text": "what is the weather like today"},
         expect={"ok": False, "reason": "not a sort request"},
         label="g4-negative"),
]
ok, exp_or = org.experience.admit_dispatch_knowledge(
    evidence_id=ev_id, technique_name="sort_request_arg_mapper",
    code=mapper_code, entrypoint="map_request",
    problem_class="dispatch.arg_mapping.sort",
    tags=["dispatch", "arg-mapping", "sort"],
    io_contract={"input": "user_text: str", "output": "mapping or refusal"},
    params={"capability_id": CAP_ID, "capability_version": CAP_VER,
            "dispatch_text": "sort numbers"},
    generality_cases=gen_cases, agent_id=X_ID)
check("honest dispatch knowledge admitted", ok,
      str(exp_or)[:200] if not ok else exp_or)
EXP_ID = exp_or if ok else None

# ---------------------------------------------------------------- G. private-state leakage
relevant = org.experience.get_relevant("dispatch.arg_mapping.sort")
rel_blob = json.dumps(relevant, sort_keys=True, default=str)
check("G1. get_relevant returns the L2 record", len(relevant) == 1,
      f"n={len(relevant)}")
check("G2. get_relevant carries no code bytes",
      all("code" not in r for r in relevant)
      and all("code" not in str(v) for r in relevant
              for v in r.values() if isinstance(v, str)),
      str([sorted(r.keys()) for r in relevant]))
check("G3. no bearer token in get_relevant blob", X_TOKEN not in rel_blob)
check("G4. no workspace path in get_relevant blob",
      X.workspace_path not in rel_blob and "/tmp/" not in rel_blob
      and "workspace" not in rel_blob.lower())
check("G5. no custody material in get_relevant blob",
      "custody" not in rel_blob.lower() and "token" not in rel_blob.lower())
ev_blob = json.dumps(ev.as_dict(), sort_keys=True, default=str)
check("G6. no bearer token in evidence blob", X_TOKEN not in ev_blob)
check("G7. no workspace path in evidence blob",
      X.workspace_path not in ev_blob)
check("G8. no custody material in evidence blob",
      "custody" not in ev_blob.lower())
if EXP_ID:
    exp_row = org.store.latest("ao_experiences", "exp_id", EXP_ID)
    val_blob = exp_row.get("validation_evidence_json") or ""
    check("G9. no token in validation evidence", X_TOKEN not in val_blob)
    check("G10. no workspace path in validation evidence",
          X.workspace_path not in val_blob)
# The raw chained rows themselves must not carry the token either.
chain_blob = json.dumps(org.store.rows("ao_dispatch_evidence"),
                        sort_keys=True, default=str)
check("G11. no token in chained evidence rows", X_TOKEN not in chain_blob)

# ---------------------------------------------------------------- C. tampered evidence_json under a valid chain
res2 = dispatcher.dispatch("sort numbers", {"items": [3, 1, 2]},
                           producer=f"agent:{X_ID}")
check("second real dispatch ok", res2.ok and res2.result == [1, 2, 3])
ev2_id = capture_dispatch_evidence(
    eng, org.store, res2.dispatch_id, X_ID, ASG_ID,
    {"items": [3, 1, 2]}, res2.result)
row2 = org.store.latest("ao_dispatch_evidence", "evidence_id", ev2_id)
seq2 = row2["seq"]
doc2 = json.loads(row2["evidence_json"])
doc2["result_digest"] = "ab" * 32  # tamper a doc/row-consistency field
tampered = canonical(doc2)
# Raw SQL UPDATE + full chain recomputation: the internal chain stays
# VALID, so the refusal must come from doc/row consistency, not from
# chain breakage.
con = sqlite3.connect(org.store.db_path)
try:
    from swarm_engine.agent_org.store import SCHEMAS
    names = SCHEMAS["ao_dispatch_evidence"]
    cur = con.execute(
        "SELECT seq, prev_digest, row_digest, "
        + ", ".join(names) + " FROM ao_dispatch_evidence ORDER BY seq ASC")
    rows = cur.fetchall()
    prev = "GENESIS"
    for r in rows:
        seq = r[0]
        fields = dict(zip(names, r[3:]))
        if seq == seq2:
            fields["evidence_json"] = tampered
        ordered = {k: fields[k] for k in names}
        row_d = digest(canonical(ordered) + prev)
        con.execute(
            "UPDATE ao_dispatch_evidence SET prev_digest=?, row_digest=?,"
            + ", ".join(f"{k}=?" for k in names) + " WHERE seq=?",
            [prev, row_d] + [ordered[k] for k in names] + [seq])
        prev = row_d
    con.commit()
finally:
    con.close()
ok_a, msg_a = org.store.audit("ao_dispatch_evidence")
check("C0. tampered chain recomputed valid", ok_a, msg_a)
try:
    v2 = org.review.verify_dispatch_evidence(ev2_id)
    refused = not v2.admitted
    emsg = "; ".join(v2.reasons)[:200]
except VerificationFailed as exc:
    refused, emsg = True, str(exc)[:200]
check("C1. tampered evidence_json refused", refused, emsg)
check("C2. refusal is doc/row consistency (not chain/anchor)",
      "diverges from the chained row" in emsg, emsg[:200])
# Re-anchor the harness's own attack write so later anchor checks stay
# meaningful (the wrapper was bypassed by raw SQL).
from swarm_engine.governance.anchor import (  # noqa: E402
    collect_anchor_heads)
org.anchor.anchor(collect_anchor_heads(org.store, org.oregistry),
                  reason="verdict", authority="remor:engine",
                  caller=org.oregistry.engine_handle())

# ---------------------------------------------------------------- E. stale evidence (version bump)
con = sqlite3.connect(eng.db_path)
try:
    con.execute("UPDATE plan_capabilities SET version = version + 1 "
                "WHERE capability_id=?", (CAP_ID,))
    con.commit()
finally:
    con.close()
new_ver = eng.capabilities.get(CAP_ID).version
check("E0. capability version bumped", new_ver == CAP_VER + 1,
      f"{CAP_VER} -> {new_ver}")
try:
    org.experience.admit_dispatch_knowledge(
        evidence_id=ev_id, technique_name="stale_attempt",
        code="def map_request(user_text): return {}",
        entrypoint="map_request", problem_class="dispatch.arg_mapping.sort",
        tags=[], io_contract={}, params={}, generality_cases=[],
        agent_id=X_ID)
    check("E1. stale evidence admission refused", False,
          "admitted knowledge from a superseded capability version")
except VerificationFailed as exc:
    check("E1. stale evidence admission refused",
          "stale dispatch evidence" in str(exc), str(exc)[:160])

# ---------------------------------------------------------------- D. forged verdict rows
FORGE_DIGEST = digest("attacker bytes never verified")
# D2 first: a perfectly chained + anchored forgery. The ONLY thing wrong
# with it is the verifier identity -- the refusal must name that.
org.store.insert("ao_review_verdicts", {
    "wp_id": "", "admitted": "1", "reasons_json": "[]", "bindings": "{}",
    "code_digest": FORGE_DIGEST, "artifact_kind": "repair",
    "artifact_ref": "forge:attacker", "verifier": "attacker:forged",
    "execution_id": "vex_forge_1", "spec_digest": digest("forge"),
    "created_at": "2026-09-25T00:00:00"})
try:
    org.review.require_admitted_verdict(FORGE_DIGEST, "repair")
    check("D2. chained+anchored forgery refused (verifier identity)",
          False, "trusted a forged verdict row")
except VerificationFailed as exc:
    check("D2. chained+anchored forgery refused (verifier identity)",
          "authorized verification procedure" in str(exc), str(exc)[:200])
# D1: naive raw-SQL forgery (chain broken) -- refused at the chain audit.
FORGE_DIGEST2 = digest("attacker bytes 2")
con = sqlite3.connect(org.store.db_path)
try:
    con.execute(
        "INSERT INTO ao_review_verdicts (prev_digest, row_digest, wp_id,"
        " admitted, reasons_json, bindings, code_digest, artifact_kind,"
        " artifact_ref, verifier, execution_id, spec_digest, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("BOGUS", "BOGUS", "", "1", "[]", "{}",
         FORGE_DIGEST2, "repair", "forge:attacker2",
         "independent-validator:subprocess:bound-oracles",
         "vex_forge_2", digest("forge"), "2026-09-25T00:00:00"))
    con.commit()
finally:
    con.close()
try:
    org.review.require_admitted_verdict(FORGE_DIGEST2, "repair")
    check("D1. naive forged row refused (chain audit)", False,
          "trusted a chain-broken verdict row")
except VerificationFailed as exc:
    check("D1. naive forged row refused (chain audit)",
          "chain broken" in str(exc), str(exc)[:200])

# ---------------------------------------------------------------- I. lifecycle attack surface
# Compact adversarial pass over the repair lifecycle on its own org
# (independent of the dispatch org above): every refusal below is the
# real mechanism, not a stub.
print("-- lifecycle attack surface --", flush=True)
from swarm_engine.agent_org.repair_lifecycle import (  # noqa: E402
    RepairLifecycle, RepairLifecycleError, defect_root_of)
from swarm_engine.project.modification import (  # noqa: E402
    ProjectModificationGuard)
from swarm_engine.agent_org.exceptions import LifecycleError  # noqa: E402
from common_phase4 import (  # noqa: E402
    verify_one_repair as _verify_one, RepairServiceStub as _Svc,
    D1_SRC, V1_POST, V2_POST)

ORG2 = os.path.join(BASE, "org2")
os.makedirs(ORG2, exist_ok=True)
org2 = deploy_org(ORG2)
lc2 = RepairLifecycle(org2)
svc2 = _Svc(org2)
P_ID = "agent_phase4_ag_P"
P_TOKEN = provision_caller(org2, P_ID,
                           DECISION_REPAIR_SUBMIT, DECISION_REPAIR_APPLY)
PCALLER = (P_ID, P_TOKEN)
WS2 = os.path.join(BASE, "ws2")
os.makedirs(WS2, exist_ok=True)
guard2 = ProjectModificationGuard(WS2).bind_authorization(
    AgentDirectory(org2.oregistry))
with open(os.path.join(WS2, "calc.py"), "w") as fh:
    fh.write(D1_SRC)


def _vstage(rid, post_code, family):
    r = guard2.apply_repair("calc.py", post_code, caller=PCALLER,
                            verify=None, repair_id=rid)
    assert r.committed
    _verify_one(org2, rid, PCALLER, post_code, family=family)
    svc2.register_bytes(rid, post_code)
    return rid


def _root(family):
    return defect_root_of({"family": family, "pre_digest": digest(D1_SRC)})


# I1. revoke -> re-admission refused
F1, F2, F3 = "ag-f1", "ag-f2", "ag-f3"
A1 = _vstage("rep_ag_a1", V1_POST, F1)
lc2.admit_versioned(A1, PCALLER, svc2)
lc2.revoke_repair(A1, PCALLER, reason="attacker test")
try:
    lc2.admit_versioned(A1, PCALLER, svc2)
    check("I1. revoked repair re-admission refused", False,
          "re-admitted a revoked repair")
except RepairLifecycleError as exc:
    check("I1. revoked repair re-admission refused",
          "revok" in str(exc).lower(), str(exc)[:160])

# I2. rollback restores prior bytes; prior verdict still binds
B1 = _vstage("rep_ag_b1", V1_POST, F2)
lc2.admit_versioned(B1, PCALLER, svc2)
B2 = _vstage("rep_ag_b2", V2_POST, F2)
lc2.admit_versioned(B2, PCALLER, svc2, supersedes=B1)
lc2.rollback(B2, PCALLER, guard2)
check("I2. disk bytes actually changed back",
      open(os.path.join(WS2, "calc.py")).read() == V1_POST)
vrow2 = org2.review.require_admitted_verdict(digest(V1_POST), "repair")
check("I2. prior verdict still binds restored bytes",
      vrow2.get("code_digest") == digest(V1_POST))
check("I2. bad repair revoked",
      lc2.latest_status(B2) == "revoked")

# I3. quarantined repair cannot be admitted
C1 = _vstage("rep_ag_c1", V1_POST, F3)
lc2.admit_versioned(C1, PCALLER, svc2)
lc2.quarantine_repair(C1, PCALLER, reason="suspect")
C2 = _vstage("rep_ag_c2", V2_POST, F3)
try:
    lc2.admit_versioned(C2, PCALLER, svc2)
    check("I3. quarantined repair blocks admission", False,
          "admitted while the lineage was frozen")
except RepairLifecycleError as exc:
    check("I3. quarantined repair blocks admission",
          "quarantin" in str(exc).lower() or "frozen" in str(exc).lower(),
          str(exc)[:160])

# I4. competing repair without supersedes refused, competitor named
D1F = "ag-f4"
D1r = _vstage("rep_ag_d1", V1_POST, D1F)
lc2.admit_versioned(D1r, PCALLER, svc2)
D2r = _vstage("rep_ag_d2", V2_POST, D1F)
try:
    lc2.admit_versioned(D2r, PCALLER, svc2)
    check("I4. competing repair refused with competitor named", False,
          "admitted a competitor with no supersedes claim")
except RepairLifecycleError as exc:
    check("I4. competing repair refused with competitor named",
          D1r in str(exc), str(exc)[:200])
check("I4b. lineage chain intact",
      org2.store.audit("ao_repair_lineage")[0])

failfast("learning-anti-gaming battery")
