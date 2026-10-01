"""P1 — exposure defaults ON (the RECURRENCE-1 mandate #1 change).

Fresh process, REMOR_RECURRENCE_ENABLED UNSET: the service must be
enabled, create/list must work (not 501), the pumper thread must be
alive, and exposure() must report enabled:true with the tier decision
still open.
"""
import os

os.environ.pop("REMOR_RECURRENCE_ENABLED", None)
os.environ.pop("REMOR_RECURRENCE_TIER", None)

from common import check, fresh_dir, SpySubmit  # noqa: E402
from swarm_engine.services.recurrence import (  # noqa: E402
    RecurrenceService, is_enabled)

check("is_enabled() defaults True with env unset", is_enabled() is True)

d = fresh_dir()
spy = SpySubmit()
svc = RecurrenceService(os.path.join(d, "r.db"), spy, poll_interval=0.2)
try:
    check("service enabled with env unset", svc.enabled is True)
    check("pumper thread started",
          svc._pumper is not None and svc._pumper.is_alive())

    exp = svc.exposure()
    check("exposure() ok", exp.get("ok") is True)
    check("exposure() enabled true", exp.get("enabled") is True)
    check("tier decision still OPEN",
          "OPEN" in exp.get("tier_decision", ""),
          str(exp.get("tier_decision"))[:80])

    created = svc.create_schedule(
        goal="p1 default-on probe", spec={"every_seconds": 60})
    check("create_schedule works (not 501)", created.get("ok") is True,
          str(created)[:200])
    check("schedule_id returned", bool(created.get("schedule_id")))

    listed = svc.list_schedules()
    check("list_schedules works",
          listed.get("ok") is True and len(listed["schedules"]) == 1,
          str(listed)[:200])
finally:
    svc.close()

print("P1 PASS — exposure defaults ON, no env var needed")
