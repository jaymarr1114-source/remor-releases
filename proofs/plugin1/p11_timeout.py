"""p11: runaway plugin is killed at its time budget (PLUGIN_TIMEOUT)."""
from common import FIX, check, fresh_service
import os
import time

svc = fresh_service()
res = svc.register(os.path.join(FIX, "sleeper"))
check(res["ok"], "sleeper fixture registered")
start = time.time()
res = svc.execute("tool.sleeper/sleeper", {}, timeout_s=2)
wall = time.time() - start
check(not res["ok"] and res["error"]["code"] == "PLUGIN_TIMEOUT",
      f"killed at budget: {res}")
check(wall < 10, f"killed promptly ({wall:.1f}s, not 30s)")
print("P11 PASS")
