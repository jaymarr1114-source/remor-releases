"""P8 — end-to-end through the REAL merged adapter route table.

Fresh process, env unset (default ON). build_services() is the actual
product wiring: the recurrence routes must overwrite the tasks_api 501
for POST /api/tasks/recurring, and a full create -> list -> cancel
cycle must work through contract_types.dispatch — the same dispatch
path the HTTP server uses.
"""
import os
import shutil
import tempfile

os.environ.pop("REMOR_RECURRENCE_ENABLED", None)
os.environ.pop("REMOR_RECURRENCE_TIER", None)

from common import check  # noqa: E402
from swarm_engine.services.contract_types import dispatch  # noqa: E402
from swarm_engine.services.http_adapter import (  # noqa: E402
    build_services, close_services)

tmp = tempfile.mkdtemp(prefix="r1_p8_")
svc = build_services(tmp)
try:
    routes = svc["contract_routes"]
    for key in (("POST", "/api/tasks/recurring"),
                ("GET", "/api/tasks/recurring"),
                ("POST", "/api/tasks/recurring/cancel"),
                ("GET", "/api/tasks/recurring/exposure")):
        check(f"route present: {key}", key in routes)

    created = dispatch(routes, "POST", "/api/tasks/recurring", {
        "goal": "p8 e2e probe", "spec": {"every_seconds": 60}})
    check("POST creates (not 501)", created.get("ok") is True,
          str(created)[:200])
    sid = created["schedule_id"]
    check("no unavailable payload on success",
          "unavailable" not in created)

    listed = dispatch(routes, "GET", "/api/tasks/recurring", {})
    check("GET lists",
          listed.get("ok") is True and any(
              s["schedule_id"] == sid for s in listed["schedules"]),
          str(listed)[:200])

    exp = dispatch(routes, "GET", "/api/tasks/recurring/exposure", {})
    check("exposure route live",
          exp.get("ok") is True and exp.get("enabled") is True,
          str(exp)[:200])
    check("tier decision surfaced",
          "OPEN" in exp.get("tier_decision", ""))

    cancelled = dispatch(routes, "POST", "/api/tasks/recurring/cancel",
                         {"schedule_id": sid})
    check("cancel through the route table",
          cancelled.get("ok") is True, str(cancelled)[:160])

    # The service is in the services dict (lifecycle owned by adapter).
    check("recurrence service registered",
          "recurrence" in svc and svc["recurrence"].enabled is True)
finally:
    try:
        close_services(svc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

print("P8 PASS — the live route table serves real recurrence end-to-end")
