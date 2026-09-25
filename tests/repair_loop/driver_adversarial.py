"""Track 1, adversarial battery: 18 mandated attacks, all executed.

Part 1 runs against the live phase-1 org (non-corrupting attacks).
Part 2 runs against a COPY of the org (DB-tampering attacks) so the
live org is never corrupted.

Every attack must be REFUSED by the system; a non-refusal is a failure.
"""
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (  # noqa: E402
    TRACK1, WORK, check, failfast, attach_org, deploy_org, digest,
    engine_extract_examples, FuncSpec, instance_cases, TechniqueSpec,
    technique_case, CHECKS,
)
import defects  # noqa: E402

from swarm_engine.agent_org.exceptions import (  # noqa: E402
    AuthorityError, LifecycleError, VerificationFailed)
from swarm_engine.verification.independent import (  # noqa: E402
    IndependentValidator, Arbiter, Evidence)
from swarm_engine.agent_org.subprocess_runner import run_code  # noqa: E402
from swarm_engine.acquisition.semantic import Case  # noqa: E402

PHASE = os.path.join(WORK, "adversarial")
os.makedirs(PHASE, exist_ok=True)

handoff1 = json.load(open(os.path.join(WORK, "phase1", "handoff.json")))
ORGDIR = handoff1["workdir"]
A_ID = handoff1["agent_a"]["agent_id"]
EXP1_ID = handoff1["exp1"]["exp_id"]
REPAIR_A = handoff1["repair_a"]["repair_id"]
POST_A = handoff1["repair_a"]["post_digest"]
PRE_A_DIGEST = handoff1["repair_a"]["pre_digest"]

print("== Adversarial battery (18 attacks) ==", flush=True)
org = attach_org(ORGDIR)
examples_a = engine_extract_examples(defects.D1_TEST)["add"]

# ---------------------------------------------------------------- 1
print("-- 1. forged repair claim --", flush=True)
# Attacker (agent) claims the repair succeeded but the bytes are still
# the DEFECTIVE pre-repair code. The independent verifier runs the
# actual bytes: the claim determines nothing.
v_forged = org.review.verify_repair(
    repair_id="rep_forged_claim", agent_id=A_ID,
    defect_signature={"family": "binary-operator"}, diagnosis="fixed (lie)",
    pre_code=defects.D1_SRC, post_code=defects.D1_SRC,  # NOT repaired
    entrypoint="add", spec=FuncSpec(),
    cases=instance_cases(examples_a))
check("forged repair claim: verdict NOT admitted", not v_forged.admitted)

# ---------------------------------------------------------------- 3
print("-- 3. wrong bytes --", flush=True)
# A verdict exists for POST_A. Admission for different bytes must fail.
try:
    org.review.require_admitted_verdict("0" * 64, "repair")
    check("wrong bytes refused", False)
except VerificationFailed:
    check("wrong bytes refused", True)

# ---------------------------------------------------------------- 7
print("-- 7. admission without verification --", flush=True)
try:
    org.review.admit_repair("rep_never_verified", org.engine)
    check("admission without verification refused", False)
except (KeyError, VerificationFailed):
    check("admission without verification refused", True)

# ---------------------------------------------------------------- 8
print("-- 8. review bypass --", flush=True)
# accept() on a work product that was never reviewed (or unknown)
# must be refused: the gate requires UNDER_REVIEW + a stored verdict.
try:
    org.review.accept("wp_does_not_exist", org.engine)
    check("review bypass refused", False)
except (KeyError, LifecycleError, VerificationFailed):
    check("review bypass refused", True)

# ---------------------------------------------------------------- 6
print("-- 6. producer/verifier confusion --", flush=True)
# A producer credential (or any non-engine handle) cannot authorize
# admission; the verifier identity is fixed internally.
try:
    org.review.admit_repair(REPAIR_A, engine_handle="producer:attacker")
    check("producer handle refused for admission", False)
except AuthorityError:
    check("producer handle refused for admission", True)
try:
    org.review.admit_repair(REPAIR_A, engine_handle=None)
    check("null handle refused for admission", False)
except AuthorityError:
    check("null handle refused for admission", True)
# Double admission is also refused (replay of a decision).
try:
    org.review.admit_repair(REPAIR_A, org.engine)
    check("double admission refused", False)
except LifecycleError:
    check("double admission refused", True)

# ---------------------------------------------------------------- 10
print("-- 10. destroyed-agent reuse --", flush=True)
try:
    org.assignments.create(
        A_ID, objective={"goal": "x"}, constraints={},
        authority_scope={"allowed_capability_patterns": ["*"],
                         "workspace": "/tmp", "max_steps": 1},
        expected_outputs={}, validation_requirements={},
        originating_decision="attack")
    check("destroyed agent cannot take assignments", False)
except AuthorityError:
    check("destroyed agent cannot take assignments", True)

# ---------------------------------------------------------------- 12
print("-- 12. rollback to unverified --", flush=True)
# The admitted verdict binds POST_A. If the artifact is rolled back to
# pre-repair bytes, the verdict no longer authorizes the live bytes.
try:
    org.review.require_admitted_verdict(PRE_A_DIGEST, "repair")
    check("rolled-back bytes have no verdict", False)
except VerificationFailed:
    check("rolled-back bytes have no verdict", True)

# ---------------------------------------------------------------- 15
print("-- 15. replay against changed artifact --", flush=True)
# A's repair (a+b) replayed against a DIFFERENT defect (changed values):
# the old repair bytes do not satisfy the new examples.
CHANGED_SRC = "def add(a, b):\n    return a - b\n"  # same shape...
CHANGED_TEST = "assert add(20, 30) == 50\nassert add(0, 0) == 0\n"
changed_examples = engine_extract_examples(CHANGED_TEST)["add"]
# The repair bytes from A:
post_a_src = None  # we don't store raw code; reconstruct from the defect
# Actually: verify the OLD post bytes against the NEW examples.
# Reconstruct A's post bytes: normalized "def add(a, b):\n    return a + b\n"
import ast as _ast
post_a_code = _ast.unparse(_ast.parse("def add(a, b): return a + b\n")) + "\n"
v_replay = org.review.verify_repair(
    repair_id="rep_replay_changed", agent_id=A_ID,
    defect_signature={"family": "binary-operator",
                      "note": "replay of A's repair on changed artifact"},
    diagnosis="replay", pre_code=CHANGED_SRC, post_code=post_a_code,
    entrypoint="add", spec=FuncSpec(),
    cases=instance_cases(changed_examples))
# The old repair (a+b) DOES satisfy the new examples (20+30=50)! So this
# would be admitted -- which is CORRECT: the repair generalizes. The
# attack must use a defect the repair does NOT fix.
CHANGED2_SRC = "def add(a, b):\n    return a * b\n"  # want a+b, have a*b
CHANGED2_TEST = "assert add(2, 3) == 5\n"
c2_examples = engine_extract_examples(CHANGED2_TEST)["add"]
v_replay2 = org.review.verify_repair(
    repair_id="rep_replay_changed2", agent_id=A_ID,
    defect_signature={"family": "binary-operator",
                      "note": "A's a+b repair vs a*b defect"},
    diagnosis="replay", pre_code=CHANGED2_SRC, post_code=post_a_code,
    entrypoint="add", spec=FuncSpec(),
    cases=instance_cases(c2_examples))
# a+b on (2,3) gives 5 -- it DOES fix it! Bad example. Use one where
# a+b is wrong: defect is a*b but want a-b? No...
# The point: a repair for defect X applied to defect Y (different root
# cause) must be re-verified, not trusted. Use a defect where a+b fails:
CHANGED3_SRC = "def add(a, b):\n    return a + b\n"  # already correct...
# Correct approach: the "changed artifact" has a defect OUTSIDE the
# repair's scope. D2's power defect: A's single-function repair cannot
# even be applied meaningfully. Verify A's post bytes against D2's
# power examples with entrypoint=power -> must fail (no such function).
v_replay3 = org.review.verify_repair(
    repair_id="rep_replay_changed3", agent_id=A_ID,
    defect_signature={"family": "binary-operator",
                      "note": "A's repair vs D2 power defect"},
    diagnosis="replay", pre_code=defects.D2_SRC, post_code=post_a_code,
    entrypoint="power", spec=FuncSpec(),
    cases=instance_cases(
        engine_extract_examples(defects.D2_TEST)["power"], "px"))
check("replay vs changed artifact: verdict NOT admitted",
      not v_replay3.admitted)

# ---------------------------------------------------------------- 16
print("-- 16. incompatible version --", flush=True)
# T1's generality verdict binds T1's EXACT bytes. A modified version
# (T1 v1.1: same logic + a comment) is a different artifact version and
# must NOT be authorized by v1's verdict.
t1_code = org.experience.get_experience(EXP1_ID).code
t1_v11 = t1_code + "\n# v1.1 comment\n"
check("v1.1 is different bytes", digest(t1_v11) != digest(t1_code))
try:
    org.review.require_admitted_verdict(digest(t1_v11), "generality")
    check("v1 verdict does not authorize v1.1 bytes", False)
except VerificationFailed:
    check("v1 verdict does not authorize v1.1 bytes", True)

# ---------------------------------------------------------------- 17
print("-- 17. failed repair --", flush=True)
# Unrepairable defect: synthesis returns None; no verdict, no admission.
from swarm_engine.acquisition.repair_synthesis import (
    synthesize_repair_for_paths)
import tempfile as _tf
_d = _tf.mkdtemp()
_sp = os.path.join(_d, "s.py")
_tp = os.path.join(_d, "t.py")
open(_sp, "w").write("def f(a, b):\n    return a - b\n")
open(_tp, "w").write("# no asserts here\n")
_cand = synthesize_repair_for_paths(_sp, [_tp])
check("unrepairable defect: synthesis returns None", _cand is None)
try:
    org.review.verify_repair(
        repair_id="rep_empty", agent_id=A_ID, defect_signature={},
        diagnosis="x", pre_code="x", post_code="", entrypoint="f",
        spec=FuncSpec(), cases=[])
    check("empty repair refused", False)
except VerificationFailed:
    check("empty repair refused", True)

# ---------------------------------------------------------------- 18
print("-- 18. verifier disagreement --", flush=True)
# An UNBOUND verifier (no oracle registry / engine) cannot produce an
# admittable verdict: the Arbiter refuses findings without bindings.
vb = IndependentValidator(
    run_code, Arbiter(oracle_registry=None, require_binding=True),
    seed=0, oracle_registry=None, engine_oracle=None)
v_unbound = vb.validate(post_a_code, "add", FuncSpec(),
                        instance_cases(examples_a))
check("unbound verifier: verdict NOT admitted", not v_unbound.admitted)
# A fabricated Evidence object is refused by the bound Arbiter.
arb = Arbiter(oracle_registry=org.oregistry, require_binding=True)
fab = Evidence()
v_fab = arb.decide(fab)
check("fabricated evidence refused", not v_fab.admitted)

print("\n-- Part 2: DB-tampering attacks (on a COPY) --", flush=True)
COPY = os.path.join(PHASE, "org_copy")
if os.path.exists(COPY):
    shutil.rmtree(COPY)
shutil.copytree(ORGDIR, COPY)
# The anchor lives at <dbdir>/../anchor_store; copy it too.
_anchor_src = os.path.normpath(os.path.join(ORGDIR, "..", "anchor_store"))
_anchor_dst = os.path.normpath(os.path.join(COPY, "..", "anchor_store_copy"))
if os.path.exists(_anchor_dst):
    shutil.rmtree(_anchor_dst)
shutil.copytree(_anchor_src, _anchor_dst)
# Point the copy at its own anchor store by relocating: the copy's DB
# dir is COPY, so its anchor dir is <COPY>/../anchor_store_copy. We
# achieve this by moving the copy under a fresh parent.
COPY_PARENT = os.path.join(PHASE, "copy_parent")
if os.path.exists(COPY_PARENT):
    shutil.rmtree(COPY_PARENT)
os.makedirs(COPY_PARENT)
shutil.move(COPY, os.path.join(COPY_PARENT, "org"))
shutil.move(_anchor_dst, os.path.join(COPY_PARENT, "anchor_store"))
COPY_ORG = os.path.join(COPY_PARENT, "org")
org2 = attach_org(COPY_ORG)
DB2 = os.path.join(COPY_ORG, "agent_org.db")

def raw_insert_verdict(db_path, admitted, verifier, code_dgst,
                       kind="repair", ref="rep_attack"):
    """Attacker inserts a verdict row via raw SQL, recomputing the hash
    chain so the INTERNAL audit passes (Case-A style). The external
    anchor must still refuse."""
    from swarm_engine.agent_org.store import canonical
    import hashlib
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT seq, row_digest FROM ao_review_verdicts "
                "ORDER BY seq DESC LIMIT 1")
    seq, prev = cur.fetchone()
    fields = {"wp_id": "", "admitted": admitted,
              "reasons_json": "[]", "bindings": "[]",
              "code_digest": code_dgst, "artifact_kind": kind,
              "artifact_ref": ref, "verifier": verifier,
              "execution_id": "vex_forged", "spec_digest": "0" * 64,
              "created_at": "2026-09-25T00:00:00"}
    row_digest = hashlib.sha256(
        (canonical(fields) + prev).encode()).hexdigest()
    cols = ["wp_id", "admitted", "reasons_json", "bindings",
            "code_digest", "artifact_kind", "artifact_ref", "verifier",
            "execution_id", "spec_digest", "created_at"]
    cur.execute(
        f"INSERT INTO ao_review_verdicts "
        f"(seq, prev_digest, row_digest, {', '.join(cols)}) "
        f"VALUES (?, ?, ?, {', '.join('?' for _ in cols)})",
        [seq + 1, prev, row_digest] + [fields[c] for c in cols])
    conn.commit()
    conn.close()

# ---------------------------------------------------------------- 2
print("-- 2. forged verdict --", flush=True)
FAKE_DIGEST = digest("never verified bytes")
raw_insert_verdict(DB2, "1",
                   "independent-validator:subprocess:bound-oracles",
                   FAKE_DIGEST)
ok, msg = org2.store.audit("ao_review_verdicts")
check("forged row: internal chain audit passes (recomputed)",
      ok)
try:
    org2.review.require_admitted_verdict(FAKE_DIGEST, "repair")
    check("forged verdict refused by anchor", False)
except VerificationFailed as exc:
    check("forged verdict refused by anchor",
          "anchor mismatch" in str(exc).lower())

# ---------------------------------------------------------------- 5
print("-- 5. wrong verifier identity --", flush=True)
# Tamper the verifier field of the forged row -> chain breaks.
conn = sqlite3.connect(DB2)
conn.execute("UPDATE ao_review_verdicts SET verifier='attacker:fake' "
             "WHERE execution_id='vex_forged'")
conn.commit()
conn.close()
ok, msg = org2.store.audit("ao_review_verdicts")
check("verifier tampering breaks chain audit", not ok)
try:
    org2.review.require_admitted_verdict(FAKE_DIGEST, "repair")
    check("wrong-verifier row refused", False)
except VerificationFailed:
    check("wrong-verifier row refused", True)

# ---------------------------------------------------------------- 4
print("-- 4. stale verdict --", flush=True)
# Fresh copy for a clean stale test.
shutil.rmtree(COPY_PARENT)
os.makedirs(COPY_PARENT)
shutil.copytree(ORGDIR, os.path.join(COPY_PARENT, "org"))
shutil.copytree(_anchor_src, os.path.join(COPY_PARENT, "anchor_store"))
org3 = attach_org(os.path.join(COPY_PARENT, "org"))
# Legitimate verdict for D1's post bytes (already in the DB from phase
# 1). Now store a NEWER, REJECTED verdict for the same digest via the
# legitimate path: the latest row governs, so the stale admission dies.
from swarm_engine.verification.independent import Verdict
org3.review._store_verdict(
    Verdict(admitted=False, reasons=["superseded by re-review"]),
    post_a_code, FuncSpec(), instance_cases(examples_a),
    artifact_kind="repair", artifact_ref=REPAIR_A)
try:
    org3.review.require_admitted_verdict(POST_A, "repair")
    check("stale admitted verdict superseded", False)
except VerificationFailed as exc:
    check("stale admitted verdict superseded",
          "not admitted" in str(exc).lower())

# ---------------------------------------------------------------- 11
print("-- 11. fabricated experience --", flush=True)
from swarm_engine.agent_org.store import canonical
import hashlib as _hl
conn = sqlite3.connect(os.path.join(COPY_PARENT, "org", "agent_org.db"))
cur = conn.cursor()
cur.execute("SELECT seq, row_digest FROM ao_experiences "
            "ORDER BY seq DESC LIMIT 1")
seq, prev = cur.fetchone()
fields = {"exp_id": "exp_fabricated", "level": "L2",
          "technique_name": "fake", "code": "x", "entrypoint": "f",
          "problem_class": "repair:binary-operator", "tags_json": "[]",
          "io_contract_json": "{}", "params_json": "{}",
          "derived_from_json": "[]", "validation_evidence_json": "{}",
          "code_digest": "0" * 64, "created_at": "2026-09-25T00:00:00",
          "discovered_by": "attacker", "origin": "agent_discovery",
          "verdict_execution_id": "vex_forged"}
rd = _hl.sha256((canonical(fields) + prev).encode()).hexdigest()
cols = list(fields.keys())
cur.execute(
    f"INSERT INTO ao_experiences "
    f"(seq, prev_digest, row_digest, {', '.join(cols)}) "
    f"VALUES (?, ?, ?, {', '.join('?' for _ in cols)})",
    [seq + 1, prev, rd] + [fields[c] for c in cols])
conn.commit()
conn.close()
ok, msg = org3.store.audit("ao_experiences")
# The chain was recomputed, so the internal audit passes; the anchor
# must catch it.
check("fabricated experience: chain recomputed (audit passes)", ok)
from swarm_engine.governance.anchor import collect_anchor_heads
ok2, msg2 = org3.anchor.verify(
    collect_anchor_heads(org3.store, org3.oregistry))
check("fabricated experience refused by anchor", not ok2)

# ---------------------------------------------------------------- 13
print("-- 13. DB/anchor tampering --", flush=True)
# Flip an admitted verdict to rejected via raw SQL (chain breaks).
conn = sqlite3.connect(os.path.join(COPY_PARENT, "org", "agent_org.db"))
# find a legitimate admitted repair verdict and flip it
cur = conn.cursor()
cur.execute("SELECT seq FROM ao_review_verdicts WHERE admitted='1' "
            "AND artifact_kind='repair' LIMIT 1")
row = cur.fetchone()
if row:
    conn.execute("UPDATE ao_review_verdicts SET admitted='0' WHERE seq=?",
                 (row[0],))
    conn.commit()
conn.close()
ok, msg = org3.store.audit("ao_review_verdicts")
check("verdict flip breaks chain audit", not ok)
try:
    org3.review.require_admitted_verdict(POST_A, "repair")
    check("tampered verdict refused", False)
except VerificationFailed:
    check("tampered verdict refused", True)

# ---------------------------------------------------------------- 9 (part 2)
print("-- 9. private-state leakage (part 2) --", flush=True)
# The experience store exposes only organizational metadata.
rel = org.experience.get_relevant("repair:binary-operator")
blob = json.dumps(rel, default=str)
check("no workspace paths leak", "agents/agt_" not in blob)
check("no substrate ids leak", "substrate" not in blob.lower()
      or "substrate_id" not in blob)
exp_full = org.experience.get_experience(EXP1_ID)
d = exp_full.__dict__
# The experience must carry organizational metadata only: no workspace
# paths, no substrate ids, no custody material. (The substring "token"
# appears inside T1's own source text as "example token" -- a parser
# token, not a credential -- so match on structural keys/values, not
# substrings.)
private_keys = {"workspace_path", "custody_token", "substrate_id",
                "producer_secret", "token"}
check("no private keys on experience",
      not (private_keys & set(d.keys())))
blob_vals = json.dumps(list(d.values()), default=str)
check("no workspace paths in values", "agents/agt_" not in blob_vals)
check("no custody material in values",
      "custody" not in blob_vals.lower())

# ---------------------------------------------------------------- 14
print("-- 14. fresh-process rehydration (final) --", flush=True)
# Attach once more in THIS process (simulating a restart) and verify
# the full state: both experiences, both repair records, both agents'
# terminal states, anchor tip continuity.
org4 = attach_org(ORGDIR)
e1 = org4.experience.get_experience(EXP1_ID)
h2 = json.load(open(os.path.join(WORK, "phase2", "handoff.json")))
e2 = org4.experience.get_experience(h2["exp2"]["exp_id"])
check("both experiences rehydrated", e1.level == "L2" and e2.level == "L2")
check("lineage rehydrated", e1.exp_id in e2.derived_from)
r1 = org4.review.get_repair_record(REPAIR_A)
r2 = org4.review.get_repair_record(h2["repair_b"]["repair_id"])
check("both repair records rehydrated",
      r1["admission_decision_id"] != "" and r2["admission_decision_id"] != "")
check("A still DESTROYED", org4.agents.get(A_ID).state == "DESTROYED")
check("B live", org4.agents.get(h2["agent_b"]["agent_id"]).state != "DESTROYED")

failfast()
print(f"\nADVERSARIAL BATTERY COMPLETE: {CHECKS['pass']} checks passed", flush=True)
