"""p5_revoked_mid_escalation: enforcement flips to SUSPENDED_SAFETY after
the fast answer. The router's live enforcement check (consulted after
the fast leg, before the deep path) must refuse the escalation with the
real reason; the deep path is never attempted; zero deep charge.

Note: the fast leg does not consult live enforcement state -- it relies
on the grant's enforcement_state_at_issue (RUNNING here). So setting the
flipped state before chat() faithfully models revocation landing between
the fast answer and the deep attempt."""
from common import make_router, make_grant, check

# Revocation has already landed by the time the router checks liveness
# (after fast, before deep).
router, substrate = make_router(
    enforcement_state=lambda: "SUSPENDED_SAFETY")
sg = make_grant(budget_s=400.0,
                enforcement_state_at_issue="RUNNING")
dg = make_grant(budget_s=2000.0,
                enforcement_state_at_issue="RUNNING")

out = router.chat("Think hard about this: what is the capital of France?",
                  student_grant=sg, deep_grant=dg, think_hard=True)

check("p5_fast_served", out["fast"]["ok"] is True,
      f"error={out['fast'].get('error')}")
check("p5_escalation_triggered", out["escalation"]["triggered"] is True)
check("p5_deep_not_attempted", out["deep"]["attempted"] is False)
check("p5_refusal_names_state",
      out["deep"]["ok"] is False
      and "SUSPENDED_SAFETY" in out["deep"]["error"]
      and "escalation_blocked" in out["deep"]["error"],
      f"error={out['deep'].get('error')}")
check("p5_zero_deep_charge",
      out["deep"].get("charged_s", 0.0) == 0.0
      and router._deep.grant_consumed_s(dg.grant_id) == 0.0)
check("p5_fast_charge_stands", out["fast"]["charged_s"] > 0,
      "the served fast answer was real work and stays charged")
