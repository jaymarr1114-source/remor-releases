"""Worker C / Task 3 — recurrence scheduler: service-level tests.

Real components only: sqlite store, real RunScheduler, real
MeteringService.guarded_submit. The pumper thread fires through the
same guarded path manual submits use, so quota refusals here are the
production mechanism, not stubs. (The production constants 35/day and
25-queue are injected as 2/0 in the quota tests to keep the suite fast —
the enforcement code path, refusal codes, and row counting are
identical; the e2e battery additionally proves the real constants via
metering.tier().)

Run: python tests/track2_new/test_recurrence.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pylib"))

from swarm_engine.services.scheduler import RunScheduler
from swarm_engine.services.metering import (
    MeteringService, DAILY_CAP_CODE, QUEUE_CAP_CODE)
from swarm_engine.services.recurrence import (
    RecurrenceService, parse_spec,
    DISABLED_CODE, BAD_SPEC_CODE, UNKNOWN_SCHEDULE_CODE)

SCRATCH = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name +
          (f" -- {detail}" if detail and not cond else ""))


def fresh_tree(prefix="recur_"):
    d = tempfile.mkdtemp(prefix=prefix, dir=SCRATCH)
    return d


def boot(d, **kw):
    sched = RunScheduler(
        db_path=os.path.join(d, "scheduler.db"),
        runtime_db_path=os.path.join(d, "runtime.db"))
    metering = MeteringService(
        sched,
        tasks_per_day=kw.get("tasks_per_day", 35),
        max_queue=kw.get("max_queue", 25))
    rec = RecurrenceService(
        os.path.join(d, "recurrence.db"), metering.guarded_submit,
        poll_interval=kw.get("poll_interval", 0.1),
        enabled=kw.get("enabled", True),
        dispatcher_enabled=kw.get("dispatcher_enabled", True))
    return sched, metering, rec


def wait_for(cond, timeout=10.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def t_spec():
    for bad, why in [
        (None, "non-dict"), ("x", "string"), ({}, "empty"),
        ({"every_seconds": 5, "every_minutes": 1}, "both keys"),
        ({"every_seconds": 0}, "zero seconds"), ({"every_seconds": 0.5}, "sub-second"),
        ({"every_seconds": -3}, "negative seconds"),
        ({"every_seconds": "5"}, "string number"),
        ({"every_seconds": True}, "bool"),
        ({"every_minutes": 0}, "zero minutes"),
        ({"every_minutes": -1}, "negative minutes"),
        ({"daily_at": "09:00"}, "cron-ish key"),
    ]:
        try:
            parse_spec(bad)
            check(f"spec: {why} rejected", False, f"{bad!r} parsed")
        except ValueError:
            check(f"spec: {why} rejected", True)
    check("spec: every_seconds=5 -> 5.0", parse_spec({"every_seconds": 5}) == 5.0)
    check("spec: every_minutes=2 -> 120.0",
          parse_spec({"every_minutes": 2}) == 120.0)
    check("spec: every_minutes=1.5 -> 90.0",
          parse_spec({"every_minutes": 1.5}) == 90.0)


def t_crud():
    d = fresh_tree()
    sched, metering, rec = boot(d)
    try:
        r = rec.create_schedule(
            "ping the widget", {"every_seconds": 60},
            metadata={"src": "t"})
        check("crud: create ok", r.get("ok") and r.get("schedule_id"),
              f"{r}")
        sid = r.get("schedule_id")
        check("crud: create reports interval",
              r.get("interval_seconds") == 60.0, f"{r}")
        check("crud: tier_flag recorded", r.get("tier_flag") == "unset",
              f"{r}")
        g = rec.get_schedule(sid)
        check("crud: get round-trips",
              g.get("ok") and g["schedule"]["goal"] == "ping the widget"
              and g["schedule"]["active"] is True
              and g["schedule"]["spec"] == {"every_seconds": 60}, f"{g}")
        lst = rec.list_schedules()
        check("crud: list contains it",
              lst.get("ok") and len(lst["schedules"]) == 1, f"{lst}")
        c = rec.cancel_schedule(sid)
        check("crud: cancel ok", c.get("ok") and c.get("cancelled"), f"{c}")
        g2 = rec.get_schedule(sid)
        check("crud: cancelled row stays readable, inactive",
              g2.get("ok") and g2["schedule"]["active"] is False, f"{g2}")
        c2 = rec.cancel_schedule(sid)
        check("crud: double-cancel refused", not c2.get("ok"), f"{c2}")
        cu = rec.cancel_schedule("nope")
        check("crud: unknown cancel -> typed code",
              not cu.get("ok") and cu.get("code") == UNKNOWN_SCHEDULE_CODE,
              f"{cu}")
        gu = rec.get_schedule("nope")
        check("crud: unknown get -> typed code",
              not gu.get("ok") and gu.get("code") == UNKNOWN_SCHEDULE_CODE,
              f"{gu}")
        b1 = rec.create_schedule("", {"every_seconds": 5})
        check("crud: empty goal refused", not b1.get("ok"), f"{b1}")
        b2 = rec.create_schedule("x", {"every_seconds": 0.5})
        check("crud: bad spec -> typed code",
              not b2.get("ok") and b2.get("code") == BAD_SPEC_CODE, f"{b2}")
        b3 = rec.create_schedule("x", {"every_seconds": 5},
                                 start_in_seconds=-1)
        check("crud: negative start_in_seconds refused",
              not b3.get("ok"), f"{b3}")
    finally:
        rec.close()
        sched.close()


def t_disabled():
    d = fresh_tree()
    sched, metering, rec = boot(d, enabled=False, dispatcher_enabled=True)
    try:
        check("disabled: no pumper thread started", rec._pumper is None)
        r = rec.create_schedule("x", {"every_seconds": 5})
        check("disabled: create -> 501 typed recurring_disabled",
              not r.get("ok") and r.get("unavailable", {}).get("code") ==
              DISABLED_CODE, f"{r}")
        lst = rec.list_schedules()
        check("disabled: list -> same typed refusal",
              not lst.get("ok") and lst.get("unavailable", {}).get("code")
              == DISABLED_CODE, f"{lst}")
        c = rec.cancel_schedule("x")
        check("disabled: cancel -> same typed refusal",
              not c.get("ok") and c.get("unavailable", {}).get("code")
              == DISABLED_CODE, f"{c}")
        ex = rec.exposure()
        check("disabled: exposure reports gate + open decision",
              ex.get("enabled") is False
              and "OPEN" in ex.get("tier_decision", ""), f"{ex}")
    finally:
        rec.close()
        sched.close()


def t_firing():
    d = fresh_tree()
    sched, metering, rec = boot(d)
    try:
        r = rec.create_schedule("recurrence e2e goal alpha",
                                {"every_seconds": 1})
        sid = r["schedule_id"]
        ok = wait_for(
            lambda: rec.get_schedule(sid)["schedule"]["fire_count"] >= 2,
            timeout=15)
        g = rec.get_schedule(sid)
        sch = g["schedule"]
        check("fire: schedule fired >= 2 times", ok, f"{sch}")
        check("fire: last_run_id recorded", bool(sch["last_run_id"]), f"{sch}")
        run = sched.get_run(sch["last_run_id"]) if sch["last_run_id"] else None
        check("fire: run exists in the REAL scheduler",
              run is not None and run.get("goal") == "recurrence e2e goal alpha",
              f"{run}")
        check("fire: no error/refusal recorded",
              sch["last_error"] is None and sch["last_refusal_code"] is None,
              f"{sch}")
        c = rec.cancel_schedule(sid)
        check("fire: cancel ok", c.get("ok"), f"{c}")
        before = rec.get_schedule(sid)["schedule"]["fire_count"]
        time.sleep(2.5)
        after = rec.get_schedule(sid)["schedule"]["fire_count"]
        check("fire: no further fires after cancel", before == after,
              f"before={before} after={after}")
    finally:
        rec.close()
        sched.close()


def t_daily_cap():
    # 2/day injected to keep the suite fast; the refusal code and the
    # count-based mechanism are the production ones.
    d = fresh_tree()
    sched, metering, rec = boot(d, tasks_per_day=2)
    try:
        r = rec.create_schedule("daily cap probe", {"every_seconds": 1})
        sid = r["schedule_id"]
        ok = wait_for(
            lambda: rec.get_schedule(sid)["schedule"]["fire_count"] >= 2,
            timeout=15)
        check("cap: two fires consumed the injected 2/day budget", ok)
        time.sleep(2.0)
        sch = rec.get_schedule(sid)["schedule"]
        check("cap: fire_count stays at 2 (guard bites)",
              sch["fire_count"] == 2, f"{sch}")
        check("cap: typed refusal recorded on the row",
              sch["last_refusal_code"] == DAILY_CAP_CODE, f"{sch}")
        # The refusal surfaces as the real typed limit payload.
        res = metering.guarded_submit("manual probe")
        check("cap: manual submit hits the same daily_task_cap code",
              not res.get("ok")
              and res.get("limit", {}).get("code") == DAILY_CAP_CODE,
              f"{res}")
        check("cap: next_fire_at kept advancing (no hot loop)",
              sch["next_fire_at"] > time.time(), f"{sch}")
    finally:
        rec.close()
        sched.close()


def t_queue_cap():
    # max_queue=0 forces every fire through the queue guard.
    d = fresh_tree()
    sched, metering, rec = boot(d, max_queue=0)
    try:
        r = rec.create_schedule("queue cap probe", {"every_seconds": 1})
        sid = r["schedule_id"]
        ok = wait_for(
            lambda: rec.get_schedule(sid)["schedule"]["last_refusal_code"]
            is not None,
            timeout=10)
        sch = rec.get_schedule(sid)["schedule"]
        check("qcap: scheduled fire refused by queue guard", ok, f"{sch}")
        check("qcap: refusal code is the real queue_cap",
              sch["last_refusal_code"] == QUEUE_CAP_CODE, f"{sch}")
        check("qcap: no run was submitted",
              sch["fire_count"] == 0 and sch["last_run_id"] is None, f"{sch}")
    finally:
        rec.close()
        sched.close()


def t_restart():
    d = fresh_tree()
    sched, metering, rec = boot(d, dispatcher_enabled=False)
    try:
        r = rec.create_schedule("restart survivor", {"every_seconds": 1},
                                start_in_seconds=0.2)
        sid = r["schedule_id"]
        time.sleep(0.1)
        sch = rec.get_schedule(sid)["schedule"]
        check("restart: nothing fired with pumper off",
              sch["fire_count"] == 0, f"{sch}")
        rec.close()  # pumper was never started: zero fires possible here
    finally:
        try:
            rec.close()
        except Exception:
            pass
        sched.close()
    # Fresh instances over the SAME db files (process-restart equivalent).
    sched2 = RunScheduler(
        db_path=os.path.join(d, "scheduler.db"),
        runtime_db_path=os.path.join(d, "runtime.db"))
    metering2 = MeteringService(sched2)
    rec2 = RecurrenceService(os.path.join(d, "recurrence.db"),
                             metering2.guarded_submit, poll_interval=0.1)
    try:
        g = rec2.get_schedule(sid)
        check("restart: schedule row survives",
              g.get("ok") and g["schedule"]["goal"] == "restart survivor",
              f"{g}")
        ok = wait_for(
            lambda: rec2.get_schedule(sid)["schedule"]["fire_count"] >= 1,
            timeout=15)
        sch2 = rec2.get_schedule(sid)["schedule"]
        check("restart: due schedule fires on the new instance", ok,
              f"{sch2}")
        run = sched2.get_run(sch2["last_run_id"]) if sch2.get("last_run_id") \
            else None
        check("restart: fired run is in the real scheduler",
              run is not None and run.get("goal") == "restart survivor",
              f"{run}")
    finally:
        rec2.close()
        sched2.close()


def t_tier_env():
    os.environ["REMOR_RECURRENCE_TIER"] = "paid"
    d = fresh_tree()
    sched, metering, rec = boot(d)
    try:
        r = rec.create_schedule("tier probe", {"every_seconds": 60})
        check("tier: REMOR_RECURRENCE_TIER=paid recorded on the row",
              r.get("tier_flag") == "paid", f"{r}")
        ex = rec.exposure()
        check("tier: exposure surfaces the tier env",
              ex.get("tier_flag_now") == "paid"
              and ex.get("tier_env") == "REMOR_RECURRENCE_TIER", f"{ex}")
    finally:
        rec.close()
        sched.close()
        del os.environ["REMOR_RECURRENCE_TIER"]


def main():
    # These env gates must be unset for the suite; tests opt in explicitly.
    os.environ.pop("REMOR_RECURRENCE_ENABLED", None)
    os.environ.pop("REMOR_RECURRENCE_TIER", None)
    for fn in (t_spec, t_crud, t_disabled, t_firing, t_daily_cap,
               t_queue_cap, t_restart, t_tier_env):
        # enable exposure for every service-level test except t_disabled,
        # which opts out via enabled=False itself.
        os.environ["REMOR_RECURRENCE_ENABLED"] = "1"
        fn()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILURES:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
