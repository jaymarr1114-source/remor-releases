import sys, os, json, tempfile
HERE = os.path.dirname(os.path.abspath(__file__))
# Battery E: absolute trust-anchor pylib (the original used a relative
# "pylib", which only worked from the verdict-binding dir).
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))
d = tempfile.mkdtemp(prefix="regr_")
db = os.path.join(d, "eng.db")
from swarm_engine.core.engine import SwarmEngine
from swarm_engine.primitives.core import Effect
from swarm_engine.governance.provenance import ProvenanceRecord, Origin, TrustLevel
from swarm_engine.governance.oracle_binding import OracleRegistry

out = []
def rec(name, ok, detail=""):
    out.append({"probe": name, "pass": bool(ok), "detail": detail[:250]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail[:120]}")

eng = SwarmEngine(db_path=db)
rec("R1_engine_boots", hasattr(eng, "oracle_registry") and hasattr(eng, "oracle"),
    "registry + handle wired")

# R2: IndependentValidator end-to-end on trivial candidate
from types import SimpleNamespace
from swarm_engine.verification.independent import IndependentValidator
from swarm_engine.acquisition.semantic import Case as SemCase
spec = SimpleNamespace(examples=[({"x": 1}, 2), ({"x": 5}, 6)],
                       input_names=["x"], description="add one")
v = IndependentValidator(eng.acquisition.process_sandbox.run,
                         oracle_registry=eng.oracle_registry,
                         engine_oracle=eng.oracle)
verdict = v.validate("def cap(x):\n    return x + 1", "cap", spec,
                     [SemCase(args={"x": 1}, expect=2)])
verdict_wrong = v.validate("def cap(x):\n    return x + 2", "cap", spec,
                           [SemCase(args={"x": 1}, expect=2)])
rec("R2_validator_end_to_end",
    verdict.admitted and not verdict_wrong.admitted,
    f"correct={verdict.admitted} wrong={verdict_wrong.admitted} reasons={verdict.reasons[:1]}")

# R3: grants persist across restart (rehydration)
eng.governor.grant(Effect.READ_FS, "/tmp/ws/*", note="regr")
del eng
import gc; gc.collect()
eng2 = SwarmEngine(db_path=db)
ok3, why3 = eng2.governor.allows(Effect.READ_FS, "/tmp/ws/f.txt")
rec("R3_grant_rehydrated", ok3, why3)

# R4: provenance record_use demotion/promotion via transition log
eng2.provenance.record(ProvenanceRecord(capability_id="cap_r", origin=Origin.ACQUIRED,
                                        trust=TrustLevel.TESTED, source="r"))
for _ in range(10):
    eng2.provenance.record_use("cap_r", True)
r = eng2.provenance.get("cap_r")
hist_ok = eng2.oracle_registry.current_trust("cap_r") == r.trust.name
rec("R4_record_use_promotion", r.trust == TrustLevel.TRUSTED and hist_ok,
    f"trust={r.trust} log_head={eng2.oracle_registry.current_trust('cap_r')}")
eng2.provenance.record_use("cap_r", False)
r2 = eng2.provenance.get("cap_r")
rec("R4b_demotion", r2.trust == TrustLevel.SANDBOXED, f"trust={r2.trust}")

# R5: registry audit_all clean after all this
aud = eng2.oracle_registry.audit_all()
bad = {t: v for t, v in aud.items() if not v[0]}
rec("R5_audit_all_clean", not bad, f"bad={list(bad)}" if bad else "all chains intact")

npass = sum(1 for r in out if r["pass"])
print(f"{npass}/{len(out)}")
# Battery E: results stay in scratch (the original wrote into the
# protected remor_oracle_binding tree).
with open(os.path.join(HERE, "regression_results.json"), "w") as fh:
    json.dump(out, fh, indent=1)
sys.exit(0 if npass == len(out) else 1)
