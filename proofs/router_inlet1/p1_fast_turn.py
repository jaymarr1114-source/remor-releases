"""p1: one production chat turn end-to-end through the inlet.

Real student server, real grant from the shared issuance path, labels
in the served output."""
from common import make_service, check
from swarm_engine.curiosity.frm.grant import FrmGrant

svc = make_service()
r = svc.chat("Say hello in one short sentence.")

check("p1_ok", r["ok"] is True, f"ok={r['ok']}")
served = r["served"]
check("p1_final_label", served["label"] == "[final]",
      f"label={served['label']}")
check("p1_label_in_text", served["text"].startswith("[final]"),
      served["text"][:60])
check("p1_via_fast", served["via"] == "fast", served["via"])
check("p1_not_escalated", r["escalation"]["triggered"] is False)

fast = r["fast"]
check("p1_fast_charged", fast["charged_s"] > 0,
      f"charged={fast['charged_s']}")
check("p1_provenance", "governed-student" in fast.get("provenance", ""),
      fast.get("provenance"))

gid = r["inlet"]["student_grant_id"]
check("p1_grant_tracked", gid is not None)
consumed = svc.grant_consumed_s(gid)
check("p1_grant_consumed", consumed > 0, f"consumed={consumed}")
check("p1_no_deep_grant", r["inlet"]["deep_grant_id"] is None)
print("BATTERY p1 PASS")
