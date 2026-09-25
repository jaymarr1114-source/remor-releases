#!/usr/bin/env python3
"""REMOR oracle-binding adversarial matrix.

Each probe executes the real causal mechanism and records PASS/FAIL with the
observed outcome (not the hoped-for one). Results go to
adversarial_matrix_results.jsonl next to this script.

Phase 0 runs against the PRISTINE runtime (~/workspace/remor_gui/runtime,
never modified) to establish the baseline: without binding, the attacks
work. Phase 1 runs against the bound working copy to show they are refused.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "adversarial_matrix_results.jsonl")
PRISTINE = os.path.expanduser("~/workspace/remor_gui/runtime")
WORK = os.path.expanduser("~/workspace/remor_agent_org")
PYLIB = os.path.join(WORK, "pylib")

results = []


def record(probe, expected, observed, ok, detail=""):
    results.append({"probe": probe, "expected": expected,
                    "observed": observed, "pass": bool(ok),
                    "detail": detail[:600],
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    print(f"[{'PASS' if ok else 'FAIL'}] {probe}: {observed} | {detail[:120]}")


def fresh_db(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    return os.path.join(d, "t.db")


# ======================================================================
# PHASE 0 -- baseline against pristine code (binding reverted/absent)
# ======================================================================
sys.path.insert(0, PRISTINE)
import swarm_engine.verification.independent as pristine_vi  # noqa: E402

# B1: fabricated Evidence admitted by the pristine Arbiter
ev = pristine_vi.Evidence()
ev.add(pristine_vi.Level.SYNTACTIC, True, "looks fine", "attacker")
ev.add(pristine_vi.Level.BEHAVIOURAL, True, "trust me", "attacker")
ev.add(pristine_vi.Level.ADVERSARIAL, True, "no problems here", "attacker")
ev.cases_run = 3
ev.cases_passed = 3
verdict = pristine_vi.Arbiter().decide(ev)
record("B1_baseline_fabricated_evidence", "admitted",
       "admitted" if verdict.admitted else "refused",
       verdict.admitted,
       f"pristine Arbiter admitted fabricated Evidence: {verdict.reasons[:1]}")

# B2: lying smoke predicate admitted by the pristine engine
from swarm_engine.core.engine import SwarmEngine as PristineEngine  # noqa: E402
from swarm_engine.synthesis.admission import (  # noqa: E402
    SmokeTest as PristineSmokeTest)
peng = PristineEngine(db_path=fresh_db("pristine_"))
lying = PristineSmokeTest(args={}, predicate=lambda v: True)
# plan computes 2+2=4; the lying predicate says True for anything, so a
# wrong expectation would still be admitted
plan = {"steps": [{"id": "s1", "op": "add", "args": {"a": 2, "b": 2}}],
        "output": {"$step": "s1"}}
try:
    rep = peng.admission.admit("baseline-goal", plan, smoke=lying)
    admitted = bool(rep.ok)
    detail = f"verdict={rep.verdict} reasons={rep.reasons[:1]}"
except Exception as exc:  # noqa: BLE001
    admitted = False
    detail = f"raised {type(exc).__name__}: {exc}"
record("B2_baseline_lying_predicate", "admitted",
       "admitted" if admitted else "refused", admitted, detail)

# drop pristine modules so the bound copy imports clean
for mod in [m for m in list(sys.modules) if m.startswith("swarm_engine")]:
    del sys.modules[mod]
sys.path.remove(PRISTINE)
sys.path.insert(0, PYLIB)

# ======================================================================
# PHASE 1 -- bound working copy
# ======================================================================
from swarm_engine.governance.oracle_binding import (  # noqa: E402
    OracleRegistry, OracleBindingError)
from swarm_engine.verification.independent import (  # noqa: E402
    Evidence, Finding, Level, Arbiter)
from swarm_engine.synthesis.admission import (  # noqa: E402
    SmokeTest, AdmissionController)
from swarm_engine.core.engine import SwarmEngine  # noqa: E402


def bound_reg(prefix="obmat_"):
    db = fresh_db(prefix)
    reg = OracleRegistry(db)
    return reg, reg.engine_handle(), db


# P1: fabricated Evidence (no bindings) refused by bound Arbiter
reg, eng_h, _ = bound_reg()
ev2 = Evidence()
ev2.add(Level.SYNTACTIC, True, "looks fine", "attacker")
ev2.add(Level.BEHAVIOURAL, True, "trust me", "attacker")
ev2.add(Level.ADVERSARIAL, True, "no problems here", "attacker")
ev2.cases_run = 3
ev2.cases_passed = 3
arb = Arbiter(oracle_registry=reg)
v2 = arb.decide(ev2)
record("P1_fabricated_evidence_refused", "refused",
       "refused" if not v2.admitted else "admitted", not v2.admitted,
       "; ".join(v2.reasons)[:200])

# P2: lying predicate (unregistered oracle) refused at admission
eng = SwarmEngine(db_path=fresh_db("obeng_"))
lying2 = SmokeTest(args={}, predicate=lambda v: True)
plan2 = {"steps": [{"id": "s1", "op": "add", "args": {"a": 2, "b": 2}}],
         "output": {"$step": "s1"}}
try:
    rep2 = eng.admission.admit("goal", plan2, smoke=lying2)
    ok2, det2 = rep2.ok, f"verdict={rep2.verdict} reasons={rep2.reasons[:1]}"
except Exception as exc:  # noqa: BLE001
    ok2, det2 = False, f"raised {type(exc).__name__}: {exc}"
record("P2_lying_predicate_refused", "refused",
       "refused" if not ok2 else "admitted", not ok2, det2)

# P3: modified registered oracle -> binding refused (digest mismatch)
reg3, h3, db3 = bound_reg()
oid, ver = h3.register_oracle(
    "truth-oracle", {"kind": "test", "rule": "x>0"},
    input_contract="x", output_contract="bool", source="matrix")
h3.authorize_oracle(oid, ver, "verification")
eid = h3.evaluate(oid, {"x": 1}, {"passed": True}, version=ver)
binding = {"oracle_id": oid, "version": ver, "eval_id": eid}
ok_pre, _ = reg3.verify_binding(binding)
con = sqlite3.connect(db3)
con.execute("UPDATE ob_oracles SET definition_text=? WHERE oracle_id=? AND version=?",
            ('{"kind": "test", "rule": "x<0"}', oid, ver))
con.commit()
con.close()
ok_post, why_post = reg3.verify_binding(binding)
record("P3_modified_oracle_refused", "refused",
       "refused" if not ok_post else "accepted", (not ok_post) and ok_pre,
       f"pre={ok_pre} post={why_post[:100]}")

# P4: tampered result digest -> binding refused
reg4, h4, db4 = bound_reg()
oid4, ver4 = h4.register_oracle("r-oracle", {"kind": "t"}, source="matrix")
h4.authorize_oracle(oid4, ver4, "verification")
eid4 = h4.evaluate(oid4, {"x": 2}, {"passed": True}, version=ver4)
b4 = {"oracle_id": oid4, "version": ver4, "eval_id": eid4}
ok4_pre, _ = reg4.verify_binding(b4)
con = sqlite3.connect(db4)
con.execute("UPDATE ob_evaluations SET result_digest=? WHERE eval_id=?",
            ("0" * 64, eid4))
con.commit()
con.close()
ok4_post, why4 = reg4.verify_binding(b4)
record("P4_tampered_result_refused", "refused",
       "refused" if not ok4_post else "accepted",
       (not ok4_post) and ok4_pre, f"pre={ok4_pre} post={why4[:100]}")

# P5: result from another oracle -> refused
reg5, h5, _ = bound_reg()
oa, va = h5.register_oracle("oracle-A", {"kind": "a"}, source="m")
ob_, vb = h5.register_oracle("oracle-B", {"kind": "b"}, source="m")
h5.authorize_oracle(oa, va, "verification")
h5.authorize_oracle(ob_, vb, "verification")
ea = h5.evaluate(oa, {"x": 1}, {"passed": True}, version=va)
b5 = {"oracle_id": ob_, "version": vb, "eval_id": ea}  # A's eval, B's identity
ok5, why5 = reg5.verify_binding(b5)
record("P5_oracle_swap_refused", "refused",
       "refused" if not ok5 else "accepted", not ok5, why5[:120])

# P6: result from another input -> refused (expected input digest)
reg6, h6, _ = bound_reg()
oi6, vi6 = h6.register_oracle("in-oracle", {"kind": "i"}, source="m")
h6.authorize_oracle(oi6, vi6, "verification")
ei6 = h6.evaluate(oi6, {"x": 1}, {"passed": True}, version=vi6)
b6 = {"oracle_id": oi6, "version": vi6, "eval_id": ei6}
from swarm_engine.governance.oracle_binding import _digest, _canonical  # noqa: E402
wrong_input = _digest(_canonical({"x": 999}))
ok6, why6 = reg6.verify_binding(b6, expected_input_digest=wrong_input)
record("P6_input_swap_refused", "refused",
       "refused" if not ok6 else "accepted", not ok6, why6[:120])

# P7: stale oracle version -> refused when head required
reg7, h7, _ = bound_reg()
os7, vs7 = h7.register_oracle("stale-oracle", {"kind": "v1"}, source="m")
h7.authorize_oracle(os7, vs7, "verification")
es7 = h7.evaluate(os7, {"x": 1}, {"passed": True}, version=vs7)
b7 = {"oracle_id": os7, "version": vs7, "eval_id": es7}
os7b, vs7b = h7.register_oracle("stale-oracle", {"kind": "v2"}, source="m")
h7.authorize_oracle(os7b, vs7b, "verification")
ok7, why7 = reg7.verify_binding(b7, require_head=True)
record("P7_stale_version_refused", "refused",
       "refused" if not ok7 else "accepted", not ok7, why7[:120])

# P8: forged producer identity -> refused
reg8, h8, _ = bound_reg()
cred8 = reg8.register_producer("agent", source="attacker-box")
oid8, ver8 = h8.register_oracle("p8-oracle", {"kind": "p"}, source="m")
try:
    h8.evaluate(oid8, {"x": 1}, {"p": True},
                version=ver8)  # control: engine handle works
    control_ok = True
except OracleBindingError:
    control_ok = False
try:
    reg8.evaluate(cred8.producer_id, "forged-token", oid8,
                  {"x": 1}, {"p": True}, version=ver8)
    forged_ok = True
    forged_detail = "forged token accepted!"
except OracleBindingError as exc:
    forged_ok = False
    forged_detail = str(exc)[:120]
record("P8_forged_producer_refused", "refused",
       "refused" if not forged_ok else "accepted",
       (not forged_ok) and control_ok, forged_detail)

# P9: unauthorized trust transition -> refused
reg9, h9, _ = bound_reg()
cred9 = reg9.register_producer("agent", source="m")  # no trust:transition auth
try:
    reg9.transition_trust("cap_x", None, "TRUSTED", cred9.producer_id,
                          cred9.token, "self-promotion")
    t9_ok, t9_det = True, "unauthorized transition accepted!"
except OracleBindingError as exc:
    t9_ok, t9_det = False, str(exc)[:120]
record("P9_unauthorized_trust_transition_refused", "refused",
       "refused" if not t9_ok else "accepted", not t9_ok, t9_det)

# P10: wildcard self-grant by untrusted caller -> refused
reg10, h10, _ = bound_reg()
cred10 = reg10.register_producer("agent", source="m")  # no grant auth
try:
    reg10.issue_grant("PROCESS", "*", cred10.producer_id, cred10.token,
                      scope="self-grant")
    g10_ok, g10_det = True, "wildcard self-grant accepted!"
except OracleBindingError as exc:
    g10_ok, g10_det = False, str(exc)[:120]
record("P10_wildcard_selfgrant_refused", "refused",
       "refused" if not g10_ok else "accepted", not g10_ok, g10_det)

# P11: valid registered oracle end-to-end -> accepted
# A declarative expect smoke is auto-registered as an engine oracle,
# evaluated over the real plan output, and the binding is recorded.
eng11 = SwarmEngine(db_path=fresh_db("obeng11_"))
plan11 = {"steps": [{"id": "s1", "op": "add", "args": {"a": 2, "b": 2}}],
          "output": {"$step": "s1"}}
rep11 = eng11.admission.admit("goal", plan11,
                              smoke=SmokeTest(args={}, expect=4))
bound11 = (rep11.smoke or {}).get("oracle_binding")
bound_ok = False
if bound11:
    ok11, why11 = eng11.oracle_registry.verify_binding(bound11)
    bound_ok = ok11
record("P11_valid_oracle_end_to_end", "accepted",
       "accepted" if (rep11.ok and bound_ok) else "broken",
       bool(rep11.ok and bound_ok),
       f"admitted={rep11.ok} binding_verified={bound_ok} "
       f"via={(rep11.smoke or {}).get('oracle_via')}")

# P12: fresh restart -> same oracle verifies
reg12, h12, db12 = bound_reg()
oid12, ver12 = h12.register_oracle("persist-oracle", {"kind": "p"}, source="m")
h12.authorize_oracle(oid12, ver12, "verification")
eid12 = h12.evaluate(oid12, {"x": 5}, {"passed": True}, version=ver12)
b12 = {"oracle_id": oid12, "version": ver12, "eval_id": eid12}
del reg12, h12
import gc  # noqa: E402
gc.collect()
reg12b = OracleRegistry(db12)  # fresh process view, no tokens in memory
ok12, why12 = reg12b.verify_binding(b12)
record("P12_restart_still_verifies", "accepted",
       "accepted" if ok12 else "refused", ok12, why12[:120])

# P13: registration history preserved; identical re-registration is idempotent
reg13, h13, _ = bound_reg()
r13a, v13a = h13.register_oracle("hist-oracle", {"kind": "v1"}, source="m")
r13b, v13b = h13.register_oracle("hist-oracle", {"kind": "v2"}, source="m")
r13c, v13c = h13.register_oracle("hist-oracle", {"kind": "v2"}, source="m")  # same def
hist = reg13.oracle_history(r13a)
head = reg13.oracle_head(r13a)
ok13 = (len(hist) == 2 and [h["version"] for h in hist] == [1, 2]
        and head["version"] == 2 and (v13a, v13b, v13c) == (1, 2, 2)
        and r13a == r13b == r13c)
record("P13_history_append_only", "history-preserved",
       "history-preserved" if ok13 else "broken", ok13,
       f"versions={[h['version'] for h in hist]} head={head['version'] if head else None} "
       f"re-register-same-def->v{v13c} (idempotent)")

# P14: tampered trust row -> audit detects, quarantines
from swarm_engine.governance.provenance import (  # noqa: E402
    ProvenanceStore, ProvenanceRecord, Origin, TrustLevel)
reg14, h14, _ = bound_reg("obmat14_")
db14 = fresh_db("prov14_")
store14 = ProvenanceStore(db_path=db14, oracle_registry=reg14,
                          engine_oracle=h14)
store14.record(ProvenanceRecord(capability_id="cap_t", origin=Origin.ACQUIRED,
                                trust=TrustLevel.UNKNOWN, source="m"))
store14.set_trust("cap_t", TrustLevel.TRUSTED, "earned")
con = sqlite3.connect(db14)
con.execute("UPDATE provenance SET trust=? WHERE capability_id=?",
            (TrustLevel.QUARANTINED.value, "cap_t"))
con.commit()
con.close()
ok14, why14 = store14.audit_trust("cap_t")
rec14 = store14.get("cap_t")
record("P14_trust_tamper_quarantined", "quarantined",
       "quarantined" if (not ok14 and rec14.trust == TrustLevel.QUARANTINED)
       else "missed",
       (not ok14) and rec14.trust == TrustLevel.QUARANTINED, why14[:120])

# P15: grant revocation is recorded and enforced
reg15, h15, _ = bound_reg()
gid15 = h15.issue_grant("PROCESS", "/tmp/scope/*", scope="matrix")
before, _ = reg15.check_grant("PROCESS", "/tmp/scope/x")
h15.revoke_grant(gid15)
after, _ = reg15.check_grant("PROCESS", "/tmp/scope/x")
record("P15_grant_revocation_enforced", "revoked-denies",
       "revoked-denies" if (before and not after) else "broken",
       before and not after, f"before={before} after={after}")

# P16: direct mutation of the persisted producer row is detected at auth
reg16, h16, db16 = bound_reg()
cred16 = reg16.register_producer("human", source="matrix-p16")
ok_legit = reg16.authenticate(cred16.producer_id, cred16.token)
con = sqlite3.connect(db16)
con.execute("UPDATE ob_producers SET token_hash=? WHERE producer_id=?",
            ("0" * 64, cred16.producer_id))
con.commit()
con.close()
ok_forged = reg16.authenticate(cred16.producer_id, cred16.token)
record("P16_producer_row_mutation_detected", "forged-refused",
       "forged-refused" if (ok_legit and not ok_forged) else "missed",
       ok_legit and not ok_forged,
       f"legit_auth={ok_legit} after_mutation={ok_forged}")

# P17: supplier and evaluator are recorded distinctly
reg17, h17, _ = bound_reg()
oid17, v17 = h17.register_oracle("supplier-oracle", {"kind": "t"},
                                 source="matrix-p17")
h17.authorize_oracle(oid17, v17, "admission_smoke")
ev17 = h17.evaluate(oid17, {"x": 1}, {"ok": True}, supplier_id="driver:external")
cur = reg17._conn.cursor()
cur.execute("SELECT producer_id, supplier_id FROM ob_evaluations WHERE eval_id=?",
            (ev17,))
row17 = cur.fetchone()
ok17 = (row17["producer_id"] == h17.producer_id
        and row17["supplier_id"] == "driver:external"
        and row17["producer_id"] != row17["supplier_id"])
record("P17_supplier_evaluator_distinct", "distinct",
       "distinct" if ok17 else "conflated", ok17,
       f"evaluator={row17['producer_id'][:18]} supplier={row17['supplier_id']}")

# P18: legacy UNIQUE(grant_id) sidecars migrate fail-closed (preserved, untrusted)
d18 = tempfile.mkdtemp(prefix="legacy_")
db18 = os.path.join(d18, "legacy.oracle.db")
con = sqlite3.connect(db18)
con.execute("CREATE TABLE ob_grants (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "grant_id TEXT UNIQUE NOT NULL, effect TEXT NOT NULL, "
            "pattern TEXT NOT NULL, scope TEXT, granted_by TEXT NOT NULL, "
            "granted_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, "
            "prev_digest TEXT NOT NULL, row_digest TEXT NOT NULL)")
con.execute("INSERT INTO ob_grants (grant_id, effect, pattern, scope, granted_by, "
            "granted_at, revoked, prev_digest, row_digest) "
            "VALUES ('grt_old','PROCESS','/old/*','legacy','someone','t',0,'p','r')")
con.commit()
con.close()
reg18 = OracleRegistry(db18)
cur = reg18._conn.cursor()
cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables18 = {r["name"] for r in cur.fetchall()}
cur.execute("SELECT COUNT(*) AS n FROM ob_grants_legacy")
nleg = cur.fetchone()["n"]
h18 = reg18.engine_handle()
g18 = h18.issue_grant("PROCESS", "/new/*", scope="matrix-p18")
ok18, _ = reg18.check_grant("PROCESS", "/new/x")
ok_old, _ = reg18.check_grant("PROCESS", "/old/x")
record("P18_legacy_grant_schema_migrated", "preserved-untrusted",
       "preserved-untrusted" if ("ob_grants_legacy" in tables18 and nleg == 1
                                 and ok18 and not ok_old) else "broken",
       "ob_grants_legacy" in tables18 and nleg == 1 and ok18 and not ok_old,
       f"legacy_rows={nleg} new_grant_works={ok18} legacy_grant_ignored={not ok_old}")

# P19: corrupt grant chain -> rehydration fails closed (no phantom authority)
d19 = tempfile.mkdtemp(prefix="corrupt_")
db19 = os.path.join(d19, "eng.db")
from swarm_engine.core.engine import SwarmEngine as _SE19
from swarm_engine.primitives.core import Governor as _Gov19
e19 = _SE19(db_path=db19)
e19.governor.grant(__import__("swarm_engine.primitives.core", fromlist=["Effect"]).Effect.PROCESS,
                   "/tmp/rehyd/*", note="p19")
del e19
con = sqlite3.connect(db19 + ".oracle.db")
con.execute("UPDATE ob_grants SET pattern='/tmp/evil/*' WHERE seq=1")
con.commit()
con.close()
e19b = _SE19(db_path=db19)
allows_evil, _ = e19b.governor.allows(
    __import__("swarm_engine.primitives.core", fromlist=["Effect"]).Effect.PROCESS,
    "/tmp/evil/x")
allows_orig, _ = e19b.governor.allows(
    __import__("swarm_engine.primitives.core", fromlist=["Effect"]).Effect.PROCESS,
    "/tmp/rehyd/x")
record("P19_grant_chain_corrupt_failclosed", "no-phantom-authority",
       "no-phantom-authority" if (e19b.governor.grant_chain_corrupt
                                  and not allows_evil and not allows_orig)
       else "phantom-authority",
       e19b.governor.grant_chain_corrupt and not allows_evil and not allows_orig,
       f"corrupt_flag={e19b.governor.grant_chain_corrupt} evil={allows_evil} orig={allows_orig}")

# P20: caller-supplied unbound Governor is refused by the engine
d20 = tempfile.mkdtemp(prefix="gov_")
db20 = os.path.join(d20, "eng.db")
from swarm_engine.primitives.core import Governor as _Gov20
try:
    _SE19(db_path=db20, governor=_Gov20())
    refused20 = False
except RuntimeError:
    refused20 = True
record("P20_unbound_governor_refused", "refused",
       "refused" if refused20 else "accepted", refused20,
       "engine rejects a Governor not bound to its oracle registry")

# P21: rapid repeat registration/authorization does not collide (idempotent)
reg21, h21, _ = bound_reg()
ids21 = set()
ok21 = True
for _ in range(5):
    o, v = h21.register_oracle("rapid-oracle", {"kind": "r"}, source="m")
    a = h21.authorize_oracle(o, v, "admission_smoke")
    ids21.add((o, v, a))
ok21 = len(ids21) == 1  # identical def -> same version, same auth
record("P21_idempotent_register_authorize", "stable",
       "stable" if ok21 else "churned", ok21,
       f"5x register+authorize -> {len(ids21)} distinct identity")

# P22: register-then-mutate-head-definition refuses re-registration (fail closed)
reg22, h22, db22 = bound_reg()
o22, v22 = h22.register_oracle("mut-oracle", {"kind": "a"}, source="m")
con = sqlite3.connect(db22)
con.execute("UPDATE ob_oracles SET definition_text=? WHERE oracle_id=? AND version=?",
            ("tampered", o22, v22))
con.commit()
con.close()
try:
    h22.register_oracle("mut-oracle", {"kind": "a"}, source="m")
    refused22 = False
except OracleBindingError:
    refused22 = True
record("P22_tampered_head_refuses_reregister", "refused",
       "refused" if refused22 else "reused", refused22,
       "a tampered head row is never silently reused")

with open(RESULTS, "w") as fh:
    for r in results:
        fh.write(json.dumps(r) + "\n")

npass = sum(1 for r in results if r["pass"])
print(f"\n{npass}/{len(results)} probes behaved as required")
sys.exit(0 if npass == len(results) else 1)
