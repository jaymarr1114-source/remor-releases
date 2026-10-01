"""P1: grantless student inference refuses with the real reason."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SubstrateDouble
from governed_student import GovernedStudent, R_NO_GRANT

sub = SubstrateDouble()
gs = GovernedStudent(sub, "mc-gate-1")
res = gs.turn("hello there", frm_grant=None)
assert res["ok"] is False, f"grantless turn must refuse, got {res}"
assert R_NO_GRANT in res["error"], f"wrong refusal: {res['error']}"
assert sub.calls == [], "refusal must not touch the charge path"
print(f"PASS p1_grantless: {res['error'][:60]}...")
