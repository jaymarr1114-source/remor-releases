#!/usr/bin/env python3
"""REMOR Agent Organization renovation - independent mission driver.

Phase 1 (default): runs the full §31 end-to-end chain plus §30 J/K in one process:
  1.  boot a fresh organization (isolated work dir)
  2.  create Agent A (symbolic substrate), assign byte-codec problem W1 (chunk repeats)
  3.  A executes -> work product X
  4.  independent review of X (held-out + adversarial cases, oracle-bound) -> ACCEPT
  5.  promote X -> L2 organizational experience expX
  6.  DESTROY A
  7.  create Agent B (callable substrate) with no access to A's state
  8.  B's assignment carries only REMOR's organizational experience [expX]
  9.  B executes on W2 (byte runs) measuring expX as a baseline -> work product Y
  10. independent review of Y -> ACCEPT; promote -> L2 expY
  11. RelationshipFinder measures X+Y complementarity on mixed corpus W3
  12. OrgSynthesizer synthesizes materially distinct Z (RMZ1 framing)
  13. independent verification of Z on fresh W3 variants + adversarial cases
  14. admission: ManagerDecision ACCEPT + trust TRUSTED + L3 experience record
  15. retire B; seal evidence; print work dir for phase 2

Phase 2 (--phase=rehydrate --workdir=DIR): fresh OS process rehydration (§30 L):
  - boot on the same work dir (new engine token), audit everything
  - load the L3 Z record, execute it on fresh probe data via isolated subprocess
  - assert A DESTROYED, B RETIRED, Z TRUSTED, provenance chain intact

Usage:
  python3 driver_end_to_end.py [--workdir DIR] [--phase rehydrate]
"""
import sys, os, json, time, tempfile, subprocess, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # anchor_shim (battery E harness)
from anchor_shim import (  # noqa: E402
    fresh_workdir, deploy_org, attach_org, anchor_paths_for)

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))

from swarm_engine.agent_org.org import RemorOrganization
from swarm_engine.agent_org.substrates import CallableSubstrate
from swarm_engine.agent_org.discovery import make_discovery_callable
from swarm_engine.agent_org.subprocess_runner import run_code
from swarm_engine.agent_org.synthesis import token_similarity
from swarm_engine.agent_org.store import digest as code_digest_of
from swarm_engine.acquisition.semantic import Case

EVIDENCE_DIR = HERE  # battery E: all outputs stay in scratch/batteryE

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


# ---------------------------------------------------------------- corpora
def chunk_bytes(seed, n_chunks=25, chunk_len=64):
    base = bytes(((i * 37 + seed * 11) % 251) for i in range(chunk_len))
    tail = bytes(((i * 13 + seed * 7) % 251) for i in range(37))
    return (base * n_chunks + tail).hex()


def runs_bytes(seed, scale=1):
    return ((b"\x41" * (400 * scale) + b"\x42" * (300 * scale) +
             b"\x43" * (200 * scale) + bytes((i + seed) % 256 for i in range(100))).hex())


def mixed_bytes(seed):
    chunk = bytes([0x58 + (seed % 3)]) * 24 + bytes([0x61 + (seed % 5)]) * 24
    return ((chunk * 20) + bytes([0x5A]) * 60).hex()


W1_PROBE = [chunk_bytes(1), chunk_bytes(2)]
W1_HOLD = [chunk_bytes(3), chunk_bytes(4)]
W1_GEN = [chunk_bytes(5)]
W2_PROBE = [runs_bytes(1), runs_bytes(2)]
W2_HOLD = [runs_bytes(3)]
W2_GEN = [runs_bytes(4)]
W3_PROBE = [mixed_bytes(1), mixed_bytes(2)]
W3_FRESH = [mixed_bytes(7), mixed_bytes(8)]
ADVERSARIAL = ["", "00", "ff", bytes((i * 31) % 256 for i in range(512)).hex()]


def codec_cases(corpus, ratio_cap, label):
    cases = []
    for i, h in enumerate(corpus):
        cases.append(Case(
            args={"data_hex": h},
            predicate=(lambda cap: (lambda v: bool(v.get("roundtrip_ok")) and v.get("ratio", 9) < cap))(ratio_cap),
            label=f"{label}[{i}]"))
    for i, h in enumerate(ADVERSARIAL):
        cases.append(Case(
            args={"data_hex": h},
            predicate=lambda v: bool(v.get("roundtrip_ok")),
            label=f"{label}-adv[{i}]", kind="edge"))
    return cases


def exec_code_local(code):
    ns = {}
    exec(compile(code, "<codec>", "exec"), ns)
    return ns
    ns = {}
    exec(compile(code, "<codec>", "exec"), ns)
    return ns


def consuming_substrate(prior_exp):
    """Driver-local substrate: measures the prior organizational experience as a
    real baseline on the probe corpus, then runs the honest grid search. This
    makes B's reuse of X observable (baseline measurements in the result)."""
    prior_code = prior_exp["code"] if isinstance(prior_exp, dict) else prior_exp.code
    prior_name = prior_exp["technique_name"] if isinstance(prior_exp, dict) else prior_exp.technique_name
    ns = exec_code_local(prior_code)
    st = ns["selftest"]

    def fn(task):
        corpus = task["probe_corpus"]
        baseline = []
        for h in corpus:
            r = st(h)
            baseline.append({"roundtrip_ok": r["roundtrip_ok"], "ratio": r["ratio"]})
        winner = make_discovery_callable().run(task)
        notes = (winner.get("notes", "") +
                 f" | baseline[{prior_name}] mean_ratio={sum(b['ratio'] for b in baseline)/len(baseline):.3f}")
        winner["notes"] = notes
        winner["baseline_technique"] = prior_name
        winner["baseline_measurements"] = baseline
        return winner

    return CallableSubstrate("baseline_consuming", fn)


def make_assignment(org, agent_id, ws, origin):
    asg = org.assignments.create(
        agent_id=agent_id,
        objective={"problem_class": "byte_codec", "goal": "discover compressive codec"},
        constraints={"max_corpus_bytes": 200000},
        authority_scope={"allowed_capability_patterns": ["codec:*"],
                         "workspace": ws, "max_steps": 500},
        expected_outputs=["implementation"],
        validation_requirements={"entrypoint": "selftest"},
        originating_decision=origin)
    org.assignments.activate(asg.assignment_id)
    return asg


def anchor_cli_verify(workdir, label):
    """Run the REAL anchor_admin verify CLI against this org's DBs."""
    org_db = os.path.join(workdir, "agent_org.db")
    oracle_db = os.path.join(workdir, "oracle.db")
    p = subprocess.run(
        [sys.executable, "-m", "swarm_engine.governance.anchor_admin",
         "--org-db", org_db, "--oracle-db", oracle_db, "verify"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": PYLIB})
    ok = p.returncode == 0
    msg = (p.stdout or "").strip() or (p.stderr or "").strip()
    print(f"ANCHOR-CLI-VERIFY[{label}] ok={ok} :: {msg[:160]}")
    return ok, msg


def phase1(workdir):
    t0 = time.time()
    # Battery E: explicit anchor deployment (genesis) before any verdict.
    org = deploy_org(workdir, authority="batteryE:e2e-deploy")
    journal, key, _ = anchor_paths_for(workdir)
    print(f"PHASE=A-deploy anchor journal={journal} records={org.anchor.record_count}")

    # -- steps 2-4: Agent A discovers X -------------------------------------
    A = org.factory.create("tpl_symbolic_coder_v1")
    check("e2e.A-created", A.state == "AVAILABLE", A.agent_id)
    asgA = make_assignment(org, A.agent_id, A.workspace_path, "driver:e2e")
    taskA = {"problem_class": "byte_codec", "probe_corpus": W1_PROBE,
             "objective": "max_compression_exact"}
    wpA = org.runner.execute(asgA.assignment_id, taskA)
    wpa = org.review.get_work_product(wpA)
    techX = wpa.outputs["technique"] if isinstance(wpa.outputs, dict) else None
    check("e2e.A-executed", wpa.agent_id == A.agent_id, f"technique={techX}")

    # -- step: independent review of X ---------------------------------------
    org.review.submit_for_review(wpA)
    casesX = codec_cases(W1_HOLD, 0.5, "X")
    specX = CodecSpec("byte_codec chunk repeats",
                      [({"data_hex": h}, None) for h in W1_HOLD])
    verdictX = org.review.review(wpA, specX, cases=casesX)
    check("e2e.X-reviewed-accepted", verdictX.admitted, ";".join(verdictX.reasons[:3]))
    check("J.independent-verification",
          verdictX.admitted and len((verdictX.evidence or {}).get("findings", [])) > 0,
          f"findings={len((verdictX.evidence or {}).get('findings', []))}")

    # -- step: promote X to L2 ----------------------------------------------
    # The generality gauntlet is engine-executed now: promote() runs the
    # ReviewBoard's verify_generality (verdict persisted, trust derived
    # from the stored row). No caller validator -- the driver supplies only
    # the cases (the question), never the oracle (the judge).
    org.review.accept(wpA, org.engine)
    cands = org.experience.list_candidates()
    candA = [c for c in cands if c.wp_id == wpA][0]
    okA, resA = org.experience.promote(
        candA.candidate_id, generality_cases=codec_cases(W1_GEN, 0.6, "Xg"))
    check("e2e.X-promoted", okA is True, str(resA)[:120])
    expX = org.experience.get_experience(resA)
    check("e2e.X-promoted-L2", expX.level == "L2", expX.exp_id)
    print(f"PHASE=X-L2 anchor records={org.anchor.record_count}")

    # -- step 7: destroy A ----------------------------------------------------
    org.agents.destroy(A.agent_id, actor="remor:engine", reason="e2e: step 7")
    check("e2e.A-destroyed", org.agents.get(A.agent_id).state == "DESTROYED")
    print(f"PHASE=A-destroyed anchor records={org.anchor.record_count}")
    check("E.experience-survives", any(e.exp_id == expX.exp_id
                                       for e in org.experience.list_experiences("byte_codec")),
          "expX still retrievable after A destroyed")

    # -- steps 8-11: Agent B reuses organizational experience -----------------
    B = org.factory.create("tpl_callable_coder_v1", substrate=consuming_substrate(expX))
    check("e2e.B-created-distinct", B.agent_id != A.agent_id and B.workspace_path != A.workspace_path)
    asgB = make_assignment(org, B.agent_id, B.workspace_path, "driver:e2e")
    expX_dict = {"technique_name": expX.technique_name, "code": expX.code,
                 "entrypoint": expX.entrypoint}
    taskB = {"problem_class": "byte_codec", "probe_corpus": W2_PROBE,
             "objective": "max_compression_exact", "prior_experience": [expX_dict]}
    wpB = org.runner.execute(asgB.assignment_id, taskB)
    wpb = org.review.get_work_product(wpB)
    techY = wpb.outputs["technique"] if isinstance(wpb.outputs, dict) else None
    check("e2e.B-executed", wpb.agent_id == B.agent_id, f"technique={techY}")
    check("F.replacement-reuses-experience",
          wpb.outputs.get("baseline_technique") == expX.technique_name if isinstance(wpb.outputs, dict) else False,
          f"B measured baseline {expX.technique_name}")

    # -- steps 12-13: review + promote Y --------------------------------------
    org.review.submit_for_review(wpB)
    specY = CodecSpec("byte_codec byte runs",
                      [({"data_hex": h}, None) for h in W2_HOLD])
    verdictY = org.review.review(wpB, specY, cases=codec_cases(W2_HOLD, 0.5, "Y"))
    check("e2e.Y-reviewed-accepted", verdictY.admitted, ";".join(verdictY.reasons[:3]))
    org.review.accept(wpB, org.engine)
    cands = org.experience.list_candidates()
    candB = [c for c in cands if c.wp_id == wpB][0]
    okB, resB = org.experience.promote(
        candB.candidate_id, generality_cases=codec_cases(W2_GEN, 0.6, "Yg"))
    check("e2e.Y-promoted", okB is True, str(resB)[:120])
    expY = org.experience.get_experience(resB)
    check("e2e.Y-promoted-L2", expY.level == "L2" and expY.exp_id != expX.exp_id,
          f"X={expX.technique_name} Y={expY.technique_name}")
    check("e2e.XY-distinct", expX.technique_name != expY.technique_name,
          f"{expX.technique_name} vs {expY.technique_name}")
    print(f"PHASE=Y-L2 anchor records={org.anchor.record_count}")

    # -- step 14: measured X+Y relationship -----------------------------------
    # RelationshipFinder.find(exp_x, exp_y, runner) measures on its own
    # fixed mixed corpus; returns a Relationship or None.
    rel = org.relationships.find(expX, expY, run_code)
    check("e2e.relationship-found", rel is not None,
          f"order={rel.order}" if rel else "no complementary relationship measured")
    if rel is not None:
        ev = rel.evidence
        rx, ry, rzp = ev["x_ratio"], ev["y_ratio"], ev["xy_ratio"]
        check("e2e.relationship-measured",
              bool(ev.get("strictly_better")) and rzp < min(rx, ry),
              f"order={rel.order} rx={rx:.3f} ry={ry:.3f} rxy={rzp:.3f}")
    else:
        ev = {}

    if rel is None:
        # Chain stops here: no measured complementarity, no synthesis.
        org.close()
        with open(os.path.join(EVIDENCE_DIR, "driver_e2e_phase1.jsonl"), "w") as f:
            for c in CHECKS:
                f.write(json.dumps(c) + "\n")
        print(f"phase1: {sum(1 for c in CHECKS if c['pass'])}/{len(CHECKS)} checks passed")
        return False

    # -- step 15: REMOR synthesizes distinct Z ---------------------------------
    code_z, entry_z, rel_ev = org.synthesizer.synthesize(rel, org.engine)
    check("e2e.Z-synthesized", entry_z == "selftest" and "RMZ1" in code_z,
          f"{len(code_z)} chars")
    sim_x = token_similarity(code_z, expX.code)
    sim_y = token_similarity(code_z, expY.code)
    check("K.distinct-artifact",
          code_z != expX.code and code_z != expY.code and "RMZ1" in code_z
          and sim_x < 0.95 and sim_y < 0.95,
          f"own RMZ1 framing; token-sim vs X={sim_x:.3f} vs Y={sim_y:.3f}")

    # -- steps 16-17: independent verification of Z ---------------------------
    # Engine-executed: verify_artifact persists the authoritative verdict
    # row (artifact_kind="synthesis"); record_l3() below derives trust
    # ONLY from that stored row. The driver supplies the cases (the
    # question) -- never the verdict (the answer).
    z_cases = codec_cases(W3_FRESH, 0.5, "Z")
    specZ = CodecSpec("fused codec", [({"data_hex": h}, None) for h in W3_FRESH])
    verdictZ = org.review.verify_artifact(
        code_z, entry_z, specZ, z_cases, artifact_ref="fused_rmz1")
    check("e2e.Z-verified", verdictZ.admitted, ";".join(verdictZ.reasons[:3]))
    check("K.verified-before-trust",
          verdictZ.admitted and len((verdictZ.evidence or {}).get("findings", [])) > 0,
          f"findings={len((verdictZ.evidence or {}).get('findings', []))}")
    stored_z = org.review.latest_artifact_verdict(
        code_digest_of(code_z), "synthesis")
    check("K.verdict-stored",
          stored_z is not None and stored_z.get("admitted") == "1",
          f"execution_id={stored_z.get('execution_id') if stored_z else None}")
    print(f"PHASE=Z-verdict-anchored records={org.anchor.record_count} "
          f"execution_id={stored_z.get('execution_id') if stored_z else None}")

    # -- step 18: admission ----------------------------------------------------
    org.engine.transition_trust("artifact:codec_z", None, "TRUSTED",
                                reason="e2e: independent verification passed")
    expZ = org.experience.record_l3(
        technique_name="fused_rmz1", code=code_z, entrypoint=entry_z,
        problem_class="byte_codec", tags=["synthesized", "fused"],
        io_contract={"input": "bytes", "output": "bytes"},
        derived_from=[expX.exp_id, expY.exp_id],
        params=rel_ev.get("z_params", {}),
        engine=org.engine)
    check("e2e.Z-admitted-L3", expZ.level == "L3" and
          set(expZ.derived_from) == {expX.exp_id, expY.exp_id}, expZ.exp_id)
    check("K.l3-bound-to-verdict",
          stored_z is not None and
          expZ.validation_evidence.get("execution_id") ==
          stored_z.get("execution_id") and
          expZ.validation_evidence.get("verdict") == "admitted",
          f"L3 trust derived from stored verdict "
          f"{expZ.validation_evidence.get('execution_id')}")
    print(f"PHASE=Z-admitted-L3-TRUSTED records={org.anchor.record_count} "
          f"l3={expZ.exp_id}")

    # -- retire B; seal --------------------------------------------------------
    org.agents.transition(B.agent_id, "RETIRED", actor="remor:engine", reason="e2e: done")
    ok_aud, aud_msg = audit_ok(org.audit())
    check("e2e.final-audit", ok_aud, aud_msg)
    cli_ok, cli_msg = anchor_cli_verify(workdir, "phase1-end")
    if not cli_ok:
        # hard gate (not a CHECKS entry): the inherited count stays 25/25
        raise SystemExit(f"anchor CLI verify failed at phase1 end: {cli_msg}")
    org.close()

    meta = {"workdir": workdir, "A": A.agent_id, "B": B.agent_id,
            "expX": expX.exp_id, "expY": expY.exp_id, "expZ": expZ.exp_id,
            "techX": expX.technique_name, "techY": expY.technique_name,
            "elapsed_s": round(time.time() - t0, 1)}
    with open(os.path.join(EVIDENCE_DIR, "e2e_meta.json"), "w") as f:
        json.dump(meta, f, indent=2)
    with open(os.path.join(EVIDENCE_DIR, "driver_e2e_phase1.jsonl"), "w") as f:
        for c in CHECKS:
            f.write(json.dumps(c) + "\n")
    print("WORKDIR=" + workdir)
    print(f"phase1: {sum(1 for c in CHECKS if c['pass'])}/{len(CHECKS)} checks passed")
    return all(c["pass"] for c in CHECKS)


def phase2(workdir):
    # Battery E: fresh OS process. First the external CLI verifies the
    # journal against the DBs; then attach (boot re-verifies in-process).
    cli_ok, cli_msg = anchor_cli_verify(workdir, "phase2-preboot")
    if not cli_ok:
        raise SystemExit(f"anchor CLI verify failed before phase2 boot: {cli_msg}")
    with open(os.path.join(EVIDENCE_DIR, "e2e_meta.json")) as f:
        meta = json.load(f)
    org = attach_org(workdir)  # fresh process, fresh engine token
    print(f"PHASE=rehydrate-boot anchor records={org.anchor.record_count}")
    ok_aud, aud_msg = audit_ok(org.audit())
    check("L.rehydrate-audit", ok_aud, aud_msg)

    exps = {e.exp_id: e for e in org.experience.list_experiences("byte_codec")}
    check("L.X-preserved", meta["expX"] in exps and exps[meta["expX"]].level == "L2")
    check("L.Y-preserved", meta["expY"] in exps and exps[meta["expY"]].level == "L2")
    z = exps.get(meta["expZ"])
    check("L.Z-preserved", z is not None and z.level == "L3")
    check("L.Z-provenance",
          z is not None and set(z.derived_from) == {meta["expX"], meta["expY"]},
          str(z.derived_from) if z else "")

    # execute Z on fresh probe data in an isolated subprocess
    fresh = [mixed_bytes(21), chunk_bytes(22), runs_bytes(23)]
    results = []
    for h in fresh:
        ok, res, err = run_code(z.code, z.entrypoint,
                                [Case(args={"data_hex": h}, predicate=lambda v: True, label="lz")])
        assert ok, err
        results.append(res[0]["value"])
    check("L.Z-runs-fresh", all(r["roundtrip_ok"] for r in results),
          f"ratios={[round(r['ratio'],3) for r in results]}")
    trust = org.oregistry.current_trust("artifact:codec_z")
    check("L.Z-trust", trust == "TRUSTED", trust)

    check("L.A-destroyed", org.agents.get(meta["A"]).state == "DESTROYED")
    check("L.B-retired", org.agents.get(meta["B"]).state == "RETIRED")
    cli_ok2, cli_msg2 = anchor_cli_verify(workdir, "phase2-end")
    if not cli_ok2:
        raise SystemExit(f"anchor CLI verify failed at phase2 end: {cli_msg2}")
    print(f"PHASE=rehydrate-done trust=TRUSTED anchor records={org.anchor.record_count}")
    org.close()

    with open(os.path.join(EVIDENCE_DIR, "driver_e2e_phase2.jsonl"), "w") as f:
        for c in CHECKS:
            f.write(json.dumps(c) + "\n")
    print(f"phase2: {sum(1 for c in CHECKS if c['pass'])}/{len(CHECKS)} checks passed")
    return all(c["pass"] for c in CHECKS)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--phase", default="phase1", choices=["phase1", "rehydrate"])
    args = ap.parse_args()
    if args.phase == "phase1":
        wd = args.workdir or fresh_workdir("ao_e2e_")
        ok = phase1(wd)
    else:
        ok = phase2(args.workdir)
    sys.exit(0 if ok else 1)
