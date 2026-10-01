"""P4 — listing honesty: created schedules list back with true state.

Fresh process. Two schedules (one due-immediately, one delayed) must
both appear in GET / list_schedules with honest fields: active flags,
specs round-trip, tier_flag recorded, fire counts truthful.
"""
import os

os.environ.pop("REMOR_RECURRENCE_ENABLED", None)
os.environ.pop("REMOR_RECURRENCE_TIER", None)

from common import check, fresh_dir, SpySubmit  # noqa: E402
from swarm_engine.services.recurrence import RecurrenceService  # noqa: E402

d = fresh_dir()
spy = SpySubmit()
svc = RecurrenceService(os.path.join(d, "r.db"), spy, poll_interval=0.2,
                        dispatcher_enabled=False)
try:
    a = svc.create_schedule(goal="p4 first",
                            spec={"every_seconds": 30})
    b = svc.create_schedule(goal="p4 second",
                            spec={"every_minutes": 5},
                            start_in_seconds=120)
    check("both created", a.get("ok") and b.get("ok"))

    listed = svc.list_schedules()
    check("list ok", listed.get("ok") is True)
    by_goal = {s["goal"]: s for s in listed["schedules"]}
    check("both schedules listed", set(by_goal) == {"p4 first", "p4 second"},
          str(sorted(by_goal)))
    check("specs round-trip",
          by_goal["p4 first"]["spec"] == {"every_seconds": 30}
          and by_goal["p4 second"]["spec"] == {"every_minutes": 5})
    check("both active", by_goal["p4 first"]["active"]
          and by_goal["p4 second"]["active"])
    check("delayed schedule not yet due",
          by_goal["p4 second"]["next_fire_at"] >
          by_goal["p4 first"]["next_fire_at"])
    check("tier_flag recorded", by_goal["p4 first"]["tier_flag"] == "unset",
          str(by_goal["p4 first"]["tier_flag"]))
    check("fire_count starts at 0",
          by_goal["p4 first"]["fire_count"] == 0)
    check("no phantom fires with pumper off", spy.calls == [])
finally:
    svc.close()

print("P4 PASS — listing is honest")
