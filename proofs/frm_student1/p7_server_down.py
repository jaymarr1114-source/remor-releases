"""P7: server down + valid grant -> honest failure, zero charge."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SubstrateDouble, make_grant
from governed_student import GovernedStudent

sub = SubstrateDouble()
gs = GovernedStudent(sub, "mc-gate-7")
grant = make_grant(budget_s=400.0)
# Port 9 (discard) is never the server: transport must fail.
res = gs.turn("hello there", frm_grant=grant, port=9)
assert res["ok"] is False, f"dead server must fail honestly: {res}"
assert "transport" in res["error"], f"must name the transport: {res['error']}"
assert res["charged_s"] == 0.0, "failed turn charges nothing"
assert sub.calls == [], f"no substrate charge on failure: {sub.calls}"
assert gs.grant_consumed_s(grant.grant_id) == 0.0
print(f"PASS p7_server_down: {res['error'][:60]}... charged=0.0")
