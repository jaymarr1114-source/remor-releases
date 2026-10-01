"""p3: issuance, charging, and budget exhaustion through the inlet.

(a) The inlet's grants come from the single shared issuance path.
(b) Real turns deplete real budgets -- per-grant consumption tracked,
    no leakage across grants.
(c) Substrate exhaustion surfaces honestly in the served turn."""
import inspect
from common import make_service, check
from swarm_engine.curiosity.frm.grant import FrmGrant, issue_run_grant
from swarm_engine.services import chat_api as chat_api_mod
from swarm_engine.services import agent_api as agent_api_mod

# (a) one issuer, not two -------------------------------------------
src_chat = inspect.getsource(chat_api_mod)
src_agent = inspect.getsource(agent_api_mod)
check("p3a_chat_uses_shared_issuer", "issue_run_grant" in src_chat)
check("p3a_agent_uses_shared_issuer", "issue_run_grant" in src_agent)
g0 = issue_run_grant(domain="chat", estimated_cost_s=2.0)
check("p3a_grant_shape", isinstance(g0, FrmGrant)
      and abs(g0.budget_s - 122.0) < 1e-9, f"budget={g0.budget_s}")

# (b) charging depletes, per-grant, no leakage -----------------------
# NOTE: the student prunes per-grant consumption when the epoch second
# rolls over (correct per-epoch accounting), so consumption is captured
# immediately after each turn, before the next turn can prune it.
svc = make_service()
gids = []
consumed_now = []
for i in range(3):
    r = svc.chat(f"Name one planet. One sentence. ({i})")
    check(f"p3b_turn{i}_ok", r["ok"] is True)
    gid = r["inlet"]["student_grant_id"]
    gids.append(gid)
    consumed_now.append(svc.grant_consumed_s(gid))
check("p3b_distinct_grants", len(set(gids)) == 3, gids)
for gid, c in zip(gids, consumed_now):
    check("p3b_consumed", c > 0, f"{gid[:8]} consumed={c}")

# (c) exhaust the student budget pool, then serve --------------------
sub = svc._student_substrate
mc = svc._student_mc_id
# Drain the pool via the real public charge path.
while True:
    exhausted, state = sub.charge(mc, 3600.0)
    if exhausted:
        break
r = svc.chat("Say hello in one short sentence.")
check("p3c_still_serves", r["ok"] is True, f"ok={r['ok']}")
# The pool was already exhausted, so this charge did not CAUSE
# exhaustion (mc_exhausted=False) -- but the pool state is reported
# honestly as "exhausted". Both are true; the state string is the
# exhaustion signal.
check("p3c_exhaustion_honest",
      r["fast"].get("mc_charge_state") == "exhausted",
      f"mc_exhausted={r['fast'].get('mc_exhausted')} "
      f"state={r['fast'].get('mc_charge_state')}")
check("p3c_charged_despite_exhaustion",
      r["fast"]["charged_s"] > 0)
print("BATTERY p3 PASS")
