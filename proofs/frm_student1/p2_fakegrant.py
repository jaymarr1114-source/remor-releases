"""P2: a non-FrmGrant 'grant' is refused fail-closed, naming the confusion."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SubstrateDouble
from governed_student import GovernedStudent, R_BAD_GRANT


class FakeGrant:
    """A plausible-looking impostor: has budget_s and grant_id but is
    not an FrmGrant."""
    grant_id = "fake-123"
    budget_s = 9999.0
    max_concurrent = 99
    epoch_id = 1
    enforcement_state_at_issue = "RUNNING"


sub = SubstrateDouble()
gs = GovernedStudent(sub, "mc-gate-2")
for impostor in (FakeGrant(), {"grant_id": "x", "budget_s": 5.0}, "grant!"):
    res = gs.turn("hello there", frm_grant=impostor)
    assert res["ok"] is False, f"impostor must refuse, got {res}"
    assert R_BAD_GRANT in res["error"], f"wrong refusal: {res['error']}"
assert sub.calls == [], "refusal must not touch the charge path"
print("PASS p2_fakegrant: 3 impostor types refused, confusion named")
