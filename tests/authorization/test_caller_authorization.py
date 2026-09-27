"""Caller authorization adversarial battery.

Every privileged runtime operation requires an authenticated and
authorized caller (default-deny). This battery EXECUTES each attack --
assertions alone are not verification:

  lifecycle:      create / authenticate / destroy / enumerate
  refusals:       unauthenticated, unauthorized, forged token,
                  destroyed-token replay, confused deputy (id/token
                  mismatch), claimed-identity mismatch
  grant attacks:  self-grant, wildcard/unknown class, unprivileged
                  issuer, live revocation during work
  persistence:    identities, grants, and revocations survive a fresh
                  process; destroyed credentials stay dead

Run:  python3 test_caller_authorization.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pylib"))

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.governance.caller_authorization import (
    AgentDirectory, AuthorizationError)
from swarm_engine.governance.oracle_binding import (
    OracleRegistry, OracleBindingError, ENGINE_PRODUCER_ID,
    DECISION_ADMIT_CAPABILITY, DECISION_QUARANTINE, DECISION_RESTORE,
    DECISION_REPAIR_SUBMIT, DECISION_REPAIR_APPLY, DECISION_ANCHOR_WRITE,
    DECISION_GRANT, AGENT_DECISION_CLASSES)
from swarm_engine.synthesis.integrity import (
    quarantine_everywhere, restore_everywhere, RestoreRefused)
from swarm_engine.synthesis.admission import Verdict

SCRATCH = os.path.dirname(os.path.abspath(__file__))
PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def expect_refused(name, fn):
    from swarm_engine.project.modification import ProjectModificationRefused
    try:
        fn()
        check(name, False, "no refusal raised")
    except (AuthorizationError, RestoreRefused, OracleBindingError,
            ProjectModificationRefused) as ex:
        check(name, True)
    except Exception as ex:  # noqa: BLE001
        check(name, False, f"wrong exception: {type(ex).__name__}: {ex}")


ADD_PLAN = {
    "name": "add_two", "params": {"a": "num", "b": "num"},
    "steps": [{"id": "s1", "op": "add",
               "args": {"a": {"$param": "a"}, "b": {"$param": "b"}}}],
    "output": {"$step": "s1"},
}
GOAL = "add two numbers together"


def main():
    db = os.path.join(tempfile.mkdtemp(prefix="authz_", dir=SCRATCH), "eng.db")
    eng = SwarmEngine(db_path=db)
    agents = AgentDirectory(eng.oracle_registry)
    eng_caller = eng.oracle  # engine handle: root authority

    # ---- lifecycle: create / authenticate / destroy ----
    cred_a = agents.register_agent(
        eng_caller, source="battery",
        decision_classes=(DECISION_ADMIT_CAPABILITY,))
    check("register: id minted", cred_a.agent_id.startswith("agent_"))
    check("register: token 256-bit hex",
          len(cred_a.token) == 64 and all(c in "0123456789abcdef"
                                          for c in cred_a.token))
    check("authenticate: good token",
          agents.authenticate(cred_a.agent_id, cred_a.token))
    check("authenticate: forged token refused",
          not agents.authenticate(cred_a.agent_id, "f" * 64))
    check("authenticate: unknown id refused",
          not agents.authenticate("agent_nope", cred_a.token))

    cred_b = agents.register_agent(eng_caller, source="battery")
    check("register: no-grant agent has no authorizations",
          agents.agent_grants(cred_b.agent_id)["authorizations"] == [])

    # enumerate requires grant-issuance privilege
    expect_refused("enumerate: unauthenticated refused",
                   lambda: agents.enumerate_agents(None))
    expect_refused("enumerate: unauthorized agent refused",
                   lambda: agents.enumerate_agents(cred_b.context()))
    inv = agents.enumerate_agents(eng_caller)
    check("enumerate: engine lists both agents",
          {r["agent_id"] for r in inv} >= {cred_a.agent_id, cred_b.agent_id})
    check("enumerate: shows live classes",
          next(r for r in inv
               if r["agent_id"] == cred_a.agent_id)["live_decision_classes"]
          == [DECISION_ADMIT_CAPABILITY])

    # ---- unauthenticated callers refused everywhere ----
    expect_refused("admit: no caller refused",
                   lambda: eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN)))
    expect_refused("admit: None caller refused",
                   lambda: eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN),
                                              caller=None))
    cap = eng.admit_as_engine(goal=GOAL, plan=dict(ADD_PLAN))
    assert cap.verdict == Verdict.ADMITTED, cap.reasons
    cap_id = cap.capability_id
    expect_refused("quarantine: no caller refused",
                   lambda: quarantine_everywhere(eng, cap_id, "x"))
    expect_refused("restore: no caller refused",
                   lambda: restore_everywhere(eng, cap_id, reason="x"))
    expect_refused("issue_grant: no caller refused",
                   lambda: agents.issue_grant(None, cred_b.agent_id,
                                              DECISION_QUARANTINE))
    expect_refused("revoke_grant: no caller refused",
                   lambda: agents.revoke_grant(None, cred_a.agent_id,
                                               DECISION_ADMIT_CAPABILITY))
    expect_refused("destroy: no caller refused",
                   lambda: agents.destroy_agent(cred_b.agent_id, None))

    # ---- unauthorized (authenticated, no grant) refused ----
    expect_refused(
        "admit: unauthorized agent refused",
        lambda: eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN),
                                   caller=cred_b.context()))
    expect_refused(
        "quarantine: unauthorized agent refused",
        lambda: quarantine_everywhere(eng, cap_id, "x",
                                      caller=cred_b.context()))

    # ---- forged token refused ----
    forged = (cred_a.agent_id, "e" * 64)
    expect_refused(
        "admit: forged token refused",
        lambda: eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN),
                                   caller=forged))

    # ---- confused deputy: A's id with B's token ----
    mixed = (cred_a.agent_id, cred_b.token)
    expect_refused(
        "admit: id/token mismatch refused",
        lambda: eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN),
                                   caller=mixed))

    # ---- claimed supplier must match authenticated caller ----
    expect_refused(
        "admit: claimed supplier mismatch refused",
        lambda: eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN),
                                   caller=cred_a.context(),
                                   supplier_id="agent_impostor"))

    # ---- authorized agent can act; engine mediation recorded ----
    q = quarantine_everywhere(eng, cap_id, "battery", caller=eng_caller)
    check("quarantine: authorized engine caller works",
          q.get("store") == "quarantined")

    # grant B restore+trust, B restores
    agents.issue_grant(eng_caller, cred_b.agent_id, DECISION_RESTORE)
    agents.issue_grant(eng_caller, cred_b.agent_id, "trust:transition")
    r = restore_everywhere(eng, cap_id, caller=cred_b.context(),
                           reason="battery restore")
    check("restore: authorized agent works", r["trust"] == 5)

    # ---- live revocation during work ----
    agents.revoke_grant(eng_caller, cred_b.agent_id, DECISION_RESTORE,
                        reason="battery live-revoke")
    quarantine_everywhere(eng, cap_id, "battery re-quarantine",
                          caller=eng_caller)
    expect_refused(
        "restore: revoked grant refused mid-work",
        lambda: restore_everywhere(eng, cap_id, caller=cred_b.context(),
                                   reason="x"))
    check("revocation: grant no longer live",
          DECISION_RESTORE not in
          [a["decision_class"] for a in
           agents.agent_grants(cred_b.agent_id)["authorizations"]])

    # ---- grant attacks ----
    # unprivileged issuer (B holds no grant-issuance)
    expect_refused("issue_grant: non-issuer refused",
                   lambda: agents.issue_grant(cred_b.context(),
                                              cred_b.agent_id,
                                              DECISION_QUARANTINE))
    # self-grant: give B grant-issuance, B tries to grant ITSELF admit
    agents.issue_grant(eng_caller, cred_b.agent_id, DECISION_GRANT)
    expect_refused(
        "issue_grant: self-grant refused",
        lambda: agents.issue_grant(cred_b.context(), cred_b.agent_id,
                                   DECISION_ADMIT_CAPABILITY))
    # wildcard / unknown classes refused even by the engine
    for bad in ("*", "agent:*", "grant:*", "no.such.class", ""):
        expect_refused(
            f"issue_grant: {bad!r} refused",
            lambda bad=bad: agents.issue_grant(eng_caller, cred_b.agent_id,
                                               bad))
    # grant to unknown/destroyed agent refused
    expect_refused("issue_grant: unknown agent refused",
                   lambda: agents.issue_grant(eng_caller, "agent_nope",
                                              DECISION_QUARANTINE))

    # ---- destroy: credential dies and stays dead ----
    rep = agents.destroy_agent(cred_a.agent_id, eng_caller,
                               reason="battery destroy")
    check("destroy: revocation enumeration",
          rep["revoked_authorizations"] == [DECISION_ADMIT_CAPABILITY],
          str(rep))
    check("destroy: token dead",
          not agents.authenticate(cred_a.agent_id, cred_a.token))
    expect_refused(
        "admit: destroyed-token replay refused",
        lambda: eng.admission.admit(goal=GOAL, plan=dict(ADD_PLAN),
                                   caller=cred_a.context()))
    expect_refused("destroy: engine producer cannot be destroyed",
                   lambda: agents.destroy_agent(ENGINE_PRODUCER_ID,
                                                eng_caller))
    expect_refused("issue_grant: destroyed agent refused",
                   lambda: agents.issue_grant(eng_caller, cred_a.agent_id,
                                              DECISION_QUARANTINE))

    # ---- fresh process: identities/grants/revocations persist ----
    # D11: single-owner handoff. The first engine releases the database
    # (a real restart would have exited the old process) before the fresh
    # engine boots on the same file; the fresh engine then becomes the
    # live engine for the rest of the battery (agents/eng_caller rebound
    # to its re-attested registry/handle -- all state is file-backed).
    eng.close()
    eng2 = SwarmEngine(db_path=db)
    eng = eng2
    agents = AgentDirectory(eng.oracle_registry)
    eng_caller = eng.oracle
    agents2 = AgentDirectory(eng2.oracle_registry)
    check("fresh: B still authenticates",
          agents2.authenticate(cred_b.agent_id, cred_b.token))
    check("fresh: destroyed A stays dead",
          not agents2.authenticate(cred_a.agent_id, cred_a.token))
    check("fresh: B's live grants survived",
          "trust:transition" in
          [a["decision_class"] for a in
           agents2.agent_grants(cred_b.agent_id)["authorizations"]])
    check("fresh: revoked grant stays revoked",
          DECISION_RESTORE not in
          [a["decision_class"] for a in
           agents2.agent_grants(cred_b.agent_id)["authorizations"]])
    expect_refused(
        "fresh: revoked restore still refused",
        lambda: restore_everywhere(eng2, cap_id, caller=cred_b.context(),
                                   reason="x"))
    # chain audits intact across the restart
    bad = [t for t, (ok, _) in eng2.oracle_registry.audit_all().items()
           if not ok]
    check("fresh: chain audits intact", not bad, str(bad)[:200])

    # ---- agent:repair_apply on the real file-modification path ----
    import tempfile as _tf
    from swarm_engine.project.modification import (
        ProjectModificationGuard, ProjectModificationRefused)
    _pw = _tf.mkdtemp(prefix="authz_repair_")
    _target = os.path.join(_pw, "calc.py")
    with open(_target, "w") as fh:
        fh.write("def add(a, b):\n    return a - b\n")
    guard = ProjectModificationGuard(_pw).bind_authorization(agents)
    expect_refused("apply_repair: no caller refused",
                   lambda: guard.apply_repair("calc.py", "x", None))
    expect_refused("apply_repair: forged token refused",
                   lambda: guard.apply_repair("calc.py", "x", forged))
    expect_refused("apply_repair: unauthorized agent refused",
                   lambda: guard.apply_repair("calc.py", "x",
                                              cred_b.context()))
    with open(_target) as fh:
        check("apply_repair: refused writes changed nothing",
              "a - b" in fh.read())
    expect_refused("apply_repair: unbound guard refused",
                   lambda: ProjectModificationGuard(_pw).apply_repair(
                       "calc.py", "x", eng_caller))
    agents.issue_grant(eng_caller, cred_b.agent_id, DECISION_REPAIR_APPLY)
    res = guard.apply_repair(
        "calc.py", "def add(a, b):\n    return a + b\n",
        caller=cred_b.context())
    check("apply_repair: authorized agent stages bytes", res.committed)
    with open(_target) as fh:
        check("apply_repair: bytes actually changed on disk",
              "a + b" in fh.read())
    check("apply_repair: history attributes the caller",
          guard.history[-1].get("applied_by") == cred_b.agent_id)
    # destroyed credential cannot stage a repair
    agents.destroy_agent(cred_b.agent_id, eng_caller, reason="battery")
    expect_refused("apply_repair: destroyed-token replay refused",
                   lambda: guard.apply_repair("calc.py", "x",
                                              cred_b.context()))

    # ---- agent:anchor_write on a bound journal ----
    from swarm_engine.governance.anchor import AnchorStore
    _jd = _tf.mkdtemp(prefix="authz_anchor_")
    _dd = _tf.mkdtemp(prefix="authz_anchor_db_")
    store = AnchorStore(os.path.join(_jd, "j.journal"),
                        os.path.join(_jd, "j.key"),
                        os.path.join(_dd, "j.db"))
    store.bind_registry(eng.oracle_registry)
    expect_refused("anchor: initialize without caller refused",
                   lambda: store.initialize({"h": "x"}, "someone"))
    expect_refused("anchor: unauthorized caller refused",
                   lambda: store.initialize({"h": "x"}, cred_b.agent_id,
                                            caller=cred_b.context()))
    d0 = store.initialize({"h": "x"}, ENGINE_PRODUCER_ID, caller=eng_caller)
    check("anchor: authorized initialize works", bool(d0))
    cred_c = agents.register_agent(
        eng_caller, source="battery",
        decision_classes=(DECISION_ANCHOR_WRITE,))
    expect_refused("anchor: claimed authority mismatch refused",
                   lambda: store.anchor({"h": "y"}, reason="verdict",
                                        authority="agent_impostor",
                                        caller=cred_c.context()))
    d1 = store.anchor({"h": "y"}, reason="verdict",
                      authority=cred_c.agent_id, caller=cred_c.context())
    check("anchor: authorized write works", bool(d1))
    ok, msg = store.verify({"h": "y"})
    check("anchor: journal verifies", ok, msg)
    # key rotation is a privileged write on a bound journal
    cred_d = agents.register_agent(eng_caller, source="battery")
    expect_refused("rotate_key: no caller refused",
                   lambda: store.rotate_key())
    expect_refused("rotate_key: unauthorized caller refused",
                   lambda: store.rotate_key(caller=cred_d.context()))
    new_kid = store.rotate_key(caller=cred_c.context())
    check("rotate_key: authorized rotation works", bool(new_kid))
    d2 = store.anchor({"h": "z"}, reason="verdict",
                      authority=cred_c.agent_id, caller=cred_c.context())
    check("anchor: post-rotation write works", bool(d2))
    ok, msg = store.verify({"h": "z"})
    check("anchor: journal verifies after rotation", ok, msg)

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
