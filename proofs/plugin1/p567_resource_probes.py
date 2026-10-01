"""p5/p6/p7: resource-exhaustion probes — rlimits contain the breach.

p5: infinite loop with a 2s CPU cap -> killed fast, no hang.
p6: 1GB allocation with a 128MB address-space cap -> dies, no OOM.
p7: 50MB file with a 1MB file-size cap -> SIGXFSZ, no disk fill.
Each asserts ok:false AND a tight wall-clock bound (containment, not luck).
"""
from common import FIX, check, fresh_service
import os
import sys
import time

which = sys.argv[1]
svc = fresh_service()

if which == "cpu":
    svc.register(os.path.join(FIX, "looper"))
    env = {"REMOR_SANDBOX_CPU_SECONDS": "2"}
    plugin, task = "tool.looper/looper", {}
elif which == "mem":
    svc.register(os.path.join(FIX, "hog"))
    env = {"REMOR_SANDBOX_AS_MB": "128"}
    plugin, task = "tool.hog/hog", {}
elif which == "fsize":
    svc.register(os.path.join(FIX, "fbomb"))
    env = {"REMOR_SANDBOX_FSIZE_MB": "1"}
    plugin, task = "tool.fbomb/fbomb", {}
else:
    raise SystemExit(f"unknown probe {which}")

start = time.time()
res = svc.execute(plugin, task, timeout_s=60, limits_env=env)
wall = time.time() - start
check(res.get("ok") is False, f"{which}: run not ok: {res}")
check(wall < 30, f"{which}: contained in {wall:.1f}s wall (no hang, no fill)")
code = res.get("error", {}).get("code")
check(code in ("PLUGIN_EXECUTION_FAILED", "PLUGIN_TIMEOUT"),
      f"{which}: typed failure code: {code}")
print(f"P5-7/{which} PASS — breach contained in {wall:.1f}s")
