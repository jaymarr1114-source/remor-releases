"""P7 — honest refusal surface: bad specs, bad goals, unknown ids.

Fresh process. Every malformed input must produce a TYPED refusal
(BAD_SPEC_CODE / clear error / UNKNOWN_SCHEDULE_CODE) — never a
traceback, never a silent accept, never a row written for garbage.
"""
import os

os.environ.pop("REMOR_RECURRENCE_ENABLED", None)
os.environ.pop("REMOR_RECURRENCE_TIER", None)

from common import check, fresh_dir, SpySubmit  # noqa: E402
from swarm_engine.services.recurrence import (  # noqa: E402
    RecurrenceService, BAD_SPEC_CODE, UNKNOWN_SCHEDULE_CODE)

d = fresh_dir()
spy = SpySubmit()
svc = RecurrenceService(os.path.join(d, "r.db"), spy,
                        dispatcher_enabled=False)
try:
    bad_specs = [
        None, "every 5s", {}, {"every_seconds": 0},
        {"every_seconds": 0.5}, {"every_seconds": -3},
        {"every_seconds": "5"}, {"every_seconds": True},
        {"every_minutes": 0}, {"daily_at": "09:00"},
        {"every_seconds": 5, "every_minutes": 1},
    ]
    for spec in bad_specs:
        r = svc.create_schedule(goal="p7 probe", spec=spec)
        check(f"bad spec refused: {str(spec)[:40]}",
              r.get("ok") is not True and r.get("code") == BAD_SPEC_CODE,
              str(r)[:160])

    for goal in (None, "", "   ", 123):
        r = svc.create_schedule(goal=goal, spec={"every_seconds": 5})
        check(f"bad goal refused: {str(goal)[:20]!r}",
              r.get("ok") is not True and "goal" in str(r.get("error", "")),
              str(r)[:160])

    listed = svc.list_schedules()
    check("no garbage rows written",
          listed.get("schedules") == [], str(listed["schedules"])[:160])

    r = svc.cancel_schedule("does-not-exist")
    check("unknown cancel -> UNKNOWN_SCHEDULE_CODE",
          r.get("code") == UNKNOWN_SCHEDULE_CODE, str(r)[:160])
    r = svc.cancel_schedule("")
    check("empty cancel refused", r.get("ok") is not True)
    r = svc.get_schedule("does-not-exist")
    check("unknown get -> UNKNOWN_SCHEDULE_CODE",
          r.get("code") == UNKNOWN_SCHEDULE_CODE, str(r)[:160])
finally:
    svc.close()

print("P7 PASS — every malformed input refused with a typed code")
