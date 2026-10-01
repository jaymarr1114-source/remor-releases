"""P2 — the kill switch: REMOR_RECURRENCE_ENABLED=0 disables the surface.

Fresh process with the env var explicitly off: create/list/cancel must
return the typed recurring_disabled 501 (not a crash, not a silent
empty), the pumper must never start, and exposure() must report
enabled:false.
"""
import os

os.environ["REMOR_RECURRENCE_ENABLED"] = "0"
os.environ.pop("REMOR_RECURRENCE_TIER", None)

from common import check, fresh_dir, SpySubmit  # noqa: E402
from swarm_engine.services.recurrence import (  # noqa: E402
    RecurrenceService, is_enabled, DISABLED_CODE)

check("is_enabled() False with =0", is_enabled() is False)

d = fresh_dir()
spy = SpySubmit()
svc = RecurrenceService(os.path.join(d, "r.db"), spy, poll_interval=0.2)
try:
    check("service disabled", svc.enabled is False)
    check("pumper never starts when disabled", svc._pumper is None)

    created = svc.create_schedule(
        goal="p2 kill-switch probe", spec={"every_seconds": 60})
    check("create refused", created.get("ok") is not True,
          str(created)[:160])
    un = created.get("unavailable") or {}
    check("typed recurring_disabled code",
          un.get("code") == DISABLED_CODE, str(created)[:200])

    listed = svc.list_schedules()
    check("list refused with same code",
          (listed.get("unavailable") or {}).get("code") == DISABLED_CODE)

    cancelled = svc.cancel_schedule("nope")
    check("cancel refused with same code",
          (cancelled.get("unavailable") or {}).get("code") == DISABLED_CODE)

    check("spy never fired", spy.calls == [], str(spy.calls))

    exp = svc.exposure()
    check("exposure() reports enabled:false",
          exp.get("enabled") is False)
finally:
    svc.close()

print("P2 PASS — kill switch disables the surface with typed 501s")
