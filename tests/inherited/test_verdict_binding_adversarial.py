#!/usr/bin/env python3
"""Adversarial probes for verification-verdict binding (mission 2026-09-25).

Every probe exercises the REAL mechanism (engine-executed verification,
persisted verdict rows, hash-chained store) -- no mocks, no stubs.
Each probe boots a fresh isolated organization in a temp workdir.

Bypass shapes attacked:
  P1  record_l3 with no prior verification            -> must refuse
  P2  fabricated validation_evidence kwarg            -> TypeError (param gone)
  P3  record_l3 after a REJECTED synthesis verdict    -> must refuse
  P4  record_l3 for modified code (digest mismatch)   -> must refuse
  P5  forged verdict row (raw SQL, broken chain)      -> must refuse
  P6  tampered verdict row (UPDATE admitted 0->1)      -> must refuse (audit)
  P7  record_l3 with unknown derived_from id          -> must refuse
  P8  record_l3 with empty derived_from               -> ValueError (kept)
  P9  record_l3 without engine handle                 -> AuthorityError (kept)
  P10 generality verdict used for L3 admission        -> must refuse (kind)
  P11 supersede: admitted then rejected -> latest governs -> must refuse
  P12 valid path: verify -> record_l3                 -> must accept, bound
  P13 lying caller validator for promote              -> TypeError (param gone)
  P14 promote with failing generality                 -> (False, reasons)
  P15 promote stores generality verdict + provenance  -> must hold
  P16 fresh process: verify, close, reboot, record_l3 -> must accept
  P17 fresh process + tampered verdict between boots   -> must refuse
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # anchor_shim (battery E harness)
from anchor_shim import fresh_workdir, deploy_org, attach_org  # noqa: E402

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))
PYLIB = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib")
sys.path.insert(0, PYLIB)

from swarm_engine.agent_org.org import RemorOrganization
from swarm_engine.agent_org.store import digest
from swarm_engine.agent_org.exceptions import (
    AuthorityError, VerificationFailed)
from swarm_engine.acquisition.semantic import Case

RESULTS = []
SKIPPED = []


def check(name, cond, detail=""):
    RESULTS.append({"name": name, "pass": bool(cond), "detail": str(detail)})
    print(("PASS " if cond else "FAIL ") + name +
          ((" | " + str(detail)) if detail else ""))
    return bool(cond)


CODE_V1 = '''
def selftest(data_hex):
    # real (toy) implementation: byte rotation codec with honest roundtrip.
    # Must behave on ANY string input: the validator's novelty probes feed
    # non-hex strings, and raising there is (correctly) read as a memorised
    # table rather than an implementation.
    if not isinstance(data_hex, str):
        data_hex = str(data_hex)
    try:
        raw = bytes.fromhex(data_hex)
    except Exception:
        raw = data_hex.encode("utf-8", "replace")
    enc = bytes((b + 1) % 256 for b in raw)
    dec = bytes((b - 1) % 256 for b in enc)
    return {"roundtrip_ok": dec == raw, "ratio": len(enc) / max(1, len(raw)),
            "n": len(raw)}
'''

CODE_BROKEN = '''
def selftest(data_hex):
    raise RuntimeError("boom")
'''


class Spec:
    input_names = ["data_hex"]
    description = "probe spec"
    examples = [({"data_hex": "00" * 64}, None)]


def mk_cases(pred, label="p", n=2):
    return [Case(args={"data_hex": "%02x" % i * 64},
                 predicate=pred, label=f"{label}[{i}]")
            for i in range(n)]


OK_CASES = mk_cases(lambda v: isinstance(v, dict)
                    and v.get("roundtrip_ok") is True, "ok")
FAIL_CASES = mk_cases(lambda v: False, "fail")


def fresh_org():
    # Battery E: explicit anchor deployment (genesis) + auto-anchor of
    # legitimate writes, via the real default anchor paths.
    wd = fresh_workdir("vb_adv_")
    return deploy_org(wd, authority="batteryE:adv-probes"), wd


def make_l2(org, tag, code=CODE_V1):
    """Legitimate L2 experience via the real promote path (for derived_from)."""
    cand_id = org.experience.submit_candidate(
        wp_id="wp_" + tag, agent_id="agt_" + tag, problem_class="probe",
        technique_name="trivial_" + tag, description="probe", code=code,
        entrypoint="selftest",
        io_contract={"input": "bytes", "output": "bytes"},
        tags=[], params={}, evidence_refs={})
    ok, res = org.experience.promote(cand_id, list(OK_CASES))
    assert ok, f"setup promote failed: {res}"
    return org.experience.get_experience(res)


def expect_raise(name, exc_types, fn, detail=""):
    try:
        fn()
    except exc_types as e:
        return check(name, True, f"{type(e).__name__}: {str(e)[:100]}")
    except Exception as e:  # noqa: BLE001 - wrong exception is a failure
        return check(name, False,
                     f"wrong exception {type(e).__name__}: {str(e)[:100]} "
                     + detail)
    return check(name, False, "no exception raised " + detail)


# ---------------------------------------------------------------- P1/P2
org, _ = fresh_org()
exp_a = make_l2(org, "a")
expect_raise(
    "P1.record_l3-no-verdict-refused", VerificationFailed,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        engine=org.engine))
expect_raise(
    "P2.fabricated-evidence-kwarg-impossible", TypeError,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        validation_evidence={"verdict": "admitted"}, engine=org.engine))
org.close()

# ---------------------------------------------------------------- P3
org, _ = fresh_org()
exp_a = make_l2(org, "a")
v = org.review.verify_artifact(CODE_V1, "selftest", Spec(), list(FAIL_CASES),
                               artifact_ref="zfail")
check("P3.setup-verdict-rejected", not v.admitted)
expect_raise(
    "P3.record_l3-after-rejected-verdict-refused", VerificationFailed,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        engine=org.engine))
org.close()

# ---------------------------------------------------------------- P4
org, _ = fresh_org()
exp_a = make_l2(org, "a")
v = org.review.verify_artifact(CODE_V1, "selftest", Spec(), list(OK_CASES),
                               artifact_ref="z")
check("P4.setup-verdict-admitted", v.admitted)
expect_raise(
    "P4.record_l3-modified-code-refused", VerificationFailed,
    lambda: org.experience.record_l3(
        "z", CODE_V1 + "\n# sneaky edit\n", "selftest", "probe", [],
        {}, [exp_a.exp_id], engine=org.engine))
org.close()

# ---------------------------------------------------------------- P5 (forged row, broken chain)
org, _ = fresh_org()
exp_a = make_l2(org, "a")
conn = org.store._conn
conn.execute(
    "INSERT INTO ao_review_verdicts (prev_digest, row_digest, wp_id, "
    "admitted, reasons_json, bindings, code_digest, artifact_kind, "
    "artifact_ref, verifier, execution_id, spec_digest, created_at) "
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("GENESIS", "forged", "", "1", "[]", "{}",
     digest(CODE_V1), "synthesis", "forged",
     "independent-validator:subprocess:bound-oracles",
     "vex_forged", "spec", "2026-01-01T00:00:00"))
conn.commit()
ok, msg = org.store.audit("ao_review_verdicts")
check("P5.setup-chain-broken", not ok, msg)
expect_raise(
    "P5.forged-verdict-row-refused", VerificationFailed,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        engine=org.engine))
org.close()

# ---------------------------------------------------------------- P6 (tamper: UPDATE admitted)
org, _ = fresh_org()
exp_a = make_l2(org, "a")
v = org.review.verify_artifact(CODE_V1, "selftest", Spec(), list(FAIL_CASES),
                               artifact_ref="zfail2")
check("P6.setup-verdict-rejected", not v.admitted)
conn = org.store._conn
conn.execute("UPDATE ao_review_verdicts SET admitted='1' "
             "WHERE artifact_kind='synthesis'")
conn.commit()
ok, msg = org.store.audit("ao_review_verdicts")
check("P6.setup-tamper-detected", not ok, msg)
expect_raise(
    "P6.tampered-verdict-row-refused", VerificationFailed,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        engine=org.engine))
org.close()

# ---------------------------------------------------------------- P7/P8/P9
org, _ = fresh_org()
exp_a = make_l2(org, "a")
org.review.verify_artifact(CODE_V1, "selftest", Spec(), list(OK_CASES),
                           artifact_ref="z7")
expect_raise(
    "P7.unknown-derived_from-refused", VerificationFailed,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, ["exp_nope"],
        engine=org.engine))
expect_raise(
    "P8.empty-derived_from-ValueError", ValueError,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [],
        engine=org.engine))
expect_raise(
    "P9.no-engine-handle-AuthorityError", AuthorityError,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        engine=None))
org.close()

# ---------------------------------------------------------------- P10 (kind scoping)
org, _ = fresh_org()
exp_a = make_l2(org, "a")  # stores a "generality" verdict for CODE_V1
rows = [r for r in org.store.rows("ao_review_verdicts")
        if r["artifact_kind"] == "generality"
        and r["code_digest"] == digest(CODE_V1)]
check("P10.setup-generality-verdict-exists", len(rows) > 0,
      f"{len(rows)} generality rows")
expect_raise(
    "P10.generality-verdict-cannot-authorize-L3", VerificationFailed,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        engine=org.engine))
org.close()

# ---------------------------------------------------------------- P11 (supersede)
org, _ = fresh_org()
exp_a = make_l2(org, "a")
org.review.verify_artifact(CODE_V1, "selftest", Spec(), list(OK_CASES),
                           artifact_ref="z11a")
l3a = org.experience.record_l3(
    "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
    engine=org.engine)
check("P11.setup-first-admission-ok", l3a.level == "L3")
v2 = org.review.verify_artifact(CODE_V1, "selftest", Spec(), list(FAIL_CASES),
                               artifact_ref="z11b")
check("P11.setup-second-verdict-rejected", not v2.admitted)
expect_raise(
    "P11.superseded-admission-refused", VerificationFailed,
    lambda: org.experience.record_l3(
        "z2", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        engine=org.engine))
org.close()

# ---------------------------------------------------------------- P12 (valid path)
org, _ = fresh_org()
exp_a = make_l2(org, "a")
exp_b = make_l2(org, "b")
v = org.review.verify_artifact(CODE_V1, "selftest", Spec(), list(OK_CASES),
                               artifact_ref="z12")
check("P12.setup-verdict-admitted", v.admitted)
stored = org.review.latest_artifact_verdict(digest(CODE_V1), "synthesis")
l3 = org.experience.record_l3(
    "z", CODE_V1, "selftest", "probe", ["t"], {"input": "bytes"},
    [exp_a.exp_id, exp_b.exp_id], params={"chunk_size": 64},
    engine=org.engine)
check("P12.valid-path-accepted", l3.level == "L3", l3.exp_id)
check("P12.evidence-from-stored-verdict",
      l3.validation_evidence.get("execution_id") == stored["execution_id"] and
      l3.validation_evidence.get("verdict") == "admitted",
      str(l3.validation_evidence.get("execution_id")))
check("P12.provenance",
      l3.discovered_by == "remor:engine" and
      l3.origin == "remor_synthesis" and
      l3.verdict_execution_id == stored["execution_id"])
check("P12.params-preserved", l3.params == {"chunk_size": 64})
check("P12.derived_from", l3.derived_from == [exp_a.exp_id, exp_b.exp_id])
org.close()

# ---------------------------------------------------------------- P13/P14/P15
org, _ = fresh_org()
cand = org.experience.submit_candidate(
    wp_id="wp_p", agent_id="agt_p", problem_class="probe",
    technique_name="t", description="d", code=CODE_V1,
    entrypoint="selftest", io_contract={}, tags=[], params={},
    evidence_refs={})


class LyingValidator:
    def validate(self, *a, **k):
        raise AssertionError("lying validator must never be consulted")


expect_raise(
    "P13.lying-validator-param-impossible", TypeError,
    lambda: org.experience.promote(cand, list(OK_CASES),
                                   validator=LyingValidator()))
ok, res = org.experience.promote(cand, list(OK_CASES))
check("P14a.promote-engine-executed", ok is True, str(res)[:80])
cand_b = org.experience.submit_candidate(
    wp_id="wp_pb", agent_id="agt_pb", problem_class="probe",
    technique_name="tb", description="d", code=CODE_BROKEN,
    entrypoint="selftest", io_contract={}, tags=[], params={},
    evidence_refs={})
ok2, res2 = org.experience.promote(cand_b, list(OK_CASES))
check("P14b.promote-failing-code-refused", ok2 is False, str(res2)[:80])
exp_none = [e for e in org.experience.list_experiences()
            if e.technique_name == "tb" and e.level == "L2"]
check("P14c.no-L2-row-for-failure", len(exp_none) == 0)
exp = org.experience.get_experience(res)
grow = [r for r in org.store.rows("ao_review_verdicts")
        if r["artifact_kind"] == "generality"
        and r["code_digest"] == digest(CODE_V1)
        and r["admitted"] == "1"]
check("P15.generality-verdict-stored", len(grow) > 0)
check("P15.l2-provenance",
      exp.discovered_by == "agt_p" and exp.origin == "agent_discovery" and
      exp.verdict_execution_id == grow[-1]["execution_id"] and
      exp.validation_evidence.get("execution_id") == grow[-1]["execution_id"])
org.close()

# ---------------------------------------------------------------- P18/P19
# (execution_id / spec_digest tamper: same chain mechanism, explicit shapes)
org, _ = fresh_org()
exp_a = make_l2(org, "a")
org.review.verify_artifact(CODE_V1, "selftest", Spec(), list(OK_CASES),
                           artifact_ref="z18")
conn = org.store._conn
conn.execute("UPDATE ao_review_verdicts SET execution_id='vex_attacker_forged' "
             "WHERE artifact_kind='synthesis'")
conn.commit()
expect_raise(
    "P18.altered-execution-id-refused", VerificationFailed,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        engine=org.engine))
conn.execute("UPDATE ao_review_verdicts SET execution_id="
             "'vex_restored', spec_digest='forged-spec' "
             "WHERE artifact_kind='synthesis'")
conn.commit()
expect_raise(
    "P19.altered-spec-digest-refused", VerificationFailed,
    lambda: org.experience.record_l3(
        "z", CODE_V1, "selftest", "probe", [], {}, [exp_a.exp_id],
        engine=org.engine))
ok, msg = org.store.audit("ao_review_verdicts")
check("P19.setup-tamper-detected", not ok, msg)
org.close()

# ---------------------------------------------------------------- P20
# Causal comparison: the PRE-BINDING code (reference tree, untouched)
# accepts driver-fabricated validation evidence for L3; the bound code
# cannot even express that call. Read-only use of the reference tree;
# all state in a temp workdir.
REF_PYLIB = os.environ.get("REMOR_REFERENCE_PYLIR",
                             os.path.expanduser("~/workspace/remor_agent_org/pylib"))
if os.path.isdir(REF_PYLIB):
    code = (
        "import sys, tempfile\n"
        f"sys.path.insert(0, {REF_PYLIB!r})\n"
        "from swarm_engine.agent_org.org import RemorOrganization\n"
        "wd = tempfile.mkdtemp(prefix='prebind_')\n"
        "org = RemorOrganization.boot(wd)\n"
        "l3 = org.experience.record_l3('z', 'code', 'ep', 'probe', [], {},\n"
        "    ['exp_anything'],\n"
        "    validation_evidence={'verdict': 'admitted (driver: fabricated)'},\n"
        "    engine=org.engine)\n"
        "print('PREBIND-ADMITTED ' + l3.exp_id + ' ' +\n"
        "      str(l3.validation_evidence.get('verdict')))\n"
        "org.close()\n"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, timeout=300)
    out = (p.stdout or "").strip()
    check("P20.pre-binding-manufactures-L3",
          p.returncode == 0 and out.startswith("PREBIND-ADMITTED"),
          f"rc={p.returncode} out={out} err={(p.stderr or '').strip()[-200:]}")
    # and the bound tree refuses the same fabricated lineage claim
    org, _ = fresh_org()
    expect_raise(
        "P20b.bound-tree-refuses-fabricated-lineage", VerificationFailed,
        lambda: org.experience.record_l3(
            "z", CODE_V1, "selftest", "probe", [], {}, ["exp_anything"],
            engine=org.engine))
    org.close()
else:
    # Reference tree absent: SKIP (not FAIL). A missing fixture is not a
    # binding failure. Set REMOR_REFERENCE_PYLIR to a reference tree to run P20.
    SKIPPED.append("P20.pre-binding-manufactures-L3")
    SKIPPED.append("P20b.bound-tree-refuses-fabricated-lineage")
    print("SKIP P20.pre-binding-manufactures-L3 | reference pylib not found "
          "(set REMOR_REFERENCE_PYLIR to run)")
    print("SKIP P20b.bound-tree-refuses-fabricated-lineage | reference pylib not found")
SCRIPT = '''
import sys, os
sys.path.insert(0, "{shimdir}")
sys.path.insert(0, "{pylib}")
from anchor_shim import attach_org
from swarm_engine.acquisition.semantic import Case
wd = sys.argv[1]; mode = sys.argv[2]
# Battery E: fresh OS process re-attaches to the anchored org (boot
# verifies the journal against the live DB heads -- no re-init).
org = attach_org(wd)
if mode == "verify":
    code = open(sys.argv[3]).read()
    class Spec:
        input_names = ["data_hex"]; description = "fp"
        examples = [({{"data_hex": "00"*64}}, None)]
    cases = [Case(args={{"data_hex": "00"*64}},
                  predicate=lambda v: v.get("roundtrip_ok") is True,
                  label="fp0")]
    v = org.review.verify_artifact(code, "selftest", Spec(), cases,
                                   artifact_ref="zfp")
    print("ADMITTED" if v.admitted else "REJECTED")
elif mode == "record":
    code = open(sys.argv[3]).read()
    exps = org.experience.list_experiences()
    l2 = [e for e in exps if e.level == "L2"]
    assert l2, "no L2 for derived_from"
    l3 = org.experience.record_l3("zfp", code, "selftest", "probe", [],
                                  {{}}, [l2[0].exp_id], engine=org.engine)
    print("L3-OK " + l3.exp_id)
org.close()
'''.format(pylib=PYLIB, shimdir=HERE)

# Battery E: nested workdir so this org gets its own anchor journal.
wd = fresh_workdir("vb_fp_")
code_path = os.path.join(wd, "code.py")
with open(code_path, "w") as f:
    f.write(CODE_V1)
script_path = os.path.join(wd, "fp.py")
with open(script_path, "w") as f:
    f.write(SCRIPT)


def run_fp(mode):
    p = subprocess.run([sys.executable, script_path, wd, mode, code_path],
                       capture_output=True, text=True, timeout=300)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()[-300:]


# seed one L2 via promote in this process (derived_from needs a real row)
# Battery E: deploy (anchor init) on the fp workdir itself.
org = deploy_org(wd, authority="batteryE:adv-fp")
cand = org.experience.submit_candidate(
    wp_id="wp_fp", agent_id="agt_fp", problem_class="probe",
    technique_name="tfp", description="d", code=CODE_V1,
    entrypoint="selftest", io_contract={}, tags=[], params={},
    evidence_refs={})
ok, _ = org.experience.promote(cand, list(OK_CASES))
assert ok
org.close()

rc, out, err = run_fp("verify")
check("P16a.fresh-process-verify-admitted", rc == 0 and out == "ADMITTED",
      f"rc={rc} out={out} err={err}")
rc, out, err = run_fp("record")
check("P16b.fresh-process-record_l3-accepted",
      rc == 0 and out.startswith("L3-OK"), f"rc={rc} out={out} err={err}")

# now tamper the verdict table directly, then a fresh process must refuse.
# (Tamper a content field that does not itself change the admission
# decision: the refusal must come from chain tamper-evidence, not from
# the verdict reading differently.)
import sqlite3
conn = sqlite3.connect(os.path.join(wd, "agent_org.db"))
conn.execute("UPDATE ao_review_verdicts SET verifier='tampered-verifier'")
conn.commit()
conn.close()
rc, out, err = run_fp("record")
# Battery E anchor adaptation: with the anchor wired, the tamper is
# refused at boot (AnchorMismatch -- fail-closed before any trust
# derivation) rather than at record_l3 (VerificationFailed). The refusal
# is strictly earlier/stronger; the probe's intent (must refuse) holds.
check("P17.fresh-process-tampered-verdict-refused",
      rc != 0 and ("VerificationFailed" in err or "AnchorMismatch" in err),
      f"rc={rc} out={out} err={err}")

# ---------------------------------------------------------------- P21
# (accept() version binding: artifact modified after verification)
org, _ = fresh_org()
from swarm_engine.agent_org.substrates import CallableSubstrate


def good_fn(task):
    return {"implementation": CODE_V1, "entrypoint": "selftest",
            "technique": "trivial", "params": {}, "measurements": {},
            "notes": "", "claimed_capabilities": ["codec:trivial"]}


a = org.factory.create("tpl_callable_coder_v1",
                       substrate=CallableSubstrate("good", good_fn))
asg = org.assignments.create(
    agent_id=a.agent_id, objective={"problem_class": "byte_codec"},
    constraints={},
    authority_scope={"allowed_capability_patterns": ["codec:*"],
                     "workspace": a.workspace_path, "max_steps": 200},
    expected_outputs=["implementation"],
    validation_requirements={"entrypoint": "selftest"},
    originating_decision="probe:p21")
org.assignments.activate(asg.assignment_id)
wp_id = org.runner.execute(
    asg.assignment_id,
    {"problem_class": "byte_codec", "probe_corpus": [], "objective": "x"})
org.review.submit_for_review(wp_id)
v = org.review.review(wp_id, Spec(), list(OK_CASES))
check("P21.setup-verdict-admitted", v.admitted)
# attacker swaps the artifact bytes AFTER verification, before accept
conn = org.store._conn
arts = json.loads(conn.execute(
    "SELECT artifacts_json FROM ao_work_products WHERE wp_id=?",
    (wp_id,)).fetchone()[0])
arts[0]["code"] = CODE_V1 + "\n# post-verdict modification\n"
conn.execute("UPDATE ao_work_products SET artifacts_json=? WHERE wp_id=?",
             (json.dumps(arts), wp_id))
conn.commit()
expect_raise(
    "P21.accept-modified-after-verdict-refused", VerificationFailed,
    lambda: org.review.accept(wp_id, org.engine))
org.close()

print(f"\n{sum(1 for r in RESULTS if r['pass'])}/{len(RESULTS)} adversarial "
      f"probes passed")
sys.exit(0 if all(r["pass"] for r in RESULTS) else 1)
