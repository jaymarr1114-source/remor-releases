"""p2_explicit_escalation: 'think hard' prompt. Fast answer served first
with the provisional label; deep path runs the REAL 8B under its own
grant; the deeper result arrives labeled. (~2-3 min: real inference.)"""
from common import make_router, make_grant, check

router, substrate = make_router()
sg = make_grant(budget_s=400.0)
dg = make_grant(budget_s=2000.0)

prompt = ("Think hard about this: is free will compatible with "
          "determinism? Explain your reasoning step by step.")
out = router.chat(prompt, student_grant=sg, deep_grant=dg)

check("p2_escalation_triggered", out["escalation"]["triggered"] is True)
check("p2_mode_explicit", out["escalation"]["mode"] == "explicit",
      f"mode={out['escalation']['mode']}")
check("p2_fast_provisional_label",
      out["fast"]["provisional"] is True
      and "[provisional" in out["fast"]["served_text"],
      f"served={out['fast']['served_text'][:80]}")
check("p2_deep_attempted", out["deep"]["attempted"] is True)
check("p2_deep_ok", out["deep"].get("ok") is True,
      f"error={out['deep'].get('error')}")
check("p2_deep_provenance",
      (out["deep"].get("provenance") or "").startswith("borrowed:qwen3@"),
      f"prov={out['deep'].get('provenance')}")
check("p2_deep_charged", (out["deep"].get("charged_s") or 0) > 0,
      f"charged={out['deep'].get('charged_s')}")
check("p2_student_charged", out["fast"]["charged_s"] > 0)
print(f"  diff_ratio={out['served'].get('diff_ratio')} "
      f"via={out['served']['via']} materially_better="
      f"{out['served'].get('materially_better')}", flush=True)
check("p2_deeper_label_when_served",
      out["served"]["via"] != "deep"
      or "[deeper result]" in out["served"]["text"])
