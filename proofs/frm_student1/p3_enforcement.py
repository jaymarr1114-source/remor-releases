"""P3: a grant issued under a non-RUNNING enforcement state refuses."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SubstrateDouble, make_grant
from governed_student import GovernedStudent, R_ENFORCEMENT

sub = SubstrateDouble()
gs = GovernedStudent(sub, "mc-gate-3")
for state in ("SUSPENDED_SAFETY", "HARD_SHUTDOWN_RESOURCE", "WARNING_1",
              "BANNED_6M", "SOME_FUTURE_STATE"):
    grant = make_grant(enforcement_state_at_issue=state)
    res = gs.turn("hello there", frm_grant=grant)
    assert res["ok"] is False, f"state {state} must refuse, got {res}"
    assert R_ENFORCEMENT in res["error"], f"wrong refusal: {res['error']}"
assert sub.calls == [], "refusal must not touch the charge path"
print("PASS p3_enforcement: 5 non-RUNNING states refused (unknown too)")
