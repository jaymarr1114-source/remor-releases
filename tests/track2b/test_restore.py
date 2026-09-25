"""Capability recovery (§2) — causal tests for restore_everywhere.

Causal chain under test:
  admit (real trust path) -> quarantine_everywhere (deliberate)
  -> destroy runtime representation (fresh engine / fresh process)
  -> restore_everywhere (governed inverse)
  -> executable again, effective_status active across all three systems.

Plus the adversarial battery. All state in scratch DBs. Run:
  python3 test_restore.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pylib"))

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.synthesis.integrity import (
    effective_status, quarantine_everywhere, restore_everywhere, RestoreRefused)
from swarm_engine.synthesis.admission import Verdict

SCRATCH = os.path.dirname(os.path.abspath(__file__))
PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def fresh_db(tag):
    d = tempfile.mkdtemp(prefix=f"restore_{tag}_", dir=SCRATCH)
    return os.path.join(d, "eng.db")


ADD_PLAN = {
    "name": "add_two",
    "params": {"a": "num", "b": "num"},
    "steps": [{"id": "s1", "op": "add", "args": {"a": {"$param": "a"}, "b": {"$param": "b"}}}],
    "output": {"$step": "s1"},
}
GOAL = "add two numbers together"


def boot(db):
    return SwarmEngine(db_path=db)


def admit_add(engine):
    res = engine.admission.admit(goal=GOAL, plan=dict(ADD_PLAN))
    assert res.verdict == Verdict.ADMITTED, f"admit failed: {res.reasons}"
    return res.capability_id


def call_acquired(engine, cap_id, **kw):
    prim = engine.primitives.get(f"acquired.{cap_id}")
    assert prim is not None, "primitive not registered"
    return prim.fn(**kw)


def main():
    # ---- causal: full restore loop on one engine ----
    db = fresh_db("causal")
    eng = boot(db)
    cap_id = admit_add(eng)
    check("admit: verdict ADMITTED", True)
    check("admit: executable", call_acquired(eng, cap_id, a=2, b=3) == 5)
    check("admit: goal bound",
          eng.capabilities.goal_bindings().get(GOAL.strip().lower()) == cap_id)

    q = quarantine_everywhere(eng, cap_id, "test deliberate quarantine")
    check("quarantine: unregistered primitive",
          f"acquired.{cap_id}" in q.get("unregistered", []))
    eff = effective_status(eng, cap_id)
    check("quarantine: effective quarantined", eff["effective"] == "quarantined", json.dumps(eff))
    check("quarantine: tri-system consistent",
          eff["store_status"] == "quarantined" and eff["consistent"])
    try:
        call_acquired(eng, cap_id, a=2, b=3)
        check("quarantine: not executable", False, "still callable!")
    except (AssertionError, Exception):
        check("quarantine: not executable", True)

    # ---- destroy runtime representation: fresh engine, same DB ----
    eng2 = boot(db)
    eff2 = effective_status(eng2, cap_id)
    check("fresh engine: still quarantined", eff2["effective"] == "quarantined", json.dumps(eff2))
    check("fresh engine: primitive absent",
          eng2.primitives.get(f"acquired.{cap_id}") is None)

    # ---- restore through the governed path ----
    r = restore_everywhere(eng2, cap_id, authority=eng2.oracle,
                           reason="test restore after deliberate quarantine")
    check("restore: returned actions", r["trust"] == 5, str(r.get("trust")))
    eff3 = effective_status(eng2, cap_id)
    check("restore: effective active", eff3["effective"] == "active", json.dumps(eff3))
    check("restore: store active", eff3["store_status"] == "active")
    check("restore: trust TRUSTED", eff3["trust"] == 5, str(eff3["trust"]))
    check("restore: lifecycle DEPLOYED", eff3["lifecycle"] == "deployed", str(eff3["lifecycle"]))
    check("restore: tri-system consistent", eff3["consistent"] is True)
    check("restore: executable again", call_acquired(eng2, cap_id, a=20, b=22) == 42)
    check("restore: goal re-bound",
          eng2.capabilities.goal_bindings().get(GOAL.strip().lower()) == cap_id)
    evs = [e.get("event") for e in eng2.capabilities.events(cap_id, limit=200)]
    check("restore: restored_everywhere logged", "restored_everywhere" in evs)
    check("restore: quarantine event retained (not rewritten)",
          "quarantined_everywhere" in evs)

    # ---- fresh process: state survives, capability usable ----
    probe = os.path.join(SCRATCH, "probe_restore_state.py")
    with open(probe, "w") as f:
        f.write(
            "import sys, os\n"
            "sys.path.insert(0, os.path.join(r'%s', '..', 'pylib'))\n"
            % SCRATCH +
            "from swarm_engine.core.engine import SwarmEngine\n"
            "from swarm_engine.synthesis.integrity import effective_status\n"
            "eng = SwarmEngine(db_path=r'%s')\n" % db +
            "eff = effective_status(eng, r'%s')\n" % cap_id +
            "prim = eng.primitives.get('acquired.%s')\n" % cap_id +
            "val = prim.fn(a=7, b=8) if prim else None\n"
            "print('EFF:' + eff['effective'] + ' VAL:' + str(val))\n")
    out = subprocess.run([sys.executable, probe], capture_output=True, text=True, timeout=300)
    line = [l for l in out.stdout.splitlines() if l.startswith("EFF:")]
    check("fresh process: effective active + executable",
          line and line[0] == "EFF:active VAL:15", (line[0] if line else out.stderr[-500:]))

    # ---- adversarial battery ----
    db2 = fresh_db("adv")
    e = boot(db2)
    cid = admit_add(e)
    quarantine_everywhere(e, cid, "adv quarantine")

    def expect_refused(name, fn):
        try:
            fn()
            check(name, False, "no refusal raised")
        except RestoreRefused as ex:
            check(name, True)
        except Exception as ex:
            check(name, False, f"wrong exception: {type(ex).__name__}: {ex}")

    expect_refused("adv: unknown id refused",
                   lambda: restore_everywhere(e, "cap_nonexistent", authority=e.oracle, reason="x"))
    expect_refused("adv: empty reason refused",
                   lambda: restore_everywhere(e, cid, authority=e.oracle, reason=""))

    # active (non-quarantined) capability cannot be "restored"
    db3 = fresh_db("adv2")
    e3 = boot(db3)
    cid3 = admit_add(e3)
    expect_refused("adv: active capability refused (not a generic activator)",
                   lambda: restore_everywhere(e3, cid3, authority=e3.oracle, reason="x"))

    # no authority
    expect_refused("adv: authority=None refused",
                   lambda: restore_everywhere(e, cid, authority=None, reason="x"))

    # unauthorized producer
    class FakeAuth:
        producer_id = "attacker:nobody"
    expect_refused("adv: unauthorized producer refused",
                   lambda: restore_everywhere(e, cid, authority=FakeAuth(), reason="x"))

    # corrupted plan (tamper with persisted plan_json)
    db4 = fresh_db("adv3")
    e4 = boot(db4)
    cid4 = admit_add(e4)
    quarantine_everywhere(e4, cid4, "adv quarantine")
    con = sqlite3.connect(db4)
    row = con.execute("SELECT plan_json FROM plan_capabilities WHERE capability_id=?",
                      (cid4,)).fetchone()
    pj = json.loads(row[0])
    pj["steps"][0]["args"]["a"] = {"$param": "b"}  # subtle tamper
    con.execute("UPDATE plan_capabilities SET plan_json=? WHERE capability_id=?",
                (json.dumps(pj), cid4))
    con.commit()
    con.close()
    e4b = boot(db4)
    expect_refused("adv: tampered plan refused (fingerprint mismatch)",
                   lambda: restore_everywhere(e4b, cid4, authority=e4b.oracle, reason="x"))
    evs4 = [ev.get("event") for ev in e4b.capabilities.events(cid4, limit=200)]
    check("adv: corruption_detected logged", "corruption_detected" in evs4)

    # derived quarantine is not restorable via this path
    db5 = fresh_db("adv4")
    e5 = boot(db5)
    cid5 = admit_add(e5)
    e5.capabilities.set_status(cid5, "quarantined")
    e5.capabilities.log(cid5, "dependency_quarantined", "test derived")
    expect_refused("adv: derived quarantine refused",
                   lambda: restore_everywhere(e5, cid5, authority=e5.oracle, reason="x"))

    # epistemic revocation is not restorable via this path
    db6 = fresh_db("adv5")
    e6 = boot(db6)
    cid6 = admit_add(e6)
    e6.capabilities.set_status(cid6, "quarantined")
    e6.capabilities.log(cid6, "epistemic_revocation", "test epistemic")
    expect_refused("adv: epistemic revocation refused",
                   lambda: restore_everywhere(e6, cid6, authority=e6.oracle, reason="x"))

    # goal stolen by another active capability (genuinely different plan ->
    # different fingerprint -> different capability id)
    db7 = fresh_db("adv6")
    e7 = boot(db7)
    cid7 = admit_add(e7)
    quarantine_everywhere(e7, cid7, "adv quarantine")
    other_plan = {
        "name": "add_two_plus_zero",
        "params": {"a": "num", "b": "num"},
        "steps": [
            {"id": "s1", "op": "add", "args": {"a": {"$param": "a"}, "b": {"$param": "b"}}},
            {"id": "s2", "op": "add", "args": {"a": {"$step": "s1"}, "b": 0}},
        ],
        "output": {"$step": "s2"},
    }
    res_other = e7.admission.admit(goal=GOAL, plan=other_plan)
    check("adv setup: second capability admitted", res_other.verdict == Verdict.ADMITTED)
    check("adv setup: distinct id", res_other.capability_id != cid7)
    expect_refused("adv: stolen goal refused",
                   lambda: restore_everywhere(e7, cid7, authority=e7.oracle, reason="x"))

    # registry drift: op removed -> re-verification rejects
    db8 = fresh_db("adv7")
    e8 = boot(db8)
    cid8 = admit_add(e8)
    quarantine_everywhere(e8, cid8, "adv quarantine")
    assert e8.primitives.unregister("add"), "could not unregister add"
    expect_refused("adv: registry drift refused (op missing -> admit rejects)",
                   lambda: restore_everywhere(e8, cid8, authority=e8.oracle, reason="x"))
    eff8 = effective_status(e8, cid8)
    check("adv: failed restore leaves tri-system quarantined",
          eff8["effective"] == "quarantined" and eff8["consistent"], json.dumps(eff8))

    # double restore: second call refused (no longer quarantined)
    expect_refused("adv: double restore refused",
                   lambda: restore_everywhere(eng2, cap_id, authority=eng2.oracle, reason="x"))

    # silent resurrection via re-admission is refused by the new 0c guard
    db9 = fresh_db("adv8")
    e9 = boot(db9)
    cid9 = admit_add(e9)
    quarantine_everywhere(e9, cid9, "adv quarantine")
    res9 = e9.admission.admit(goal=GOAL, plan=dict(ADD_PLAN))
    check("adv: re-admission of deliberately-quarantined plan REJECTED",
          res9.verdict == Verdict.REJECTED, str(res9.verdict))
    check("adv: primitive still unregistered after refused re-admission",
          e9.primitives.get(f"acquired.{cid9}") is None)
    eff9 = effective_status(e9, cid9)
    check("adv: still quarantined after refused re-admission",
          eff9["effective"] == "quarantined", json.dumps(eff9))
    r9 = restore_everywhere(e9, cid9, authority=e9.oracle,
                            reason="restore after refused re-acquisition")
    check("adv: restore_everywhere works after refused re-admission",
          effective_status(e9, cid9)["effective"] == "active")
    check("adv: executable after restore", call_acquired(e9, cid9, a=1, b=2) == 3)

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
