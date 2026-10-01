"""p4_grantless_both: no grants anywhere. The student inlet refuses,
the router refuses the escalation for lack of a deep grant, AND the
deep GrantedCognitionProvider itself refuses grantless. Zero charges."""
from common import make_router, check

router, substrate = make_router()
out = router.chat("Think hard about this: what is 2+2?",
                  student_grant=None, deep_grant=None, think_hard=True)

check("p4_fast_refused_no_grant",
      out["fast"]["ok"] is False
      and "student_refused:no_grant" in out["fast"]["error"],
      f"error={out['fast'].get('error')}")
check("p4_escalation_still_triggered",
      out["escalation"]["triggered"] is True)
check("p4_deep_refused_no_grant",
      out["deep"]["attempted"] is False
      and "route_refused:no_deep_grant" in out["deep"]["error"])
check("p4_zero_charges",
      out["fast"].get("charged_s", 0.0) == 0.0
      and out["deep"].get("charged_s", 0.0) == 0.0
      and substrate.calls == [],
      f"calls={substrate.calls}")

# The deep inlet itself, grantless, direct:
res = router._deep.request_cognition(
    mc_id="auto-route-gate", prompt="hello",
    context={"purpose": "p4-direct"})
check("p4_deep_inlet_refuses_grantless",
      res.ok is False and "frm_grant" in res.error,
      f"error={res.error}")
