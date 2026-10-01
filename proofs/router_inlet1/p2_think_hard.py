"""p2: explicit think_hard through the inlet -- real 8B deep turn.

Provisional label on the fast answer; the deeper answer arrives labeled
when materially better. The user never chose a brain."""
from common import make_service, check

svc = make_service()
prompt = ("Think hard: compare how central banks fight inflation, "
          "why their tools differ, and how exchange rates complicate "
          "the response. Show the reasoning in depth.")
r = svc.chat(prompt, think_hard=True)

check("p2_ok", r["ok"] is True, f"ok={r['ok']}")
check("p2_escalated", r["escalation"]["triggered"] is True)
check("p2_mode_explicit", r["escalation"]["mode"] == "explicit",
      r["escalation"]["mode"])

fast = r["fast"]
check("p2_fast_provisional", fast["provisional"] is True)
check("p2_provisional_in_text",
      fast["served_text"].startswith("[provisional"),
      fast["served_text"][:60])

deep = r["deep"]
check("p2_deep_attempted", deep["attempted"] is True)
check("p2_deep_ok", deep.get("ok") is True,
      f"error={deep.get('error')}")
check("p2_deep_provenance", "borrowed:qwen3@" in deep.get("provenance", ""),
      deep.get("provenance"))
check("p2_deep_charged", deep.get("charged_s", 0) > 0,
      f"charged={deep.get('charged_s')}")

served = r["served"]
check("p2_served_deeper_label",
      served["label"] in ("[deeper result]", "[final]"),
      f"label={served['label']} via={served['via']} "
      f"diff={served.get('diff_ratio')}")
check("p2_deep_grant_issued", r["inlet"]["deep_grant_id"] is not None)
print("BATTERY p2 PASS")
