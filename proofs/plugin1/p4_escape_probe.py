"""p4: filesystem-escape probe — the malicious plugin's absolute-path
write is PREVENTED by the mount-namespace + pivot_root jail, not merely
detected after the fact (PLUGIN-FIX-1).

Honest adversarial contrast: the evil fixture genuinely attempts the
write (its stderr names the canary path — FileNotFoundError against the
jail wall), yet the host canary NEVER comes into existence. The run is
refused because the plugin crashed, and the canary tripwire stays
silent (a fired tripwire would mean the jail was breached)."""
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
check(not os.path.isfile(canary),
      "host canary ABSENT after the run — the write was prevented, "
      "not merely detected")
check(res.get("ok") is False, f"run refused: {res}")
check(res["error"]["code"] == "PLUGIN_EXECUTION_FAILED",
      f"plugin crashed against the jail wall: {res['error']['code']}")
detail = res["error"].get("detail", {})
check(canary in detail.get("stderr_head", ""),
      "attack was genuine: stderr names the attempted canary path")
check("escaped_paths" not in detail,
      "canary tripwire silent — the jail was not breached")
cont = detail.get("containment", {})
check(cont.get("enforced") is True, "containment posture disclosed")
check(cont.get("mount_namespace") is True, "mount namespace enforced")
check("pivot_root" in cont.get("root", ""),
      "pivot_root is the filesystem boundary")
print("P4 PASS — escape prevented by the jail; residual disclosed")
