"""p3: unsigned / tampered / symlinked code is never executed."""
from common import FIX, check, fresh_service
import os

svc = fresh_service()

res = svc.register(os.path.join(FIX, "unsigned"))
check(not res["ok"] and res["error"]["code"] == "PLUGIN_MANIFEST_INVALID",
      f"no-manifest package refused: {res}")

res = svc.register(os.path.join(FIX, "tampered"))
check(not res["ok"] and res["error"]["code"] == "PLUGIN_MANIFEST_INVALID",
      f"hash-mismatched package refused: {res}")
check("sha256" in res["error"]["reason"] or "match" in res["error"]["reason"],
      "refusal names the pin mismatch")

res = svc.register(os.path.join(FIX, "symlinked"))
check(not res["ok"] and res["error"]["code"] == "PLUGIN_MANIFEST_INVALID",
      f"symlink entry refused: {res}")

# none of the adversarial packages entered the registry
names = [(p["kind"], p["name"]) for p in svc.list()["plugins"]]
for bad in (("tool.tampered", "tampered"), ("tool.symlinked", "symlinked")):
    check(bad not in names, f"{bad} not in registry")
print("P3 PASS — no unsigned code executes, ever")
