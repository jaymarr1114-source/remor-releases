"""p2: happy path — a real plugin runs end-to-end with provenance."""
from common import check, fresh_service
import json
import time

svc = fresh_service()
marker = f"marker-{time.time_ns()}"
task = {"goal": "prove the loop", "marker": marker}
res = svc.execute("tool.echo/echo", task)
check(res.get("ok") is True, f"execute ok: {res}")
check(res["result"] == {"echo": task}, "task echoed back intact")
check(res["plugin"] == "tool.echo/echo", "plugin ref in result")
prov = res["provenance"]
check(prov["protocol"] == "remor-plugin/1", "protocol in provenance")
check(len(prov["manifest_sha256"]) == 64, "manifest pin in provenance")
check(prov["entry_hash_verified_at_load"] is True, "hash verification attested")
check("echo_output.txt" in res["generated_files"], "generated files diffed")
check("mode" in res["sandbox"] and "limits" in res["sandbox"],
      "sandbox disclosure present")
json.dumps(res)  # wire-serializable
print("P2 PASS")
