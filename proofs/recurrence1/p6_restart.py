"""P6 — restart survival: a fresh service instance re-arms persisted rows.

Fresh process part 1: create a schedule delayed 2s out, then close the
service (pumper dead, process-equivalent shutdown). Part 2 (same
process, NEW service object over the SAME db file — the restart): the
schedule must fire without any in-memory state carried over. This
proves zero in-memory-only state: next_fire_at lives in sqlite.
"""
import os

os.environ.pop("REMOR_RECURRENCE_ENABLED", None)
os.environ.pop("REMOR_RECURRENCE_TIER", None)

from common import check, fresh_dir, SpySubmit, wait_for  # noqa: E402
from swarm_engine.services.recurrence import RecurrenceService  # noqa: E402

d = fresh_dir()
db = os.path.join(d, "r.db")

spy1 = SpySubmit()
svc1 = RecurrenceService(db, spy1, poll_interval=0.1)
created = svc1.create_schedule(
    goal="p6 restart probe", spec={"every_seconds": 60},
    start_in_seconds=2)
check("created pre-restart", created.get("ok") is True)
sid = created["schedule_id"]
svc1.close()  # full shutdown: pumper dead, sqlite closed
check("nothing fired before restart", spy1.calls == [])

# "Restart": brand-new service object, same DB file, fresh spy.
spy2 = SpySubmit()
svc2 = RecurrenceService(db, spy2, poll_interval=0.1)
try:
    check("row survived the restart",
          svc2.get_schedule(sid)["schedule"]["active"] is True)
    check("re-armed instance fires the due schedule",
          wait_for(lambda: len(spy2.calls) >= 1, timeout=10.0),
          f"calls={len(spy2.calls)}")
    check("restarted fire carries the goal",
          spy2.calls[0]["goal"] == "p6 restart probe")
finally:
    svc2.close()

print("P6 PASS — schedules survive restart and re-arm")
