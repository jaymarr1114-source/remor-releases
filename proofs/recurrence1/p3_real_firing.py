"""P3 — REAL firing: the pumper fires due schedules through guarded_submit.

Fresh process, env unset (default ON). A 1-second schedule must produce
real fires observed through the spy: fire_count advances on the row and
each fire carries the schedule's goal. Not scheduled-then-asserted —
actually fired by the pumper thread on the clock.
"""
import os

os.environ.pop("REMOR_RECURRENCE_ENABLED", None)
os.environ.pop("REMOR_RECURRENCE_TIER", None)

from common import check, fresh_dir, SpySubmit, wait_for  # noqa: E402
from swarm_engine.services.recurrence import RecurrenceService  # noqa: E402

d = fresh_dir()
spy = SpySubmit()
svc = RecurrenceService(os.path.join(d, "r.db"), spy, poll_interval=0.1)
try:
    created = svc.create_schedule(
        goal="p3 firing probe", spec={"every_seconds": 1})
    check("created", created.get("ok") is True, str(created)[:160])
    sid = created["schedule_id"]

    fired = wait_for(lambda: len(spy.calls) >= 3, timeout=12.0)
    check("pumper fired >= 3 times in ~12s", fired,
          f"calls={len(spy.calls)}")
    check("fires carry the schedule goal",
          all(c["goal"] == "p3 firing probe" for c in spy.calls))

    # Cadence honesty: ~1s apart, not a burst, not drifting wildly.
    ats = [c["at"] for c in spy.calls[:3]]
    gaps = [b - a for a, b in zip(ats, ats[1:])]
    check("cadence ~1s (no burst, no stall)",
          all(0.5 < g < 2.5 for g in gaps),
          f"gaps={gaps}")

    got = svc.get_schedule(sid)
    check("fire_count recorded on the row",
          got["schedule"]["fire_count"] >= 3,
          str(got["schedule"])[:160])
    check("last_run_id recorded",
          bool(got["schedule"]["last_run_id"]))
finally:
    svc.close()

print("P3 PASS — real fires on the clock through guarded_submit")
