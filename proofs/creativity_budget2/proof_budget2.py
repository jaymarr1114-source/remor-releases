"""CREATIVITY-BUDGET-2 proof battery: James's locked budget policy as real fields.

Usage: proof_budget2.py <test-name>  (one test per fresh process)
Each test builds its own scratch sqlite db in a temp dir and uses the REAL
FRM grant path (issue_run_grant / FrmGrant) — no mocks of the FRM.
"""
import os
import sys
import tempfile
import time
import traceback

from runtime.creativity.budget import (
    BudgetRefused,
    BudgetEnvelope,
    CostBound,
    COMPUTE,
    MONETARY,
    MEASURE_WALL_CLOCK,
    MEASURE_FRM_COST_INPUT,
    CreativityBudget,
    CreativityBudgetController,
    CapacityReading,
    SlotToken,
    default_capacity_probe,
    POLICY_NORMAL_SLOTS,
    POLICY_BURST_CEILING,
)

# Largest measured single-run cost from the BUDGET-1 battery replays
# (critique1 1.40s) — a measurement, passed in, never invented here.
MEASURED_MAX_RUN_S = 1.4


def make_envelope(compute_bound=1000.0):
    return BudgetEnvelope(bounds={
        COMPUTE: CostBound(dimension=COMPUTE, unit="seconds",
                           bound=compute_bound,
                           measurement=MEASURE_WALL_CLOCK,
                           provenance="budget2 battery: test envelope"),
        MONETARY: CostBound(dimension=MONETARY, unit="currency", bound=None,
                            measurement=MEASURE_FRM_COST_INPUT,
                            provenance="budget2 battery: monetary UNSET"),
    })


def make_controller(tmp, total_budget_s=1000.0,
                    measured_max_run_s=MEASURED_MAX_RUN_S,
                    probe=None, compute_bound=1000.0):
    b = CreativityBudget(make_envelope(compute_bound),
                         spend_db_path=os.path.join(tmp, "spend.db"))
    kw = {} if probe is None else {"capacity_probe": probe}
    return CreativityBudgetController(
        total_budget_s, budget=b,
        measured_max_run_s=measured_max_run_s, **kw)


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# -- construction -------------------------------------------------------

def t_construction_refuses_absurd():
    tmp = tempfile.mkdtemp()
    try:
        make_controller(tmp, total_budget_s=1e12)
    except BudgetRefused as e:
        msg = str(e)
        check("exceeds measured sustainable capacity" in msg,
              f"refusal must name measured sustainable capacity: {msg}")
        check("never an arbitrary large number" in msg,
              f"refusal must name the anti-arbitrary rule: {msg}")
        return
    raise AssertionError("total_budget_s=1e12 was NOT refused")


def t_construction_accepts_sane_and_shows_derivation():
    tmp = tempfile.mkdtemp()
    c = make_controller(tmp, total_budget_s=3600.0)
    check(c.total_budget_s == 3600.0, "total_budget_s field wrong")
    d = c.derivation
    check(d["total_budget_s"] == 3600.0, "derivation missing total")
    check(d["measured_max_run_s"] == MEASURED_MAX_RUN_S,
          "derivation must show the measured max run cost")
    check(abs(d["sustainable_ceiling_s"]
              - MEASURED_MAX_RUN_S * 1e6) < 1e-3,
          "derivation must show the computed sustainable ceiling")
    check(d["cpu_count_at_construction"] == os.cpu_count(),
          "derivation must show the measured cpu count")
    check(c.normal_slots == POLICY_NORMAL_SLOTS == 1, "normal slots != 1")
    check(c.concurrency_ceiling == POLICY_BURST_CEILING == 2,
          "burst ceiling != 2")


# -- slots ---------------------------------------------------------------

def t_normal_slot_one_then_refused():
    tmp = tempfile.mkdtemp()
    c = make_controller(tmp)
    t1 = c.acquire_slot()
    check(isinstance(t1, SlotToken), "acquire must return a SlotToken")
    check(c.active_slot_count == 1, "one slot should be active")
    try:
        c.acquire_slot()
    except BudgetRefused as e:
        check("1 slot" in str(e), f"refusal must name the 1-slot ceiling: {e}")
    else:
        raise AssertionError("2nd normal slot was NOT refused")
    c.release_slot(t1)
    check(c.active_slot_count == 0, "slot not released")
    t2 = c.acquire_slot()  # free again after release
    c.release_slot(t2)


def t_burst_granted_with_capacity_third_always_refused():
    tmp = tempfile.mkdtemp()
    probe = lambda: CapacityReading(cpu_count=4, load_1m=0.5,
                                    measured_at=time.time())
    c = make_controller(tmp, probe=probe)
    t1 = c.acquire_slot()
    t2 = c.acquire_slot(burst=True)  # available 3.5 cpus >= 1.0
    check(t2.burst, "burst token must be marked burst")
    check(c.active_slot_count == 2, "two slots should be active")
    try:
        c.acquire_slot(burst=True)
    except BudgetRefused as e:
        check("always refused" in str(e),
              f"3rd slot refusal must say always: {e}")
    else:
        raise AssertionError("3rd slot was NOT refused")
    c.release_slot(t1)
    c.release_slot(t2)
    check(c.active_slot_count == 0, "slots not released")


def t_burst_refused_under_load_and_default_probe_real():
    tmp = tempfile.mkdtemp()
    probe = lambda: CapacityReading(cpu_count=2, load_1m=1.9,
                                    measured_at=time.time())
    c = make_controller(tmp, probe=probe)
    t1 = c.acquire_slot()
    try:
        c.acquire_slot(burst=True)  # available 0.1 < 1.0
    except BudgetRefused as e:
        msg = str(e)
        check("sustained 2-slot operation requires actual available capacity"
              in msg, f"refusal must name the capacity rule: {msg}")
        check("cpu_count=2" in msg and "load_1m=1.9" in msg,
              f"refusal must name what was measured: {msg}")
    else:
        raise AssertionError("burst under load was NOT refused")
    c.release_slot(t1)
    # The default probe reads the real OS values (not injected).
    r = default_capacity_probe()
    check(r.cpu_count == os.cpu_count(),
          f"default probe cpu_count {r.cpu_count} != os.cpu_count() "
          f"{os.cpu_count()}")
    check(r.load_1m is not None and
          abs(r.load_1m - os.getloadavg()[0]) < 0.5,
          f"default probe load {r.load_1m} not a real os.getloadavg value")


# -- accounting ------------------------------------------------------------

def t_spend_accounting_reconciles_incl_error():
    tmp = tempfile.mkdtemp()
    c = make_controller(tmp, total_budget_s=1000.0)
    c.request_budget(work_id="w1", estimated_compute_s=10.0)
    c.request_budget(work_id="w2", estimated_compute_s=10.0)
    c.track_run_spend("r1", 4.0, "completed", work_id="w1")
    c.track_run_spend("r2", 3.0, "completed", work_id="w1")
    c.track_run_spend("r3", 2.5, "error", work_id="w2")  # ERROR spends too
    s = c.session_summary()
    check(abs(s["total_spent_s"] - 9.5) < 1e-9,
          f"total {s['total_spent_s']} != 9.5")
    check(abs(s["per_work_spent_s"]["w1"] - 7.0) < 1e-9, "w1 != 7.0")
    check(abs(s["per_work_spent_s"]["w2"] - 2.5) < 1e-9, "w2 != 2.5")
    check(abs(s["remaining_s"] - (1000.0 - 9.5)) < 1e-9, "remainder wrong")
    check(s["run_count"] == 3, "run_count != 3")
    check(s["per_run"]["r3"]["outcome"] == "error",
          "ERROR outcome must be recorded verbatim")
    recon = sum(r["spend_s"] for r in s["per_run"].values())
    check(abs(recon - s["total_spent_s"]) < 1e-9,
          "sum(per-run) must reconcile with the cumulative total")


def t_no_burn_down():
    tmp = tempfile.mkdtemp()
    c = make_controller(tmp, total_budget_s=1000.0)
    c.request_budget(work_id="w1", estimated_compute_s=10.0)
    c.track_run_spend("r1", 4.0, "completed", work_id="w1")
    s = c.session_summary()
    # Ceilings, not targets: 6.0s of the grant stays unspent and available.
    check(abs(s["remaining_s"] - 996.0) < 1e-9,
          f"remaining {s['remaining_s']} != 996.0 — budget burned to target?")
    # The unspent remainder is genuinely available: a further request whose
    # cumulative (6.0 unspent + 5.0 new = 11.0) fits is granted.
    g = c.request_budget(work_id="w1", estimated_compute_s=5.0)
    check(g is not None, "follow-up request refused — remainder not usable")
    s2 = c.session_summary()
    check(abs(s2["remaining_s"] - 996.0) < 1e-9,
          "requesting must not consume; only measured spend does")


def t_aggregate_ceiling_refuses():
    tmp = tempfile.mkdtemp()
    c = make_controller(tmp, total_budget_s=10.0)
    c.request_budget(work_id="w1", estimated_compute_s=6.0)
    c.track_run_spend("r1", 6.0, "completed", work_id="w1")
    try:
        c.request_budget(work_id="w1", estimated_compute_s=5.0)
    except BudgetRefused as e:
        check("total_budget_s" in str(e),
              f"refusal must name the aggregate ceiling: {e}")
    else:
        raise AssertionError("over-aggregate request was NOT refused")


# -- idle ------------------------------------------------------------------

def t_idle_domain_zero():
    tmp = tempfile.mkdtemp()
    c = make_controller(tmp)
    c.note_idle("curiosity")
    check(c.domain_consumption("curiosity") == 0.0,
          "idle domain consumption must be 0.0")
    s = c.session_summary()
    check(s["idle_domains"]["curiosity"] ==
          {"consumption_s": 0.0, "reservations": 0},
          "idle domain must show zero consumption AND zero reservation")
    try:
        c.acquire_slot(domain="curiosity")
    except BudgetRefused as e:
        check("0 compute while idle" in str(e),
              f"idle acquire refusal must name the policy: {e}")
    else:
        raise AssertionError("idle domain acquired a slot")
    c.activate_domain("curiosity")  # the event ends idle
    t1 = c.acquire_slot(domain="curiosity")
    c.release_slot(t1)


# -- adversarial -------------------------------------------------------------

def t_stale_slot_and_double_release():
    tmp = tempfile.mkdtemp()
    c = make_controller(tmp)
    t1 = c.acquire_slot()
    stale = c.stale_slots(max_age_s=0)
    check(len(stale) == 1 and stale[0]["slot_id"] == t1.slot_id,
          "acquired-but-never-released slot must be detectable")
    check(stale[0]["age_s"] >= 0.0, "stale slot must carry its age")
    c.release_slot(t1)
    try:
        c.release_slot(t1)
    except BudgetRefused as e:
        check("double-release" in str(e),
              f"double-release must be refused fail-closed: {e}")
    else:
        raise AssertionError("double-release was NOT refused")
    check(c.active_slot_count == 0,
          "active slot count went negative or non-zero after double-release")


TESTS = {
    "construction_refuses_absurd": t_construction_refuses_absurd,
    "construction_accepts_sane_and_shows_derivation":
        t_construction_accepts_sane_and_shows_derivation,
    "normal_slot_one_then_refused": t_normal_slot_one_then_refused,
    "burst_granted_with_capacity_third_always_refused":
        t_burst_granted_with_capacity_third_always_refused,
    "burst_refused_under_load_and_default_probe_real":
        t_burst_refused_under_load_and_default_probe_real,
    "spend_accounting_reconciles_incl_error":
        t_spend_accounting_reconciles_incl_error,
    "no_burn_down": t_no_burn_down,
    "aggregate_ceiling_refuses": t_aggregate_ceiling_refuses,
    "idle_domain_zero": t_idle_domain_zero,
    "stale_slot_and_double_release": t_stale_slot_and_double_release,
}


def main(argv):
    if len(argv) != 2 or argv[1] not in TESTS:
        print(f"usage: {argv[0]} <{'|'.join(sorted(TESTS))}>",
              file=sys.stderr)
        return 2
    name = argv[1]
    try:
        TESTS[name]()
    except Exception:
        print(f"[FAIL] {name}", flush=True)
        traceback.print_exc()
        return 1
    print(f"[PASS] {name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
