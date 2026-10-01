"""p8_anti_masquerade: the provisional label is IN the served fast text
when escalation triggers, and ABSENT (final instead) when it doesn't.
This is the user-visible contract, not just internal logic."""
from common import make_router, make_grant, check

router, substrate = make_router()
sg = make_grant(budget_s=400.0)
dg = make_grant(budget_s=1.0)  # deep defers; we only inspect fast text

esc = router.chat("Think hard: what is 2+2?",
                  student_grant=sg, deep_grant=dg, think_hard=True)
check("p8_provisional_present_when_escalated",
      esc["fast"]["ok"] is True
      and "[provisional" in esc["fast"]["served_text"]
      and "[final]" not in esc["fast"]["served_text"],
      f"served={esc['fast']['served_text'][:90]}")

plain = router.chat("Say hi in one short sentence.",
                    student_grant=make_grant(budget_s=400.0))
check("p8_final_present_when_not_escalated",
      plain["fast"]["ok"] is True
      and "[final]" in plain["fast"]["served_text"]
      and "provisional" not in plain["fast"]["served_text"].lower(),
      f"served={plain['fast']['served_text'][:90]}")
