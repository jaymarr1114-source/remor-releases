"""Track 1, Phase 2: Agent B's adaptation loop (FRESH PROCESS).

This driver runs as a NEW python process: no in-memory state crosses
from Phase 1. The only channel is the persisted organizational store.

B (genuinely new agent: different id, workspace, producer, substrate)
encounters a MATERIALLY DIFFERENT defect (D2: two-function module, one
correct + one defective) -> retrieves T1 from organizational experience
-> T1 honestly FAILS on D2 (replay is not adaptation) -> B diagnoses the
shape difference -> ADAPTS the technique (T2: diagnosis-driven targeting
+ surgical replacement) -> applies -> INDEPENDENT engine verification
(fix + preservation) -> admission -> B authors T2 as a work product ->
independent review -> accept -> L1 -> promote (held-out) -> L2 as a
DISTINCT experience derived from exp1.
"""
import ast
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    TRACK1, WORK, check, failfast, attach_org, digest,
    engine_extract_examples, TechniqueSpec, CHECKS, provision_caller,
)
from swarm_engine.governance.oracle_binding import (  # noqa: E402
    DECISION_REPAIR_SUBMIT, DECISION_REPAIR_APPLY)
import defects  # noqa: E402

from swarm_engine.agent_org.substrates import CallableSubstrate  # noqa: E402
from swarm_engine.agent_org.exceptions import AuthorityError  # noqa: E402
from swarm_engine.acquisition.semantic import Case  # noqa: E402
from swarm_engine.project.modification import (  # noqa: E402
    ProjectModificationGuard)
from swarm_engine.agent_org.subprocess_runner import run_code  # noqa: E402

PHASE = os.path.join(WORK, "phase2")
os.makedirs(PHASE, exist_ok=True)
ORGDIR = os.path.join(PHASE, "org")

print("== Phase 2: Agent B adaptation loop (fresh process) ==", flush=True)

# ---------------------------------------------------------------- step 1
# Fresh-process attach. Boot verifies the anchor; no memory crosses.
handoff1 = json.load(open(os.path.join(WORK, "phase1", "handoff.json")))
OLD_ORGDIR = handoff1["workdir"]
A_ID = handoff1["agent_a"]["agent_id"]
A_PRODUCER = handoff1["agent_a"]["producer_id"]
EXP1_ID = handoff1["exp1"]["exp_id"]
REPAIR_A = handoff1["repair_a"]["repair_id"]

# Phase 2 uses the SAME org (organizational memory persists) but a fresh
# process. Copy the phase-1 org dir to phase-2 location for isolation of
# the anchor journal lineage? NO -- the anchor journal is append-only and
# shared; phase 2 continues the same org. We attach the SAME workdir.
org = attach_org(OLD_ORGDIR)
print(f"   attached org at {OLD_ORGDIR}", flush=True)

# ---------------------------------------------------------------- step 2
# Verify organizational persistence across the process boundary.
exp1 = org.experience.get_experience(EXP1_ID)
check("exp1 persists across fresh process", exp1.exp_id == EXP1_ID)
check("exp1 still L2", exp1.level == "L2")
check("A remains DESTROYED", org.agents.get(A_ID).state == "DESTROYED")
rec_a = org.review.get_repair_record(REPAIR_A)
check("A's repair record persists", rec_a["admission_decision_id"] != "")
vrow_a = org.review.require_admitted_verdict(
    rec_a["post_digest"], "repair")
check("A's verdict still admitted", vrow_a.get("admitted") == "1")

# ---------------------------------------------------------------- step 3
# B is created: genuinely new identity.
T2_PATH = os.path.join(TRACK1, "technique_v2.py")


def author_t2(task):
    with open(T2_PATH, "r", encoding="utf-8") as fh:
        src = fh.read()
    return {
        "implementation": src,
        "entrypoint": "repair_procedure",
        "technique": "diagnosis-driven targeted binary-operator repair",
        "params": {"family": "binary-operator", "scope": "multi-function",
                   "adapted_from": task.get("adapted_from")},
        "measurements": {"instance_repair_id":
                          task.get("instance_evidence", {}).get("repair_id")},
        "notes": "adapted from T1: adds failing-function diagnosis and "
                 "surgical replacement; T1 cannot handle multi-function modules",
        "claimed_capabilities": ["repair:technique_author"],
        "self_reported_success": {
            "authored": True,
            "claim": "technique repairs binary-operator defects in "
                     "multi-function modules",
        },
    }


b = org.factory.create(
    "tpl_callable_coder_v1",
    substrate=CallableSubstrate("repair_author_v2", author_t2))
B_ID, B_PRODUCER, B_WS = b.agent_id, b.producer_id, b.workspace_path
check("B id differs from A", B_ID != A_ID and B_ID.startswith("agt_"))
check("B producer differs from A", B_PRODUCER != A_PRODUCER)
# Caller authorization: provision B Bearer <redacted> for repair
# submission (agent:repair_submit); the engine operator registers it.
B_TOKEN = provision_caller(org, B_ID, DECISION_REPAIR_SUBMIT,
                           DECISION_REPAIR_APPLY)
check("B caller credential provisioned", bool(B_TOKEN))
check("B workspace fresh", os.path.isdir(B_WS) and B_WS != handoff1["agent_a"].get("workspace", ""))
check("B substrate differs", b.substrate_id != handoff1["agent_a"]["substrate_id"])

# ---------------------------------------------------------------- step 4
# B encounters the materially different defect D2.
src_path = os.path.join(B_WS, "stats.py")
test_path = os.path.join(B_WS, "test_stats.py")
with open(src_path, "w") as fh:
    fh.write(defects.D2_SRC)
with open(test_path, "w") as fh:
    fh.write(defects.D2_TEST)
with open(src_path, "rb") as fh:
    PRE2_BYTES = fh.read()
PRE2_DIGEST = digest(PRE2_BYTES.decode())

# ---------------------------------------------------------------- step 5
# B RETRIEVES the technique from organizational experience (only the
# persisted knowledge -- A's workspace, substrate, and token are gone).
relevant = org.experience.get_relevant("repair:binary-operator")
check("B retrieves exp1 from org knowledge",
      any(e["exp_id"] == EXP1_ID for e in relevant))
# Private-state leakage probe: the retrieved knowledge must not contain
# agent-private material.
blob = json.dumps(relevant, default=str).lower()
check("no private state in retrieved knowledge",
      "custody" not in blob and "token" not in blob
      and "workspace_path" not in blob and A_ID not in json.dumps(relevant, default=str))
t1_code = exp1.code
check("T1 code retrieved", "def repair_procedure" in t1_code)

# ---------------------------------------------------------------- step 6
# B tries T1 on D2: honest failure (replay is not adaptation).
rr = run_code(t1_code, "repair_procedure",
              [{"source_text": defects.D2_SRC,
                "test_text": defects.D2_TEST}])
check("T1 executes on D2", rr.ok)
t1_report = json.loads(rr.value[0]["value"])
check("T1 honestly refuses D2 (not silently wrong)",
      "error" in t1_report)
print(f"   T1 on D2: {t1_report.get('error')}", flush=True)

# ---------------------------------------------------------------- step 7
# B DIAGNOSES D2: which function fails?
d2_examples = engine_extract_examples(defects.D2_TEST)
diag = {}
for fname, examples in d2_examples.items():
    r = run_code(defects.D2_SRC, fname,
                 [{"a": a_, "b": b_} for (a_, b_), _ in examples])
    bad = [i for i, ((_, exp), res) in
           enumerate(zip(examples, r.value))
           if not res.get("ok") or res.get("value") != exp]
    diag[fname] = bad
DIAGNOSIS_B = (
    f"mul: {len(diag['mul'])} failures; "
    f"power: {len(diag['power'])}/{len(d2_examples['power'])} failures. "
    "Defect is isolated to power(); mul() is correct and must be preserved. "
    "T1 assumes a single function and would delete mul(); adapting to "
    "diagnosis-driven targeting with surgical replacement.")
check("B diagnosis: power fails, mul passes",
      len(diag["power"]) == 3 and len(diag["mul"]) == 0)
print(f"   B diagnosis: {DIAGNOSIS_B}", flush=True)

# ---------------------------------------------------------------- step 8
# B ADAPTS: applies T2 (the adapted technique) to D2.
t2_src = open(T2_PATH).read()
check("T2 genuinely differs from T1", digest(t2_src) != digest(t1_code))
rr2 = run_code(t2_src, "repair_procedure",
               [{"source_text": defects.D2_SRC,
                 "test_text": defects.D2_TEST}])
check("T2 executes", rr2.ok)
rep2 = json.loads(rr2.value[0]["value"])
check("T2 repairs D2", rep2.get("target") == "power"
      and "repaired_source" in rep2)
POST2_SRC = rep2["repaired_source"]
# Surgical property: mul byte-identical.
MUL_ORIG = "def mul(a, b):\n    return a * b\n"
check("T2 preserves mul byte-for-byte", MUL_ORIG in POST2_SRC)
check("T2 fixes power", "a ** b" in POST2_SRC)
print(f"   T2 repairs: {rep2.get('repairs')}", flush=True)

# B applies via the guarded transaction.
guard2 = ProjectModificationGuard(B_WS)


def agent_check_b():
    with open(src_path) as fh:
        post = fh.read()
    # power fixed?
    rp = run_code(post, "power",
                  [{"a": a_, "b": b_}
                   for (a_, b_), _ in d2_examples["power"]])
    power_ok = rp.ok and all(
        res.get("ok") and res.get("value") == exp
        for (_, exp), res in zip(d2_examples["power"], rp.value))
    # mul preserved?
    rm = run_code(post, "mul",
                  [{"a": a_, "b": b_}
                   for (a_, b_), _ in d2_examples["mul"]])
    mul_ok = rm.ok and all(
        res.get("ok") and res.get("value") == exp
        for (_, exp), res in zip(d2_examples["mul"], rm.value))
    return (power_ok and mul_ok), {"power_ok": power_ok, "mul_ok": mul_ok}


res2 = guard2.modify("stats.py", POST2_SRC, verify=agent_check_b)
check("guard commits B's repair", res2.committed and not res2.rolled_back)
with open(src_path, "rb") as fh:
    POST2_BYTES = fh.read()
POST2_DIGEST = digest(POST2_BYTES.decode())
check("B's bytes differ from A's", POST2_DIGEST != handoff1["repair_a"]["post_digest"])
check("B's bytes differ from pre", POST2_DIGEST != PRE2_DIGEST)

# ---------------------------------------------------------------- step 9
# ENGINE independently verifies B's repair (fix + preservation).
spec2 = TechniqueSpec()  # placeholder, replaced below
from common import FuncSpec, instance_cases  # noqa: E402
REPAIR_B = "rep_" + digest(PRE2_DIGEST + POST2_DIGEST)[:16]
verdict2 = org.review.verify_repair(
    repair_id=REPAIR_B, agent_id=B_ID, caller=(B_ID, B_TOKEN),
    defect_signature={"family": "binary-operator",
                      "file": "stats.py", "function": "power",
                      "observed": "power() 3/3 examples fail; mul() correct",
                      "pre_digest": PRE2_DIGEST,
                      "shape": "multi-function (adapted technique T2)"},
    diagnosis=DIAGNOSIS_B,
    pre_code=PRE2_BYTES.decode(), post_code=POST2_BYTES.decode(),
    entrypoint="power", spec=FuncSpec(),
    cases=instance_cases(d2_examples["power"], "pex"))
check("B's independent verdict admitted", verdict2.admitted)
# Engine-side preservation check (independent of B's claim and of T2):
post_tree = ast.parse(POST2_BYTES.decode())
mul_nodes = [n for n in ast.walk(post_tree)
             if isinstance(n, ast.FunctionDef) and n.name == "mul"]
check("mul still present post-repair", len(mul_nodes) == 1)
rm2 = run_code(POST2_BYTES.decode(), "mul",
               [{"a": a_, "b": b_} for (a_, b_), _ in d2_examples["mul"]])
check("mul behavior preserved (engine check)",
      rm2.ok and all(res.get("ok") and res.get("value") == exp
                     for (_, exp), res in zip(d2_examples["mul"], rm2.value)))
rec_b = org.review.get_repair_record(REPAIR_B)
check("B's repair record persisted", rec_b["repair_id"] == REPAIR_B)

# ---------------------------------------------------------------- step 10
# ENGINE admits B's repair (stored verdict only).
decision2 = org.review.admit_repair(
    REPAIR_B, caller=org.oregistry.engine_handle())
check("B's repair admitted", decision2.startswith("dec_"))

# ---------------------------------------------------------------- step 11
# B authors T2 as a work product; independent review on R2 (held-out
# multi-function defect); the judge ALSO verifies surgical preservation.
def make_t2_predicate(test_text, target, preserve):
    def t2_judge(value):
        try:
            report = json.loads(value)
        except Exception:
            return False
        if not isinstance(report, dict) or "error" in report:
            return False
        repaired = report.get("repaired_source")
        if not repaired or report.get("target") != target:
            return False
        # fix check (independent extraction + subprocess)
        examples = engine_extract_examples(test_text).get(target, [])
        if not examples:
            return False
        rr = run_code(repaired, target,
                      [{"a": a, "b": b} for (a, b), _ in examples])
        if not rr.ok:
            return False
        for ((a, b), expected), res in zip(examples, rr.value):
            if not res.get("ok") or res.get("value") != expected:
                return False
        # preservation check: the non-target function must be
        # byte-identical between the original and repaired source.
        def fn_src(src, name):
            tree = ast.parse(src)
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name == name:
                    lines = src.splitlines(keepends=True)
                    return "".join(lines[node.lineno - 1:node.end_lineno])
            return None
        orig_fn = fn_src(engine_original(test_text), preserve)
        new_fn = fn_src(repaired, preserve)
        return orig_fn is not None and orig_fn == new_fn
    return t2_judge


def engine_original(test_text):
    # map the review test text back to its source (engine-side fixture)
    return {defects.R2_TEST: defects.R2_SRC,
            defects.H3_TEST: defects.H3_SRC}[test_text]


asg_b = org.assignments.create(
    B_ID, objective={"goal": "author adapted repair technique",
                     "defect_family": "binary-operator",
                     "adapted_from": EXP1_ID,
                     "instance_repair_id": REPAIR_B},
    constraints={"multi_function_modules": True,
                 "must_preserve_correct_functions": True},
    authority_scope={"allowed_capability_patterns": ["repair:*"],
                     "workspace": B_WS, "max_steps": 5},
    expected_outputs={"technique": "repair_procedure(source_text,test_text)"},
    validation_requirements={"entrypoint": "repair_procedure",
                             "case_labels": []},
    originating_decision=decision2)
org.assignments.activate(asg_b.assignment_id)
wp_b = org.runner.execute(
    asg_b.assignment_id,
    {"defect_family": "binary-operator",
     "problem_class": "repair:binary-operator",
     "adapted_from": EXP1_ID,
     "instance_evidence": {"repair_id": REPAIR_B}})
check("B's technique work product produced", wp_b.startswith("wp_"))
wpb = org.review.get_work_product(wp_b)
check("T2 code is B's artifact",
      digest(wpb.artifacts[0]["code"]) == digest(t2_src))

org.review.submit_for_review(wp_b)
t2_case = Case(
    args={"source_text": defects.R2_SRC, "test_text": defects.R2_TEST},
    predicate=make_t2_predicate(defects.R2_TEST, defects.R2_TARGET,
                                 defects.R2_PRESERVE),
    label="r2")
tverdict2 = org.review.review(wp_b, TechniqueSpec(), [t2_case])
check("T2 independent verdict admitted", tverdict2.admitted)

# ---------------------------------------------------------------- step 12
# ACCEPT -> L1, then PROMOTE on held-out H3 -> L2 (distinct experience).
cand_b = org.review.accept(wp_b, org.engine)
check("T2 accepted to L1", cand_b.startswith("exp_cand_"))
h3_case = Case(
    args={"source_text": defects.H3_SRC, "test_text": defects.H3_TEST},
    predicate=make_t2_predicate(defects.H3_TEST, defects.H3_TARGET,
                                 defects.H3_PRESERVE),
    label="h3")
promoted_b, info_b = org.experience.promote(
    cand_b, [h3_case], derived_from=[EXP1_ID])
check("T2 promoted to L2", promoted_b)
exp2 = org.experience.get_experience(info_b)
check("exp2 distinct from exp1", exp2.exp_id != EXP1_ID)
check("exp2 derived_from exp1", exp1.exp_id in exp2.derived_from)
check("exp2 discovered_by is B", exp2.discovered_by == B_ID)
check("exp2 code differs from exp1",
      exp2.code_digest != exp1.code_digest)
EXP2_ID = exp2.exp_id
print(f"   exp2={EXP2_ID}", flush=True)

failfast()

handoff2 = {
    "phase": "phase2",
    "agent_b": {"agent_id": B_ID, "producer_id": B_PRODUCER},
    "repair_b": {"repair_id": REPAIR_B, "pre_digest": PRE2_DIGEST,
                 "post_digest": POST2_DIGEST, "decision": decision2,
                 "verdict_execution_id": rec_b["verdict_execution_id"]},
    "exp2": {"exp_id": EXP2_ID, "derived_from": exp2.derived_from,
             "code_digest": exp2.code_digest,
             "verdict_execution_id": exp2.verdict_execution_id},
}
with open(os.path.join(PHASE, "handoff.json"), "w") as fh:
    json.dump(handoff2, fh, indent=2, sort_keys=True)
print(f"\nPHASE 2 COMPLETE: {CHECKS['pass']} checks passed", flush=True)
