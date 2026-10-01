"""p7_budget_exhausted: the deep grant's budget is far below the
teacher's estimated cost. The deep inlet defers with the real reason and
zero charge; the fast answer stands as served (provisional label kept
honest by the note)."""
from common import make_router, make_grant, check

router, substrate = make_router()
sg = make_grant(budget_s=400.0)
dg = make_grant(budget_s=1.0)  # teacher estimate is tens of seconds

out = router.chat("Think hard: why do we dream?",
                  student_grant=sg, deep_grant=dg, think_hard=True)

check("p7_fast_ok", out["fast"]["ok"] is True)
check("p7_deep_attempted", out["deep"]["attempted"] is True)
check("p7_deep_deferred_budget",
      out["deep"].get("ok") is False
      and "insufficient_grant" in (out["deep"].get("error") or ""),
      f"error={out['deep'].get('error')}")
check("p7_zero_deep_charge",
      out["deep"].get("charged_s", 0.0) == 0.0
      and router._deep.grant_consumed_s(dg.grant_id) == 0.0)
check("p7_served_fast_with_note",
      out["served"]["via"] == "fast"
      and out["served"].get("note") is not None)
