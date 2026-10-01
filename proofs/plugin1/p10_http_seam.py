"""p10: the HTTP seam — POST /api/execute/plugin end-to-end through the
real route table (dispatch + routes_for_execute), not around it."""
from common import SCRATCH, check
import json
import os
import tempfile

from swarm_engine.services.contract_types import dispatch
from swarm_engine.services.execute_api import routes_for_execute
from swarm_engine.plugin.service import PluginService

td = tempfile.mkdtemp(prefix="plugin1-seam-")
try:
    sandbox = os.path.join(td, "sandbox")
    os.makedirs(sandbox)
    reg = os.path.join(SCRATCH, "registry")
    # echo is already registered in the shared scratch registry
    routes = routes_for_execute(None, sandbox, plugin_registry_dir=reg)
    check(("POST", "/api/execute/plugin") in routes, "route present")

    res = dispatch(routes, "POST", "/api/execute/plugin",
                   {"plugin": "tool.echo/echo", "task": {"via": "http"}})
    check(res.get("ok") is True, f"seam happy path: {res}")
    check(res["result"] == {"echo": {"via": "http"}}, "task round-trips")
    json.dumps(res)

    res = dispatch(routes, "POST", "/api/execute/plugin",
                   {"plugin": "ghost/boo"})
    check(not res["ok"] and res["error"]["code"] == "PLUGIN_UNKNOWN",
          f"seam unknown refusal: {res}")

    res = dispatch(routes, "POST", "/api/execute/plugin",
                   {"plugin": "tool.echo/echo", "task": [1, 2]})
    check(not res["ok"] and res["error"]["code"] == "PLUGIN_INVALID_TASK",
          f"seam bad task refused: {res}")
finally:
    import shutil
    shutil.rmtree(td, ignore_errors=True)
print("P10 PASS — the route is real end-to-end")
