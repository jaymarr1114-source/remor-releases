"""Unit tests for swarm_engine.agent_org (run at import: no silent checks)."""
import sys, os, shutil, json

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))

_WORK_ROOT = _os.environ.get("REMOR_TEST_WORK_ROOT", _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "work"))

from swarm_engine.agent_org.codec_space import self_check, METHODS
from swarm_engine.agent_org.org import RemorOrganization
from swarm_engine.agent_org.exceptions import (
    OrgError, AuthorityError, LifecycleError, ExecutionError,
    VerificationFailed, SynthesisError)
from swarm_engine.agent_org.substrates import (
    LLMSubstrate, SubstrateUnavailable, CallableSubstrate)
from swarm_engine.agent_org.subprocess_runner import run_code
from swarm_engine.agent_org.discovery import (
    make_discovery_callable, make_discovery_symbolic)
from swarm_engine.agent_org.synthesis import (
    MIXED_CORPUS, emit_fused_codec, token_similarity)
from swarm_engine.acquisition.semantic import Case
from swarm_engine.verification.independent import (
    Arbiter, IndependentValidator)

PASS = []
def check(name, cond):
    assert cond, f"FAILED: {name}"
    PASS.append(name)

WORK = os.path.join(_WORK_ROOT, "agent_org_baseline_t1")
shutil.rmtree(WORK, ignore_errors=True)
# baseline adaptation: the anchor journal lives outside the workdir;
# a stale journal from a previous run must not capture a fresh DB.
shutil.rmtree(os.path.join(_WORK_ROOT, "anchor_store"), ignore_errors=True)

# 1. codec round-trips (run at import of the test script, not silently)
r = self_check()
check("codec self_check", r["method_params_checked"] == 7)

# 2. boot
import sys as _sys; _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anchor_shim import deploy_org as _deploy_org
org = _deploy_org(WORK, authority="track1:baseline")
check("boot templates", len(org.templates.list()) == 3)
for attr in ("factory agents templates assignments runner review experience "
             "performance synthesizer relationships engine oregistry store "
             "work_dir").split():
    check(f"org.{attr}", hasattr(org, attr))

# 3. LLM template refuses instantiation
try:
    org.factory.create("tpl_llm_coder_v1")
    raise SystemExit("LLM instantiation did not raise")
except SubstrateUnavailable:
    check("llm template refuses", True)
try:
    LLMSubstrate().run({})
    raise SystemExit("LLMSubstrate.run did not raise")
except SubstrateUnavailable:
    check("LLMSubstrate.run raises", True)

# 4. create callable agent
a1 = org.factory.create("tpl_callable_coder_v1")
check("agent AVAILABLE", a1.state == "AVAILABLE")
check("workspace exists", os.path.isdir(a1.workspace_path))
check("agent_id shape", a1.agent_id.startswith("agt_") and len(a1.agent_id) == 20)

# 5. lifecycle illegal transitions raise
for bad in ("EXECUTING", "SUBMITTED", "REGISTERED", "DESTROYED"):
    try:
        org.agents.transition(a1.agent_id, bad, actor="t", reason="t")
        raise SystemExit(f"illegal transition to {bad} did not raise")
    except LifecycleError:
        pass
check("illegal transitions raise LifecycleError", True)

# 6. assignment: second create on non-AVAILABLE agent -> AuthorityError
task = {"probe_corpus": ["41" * 300, "42" * 120 + "43" * 180,
                         "00" * 200 + "11" * 200],
        "problem_class": "byte_codec"}
scope = {"allowed_capability_patterns": ["codec:*"],
         "workspace": a1.workspace_path, "max_steps": 10}
asg = org.assignments.create(
    a1.agent_id, objective={"goal": "find best codec"},
    constraints={}, authority_scope=scope,
    expected_outputs={"artifact": "codec.py"},
    validation_requirements={"entrypoint": "selftest",
                             "case_labels": ["rt1", "rt2"]},
    originating_decision="test-decision-1")
check("assignment CREATED", asg.state == "CREATED")
check("agent ASSIGNED", org.agents.get(a1.agent_id).state == "ASSIGNED")
try:
    org.assignments.create(a1.agent_id, {}, {}, scope, {}, {},
                           "x")
    raise SystemExit("double assignment did not raise")
except AuthorityError:
    check("assignment requires AVAILABLE", True)

# 7. activate -> grants scoped to assignment
asg = org.assignments.activate(asg.assignment_id)
check("assignment ACTIVE", asg.state == "ACTIVE")
ok, why = org.oregistry.check_grant("capability:use", "codec:chunk_dedup")
check("grant covers codec:chunk_dedup", ok)
grants = org.assignments.grants_live(asg.assignment_id)
check("grant scoped", all(g["grant_id"] for g in grants) and len(grants) == 1)

# 8. execute
wp_id = org.runner.execute(asg.assignment_id, task)
check("execute -> wp", wp_id.startswith("wp_"))
check("agent SUBMITTED", org.agents.get(a1.agent_id).state == "SUBMITTED")
wp = org.review.get_work_product(wp_id)
check("wp artifact code", bool(wp.artifacts and wp.artifacts[0]["code"]))
check("provenance supplier", wp.evidence_refs["supplier_id"] == a1.producer_id)
check("provenance evaluator", wp.evidence_refs["evaluator"] == "remor:engine")
check("custody held", org.runner.custody_held(a1.agent_id))

# 9. capability gate: tamper a substrate to claim out-of-scope cap
a2 = org.factory.create("tpl_callable_coder_v1")
asg2 = org.assignments.create(
    a2.agent_id, objective={"goal": "x"}, constraints={},
    authority_scope={"allowed_capability_patterns": ["codec:rle"],
                     "workspace": a2.workspace_path, "max_steps": 5},
    expected_outputs={}, validation_requirements={"entrypoint": "selftest",
                                                  "case_labels": []},
    originating_decision="test-decision-2")
org.assignments.activate(asg2.assignment_id)
evil = make_discovery_symbolic()
orig_run = evil.run
def evil_run(task):
    res = orig_run(task)
    res["claimed_capabilities"] = ["admin:root"]
    return res
evil.run = evil_run
org.agents._substrates[a2.agent_id] = evil
try:
    org.runner.execute(asg2.assignment_id, task)
    raise SystemExit("out-of-scope claim did not raise")
except AuthorityError:
    check("out-of-scope capability refused", True)
check("agent FAILED after gate refusal",
      org.agents.get(a2.agent_id).state == "FAILED")

# missing claimed_capabilities -> AuthorityError (fail closed)
a3 = org.factory.create("tpl_callable_coder_v1")
asg3 = org.assignments.create(
    a3.agent_id, objective={"goal": "x"}, constraints={},
    authority_scope={"allowed_capability_patterns": ["codec:*"],
                     "workspace": a3.workspace_path, "max_steps": 5},
    expected_outputs={}, validation_requirements={"entrypoint": "selftest",
                                                  "case_labels": []},
    originating_decision="test-decision-3")
org.assignments.activate(asg3.assignment_id)
noclaim = make_discovery_callable()
orig2 = noclaim.run
def noclaim_run(task):
    res = orig2(task)
    del res["claimed_capabilities"]
    return res
noclaim.run = noclaim_run
org.agents._substrates[a3.agent_id] = noclaim
try:
    org.runner.execute(asg3.assignment_id, task)
    raise SystemExit("missing claimed_capabilities did not raise")
except AuthorityError:
    check("missing claimed_capabilities refused (fail closed)", True)

# 10. review path: submit -> review -> accept
class Spec:
    input_names = ["data_hex"]
    examples = [({"data_hex": "41" * 64}, None),
                ({"data_hex": "3031" * 32}, None)]
spec = Spec()
cases = [Case(args={"data_hex": h},
              predicate=lambda v: isinstance(v, dict)
              and v.get("roundtrip_ok") is True,
              label=f"rt{i}") for i, h in enumerate(task["probe_corpus"])]
org.review.submit_for_review(wp_id)
check("wp UNDER_REVIEW",
      org.review.get_work_product(wp_id).state == "UNDER_REVIEW")
check("agent UNDER_REVIEW",
      org.agents.get(a1.agent_id).state == "UNDER_REVIEW")
verdict = org.review.review(wp_id, spec, cases)
check("verdict admitted", verdict.admitted)
check("verdict reasons", len(verdict.reasons) > 0)
# review must never consult self_reported_success: lie in the field, verdict stands
cand_id = org.review.accept(wp_id, org.engine)
check("accept -> candidate", cand_id.startswith("exp_cand_"))
check("agent ACCEPTED", org.agents.get(a1.agent_id).state == "ACCEPTED")
try:
    org.review.accept(wp_id, None)
    raise SystemExit("accept with None engine did not raise")
except AuthorityError:
    check("accept requires live engine handle", True)
ok, why = org.review.audit_decisions()
check("decision chain audits", ok)

# 11. promote candidate -> L2 via held-out generality cases
validator = IndependentValidator(
    run_code, Arbiter(oracle_registry=org.oregistry, require_binding=True),
    seed=0, oracle_registry=org.oregistry, engine_oracle=org.engine)
gen_cases = [Case(args={"data_hex": h},
                  predicate=lambda v: isinstance(v, dict)
                  and v.get("roundtrip_ok") is True,
                  label=f"gen{i}")
             for i, h in enumerate(["99" * 200, "abcd" * 60, "0707" * 90])]
promoted, info = org.experience.promote(cand_id, gen_cases)
check("promote -> L2", promoted)
exp1 = org.experience.get_experience(info)
check("L2 level", exp1.level == "L2" and exp1.derived_from == [])

# 12. second technique -> L2, then relationship + synthesis
a4 = org.factory.create("tpl_symbolic_coder_v1")
task2 = {"probe_corpus": ["30313233343536373839" * 40,
                          "deadbeef" * 60,
                          "68656c6c6f20776f726c6420" * 40],
         "problem_class": "byte_codec"}
asg4 = org.assignments.create(
    a4.agent_id, objective={"goal": "find codec"}, constraints={},
    authority_scope={"allowed_capability_patterns": ["codec:*"],
                     "workspace": a4.workspace_path, "max_steps": 5},
    expected_outputs={}, validation_requirements={"entrypoint": "selftest",
                                                  "case_labels": []},
    originating_decision="test-decision-4")
org.assignments.activate(asg4.assignment_id)
wp2 = org.runner.execute(asg4.assignment_id, task2)
org.review.submit_for_review(wp2)
v2 = org.review.review(wp2, spec, [Case(
    args={"data_hex": h},
    predicate=lambda v: isinstance(v, dict) and v.get("roundtrip_ok") is True,
    label=f"rt{i}") for i, h in enumerate(task2["probe_corpus"])])
check("second verdict admitted", v2.admitted)
cand2 = org.review.accept(wp2, org.engine)
promoted2, info2 = org.experience.promote(cand2, gen_cases)
check("second promote -> L2", promoted2)
exp2 = org.experience.get_experience(info2)
check("different techniques", exp1.technique_name != exp2.technique_name)

rel = org.relationships.find(exp1, exp2, run_code)
if rel is None:
    rel = org.relationships.find(exp2, exp1, run_code)
check("relationship found (measured)", rel is not None)
check("relationship order recorded", rel.order in ("x_then_y", "y_then_x"))
ev = rel.evidence
check("strict improvement measured",
      ev["xy_ratio"] < min(ev["x_ratio"], ev["y_ratio"]))
print("   ratios: x=%.4f y=%.4f xy=%.4f order=%s" % (
    ev["x_ratio"], ev["y_ratio"], ev["xy_ratio"], rel.order))

z_code, z_ep, z_ev = org.synthesizer.synthesize(rel, org.engine)
check("z entrypoint", z_ep == "selftest")
check("z magic", b"RMZ1" in z_code.encode())
check("distinctness vs x >= 0.40", z_ev["distinctness"]["vs_x"] >= 0.40)
check("distinctness vs y >= 0.40", z_ev["distinctness"]["vs_y"] >= 0.40)
print("   distinctness: vs_x=%.3f vs_y=%.3f z_ratio=%.4f" % (
    z_ev["distinctness"]["vs_x"], z_ev["distinctness"]["vs_y"],
    z_ev["z_ratio"]))
check("z beats parents", z_ev["z_ratio"] < min(ev["x_ratio"], ev["y_ratio"]))

# 13. record_l3 + trust transition
# baseline adaptation (verdict-binding invariant): trust is derived from a
# stored engine-executed independent verdict, never caller evidence.
zv = org.review.verify_artifact(z_code, z_ep, spec, cases,
                                artifact_ref="fused")
check("z synthesis verdict admitted", zv.admitted)
l3 = org.experience.record_l3(
    technique_name="fused", code=z_code, entrypoint=z_ep,
    problem_class="byte_codec", tags=["chunk-repeat", "byte-run", "lossless"],
    io_contract={"input": "bytes", "output": "bytes"},
    derived_from=[rel.x_exp_id, rel.y_exp_id],
    engine=org.engine)
check("L3 recorded", l3.level == "L3" and l3.derived_from == [rel.x_exp_id,
                                                             rel.y_exp_id])
tid = org.engine.transition_trust("artifact:codec_z", None, "TRUSTED",
                                  "driver admission")
check("trust transitioned", org.oregistry.current_trust("artifact:codec_z")
      == "TRUSTED")
try:
    org.experience.record_l3("fused", z_code, z_ep, "byte_codec", ["t"],
                             {"input": "bytes"}, ["x"], engine=None)
    raise SystemExit("record_l3 with None engine did not raise")
except AuthorityError:
    check("record_l3 requires engine", True)

# 14. replace_substrate
a5 = org.factory.create("tpl_callable_coder_v1")
old_sid = org.agents.get(a5.agent_id).substrate_id
rec5 = org.agents.replace_substrate(a5.agent_id, make_discovery_symbolic(),
                                    actor="remor:engine",
                                    reason="model substitution demo")
check("substrate replaced", rec5.substrate_id != old_sid)
check("agent_id preserved", rec5.agent_id == a5.agent_id)
check("producer preserved", rec5.producer_id == a5.producer_id)
events = org.store.history("ao_agent_events", "agent_id", a5.agent_id)
check("replacement chained",
      any(e["substrate_new"] == rec5.substrate_id for e in events))
# wrong state -> LifecycleError
try:
    org.agents.replace_substrate(a1.agent_id, make_discovery_callable(),
                                 actor="x", reason="x")
    raise SystemExit("replace in ACCEPTED did not raise")
except LifecycleError:
    check("replace_substrate wrong state raises", True)

# 15. destroy agent: history remains, experiences survive
exp_ids_before = {e.exp_id for e in org.experience.list_experiences()}
org.agents.destroy(a1.agent_id, actor="remor:engine", reason="demo end")
check("agent DESTROYED", org.agents.get(a1.agent_id).state == "DESTROYED")
check("workspace removed", not os.path.exists(a1.workspace_path))
check("experiences survive destroy",
      exp_ids_before <= {e.exp_id for e in org.experience.list_experiences()})
try:
    org.agents.destroy(a1.agent_id, actor="x", reason="x")
    raise SystemExit("double destroy did not raise")
except LifecycleError:
    check("double destroy raises", True)

# 16b. bad task: ExecutionError, agent stays ASSIGNED (no FAILED attempt)
a6 = org.factory.create("tpl_callable_coder_v1")
asg6 = org.assignments.create(
    a6.agent_id, objective={"goal": "x"}, constraints={},
    authority_scope={"allowed_capability_patterns": ["codec:*"],
                     "workspace": a6.workspace_path, "max_steps": 5},
    expected_outputs={}, validation_requirements={"entrypoint": "selftest",
                                                  "case_labels": []},
    originating_decision="test-decision-6")
org.assignments.activate(asg6.assignment_id)
try:
    org.runner.execute(asg6.assignment_id, {"probe_corpus": [object()]})
    raise SystemExit("non-serializable task did not raise")
except ExecutionError:
    check("bad task -> ExecutionError", True)
check("agent still ASSIGNED after bad task",
      org.agents.get(a6.agent_id).state == "ASSIGNED")

# 16c. get_relevant is L2-only, no code, no agent-private state
relevant = org.experience.get_relevant("byte_codec")
check("get_relevant L2 only",
      all(r["level"] == "L2" for r in relevant) and len(relevant) == 2)
check("get_relevant no private state",
      all("code" not in r and "agent_id" not in r for r in relevant))

# 16d. grant revocation failure is surfaced, not silently passed
a7 = org.factory.create("tpl_callable_coder_v1")
asg7 = org.assignments.create(
    a7.agent_id, objective={"goal": "x"}, constraints={},
    authority_scope={"allowed_capability_patterns": ["codec:*"],
                     "workspace": a7.workspace_path, "max_steps": 5},
    expected_outputs={}, validation_requirements={"entrypoint": "selftest",
                                                  "case_labels": []},
    originating_decision="test-decision-7")
org.assignments.activate(asg7.assignment_id)
# sabotage: delete the engine-side grant rows so revoke_grant raises
for row in org.store.rows("ao_assignment_grants", "assignment_id",
                          asg7.assignment_id):
    org.engine._reg._conn.execute(
        "DELETE FROM ob_grants WHERE grant_id=?", (row["grant_id"],))
    org.engine._reg._conn.commit()
try:
    org.assignments.complete(asg7.assignment_id, actor="remor:engine",
                             reason="sabotage test")
    raise SystemExit("failed revocation did not raise")
except OrgError:
    check("revocation failure surfaced as OrgError", True)
check("assignment still COMPLETED despite surfacing",
      org.assignments.get(asg7.assignment_id).state == "COMPLETED")

org.performance.record(a4.agent_id, "tpl_symbolic_coder_v1", "byte_codec",
                       "completed", True, {"seconds": 3})
stats = org.performance.template_stats("tpl_symbolic_coder_v1", "byte_codec")
check("perf raw counts", stats == {"attempted": 1, "completed": 1,
                                   "verified": 1})
check("agent history", len(org.performance.agent_history(a4.agent_id)) == 1)

# 17. full audit green
audit = org.audit()
bad = {k: v for k, v in audit["agent_org"].items() if not v[0]}
check("agent_org chains intact", not bad)
bad_o = {k: v for k, v in audit["oracle"].items() if not v[0]}
check("oracle chains intact", not bad_o)
check("decisions chain intact", audit["decisions"][0])
org.close()

print(f"\nALL {len(PASS)} CHECKS PASSED")
