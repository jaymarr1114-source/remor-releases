"""p1_fast_only: an easy prompt takes the fast path only. No escalation,
no deep attempt, deep grant untouched, student charged real seconds."""
from common import make_router, make_grant, check

router, substrate = make_router()
sg = make_grant(budget_s=400.0)
dg = make_grant(budget_s=1000.0)

out = router.chat("Say hello in one short sentence.",
                  student_grant=sg, deep_grant=dg)

check("p1_no_escalation", out["escalation"]["triggered"] is False,
      f"reasons={out['escalation']['reasons']}")
check("p1_fast_ok", out["fast"]["ok"] is True,
      f"error={out['fast'].get('error')}")
check("p1_deep_not_attempted", out["deep"]["attempted"] is False)
check("p1_served_fast_final",
      out["served"]["via"] == "fast" and "[final]" in out["served"]["text"]
      and "provisional" not in out["served"]["text"].lower())
check("p1_student_charged", out["fast"]["charged_s"] > 0,
      f"charged={out['fast']['charged_s']}")
check("p1_student_provenance",
      out["fast"]["provenance"].startswith("governed-student:qwen3-0.6b@"))
check("p1_deep_grant_untouched",
      router._deep.grant_consumed_s(dg.grant_id) == 0.0)
check("p1_substrate_got_student_charge",
      any(c[0] == "auto-route-gate" and c[1] > 0 for c in substrate.calls))
