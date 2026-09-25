"""Track 1, Phase 1: Agent A's repair loop.

A encounters a REAL defect (D1) -> diagnoses -> proposes (synthesis) ->
applies (guarded) -> INDEPENDENT engine verification (ReviewBoard,
subprocess, oracle-bound) -> admission (from the stored verdict only) ->
A authors the repair technique as a work product -> independent review ->
accept -> L1 -> promote (held-out) -> L2 organizational experience ->
A DESTROYED with proof that no private state crosses.

Writes phase-1 evidence to scratch/track1/work/phase1/ and a handoff
JSON for the fresh-process Phase 2.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    TRACK1, WORK, check, failfast, deploy_org, digest,
    engine_extract_examples, technique_case, instance_cases,
    TechniqueSpec, FuncSpec,
)
import defects  # noqa: E402

from swarm_engine.agent_org.substrates import CallableSubstrate  # noqa: E402
from swarm_engine.agent_org.exceptions import AuthorityError  # noqa: E402
from swarm_engine.acquisition.repair_synthesis import (  # noqa: E402
    synthesize_repair_for_paths)
from swarm_engine.project.modification import (  # noqa: E402
    ProjectModificationGuard)
from swarm_engine.agent_org.subprocess_runner import run_code  # noqa: E402

PHASE = os.path.join(WORK, "phase1")
os.makedirs(PHASE, exist_ok=True)
# The anchor journal lives at <dbdir>/../anchor_store, so the org workdir
# is nested one level: each phase gets a fully isolated anchor.
ORGDIR = os.path.join(PHASE, "org")

print("== Phase 1: Agent A repair loop ==", flush=True)
org = deploy_org(ORGDIR)

# ---------------------------------------------------------------- step 1
# Agent A is created with a repair-technique-authoring substrate.
T1_PATH = os.path.join(TRACK1, "technique_v1.py")


def author_t1(task):
    with open(T1_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    return {
        "implementation": src,
        "entrypoint": "repair_procedure",
        "technique": "example-driven binary-operator repair",
        "params": {"family": "binary-operator", "scope": "single-function"},
        "measurements": {"instance_repair_id": task.get("instance_evidence", {}).get("repair_id")},
        "notes": "generalized from instance repair",
        "claimed_capabilities": ["repair:technique_author"],
        "self_reported_success": {
            "authored": True,
            "family": task.get("defect_family"),
            # The agent's CLAIM. Independent review decides.
            "claim": "technique repairs binary-operator defects",
        },
    }


a = org.factory.create(
    "tpl_callable_coder_v1",
    substrate=CallableSubstrate("repair_author_v1", author_t1))
A_ID, A_PRODUCER, A_WS = a.agent_id, a.producer_id, a.workspace_path
check("A created with unique id", A_ID.startswith("agt_"))
check("A workspace exists", os.path.isdir(A_WS))

# ---------------------------------------------------------------- step 2
# A encounters the real defect D1 in its workspace.
src_path = os.path.join(A_WS, "calc.py")
test_path = os.path.join(A_WS, "test_calc.py")
os.makedirs(os.path.dirname(src_path), exist_ok=True)
with open(src_path, "w") as fh:
    fh.write(defects.D1_SRC)
with open(test_path, "w") as fh:
    fh.write(defects.D1_TEST)
with open(src_path, "rb") as fh:
    PRE_BYTES = fh.read()
PRE_DIGEST = digest(PRE_BYTES.decode())

# ---------------------------------------------------------------- step 3
# A DIAGNOSES: run the defective code against the test examples.
examples = engine_extract_examples(defects.D1_TEST)["add"]
rr = run_code(defects.D1_SRC, "add",
              [{"a": a_, "b": b_} for (a_, b_), _ in examples])
failing = [idx for idx, (((a_, b_), exp), res)
           in enumerate(zip(examples, rr.value))
           if not res.get("ok") or res.get("value") != exp]
DIAGNOSIS = (
    "function add(a,b) returns a-b; test asserts expect addition: "
    f"{len(failing)}/{len(examples)} examples fail "
    f"(e.g. add(2,3)={rr.value[0].get('value')} != 5)")
check("A diagnosis: defect reproduces", rr.ok and len(failing) == 3)
print(f"   diagnosis: {DIAGNOSIS}", flush=True)

# ---------------------------------------------------------------- step 4
# A PROPOSES a repair via example-driven synthesis (real synthesizer).
cand = synthesize_repair_for_paths(src_path, [test_path])
check("synthesis proposes a candidate", cand is not None)
check("candidate targets add", cand.func_name == "add")
print(f"   candidate: {cand.expression} provenance={cand.provenance['origin']}",
      flush=True)

# ---------------------------------------------------------------- step 5
# A APPLIES the repair through the guarded transaction (agent's own
# check runs first; independent verification comes later).
# Note: the synthesizer emits a single-line body; the Critic requires a
# real multi-line body, so the agent normalizes formatting (semantics
# identical -- verified bytes are the applied bytes).
import ast as _ast
normalized = _ast.unparse(_ast.parse(cand.content)) + "\n"
guard = ProjectModificationGuard(A_WS)


def agent_check():
    with open(src_path) as fh:
        post = fh.read()
    r2 = run_code(post, "add",
                  [{"a": a_, "b": b_} for (a_, b_), _ in examples])
    ok = r2.ok and all(
        res.get("ok") and res.get("value") == exp
        for (_, exp), res in zip(examples, r2.value))
    return ok, {"examples_passed": ok}


res = guard.modify("calc.py", normalized, verify=agent_check)
check("guard commits repair", res.committed and not res.rolled_back)
with open(src_path, "rb") as fh:
    POST_BYTES = fh.read()
POST_DIGEST = digest(POST_BYTES.decode())
check("bytes actually changed", PRE_DIGEST != POST_DIGEST)
check("repair is a+b", b"a + b" in POST_BYTES)

# ---------------------------------------------------------------- step 6
# ENGINE independently verifies the repaired artifact. The ENGINE
# extracts the examples itself, builds the spec/cases, and runs the
# real IndependentValidator in a subprocess. A's diagnosis, A's claim,
# and the guard's agent-side check determine NOTHING here.
spec = FuncSpec()
cases = instance_cases(examples)
REPAIR_ID = "rep_" + digest(PRE_DIGEST + POST_DIGEST)[:16]
verdict = org.review.verify_repair(
    repair_id=REPAIR_ID, agent_id=A_ID,
    defect_signature={"family": "binary-operator",
                      "file": "calc.py", "function": "add",
                      "observed": f"{len(failing)}/{len(examples)} examples fail",
                      "pre_digest": PRE_DIGEST},
    diagnosis=DIAGNOSIS,
    pre_code=PRE_BYTES.decode(), post_code=POST_BYTES.decode(),
    entrypoint="add", spec=spec, cases=cases)
check("independent verdict admitted", verdict.admitted)
rec = org.review.get_repair_record(REPAIR_ID)
vrow = org.review.require_admitted_verdict(POST_DIGEST, "repair")
check("verdict kind is repair", vrow.get("artifact_kind") == "repair")
check("verdict binds post bytes", vrow.get("code_digest") == POST_DIGEST)
check("verdict execution matches record",
      vrow.get("execution_id") == rec["verdict_execution_id"])
check("repair record persisted", rec["repair_id"] == REPAIR_ID)
check("record stores agent diagnosis (as claim)",
      rec["diagnosis"] == DIAGNOSIS[:2000])
check("record binds pre/post digests",
      rec["pre_digest"] == PRE_DIGEST and rec["post_digest"] == POST_DIGEST)

# ---------------------------------------------------------------- step 7
# ENGINE admits the repair -- derived from the STORED verdict only.
decision = org.review.admit_repair(REPAIR_ID, org.engine)
check("repair admitted", decision.startswith("dec_"))
rec2 = org.review.get_repair_record(REPAIR_ID)
check("admission recorded on repair record",
      rec2["admission_decision_id"] == decision)

# ---------------------------------------------------------------- step 8
# A authors the repair TECHNIQUE as a work product (governed path).
asg = org.assignments.create(
    A_ID, objective={"goal": "author reusable repair technique",
                     "defect_family": "binary-operator",
                     "instance_repair_id": REPAIR_ID},
    constraints={"single_function_modules": True},
    authority_scope={"allowed_capability_patterns": ["repair:*"],
                     "workspace": A_WS, "max_steps": 5},
    expected_outputs={"technique": "repair_procedure(source_text,test_text)"},
    validation_requirements={"entrypoint": "repair_procedure",
                             "case_labels": []},
    originating_decision=decision)
org.assignments.activate(asg.assignment_id)
wp_id = org.runner.execute(
    asg.assignment_id,
    {"defect_family": "binary-operator",
     "problem_class": "repair:binary-operator",
     "instance_evidence": {"repair_id": REPAIR_ID,
                           "expression": cand.expression}})
check("technique work product produced", wp_id.startswith("wp_"))
wp = org.review.get_work_product(wp_id)
t1_code = wp.artifacts[0]["code"]
check("technique entrypoint", wp.entrypoint == "repair_procedure")
check("technique provenance: supplier is A",
      wp.evidence_refs["supplier_id"] == A_PRODUCER)

# ---------------------------------------------------------------- step 9
# INDEPENDENT REVIEW of the technique on a HELD-OUT defect (R1).
org.review.submit_for_review(wp_id)
tverdict = org.review.review(
    wp_id, TechniqueSpec(),
    [technique_case(defects.R1_SRC, defects.R1_TEST,
                    defects.R1_FUNC, "r1")])
check("technique verdict admitted", tverdict.admitted)

# ---------------------------------------------------------------- step 10
# ACCEPT -> L1 candidate.
cand_id = org.review.accept(wp_id, org.engine)
check("technique accepted to L1", cand_id.startswith("exp_cand_"))

# ---------------------------------------------------------------- step 11
# PROMOTE on further HELD-OUT defects (H1, H2) -> L2 experience.
gen_cases = [
    technique_case(defects.H1_SRC, defects.H1_TEST, defects.H1_FUNC, "h1"),
    technique_case(defects.H2_SRC, defects.H2_TEST, defects.H2_FUNC, "h2"),
]
promoted, info = org.experience.promote(cand_id, gen_cases)
check("T1 promoted to L2", promoted)
exp1 = org.experience.get_experience(info)
check("L2 problem_class", exp1.problem_class == "repair:binary-operator")
check("L2 discovered_by is A", exp1.discovered_by == A_ID)
check("L2 verdict_execution_id set", bool(exp1.verdict_execution_id))
EXP1_ID = exp1.exp_id
print(f"   exp1={EXP1_ID}", flush=True)

# ---------------------------------------------------------------- step 12
# DESTROY A. Prove no private state can cross to B.
ws_before = A_WS
sub_before = a.substrate_id
org.agents.destroy(A_ID, actor="remor:engine",
                   reason="track1: phase 1 complete; A retired")
check("A state DESTROYED",
      org.agents.get(A_ID).state == "DESTROYED")
check("A workspace deleted", not os.path.exists(ws_before))
try:
    org.agents.get_substrate(A_ID)
    check("A substrate gone", False)
except KeyError:
    check("A substrate gone", True)
try:
    org.agents.custody_token(A_ID)
    check("A custody token gone", False)
except AuthorityError:
    check("A custody token gone", True)
except KeyError:
    check("A custody token gone", True)

failfast()

# ---------------------------------------------------------------- handoff
handoff = {
    "phase": "phase1",
    "workdir": ORGDIR,
    "agent_a": {"agent_id": A_ID, "producer_id": A_PRODUCER,
                "substrate_id": sub_before},
    "repair_a": {"repair_id": REPAIR_ID, "pre_digest": PRE_DIGEST,
                 "post_digest": POST_DIGEST, "decision": decision,
                 "verdict_execution_id": rec["verdict_execution_id"]},
    "exp1": {"exp_id": EXP1_ID, "technique": "binary-operator-repair",
             "code_digest": exp1.code_digest,
             "verdict_execution_id": exp1.verdict_execution_id},
}
with open(os.path.join(PHASE, "handoff.json"), "w") as fh:
    json.dump(handoff, fh, indent=2, sort_keys=True)
with open(os.path.join(PHASE, "checks.json"), "w") as fh:
    json.dump({"pass": __import__("common").CHECKS["pass"],
               "fail": __import__("common").CHECKS["fail"]}, fh)
print(f"\nPHASE 1 COMPLETE: {__import__('common').CHECKS['pass']} checks passed",
      flush=True)
