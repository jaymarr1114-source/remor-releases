"""P4: exhausted budget defers with zero charge (no silent overrun)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SubstrateDouble, make_grant
from governed_student import GovernedStudent, R_DEFERRED_BUDGET

# Budget smaller than the conservative estimate (2.0s + epsilon):
# even the first turn must defer.
sub = SubstrateDouble()
gs = GovernedStudent(sub, "mc-gate-4")
grant = make_grant(budget_s=0.5)
res = gs.turn("hello there, how are you doing today", frm_grant=grant)
assert res["ok"] is False, f"exhausted budget must defer, got {res}"
assert R_DEFERRED_BUDGET in res["error"], f"wrong: {res['error']}"
assert sub.calls == [], f"deferral must charge nothing, calls={sub.calls}"
assert gs.grant_consumed_s(grant.grant_id) == 0.0, "consumed must stay 0"
print(f"PASS p4_exhausted: {res['error'][:70]}...")
