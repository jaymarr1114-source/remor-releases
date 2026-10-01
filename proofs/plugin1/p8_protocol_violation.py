"""p8: protocol violation — non-JSON stdout is a typed refusal."""
from common import FIX, check, fresh_service
import os

svc = fresh_service()
res = svc.register(os.path.join(FIX, "badjson"))
check(res["ok"], "badjson fixture registered")
res = svc.execute("tool.badjson/badjson", {"goal": "x"})
check(res.get("ok") is False, f"refused: {res}")
check(res["error"]["code"] == "PLUGIN_PROTOCOL_VIOLATION",
      f"typed protocol violation: {res['error']}")
print("P8 PASS")
