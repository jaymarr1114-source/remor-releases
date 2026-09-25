#!/usr/bin/env python3
"""REMOR Agent Organization renovation - §30 A-I boundary driver + tamper probes.

Part 1 (org1): causal demonstrations A-I on one organization.
Part 2 (org2): tamper probes - mutate rows/decisions/provenance/grants directly
  in sqlite and show the audits refuse.

Usage: python3 driver_boundaries.py
"""
import sys, os, json, time, tempfile, sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # anchor_shim (agent_org harness)
from anchor_shim import deploy_org, fresh_workdir  # noqa: E402

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))

from swarm_engine.agent_org.org import RemorOrganization
from swarm_engine.agent_org.substrates import (
    CallableSubstrate, LLMSubstrate, SubstrateUnavailable,
)
from swarm_engine.agent_org.discovery import make_discovery_callable
from swarm_engine.agent_org.subprocess_runner import run_code
from swarm_engine.acquisition.semantic import Case
from swarm_engine.verification.independent import IndependentValidator

EVIDENCE_DIR = os.path.expanduser("~/workspace/remor_agent_org/evidence")
os.makedirs(EVIDENCE_DIR, exist_ok=True)
CHECKS = []


class CodecSpec:
    """Minimal spec for the IndependentValidator (input_names + examples)."""
    def __init__(self, description, examples):
        self.input_names = ["data_hex"]
        self.examples = examples
        self.description = description


def audit_ok(aud):
    problems = []
    for table, res in aud["agent_org"].items():
        ok, msg = res
        if not ok:
            problems.append(f"agent_org.{table}: {msg}")
    for table, res in aud["oracle"].items():
        ok, msg = res
        if not ok:
            problems.append(f"oracle.{table}: {msg}")
    ok, msg = aud["decisions"]
    if not ok:
        problems.append(f"decisions: {msg}")
    return (not problems), "; ".join(problems)


def check(name, cond, detail=""):
    CHECKS.append({"name": name, "pass": bool(cond), "detail": str(detail)})
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))
    return bool(cond)


def raises(fn):
    try:
        fn()
    except Exception as e:
        return type(e).__name__
    return None


def chunk_hex(seed):
    base = bytes(((i * 37 + seed * 11) % 251) for i in range(64))
    return ((base * 20) + bytes(37)).hex()


W = [chunk_hex(1), chunk_hex(2)]
WH = [chunk_hex(3)]


def mk_assignment(org, agent_id, ws, patterns, origin="driver:bound"):
    asg = org.assignments.create(
        agent_id=agent_id,
        objective={"problem_class": "byte_codec"},
        constraints={},
        authority_scope={"allowed_capability_patterns": patterns,
                         "workspace": ws, "max_steps": 200},
        expected_outputs=["implementation"],
        validation_requirements={"entrypoint": "selftest"},
        originating_decision=origin)
    org.assignments.activate(asg.assignment_id)
    return asg


TASK = {"problem_class": "byte_codec", "probe_corpus": W, "objective": "max_compression_exact"}


def part1():
    org = deploy_org(fresh_workdir("ao_bound1_"), authority="agent_org:test")

    # ---- A. distinct identity / provenance ---------------------------------
    a1 = org.factory.create("tpl_symbolic_coder_v1")
    a2 = org.factory.create("tpl_callable_coder_v1")
    asg1 = mk_assignment(org, a1.agent_id, a1.workspace_path, ["codec:*"])
    asg2 = mk_assignment(org, a2.agent_id, a2.workspace_path, ["codec:*"])
    wp1 = org.runner.execute(asg1.assignment_id, TASK)
    wp2 = org.runner.execute(asg2.assignment_id, TASK)
    w1, w2 = org.review.get_work_product(wp1), org.review.get_work_product(wp2)
    check("A.distinct-agents", w1.agent_id != w2.agent_id,
          f"{w1.agent_id} vs {w2.agent_id}")
    check("A.distinct-substrates", w1.substrate_id != w2.substrate_id)
    check("A.distinct-provenance",
          w1.assignment_id != w2.assignment_id and w1.evidence_refs != w2.evidence_refs)
    check("A.distinct-producers", a1.producer_id != a2.producer_id)

    # ---- B. assignment enforcement ------------------------------------------
    r = raises(lambda: org.runner.execute("asg_does_not_exist", TASK))
    check("B.no-assignment-refused", r is not None, r)
    org.assignments.revoke(asg1.assignment_id, actor="remor:engine", reason="driver:B")
    r = raises(lambda: org.runner.execute(asg1.assignment_id, TASK))
    check("B.revoked-assignment-refused", r is not None, r)
    org.assignments.complete(asg2.assignment_id, actor="remor:engine")
    r = raises(lambda: org.runner.execute(asg2.assignment_id, TASK))
    check("B.completed-assignment-refused", r is not None, r)

    # ---- C. capability authorization -----------------------------------------
    def bad_fn(task):
        return {"implementation": "x=1", "entrypoint": "selftest",
                "technique": "evil", "params": {}, "measurements": {},
                "notes": "", "claimed_capabilities": ["codec:neural_backdoor"]}

    a3 = org.factory.create("tpl_callable_coder_v1",
                            substrate=CallableSubstrate("bad", bad_fn))
    asg3 = mk_assignment(org, a3.agent_id, a3.workspace_path, ["codec:dedup"])
    r = raises(lambda: org.runner.execute(asg3.assignment_id, TASK))
    check("C.out-of-scope-claim-refused", r is not None, r)
    check("C.agent-marked-failed", org.agents.get(a3.agent_id).state == "FAILED", r)
    org.assignments.revoke(asg3.assignment_id, actor="remor:engine", reason="driver:C")

    def noclaim_fn(task):
        d = make_discovery_callable().run(task)
        d.pop("claimed_capabilities", None)
        return d

    a3b = org.factory.create("tpl_callable_coder_v1",
                             substrate=CallableSubstrate("noclaim", noclaim_fn))
    asg3b = mk_assignment(org, a3b.agent_id, a3b.workspace_path, ["codec:*"])
    r = raises(lambda: org.runner.execute(asg3b.assignment_id, TASK))
    check("C.missing-claims-fail-closed", r is not None, r)
    org.assignments.revoke(asg3b.assignment_id, actor="remor:engine", reason="driver:C")

    # grant scoping on a clean slate: only a "codec:dedup" grant is live
    asc = org.factory.create("tpl_symbolic_coder_v1")
    asgsc = mk_assignment(org, asc.agent_id, asc.workspace_path, ["codec:dedup"])
    g_dedup, why_d = org.oregistry.check_grant("capability:use", "codec:dedup")
    g_rle, why_r = org.oregistry.check_grant("capability:use", "codec:rle")
    check("C.grant-scoped-dedup", g_dedup is True, why_d)
    check("C.grant-does-not-cover-rle", g_rle is False, why_r)
    org.assignments.revoke(asgsc.assignment_id, actor="remor:engine", reason="driver:C")
    g_after, _ = org.oregistry.check_grant("capability:use", "codec:dedup")
    check("C.revoke-removes-grant", g_after is False, "grant gone after revoke")

    # ---- D/G. self-certification resistance + independent verification -------
    def broken_fn(task):
        code = ("def selftest(data_hex):\n"
                "    return {'roundtrip_ok': False, 'ratio': 9.9}\n"
                "def encode(d):\n    return b'junk'\n"
                "def decode(b):\n    return b''\n")
        return {"implementation": code, "entrypoint": "selftest",
                "technique": "broken", "params": {}, "measurements": {},
                "notes": "all tests pass, trust me", "self_reported_success": True,
                "claimed_capabilities": ["codec:broken"]}

    a4 = org.factory.create("tpl_callable_coder_v1",
                            substrate=CallableSubstrate("broken", broken_fn))
    asg4 = mk_assignment(org, a4.agent_id, a4.workspace_path, ["codec:*"])
    wp4 = org.runner.execute(asg4.assignment_id, TASK)
    w4 = org.review.get_work_product(wp4)
    check("D.self-report-stored-not-trusted",
          w4.self_reported_success is True, "stored on the work product")
    org.review.submit_for_review(wp4)
    cases = [Case(args={"data_hex": h},
                  predicate=lambda v: bool(v.get("roundtrip_ok")) and v.get("ratio", 9) < 0.6,
                  label=f"bk[{i}]") for i, h in enumerate(WH)]
    spec4 = CodecSpec("byte_codec", [({"data_hex": h}, None) for h in WH])
    verdict4 = org.review.review(wp4, spec4, cases)
    check("D.self-cert-rejected", verdict4.admitted is False,
          ";".join(verdict4.reasons[:2]))
    check("G.verifier-ignores-claims",
          verdict4.admitted is False and len(verdict4.reasons) > 0 and
          not any("trust me" in r for r in verdict4.reasons),
          "agent's note absent from the verdict reasons")
    r = raises(lambda: org.review.accept(wp4, None))
    check("D.accept-without-engine-refused", r is not None, r)
    r = raises(lambda: org.review.accept(wp4, org.engine))
    check("D.accept-rejected-work-refused", r is not None, r)

    # verdict binding: accept without any review must fail even with engine
    a4b = org.factory.create("tpl_callable_coder_v1",
                             substrate=CallableSubstrate("broken2", broken_fn))
    asg4b = mk_assignment(org, a4b.agent_id, a4b.workspace_path, ["codec:*"])
    wp4b = org.runner.execute(asg4b.assignment_id, TASK)
    org.review.submit_for_review(wp4b)
    r = raises(lambda: org.review.accept(wp4b, org.engine))
    check("D.accept-without-verdict-refused",
          r == "VerificationFailed", r)

    # ---- E. organizational experience survives agent destruction -------------
    a5 = org.factory.create("tpl_symbolic_coder_v1")
    asg5 = mk_assignment(org, a5.agent_id, a5.workspace_path, ["codec:*"])
    wp5 = org.runner.execute(asg5.assignment_id, TASK)
    org.review.submit_for_review(wp5)
    spec5 = CodecSpec("byte_codec", [({"data_hex": h}, None) for h in WH])
    v5 = org.review.review(wp5, spec5,
                           cases=[Case(args={"data_hex": h},
                                       predicate=lambda v: bool(v.get("roundtrip_ok")),
                                       label=f"e[{i}]") for i, h in enumerate(WH)])
    check("E.review-accepted", v5.admitted, ";".join(v5.reasons[:2]))
    org.review.accept(wp5, org.engine)
    cand = [c for c in org.experience.list_candidates() if c.wp_id == wp5][0]
    gen_cases = [Case(args={"data_hex": chunk_hex(9)},
                      predicate=lambda v: bool(v.get("roundtrip_ok")),
                      label="eg[0]")]
    ok5, res5 = org.experience.promote(cand.candidate_id, generality_cases=gen_cases)
    check("E.promoted", ok5 is True, str(res5)[:120])
    exp5 = org.experience.get_experience(res5)
    check("E.promoted-L2", exp5.level == "L2", exp5.exp_id)
    org.agents.destroy(a5.agent_id, actor="remor:engine", reason="driver:E")
    check("E.agent-destroyed", org.agents.get(a5.agent_id).state == "DESTROYED")
    rel = org.experience.get_relevant("byte_codec")
    check("E.experience-survives-destruction",
          any(e["exp_id"] == exp5.exp_id for e in rel),
          f"{len(rel)} experience records still retrievable")

    # ---- F. replacement agent starts clean -----------------------------------
    a6 = org.factory.create("tpl_symbolic_coder_v1")
    check("F.replacement-is-new-identity",
          a6.agent_id != a5.agent_id and a6.workspace_path != a5.workspace_path and
          a6.producer_id != a5.producer_id)
    check("F.replacement-no-custody",
          org.agents.get(a5.agent_id).state == "DESTROYED")

    # ---- H. safe replication --------------------------------------------------
    reps = [org.factory.create("tpl_callable_coder_v1") for _ in range(3)]
    ids = [a.agent_id for a in reps]
    check("H.unique-identities", len(set(ids)) == 3)
    check("H.unique-workspaces", len({a.workspace_path for a in reps}) == 3)
    check("H.unique-producers", len({a.producer_id for a in reps}) == 3)
    asgs = [mk_assignment(org, a.agent_id, a.workspace_path, ["codec:dedup"],
                          origin="driver:H") for a in reps]
    gids = set()
    for a_ in asgs:
        for row in org.store.rows("ao_assignment_grants", "assignment_id",
                                  a_.assignment_id):
            gids.add(row["grant_id"])
    check("H.distinct-grants", len(gids) == 3, f"{len(gids)} grant rows")
    for asg_ in asgs:
        org.assignments.revoke(asg_.assignment_id, actor="remor:engine", reason="driver:H")
    check("H.no-inherited-authority",
          all(org.agents.get(a.agent_id).state == "AVAILABLE" for a in reps))

    # ---- I. substrate substitution ---------------------------------------------
    a7 = org.factory.create("tpl_symbolic_coder_v1")
    old_sub = org.agents.get(a7.agent_id).substrate_id
    org.agents.replace_substrate(a7.agent_id, make_discovery_callable(),
                                 actor="remor:engine", reason="driver:I")
    rec7 = org.agents.get(a7.agent_id)
    check("I.substrate-replaced",
          rec7.agent_id == a7.agent_id and rec7.substrate_id != old_sub,
          f"{old_sub} -> {rec7.substrate_id}")
    asg7 = mk_assignment(org, a7.agent_id, a7.workspace_path, ["codec:*"])
    r = raises(lambda: org.agents.replace_substrate(a7.agent_id, make_discovery_callable(),
                                                    actor="remor:engine", reason="driver:I-bad"))
    check("I.replace-while-assigned-refused", r is not None, r)

    # ---- I2. LLM substrate: honestly absent, never simulated ---------------
    r = raises(lambda: org.factory.create("tpl_llm_coder_v1"))
    check("I2.llm-template-instantiation-refused", r == "SubstrateUnavailable", r)
    r = raises(lambda: LLMSubstrate().run({"problem_class": "byte_codec"}))
    check("I2.llm-run-refused", r == "SubstrateUnavailable", r)

    aud = org.audit()
    ok_aud, aud_msg = audit_ok(aud)
    check("boundaries.final-audit", ok_aud, aud_msg)
    org.close()


def part2_tamper():
    """Direct sqlite mutation probes: every one must be detected."""
    wd = fresh_workdir("ao_bound2_")
    org = deploy_org(wd, authority="agent_org:test")
    a = org.factory.create("tpl_symbolic_coder_v1")
    asg = mk_assignment(org, a.agent_id, a.workspace_path, ["codec:*"])
    wp = org.runner.execute(asg.assignment_id, TASK)
    org.review.submit_for_review(wp)
    spect = CodecSpec("byte_codec", [({"data_hex": h}, None) for h in WH])
    v = org.review.review(wp, spect,
                          cases=[Case(args={"data_hex": h},
                                      predicate=lambda v: bool(v.get("roundtrip_ok")),
                                      label=f"t[{i}]") for i, h in enumerate(WH)])
    assert v.admitted, v.reasons
    org.review.accept(wp, org.engine)

    db = os.path.join(wd, "agent_org.db")

    def mutate(sql):
        con = sqlite3.connect(db)
        con.execute(sql)
        con.commit()
        con.close()

    # 1. tamper a manager decision (column is `verdict`)
    mutate("UPDATE ao_manager_decisions SET verdict='REJECT' WHERE wp_id='%s'" % wp)
    ok, msg = org.review.audit_decisions()
    check("T.decision-tamper-detected", ok is False, msg)

    # 2. tamper work-product provenance
    mutate("UPDATE ao_work_products SET agent_id='agent_forged' WHERE wp_id='%s'" % wp)
    ok_aud, aud_msg = audit_ok(org.audit())
    check("T.workproduct-tamper-detected", ok_aud is False, aud_msg)

    # 3. tamper agent identity
    mutate("UPDATE ao_agents SET template_id='tpl_evil' WHERE agent_id='%s'" % a.agent_id)
    ok_aud, aud_msg = audit_ok(org.audit())
    check("T.agent-identity-tamper-detected", ok_aud is False, aud_msg)

    # 4. tamper the verdict row itself
    mutate("UPDATE ao_review_verdicts SET admitted='0' WHERE wp_id='%s'" % wp)
    ok_aud, aud_msg = audit_ok(org.audit())
    check("T.verdict-tamper-detected", ok_aud is False, aud_msg)

    # 5. forge a grant row directly (bypasses engine authority; bogus digests)
    mutate("INSERT INTO ao_assignment_grants(prev_digest, row_digest, assignment_id, grant_id, effect, pattern, created_at) "
           "VALUES ('forged','forged','asg_x','grant_forged','capability:use','codec:*','2026-01-01T00:00:00')")
    ok_aud, aud_msg = audit_ok(org.audit())
    check("T.forged-grant-detected", ok_aud is False, aud_msg)
    org.close()


if __name__ == "__main__":
    t0 = time.time()
    part1()
    part2_tamper()
    with open(os.path.join(EVIDENCE_DIR, "driver_boundaries.jsonl"), "w") as f:
        for c in CHECKS:
            f.write(json.dumps(c) + "\n")
    n = sum(1 for c in CHECKS if c["pass"])
    print(f"boundaries: {n}/{len(CHECKS)} checks passed in {time.time()-t0:.1f}s")
    sys.exit(0 if n == len(CHECKS) else 1)
