"""Track 3 phase 1."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pylib"))

from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher
from swarm_engine.synthesis.integrity import effective_status
from swarm_engine.agent_org.dispatch_learning import (
    capture_dispatch_evidence, get_evidence, DISPATCH_EVIDENCE_KIND)
from swarm_engine.agent_org.store import digest


def phase1(workdir):
    """A dispatches -> evidence -> independent review -> admission -> destroy."""
    results = {"phase": "phase1", "workdir": workdir, "checks": []}

    def check(name, cond, detail=""):
        results["checks"].append({"name": name, "ok": bool(cond),
                                  "detail": detail})
        print(("PASS " if cond else "FAIL ") + name +
              (f" -- {detail}" if detail else ""), flush=True)
        if not cond:
            raise SystemExit(f"phase1 FAILED at: {name} {detail}")

    from driver_track3_battery import run_attack_battery
    from driver_track3 import (boot_engine, deploy_org, MAPPER_TEMPLATE,
                               SORT_GOAL, SORT_PLAN, A_USER_TEXT,
                               A_EXPECTED_SORTED, B_USER_TEXT, PROBLEM_CLASS)
    eng = boot_engine(workdir)
    org = deploy_org(workdir)
    org.review.dispatch_engine = eng
    dispatcher = NLToolDispatcher(eng)

    # -- admit the sort capability through the real admission path --------
    adm = eng.admission.admit(goal=SORT_GOAL, plan=dict(SORT_PLAN),
                              name="sort_numbers", caller=eng.oracle)
    check("capability admitted", adm.ok,
          f"id={getattr(adm, 'capability_id', None)}")
    cap_id = adm.capability_id
    rec = eng.capabilities.get(cap_id)
    check("capability effectively active",
          eng.admission is not None and rec is not None)

    eff = effective_status(eng, cap_id)
    check("effective_status active+consistent",
          eff.get("effective") == "active" and eff.get("consistent"), str(eff))

    # -- the gap: the router cannot handle paraphrases --------------------
    for text in ["please sort 9 2 7", "arrange 4 1 9 in ascending order",
                 B_USER_TEXT]:
        r = dispatcher.router.route(text)
        check(f"router refuses paraphrase {text!r}",
              not r.ok and r.refusal == "unknown_intent", r.refusal)
    r0 = dispatcher.router.route(SORT_GOAL)
    check("router routes the bound goal", r0.ok and r0.via == "exact_goal",
          f"{r0.via} {r0.capability_id}")

    # -- Agent A -----------------------------------------------------------
    A = org.factory.create("tpl_callable_coder_v1")
    check("agent A created", bool(A.agent_id), A.agent_id)
    asg = org.assignments.create(
        A.agent_id,
        objective={"goal": A_USER_TEXT,
                   "note": "dispatch the sort capability for this request"},
        constraints={}, authority_scope={
            "allowed_capability_patterns": ["dispatch:*"],
            "workspace": A.workspace_path, "max_steps": 5},
        expected_outputs={}, validation_requirements={},
        originating_decision="track3:phase1")
    asg = org.assignments.activate(asg.assignment_id)
    check("assignment active", asg.state == "ACTIVE", asg.assignment_id)

    # A maps the request text -> (dispatch_text, args), then dispatches.
    # (The driver plays A's cognition; the dispatch itself is real.)
    mapper_code = MAPPER_TEMPLATE.format(
        evidence_id="pending", agent_id=A.agent_id, capability_id=cap_id,
        capability_version=rec.version)
    ns = {}
    exec(mapper_code, ns)
    mapped = ns["map_request"](A_USER_TEXT)
    check("A maps request", mapped["ok"] and
          mapped["args"] == {"items": [5, 3, 8, 1]}, str(mapped))

    res = dispatcher.dispatch(mapped["dispatch_text"], mapped["args"],
                              producer=f"agent:{A.agent_id}")
    check("A dispatch ok", res.ok and res.result == A_EXPECTED_SORTED,
          f"result={res.result} refusal={res.refusal}")
    check("dispatch routed via exact_goal", res.route_via == "exact_goal",
          res.route_via)
    check("dispatch recorded with validated capability",
          res.capability_id == cap_id, res.capability_id)

    # -- attributable evidence ---------------------------------------------
    ev_id = capture_dispatch_evidence(
        eng, org.store, res.dispatch_id, A.agent_id, asg.assignment_id,
        mapped["args"], res.result)
    ev = get_evidence(org.store, ev_id)
    check("evidence captured", ev.evidence_id == ev_id, ev_id)
    check("evidence attributes agent A", ev.agent_id == A.agent_id,
          ev.agent_id)
    check("evidence binds dispatch", ev.dispatch_id == res.dispatch_id)
    check("evidence carries plan fingerprint",
          len(ev.plan_fingerprint) > 0, ev.plan_fingerprint[:20])
    check("evidence row chained+anchored",
          org.store.audit("ao_dispatch_evidence")[0],
          org.store.audit("ao_dispatch_evidence")[1])

    # -- independent review -------------------------------------------------
    verdict = org.review.verify_dispatch_evidence(ev_id)
    check("dispatch evidence verdict admitted", verdict.admitted,
          "; ".join(verdict.reasons)[:200])
    vrow = org.review.require_admitted_verdict(
        digest(ev.evidence_json), DISPATCH_EVIDENCE_KIND)
    check("verdict row bound to evidence bytes",
          vrow["artifact_ref"] == ev_id and
          vrow["verifier"] == "independent-validator:subprocess:bound-oracles",
          vrow["execution_id"])

    # -- admission to organizational experience ------------------------------
    mapper_code = MAPPER_TEMPLATE.format(
        evidence_id=ev_id, agent_id=A.agent_id, capability_id=cap_id,
        capability_version=rec.version)
    from driver_track3_battery import generality_cases
    gen_cases = generality_cases(cap_id, rec.version)
    ok, exp_or_reasons = org.experience.admit_dispatch_knowledge(
        evidence_id=ev_id, technique_name="sort_request_arg_mapper",
        code=mapper_code, entrypoint="map_request",
        problem_class=PROBLEM_CLASS,
        tags=["dispatch", "arg-mapping", "sort"],
        io_contract={"input": "user_text: str",
                     "output": "{ok, dispatch_text, capability_id, args} or "
                               "{ok: False, reason}"},
        params={"capability_id": cap_id,
                "capability_version": rec.version,
                "dispatch_text": SORT_GOAL},
        generality_cases=gen_cases, agent_id=A.agent_id)
    check("dispatch knowledge admitted", ok,
          str(exp_or_reasons)[:300] if not ok else exp_or_reasons)
    exp_id = exp_or_reasons
    exp = org.experience.get_experience(exp_id)
    check("experience is L2", exp.level == "L2", exp.level)
    check("experience origin dispatch_discovery",
          exp.origin == "dispatch_discovery", exp.origin)
    check("experience derived_from evidence",
          list(exp.derived_from) == [ev_id], str(exp.derived_from))
    check("experience discovered_by A", exp.discovered_by == A.agent_id)
    check("experience code digest matches mapper",
          exp.code_digest == digest(mapper_code))

    results.update({"agent_a": A.agent_id, "assignment": asg.assignment_id,
                    "capability_id": cap_id,
                    "capability_version": rec.version,
                    "dispatch_id": res.dispatch_id, "evidence_id": ev_id,
                    "experience_id": exp_id,
                    "verdict_execution_id": vrow["execution_id"]})

    # -- tampering battery ---------------------------------------------------
    run_attack_battery(check, eng, org, dispatcher, ev_id, ev, exp_id,
                       A, asg, cap_id, res)

    # -- destroy Agent A ------------------------------------------------------
    org.agents.destroy(A.agent_id, actor="remor:engine",
                       reason="track3: phase1 complete")
    st = org.agents.get(A.agent_id).state
    check("agent A destroyed", st == "DESTROYED", st)
    check("A workspace removed",
          not os.path.exists(A.workspace_path), A.workspace_path)
    try:
        org.agents._substrates[A.agent_id]
        check("A substrate gone", False, "substrate still present")
    except KeyError:
        check("A substrate gone", True)
    # admitted knowledge survives the worker's destruction
    exp2 = org.experience.get_experience(exp_id)
    check("knowledge survives A destruction",
          exp2.exp_id == exp_id and exp2.code_digest == digest(mapper_code))
    ev_still = get_evidence(org.store, ev_id)
    check("evidence survives A destruction", ev_still.evidence_id == ev_id)
    # I. destroyed-agent reuse: new dispatches cannot be attributed to A.
    r_i = dispatcher.dispatch("sort numbers", {"items": [2, 1]},
                              producer=f"agent:{A.agent_id}")
    check("I. post-destruction dispatch still executes", r_i.ok,
          str(r_i.result))
    try:
        capture_dispatch_evidence(eng, org.store, r_i.dispatch_id,
                                  A.agent_id, asg.assignment_id,
                                  {"items": [2, 1]}, [1, 2])
        check("I. destroyed-agent capture refused", False,
              "evidence attributed to destroyed agent")
    except ValueError as exc:
        check("I. destroyed-agent capture refused", "DESTROYED" in str(exc),
              str(exc)[:120])

    meta_path = os.path.join(workdir, "track3_meta.json")
    with open(meta_path, "w") as fh:
        json.dump(results, fh, indent=2, default=str)
    print(f"WORKDIR={workdir}", flush=True)
    print("PHASE1 COMPLETE", flush=True)
    return results
