"""p4: filesystem-escape probe — the malicious plugin's write is detected
and the run is refused. Honest adversarial contrast: the canary file WILL
exist (same-user mode cannot prevent an absolute-path write at the OS
level), which proves the probe really escaped; the run must still come
back ok:false with PLUGIN_POLICY_VIOLATION, never silent success."""
from common import FIX, SCRATCH, check, fresh_service
import os

svc = fresh_service()
res = svc.register(os.path.join(FIX, "evil"))
check(res["ok"], "evil fixture registered (it is signed; it is malicious)")

canary = os.path.join(SCRATCH, "canary_escape_proof.txt")
if os.path.exists(canary):
    os.remove(canary)
res = svc.execute("tool.evil/evil", {"canary": canary},
                  canary_paths=[canary])
check(os.path.isfile(canary),
      "probe REALLY escaped (canary exists) — the attack was genuine")
check(res.get("ok") is False, f"run refused: {res}")
check(res["error"]["code"] == "PLUGIN_POLICY_VIOLATION",
      f"typed policy violation: {res['error']}")
check(canary in res["error"]["detail"]["escaped_paths"],
      "evidence names the escaped path")
check("sandbox" in res["error"]["detail"], "sandbox posture disclosed")
os.remove(canary)
print("P4 PASS — escape detected, refused, reported; residual disclosed")
