"""P6: valid grant + live server -> real turn, real charge, real provenance."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SubstrateDouble, make_grant
from governed_student import GovernedStudent, STUDENT_PROVENANCE

sub = SubstrateDouble(budget_s=1000.0)
gs = GovernedStudent(sub, "mc-gate-6")
grant = make_grant(budget_s=400.0, max_concurrent=2)
res = gs.turn("can you do math: what is 12 times 12", frm_grant=grant)
assert res["ok"] is True, f"valid turn must serve: {res}"
assert res["text"], "served turn must return text"
assert res["provenance"] == STUDENT_PROVENANCE, \
    f"wrong provenance: {res['provenance']}"
assert res["charged_s"] > 0, "must charge actual seconds"
assert abs(res["charged_s"] - res["wall_s"]) < 1e-6, "charged == wall"
assert abs(gs.grant_consumed_s(grant.grant_id) - res["charged_s"]) < 1e-9, \
    "grant ledger must match the charge"
assert len(sub.calls) == 1 and sub.calls[0][0] == "mc-gate-6", \
    f"charge must hit the substrate path: {sub.calls}"
assert abs(sub.calls[0][1] - res["charged_s"]) < 1e-9, \
    "substrate charged the real seconds"
print(f"PASS p6_real_turn: '{res['text'][:60]}...' "
      f"charged={res['charged_s']:.2f}s prov={res['provenance'][:40]}")
