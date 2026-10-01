"""p3_heuristic_escalation: a hard prompt with no 'think hard' phrase.
The heuristic (>=2 surface signals) must trigger escalation; deep path
runs the REAL 8B. (~2-3 min: real inference.)"""
from common import make_router, make_grant, check

router, substrate = make_router()
sg = make_grant(budget_s=400.0)
dg = make_grant(budget_s=2000.0)

prompt = ("Why do economies inflate and how should a central bank "
          "respond? Compare two schools of thought.")
out = router.chat(prompt, student_grant=sg, deep_grant=dg)

check("p3_escalation_triggered", out["escalation"]["triggered"] is True,
      f"reasons={out['escalation']['reasons']}")
check("p3_mode_heuristic", out["escalation"]["mode"] == "heuristic",
      f"mode={out['escalation']['mode']}")
check("p3_fast_provisional_label",
      "[provisional" in out["fast"]["served_text"])
check("p3_deep_ok", out["deep"].get("ok") is True,
      f"error={out['deep'].get('error')}")
check("p3_deep_provenance",
      (out["deep"].get("provenance") or "").startswith("borrowed:qwen3@"))
print(f"  diff_ratio={out['served'].get('diff_ratio')} "
      f"via={out['served']['via']} materially_better="
      f"{out['served'].get('materially_better')}", flush=True)
print(f"  fast_text={out['fast'].get('text','')[:100]}", flush=True)
print(f"  deep_text={out['deep'].get('text','')[:100]}", flush=True)
