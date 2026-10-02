"""p4: adversarial -- every case fails closed with the real reason.

(i)   enforcement flips to non-RUNNING mid-turn: fast stands, deep
      blocked, zero deep charge.
(ii)  issuance raises: inlet refuses with the real reason, no raise,
      zero charges.
(iii) deep transport down (corrupt weights): deep_failed:transport,
      fast answer stands, zero deep charge.
(iv)  deep grant insufficient vs estimate: honest deferral, fast stands.
"""
import os
from common import (make_service, make_small_deep_wiring,
                    make_corrupt_deep_wiring, check)
from swarm_engine.services.chat_api import ChatService
from swarm_engine.curiosity.frm.grant import issue_run_grant

HARD = ("Think hard: why do central banks differ, how do their tools "
        "compare, and what complicates the response?")

# (i) enforcement flip mid-turn --------------------------------------
svc = make_service(
    enforcement_state=lambda: "SUSPENDED_SAFETY")
r = svc.chat(HARD, think_hard=True)
check("p4i_fast_ok", r["ok"] is True and r["fast"]["ok"] is True)
deep = r["deep"]
check("p4i_deep_blocked",
      deep.get("ok") is False
      and "escalation_blocked" in deep.get("error", ""),
      deep.get("error"))
check("p4i_zero_deep_charge", deep.get("charged_s", -1) == 0.0)
check("p4i_fast_label_prov_sticky",
      r["served"]["label"].startswith("[provisional"),
      r["served"]["label"])

# (ii) issuance failure ----------------------------------------------
class BrokenIssuer(ChatService):
    def issue_student_grant(self, prompt):
        raise RuntimeError("FRM unavailable: epoch store locked")

svc2 = make_service()
svc2.__class__ = BrokenIssuer  # same real init, broken issuer only
r = svc2.chat("Say hello.")
check("p4ii_no_raise", r["ok"] is False)
check("p4ii_real_reason",
      "issuance_failed" in r["fast"]["error"]
      and "FRM unavailable" in r["fast"]["error"],
      r["fast"]["error"])
check("p4ii_zero_charge", r["fast"]["charged_s"] == 0.0)
check("p4ii_served_refusal", r["served"]["text"].startswith("[final]"))

# (iii) deep transport down ------------------------------------------
corrupt = "/tmp/router-inlet1-corrupt.gguf"
with open(corrupt, "wb") as f:
    f.write(b"not a gguf file at all")
svc3 = ChatService("/tmp/router-inlet1-gate3",
                   llm_wiring=make_corrupt_deep_wiring(corrupt))
r = svc3.chat(HARD, think_hard=True)
check("p4iii_fast_ok", r["fast"]["ok"] is True)
deep = r["deep"]
check("p4iii_transport_error",
      deep.get("ok") is False
      and "deep_failed:transport" in deep.get("error", ""),
      (deep.get("error") or "")[:100])
check("p4iii_zero_deep_charge", deep.get("charged_s", -1) == 0.0)
check("p4iii_fast_stands", r["served"]["via"] == "fast")

# (iv) deep grant insufficient vs estimate ----------------------------
class TinyDeepGrant(ChatService):
    def issue_deep_grant(self, prompt):
        # Real issuer, real grant -- but a trivial estimate so the
        # provider's pre-borrow check defers honestly.
        return issue_run_grant(domain="chat", estimated_cost_s=0.0,
                               margin_s=0.0,
                               note="gate: insufficient deep grant")

svc4 = make_service()
svc4.__class__ = TinyDeepGrant
r = svc4.chat(HARD, think_hard=True)
check("p4iv_fast_ok", r["fast"]["ok"] is True)
deep = r["deep"]
check("p4iv_deferred",
      deep.get("ok") is False
      and "insufficient_grant" in deep.get("error", ""),
      (deep.get("error") or "")[:120])
check("p4iv_zero_deep_charge", deep.get("charged_s", -1) == 0.0)
print("BATTERY p4 PASS")
