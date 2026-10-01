"""P5 — CAUSAL cancellation: cancel stops future firings, adversarially.

Fresh process. A 1-second schedule fires twice (proving the pumper is
hot), then cancel mid-stream. After the cancel returns, the fire count
must NEVER increase again — 3.5s of observation (>3 intervals) with
zero orphan firings. This is the adversarial case: the pumper is
actively due-firing when the cancel lands.
"""
import os
import time

os.environ.pop("REMOR_RECURRENCE_ENABLED", None)
os.environ.pop("REMOR_RECURRENCE_TIER", None)

from common import check, fresh_dir, SpySubmit, wait_for  # noqa: E402
from swarm_engine.services.recurrence import RecurrenceService  # noqa: E402

d = fresh_dir()
spy = SpySubmit()
svc = RecurrenceService(os.path.join(d, "r.db"), spy, poll_interval=0.1)
try:
    created = svc.create_schedule(
        goal="p5 cancel probe", spec={"every_seconds": 1})
    sid = created["schedule_id"]

    check("pumper is hot (>=2 fires before cancel)",
          wait_for(lambda: len(spy.calls) >= 2, timeout=10.0),
          f"calls={len(spy.calls)}")

    cancelled = svc.cancel_schedule(sid)
    check("cancel returns ok", cancelled.get("ok") is True,
          str(cancelled)[:160])
    calls_at_cancel = len(spy.calls)
    count_at_cancel = svc.get_schedule(sid)["schedule"]["fire_count"]

    # Adversarial wait: >3 intervals where fires WOULD have happened.
    time.sleep(3.5)

    check("zero orphan firings after cancel",
          len(spy.calls) == calls_at_cancel,
          f"calls grew {calls_at_cancel} -> {len(spy.calls)}")
    after = svc.get_schedule(sid)["schedule"]
    check("fire_count frozen after cancel",
          after["fire_count"] == count_at_cancel,
          f"{count_at_cancel} -> {after['fire_count']}")
    check("row deactivated", after["active"] is False)

    # Double-cancel is an honest refusal, not a crash.
    again = svc.cancel_schedule(sid)
    check("double cancel refused honestly",
          again.get("ok") is not True and "already cancelled" in
          str(again.get("error", "")),
          str(again)[:160])
finally:
    svc.close()

print("P5 PASS — cancel causally stops all future firings")
