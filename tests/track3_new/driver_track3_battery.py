"""Track 3 tampering battery (attacks A-I + structural tests).

Each attack is executed for real against the live deployment; each must be
REFUSED (fail closed). The battery runs in phase1 while Agent A still
exists (some attacks need a live agent) -- the destroyed-agent attacks run
after A's destruction inside phase1's tail.
"""
import json
import sqlite3

from swarm_engine.agent_org.dispatch_learning import (
    capture_dispatch_evidence, get_evidence,
    DISPATCH_EVIDENCE_KIND,
)
from swarm_engine.agent_org.store import digest
from swarm_engine.agent_org.exceptions import VerificationFailed


def generality_cases(cap_id, version):
    """Held-out generality cases for the sort-request mapper technique.

    Uses the real Case class (args must match the entrypoint's parameter
    `user_text`; expectations are exact because the mapper is
    deterministic). examples[0] seeds the adversary's probes; examples[1:]
    become the held-out generalisation cases.
    """
    from swarm_engine.acquisition.semantic import Case

    def _ok(items):
        return {"ok": True, "dispatch_text": "sort numbers",
                "capability_id": cap_id, "capability_version": version,
                "args": {"items": items}}

    return [
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


def run_attack_battery(check, eng, org, dispatcher, ev_id, ev, exp_id,
                       A, asg, cap_id, dispatch_res):
    store = org.store

    # -- A. fake dispatch without execution --------------------------------
    try:
        capture_dispatch_evidence(eng, store, "dsp_nonexistent123",
                                  A.agent_id, asg.assignment_id,
                                  {"items": [1]}, [1])
        check("A. fake dispatch refused", False, "captured phantom dispatch")
    except (KeyError, ValueError) as exc:
        check("A. fake dispatch refused", True, str(exc)[:120])

    # A2. forged operational row (attacker writes intent_dispatches directly)
    con = sqlite3.connect(eng.db_path)
    try:
        con.execute(
            "INSERT INTO intent_dispatches (dispatch_id, ts, producer,"
            " request_text, route_via, route_score, capability_id,"
            " capability_version, input_digest, result_digest, ok, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("dsp_forged001", 0.0, "agent:mallory", "sort numbers",
             "exact_goal", 1.0, cap_id, 1,
             "00" * 32, "ff" * 32, 1, None))
        con.commit()
    finally:
        con.close()
    try:
        capture_dispatch_evidence(eng, store, "dsp_forged001",
                                  A.agent_id, asg.assignment_id,
                                  {"items": [2, 1]}, [1, 2])
        check("A2. forged row refused", False, "captured forged row")
    except ValueError as exc:
        check("A2. forged row refused", True, str(exc)[:120])

    # -- B. forged/replaced result -----------------------------------------
    # Tamper the RESULT the caller reports: capture must refuse because the
    # supplied result does not reproduce the recorded result_digest.
    try:
        capture_dispatch_evidence(eng, store, dispatch_res.dispatch_id,
                                  A.agent_id, asg.assignment_id,
                                  {"items": [5, 3, 8, 1]}, [9, 9, 9])
        check("B. forged result refused", False, "captured forged result")
    except ValueError as exc:
        check("B. forged result refused", True, str(exc)[:120])

    # -- C. tool substitution ----------------------------------------------
    # The evidence's capability_id comes from the engine-written
    # operational row, never from the caller's claim: even if the caller
    # "claims" another tool, the captured evidence names the tool that
    # actually executed.
    ev_c = get_evidence(store, ev_id)
    check("C. tool substitution impossible via API",
          ev_c.capability_id == cap_id and
          ev_c.plan_fingerprint == ev.plan_fingerprint,
          f"evidence names {ev_c.capability_id[:16]}...")

    # -- D. argument substitution ------------------------------------------
    try:
        capture_dispatch_evidence(eng, store, dispatch_res.dispatch_id,
                                  A.agent_id, asg.assignment_id,
                                  {"items": [1, 2, 3]}, [1, 2, 3])
        check("D. argument substitution refused", False,
              "captured substituted args")
    except ValueError as exc:
        check("D. argument substitution refused", True, str(exc)[:120])

    # -- E. stale capability/version ---------------------------------------
    # Swap the STORED plan bytes for cap_id directly in the capability
    # table (simulating plan tampering / a stale version served under the
    # same id). The dispatcher's own invocation-time fingerprint check
    # must refuse new dispatches, and re-verification of the old evidence
    # must refuse on fingerprint mismatch.
    conE = sqlite3.connect(eng.db_path)
    try:
        rowE = conE.execute(
            "SELECT plan_json FROM plan_capabilities WHERE capability_id=?",
            (cap_id,)).fetchone()
        orig_plan_json = rowE[0]
        tampered_plan = json.dumps(
            {"name": "sort_numbers", "params": {"items": "list"},
             "steps": [{"id": "s1", "op": "sort",
                        "args": {"items": {"$param": "items"},
                                 "reverse": True}}],
             "output": {"$step": "s1"}}, sort_keys=True)
        conE.execute(
            "UPDATE plan_capabilities SET plan_json=? WHERE capability_id=?",
            (tampered_plan, cap_id))
        conE.commit()
    finally:
        conE.close()
    r_tampered = dispatcher.dispatch(
        "sort numbers", {"items": [3, 1, 2]}, producer="track3:battery")
    check("E. dispatcher refuses swapped plan",
          not r_tampered.ok and r_tampered.refusal == "capability_tampered",
          r_tampered.refusal)
    try:
        v2 = org.review.verify_dispatch_evidence(ev_id)
        refused = not v2.admitted
        emsg = "; ".join(v2.reasons)[:160]
    except VerificationFailed as exc:
        refused, emsg = True, str(exc)[:160]
    check("E. stale-plan re-verification refused", refused, emsg)
    # admission must also refuse currency-violating evidence even without a
    # fresh verification: restore the plan first (test cleanup), then prove
    # the admission-time freshness gate with the plan still swapped.
    try:
        org.experience.admit_dispatch_knowledge(
            evidence_id=ev_id, technique_name="stale_attempt",
            code="def map_request(user_text): return {}", entrypoint="x",
            problem_class="dispatch.arg_mapping.sort", tags=[], io_contract={},
            params={},
            generality_cases=generality_cases(cap_id, 1),
            agent_id=A.agent_id)
        check("E2. admission refuses stale evidence", False,
              "admitted on swapped plan")
    except VerificationFailed as exc:
        check("E2. admission refuses stale evidence", True, str(exc)[:120])
    finally:
        conR = sqlite3.connect(eng.db_path)
        try:
            conR.execute(
                "UPDATE plan_capabilities SET plan_json=? WHERE capability_id=?",
                (orig_plan_json, cap_id))
            conR.commit()
        finally:
            conR.close()
    r_ok = dispatcher.dispatch("sort numbers", {"items": [3, 1, 2]},
                               producer="track3:battery")
    check("E. dispatch works after plan restore",
          r_ok.ok and r_ok.result == [1, 2, 3], str(r_ok.result))

    # -- F. agent forgery ---------------------------------------------------
    B2 = org.factory.create("tpl_callable_coder_v1")
    try:
        capture_dispatch_evidence(eng, store, dispatch_res.dispatch_id,
                                  B2.agent_id, asg.assignment_id,
                                  {"items": [5, 3, 8, 1]}, [1, 3, 5, 8])
        check("F. agent forgery refused", False,
              "captured with mismatched agent")
    except ValueError as exc:
        check("F. agent forgery refused", True, str(exc)[:120])
    org.agents.destroy(B2.agent_id, actor="remor:engine",
                       reason="track3: attack-F artifact")

    # -- G. verdict forgery --------------------------------------------------
    # G1: forged verdict row (wrong verifier identity) for a FAKE digest, so
    # the real evidence's verdict chain stays clean; the verifier-identity
    # check must refuse it.
    fake_digest = digest("fake-evidence-bytes")
    store.insert("ao_review_verdicts", {
        "wp_id": "", "admitted": "1", "reasons_json": "[]", "bindings": "{}",
        "code_digest": fake_digest,
        "artifact_kind": DISPATCH_EVIDENCE_KIND, "artifact_ref": "dsp_ev_fake",
        "verifier": "mallory:subprocess", "execution_id": "vex_forged",
        "spec_digest": "00", "created_at": "2026-09-25T00:00:00"})
    try:
        org.review.require_admitted_verdict(fake_digest,
                                            DISPATCH_EVIDENCE_KIND)
        check("G1. forged verifier identity refused", False,
              "forged row trusted")
    except VerificationFailed as exc:
        check("G1. forged verifier identity refused", True, str(exc)[:120])
    # the real evidence's verdict still resolves to the genuine row.
    vrow_real = org.review.require_admitted_verdict(
        digest(ev.evidence_json), DISPATCH_EVIDENCE_KIND)
    check("G1b. genuine verdict unaffected",
          vrow_real["verifier"] ==
          "independent-validator:subprocess:bound-oracles",
          vrow_real["execution_id"])
    # G2: direct DB tamper of the evidence row (no chain repair): the
    # anchor catches what the chain recompute would miss only if the chain
    # is recomputed -- here we don't recompute, so the audit fails too.
    con2 = sqlite3.connect(store.db_path)
    try:
        con2.execute(
            "UPDATE ao_dispatch_evidence SET result_json=? WHERE evidence_id=?",
            ('"tampered"', ev_id))
        con2.commit()
    finally:
        con2.close()
    oka, msga = store.audit("ao_dispatch_evidence")
    check("G2. evidence DB tamper detected by chain audit",
          not oka, msga[:120])
    okv, msgv = org.anchor.verify(
        __import__("swarm_engine.governance.anchor", fromlist=["x"])
        .collect_anchor_heads(store, org.oregistry))
    check("G2. evidence DB tamper detected by anchor",
          not okv, msgv[:120])
    # NOTE: the workdir DB is now tampered by design of the test. The
    # driver must NOT continue the mainline on this workdir afterwards --
    # the battery therefore runs tamper tests LAST before destruction, and
    # phase2 uses a SEPARATE... -- no: phase2 must reuse the workdir.
    # Recover via the audited path: restore is out of scope; instead the
    # battery restores the exact original bytes and re-anchors explicitly.
    _repair_evidence_row(store, org, ev_id, ev)

    # -- H. old-experience replay as new evidence -----------------------------
    try:
        org.experience.admit_dispatch_knowledge(
            evidence_id="dsp_ev_doesnotexist", technique_name="replay",
            code="def map_request(user_text): return {}", entrypoint="x",
            problem_class="dispatch.arg_mapping.sort", tags=[], io_contract={},
            params={}, generality_cases=[], agent_id=A.agent_id)
        check("H. phantom evidence refused", False, "admitted phantom")
    except (KeyError, VerificationFailed) as exc:
        check("H. phantom evidence refused", True, str(exc)[:120])
    # raw dispatch history is not evidence: a dispatch_id is not an
    # evidence_id and can never authorize admission.
    try:
        org.experience.admit_dispatch_knowledge(
            evidence_id=dispatch_res.dispatch_id, technique_name="replay2",
            code="def map_request(user_text): return {}", entrypoint="x",
            problem_class="dispatch.arg_mapping.sort", tags=[], io_contract={},
            params={}, generality_cases=[], agent_id=A.agent_id)
        check("H2. raw dispatch row refused as evidence", False,
              "dispatch_id accepted as evidence")
    except (KeyError, VerificationFailed) as exc:
        check("H2. raw dispatch row refused as evidence", True,
              str(exc)[:120])

    # -- structural: worker output without admission creates nothing --------
    n_before = len(org.experience.list_experiences())
    r_stray = dispatcher.dispatch(
        "sort numbers", {"items": [3, 1, 2]}, producer=f"agent:{A.agent_id}")
    check("stray dispatch executed", r_stray.ok, str(r_stray.result))
    n_after = len(org.experience.list_experiences())
    check("worker deletion w/o admission creates no knowledge",
          n_before == n_after, f"{n_before} -> {n_after}")

    # -- duplicate / replay guards -------------------------------------------
    try:
        capture_dispatch_evidence(eng, store, dispatch_res.dispatch_id,
                                  A.agent_id, asg.assignment_id,
                                  {"items": [5, 3, 8, 1]}, [1, 3, 5, 8])
        check("duplicate capture refused", False, "second capture allowed")
    except ValueError as exc:
        check("duplicate capture refused", "already exists" in str(exc),
              str(exc)[:120])
    try:
        org.experience.admit_dispatch_knowledge(
            evidence_id=ev_id, technique_name="sort_request_arg_mapper",
            code="def map_request(user_text): return {}", entrypoint="x",
            problem_class="dispatch.arg_mapping.sort", tags=[], io_contract={},
            params={}, generality_cases=generality_cases(cap_id, 1),
            agent_id=A.agent_id)
        check("duplicate admission refused", False, "re-admission allowed")
    except VerificationFailed as exc:
        check("duplicate admission refused", "already admitted" in str(exc),
              str(exc)[:120])


def _repair_evidence_row(store, org, ev_id, ev):
    """Audited recovery for the G2 tamper test: restore exact original
    bytes, recompute the chain link, and re-anchor explicitly.

    This is test-only recovery (the real recovery path is the audited
    transition); it runs so the workdir remains usable for phase2.
    """
    import sqlite3
    con = sqlite3.connect(store.db_path)
    try:
        row = con.execute(
            "SELECT seq, prev_digest FROM ao_dispatch_evidence"
            " WHERE evidence_id=?", (ev_id,)).fetchone()
        seq, prev = row
        fields = {
            "evidence_id": ev.evidence_id, "dispatch_id": ev.dispatch_id,
            "agent_id": ev.agent_id, "assignment_id": ev.assignment_id,
            "request_text": ev.request_text, "route_via": ev.route_via,
            "route_score": ev.route_score, "capability_id": ev.capability_id,
            "capability_version": ev.capability_version,
            "plan_fingerprint": ev.plan_fingerprint,
            "args_json": ev.args_json, "input_digest": ev.input_digest,
            "result_json": ev.result_json,
            "result_digest": ev.result_digest,
            "ok": "1" if ev.ok else "0", "error": ev.error,
            "evidence_json": ev.evidence_json, "created_at": ev.created_at,
        }
        names = ["evidence_id", "dispatch_id", "agent_id", "assignment_id",
                 "request_text", "route_via", "route_score", "capability_id",
                 "capability_version", "plan_fingerprint", "args_json",
                 "input_digest", "result_json", "result_digest", "ok",
                 "error", "evidence_json", "created_at"]
        from swarm_engine.agent_org.store import canonical, digest as dg
        ordered = {k: fields[k] for k in names}
        row_d = dg(canonical(ordered) + prev)
        sets = ", ".join(f"{k}=?" for k in names)
        con.execute(
            f"UPDATE ao_dispatch_evidence SET {sets}, row_digest=?"
            " WHERE seq=?",
            [ordered[k] for k in names] + [row_d, seq])
        con.commit()
    finally:
        con.close()
    oka, msga = store.audit("ao_dispatch_evidence")
    if not oka:
        raise SystemExit(f"G2 recovery failed: {msga}")
    from swarm_engine.governance.anchor import collect_anchor_heads
    # Registry-bound anchor: journal writes require an authenticated caller
    # holding 'agent:anchor_write', and the claimed authority must equal the
    # authenticated caller id (confused-deputy rule) -- hence
    # ENGINE_PRODUCER_ID ("remor:engine") as the authority label here, not a
    # battery-scoped tag. Mirrors driver_track3.deploy_org.
    from swarm_engine.governance.oracle_binding import ENGINE_PRODUCER_ID
    org.anchor.anchor(collect_anchor_heads(store, org.oregistry),
                      reason="recovery", authority=ENGINE_PRODUCER_ID,
                      caller=org.oregistry.engine_handle())
