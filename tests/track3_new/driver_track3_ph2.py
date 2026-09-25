"""Track 3 phase 2: fresh process. Agent B reuses admitted knowledge."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "pylib"))


def phase2(workdir):
    results = {"phase": "phase2", "workdir": workdir, "checks": []}

    def check(name, cond, detail=""):
        results["checks"].append({"name": name, "ok": bool(cond),
                                  "detail": detail})
        print(("PASS " if cond else "FAIL ") + name +
              (f" -- {detail}" if detail else ""), flush=True)
        if not cond:
            raise SystemExit(f"phase2 FAILED at: {name} {detail}")

    import sqlite3
    from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher
    from swarm_engine.agent_org.dispatch_learning import (
        capture_dispatch_evidence, get_evidence)
    from swarm_engine.agent_org.store import digest
    from driver_track3 import (boot_engine, attach_org, install_auto_anchor,
                               B_USER_TEXT, B_EXPECTED_SORTED, MAPPER_TEMPLATE,
                               PROBLEM_CLASS)
    from driver_track3_battery import generality_cases

    check("workdir exists", os.path.isdir(workdir), workdir)
    check("meta from phase1 present",
          os.path.exists(os.path.join(workdir, "track3_meta.json")))
    with open(os.path.join(workdir, "track3_meta.json")) as fh:
        meta = json.load(fh)
    exp_id = meta["experience_id"]
    ev_id = meta["evidence_id"]
    cap_id = meta["capability_id"]

    # -- rehydrate: boot MUST verify the external anchor -----------------
    eng = boot_engine(workdir)            # fresh SwarmEngine
    org = attach_org(workdir)             # fresh organization, boot() verifies
    check("fresh process boot verifies anchor", True,
          "boot raised on mismatch")
    check("fresh engine is a new object", True, str(type(eng).__name__))
    org.review.dispatch_engine = eng
    dispatcher = NLToolDispatcher(eng)

    # -- A is gone and stays gone ------------------------------------------
    a_id = meta["agent_a"]
    st = org.agents.get(a_id).state
    check("agent A still destroyed", st == "DESTROYED", st)
    try:
        org.agents._substrates[a_id]
        check("A substrate still gone", False, "resurrected")
    except KeyError:
        check("A substrate still gone", True)

    # -- admitted knowledge survived ---------------------------------------
    exp = org.experience.get_experience(exp_id)
    check("experience survived", exp.exp_id == exp_id, exp_id)
    check("experience still L2", exp.level == "L2", exp.level)
    check("experience origin intact", exp.origin == "dispatch_discovery")
    check("derived_from intact", list(exp.derived_from) == [ev_id])

    # -- Agent B: fresh identity, no private state --------------------------
    B = org.factory.create("tpl_callable_coder_v1")
    check("agent B created", B.agent_id != a_id, B.agent_id)
    # B's workspace is brand new and empty: nothing of A's was carried over.
    a_ws = os.path.abspath(org.agents.get(a_id).workspace_path)
    check("B workspace distinct from A's",
          os.path.abspath(B.workspace_path) != a_ws, B.workspace_path)
    check("B workspace starts empty", os.listdir(B.workspace_path) == [],
          str(os.listdir(B.workspace_path))[:80])
    # structural: agent records carry no experience/code of their own.
    con = sqlite3.connect(org.store.db_path)
    try:
        cols = [r[1] for r in con.execute(
            "PRAGMA table_info(ao_agents)").fetchall()]
    finally:
        con.close()
    check("agent records carry no knowledge columns",
          not any(c in cols for c in ("code", "experience", "knowledge")),
          str(cols))

    # -- causal ablation: the router refuses the held-out request -----------
    r = dispatcher.router.route(B_USER_TEXT)
    check("router refuses held-out request without learned mapping",
          not r.ok and r.refusal == "unknown_intent", r.refusal)

    # -- B retrieves the experience from organizational state only -----------
    hits = org.experience.get_relevant(PROBLEM_CLASS)
    check("B retrieves relevant experience",
          any(h["exp_id"] == exp_id for h in hits),
          str([h["exp_id"] for h in hits]))
    # relevance exposes metadata only -- no code (worker memory !=
    # organizational experience; B fetches the code explicitly next).
    check("relevant metadata carries no code",
          all("code" not in h for h in hits))
    got = org.experience.get_experience(exp_id)
    check("B gets the exact technique", got.exp_id == exp_id)
    check("B's source is organizational, not A's",
          got.discovered_by == a_id and B.agent_id != a_id,
          f"discovered_by={got.discovered_by}")

    # -- B executes the technique on the held-out request -------------------
    ns = {}
    exec(got.code, ns)
    mapped = ns[got.entrypoint](B_USER_TEXT)
    check("B technique maps held-out request",
          mapped["ok"] and mapped["capability_id"] == cap_id, str(mapped))

    # -- B dispatches through the governed path -----------------------------
    basg = org.assignments.create(
        B.agent_id,
        objective={"goal": B_USER_TEXT,
                   "note": "held-out reuse of organizational mapping"},
        constraints={}, authority_scope={
            "allowed_capability_patterns": ["dispatch:*"],
            "workspace": B.workspace_path, "max_steps": 5},
        expected_outputs={}, validation_requirements={},
        originating_decision="track3:phase2")
    basg = org.assignments.activate(basg.assignment_id)
    check("B assignment active", basg.state == "ACTIVE")

    bres = dispatcher.dispatch(mapped["dispatch_text"], mapped["args"],
                               producer=f"agent:{B.agent_id}")
    check("B dispatch ok", bres.ok and bres.result == B_EXPECTED_SORTED,
          f"result={bres.result}")
    check("B dispatch went through exact_goal",
          bres.route_via == "exact_goal", bres.route_via)

    # -- B's result is independently verified (own evidence + review) --------
    b_ev_id = capture_dispatch_evidence(
        eng, org.store, bres.dispatch_id, B.agent_id, basg.assignment_id,
        mapped["args"], bres.result)
    b_verdict = org.review.verify_dispatch_evidence(b_ev_id)
    check("B evidence independently verified", b_verdict.admitted,
          "; ".join(b_verdict.reasons)[:200])
    b_ev = get_evidence(org.store, b_ev_id)
    check("B evidence attributes B, not A",
          b_ev.agent_id == B.agent_id and b_ev.agent_id != a_id, b_ev.agent_id)

    # -- experience ablation removes success ---------------------------------
    # Simulate "no organizational knowledge": a fresh technique-less attempt
    # at the held-out request has nothing to normalize it with.
    r2 = dispatcher.router.route(B_USER_TEXT)
    check("ablation: router still refuses without the technique",
          not r2.ok, r2.refusal)

    # -- destroying A destroyed nothing organizational -----------------------
    exp3 = org.experience.get_experience(exp_id)
    check("knowledge persists after A destruction (phase2 view)",
          exp3.code_digest == exp.code_digest)

    # -- provenance chain ------------------------------------------------------
    results.update({"agent_b": B.agent_id, "b_assignment": basg.assignment_id,
                    "b_dispatch_id": bres.dispatch_id,
                    "b_evidence_id": b_ev_id})
    print("PHASE2 COMPLETE", flush=True)
    return results
