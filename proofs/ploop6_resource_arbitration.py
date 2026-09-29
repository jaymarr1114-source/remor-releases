"""PLOOP-6 proof: resource arbitration under genuine contention.

Proves, against the REAL MicrocontrollerSubstrate (fake clock injected --
a supported API, not a mock) and the REAL ResourceArbitrator:

  1. Contention: three loops demand 170s/9 slots against 100s/4 slots.
     Grants stay within capacity; minimums are honored; no grant
     exceeds demand; the losers fail closed (named refusals, counted).
  2. Enforcement: spawns beyond a grant are refused by the substrate's
     real admission paths (R_ADMISSION_EXHAUSTED / R_CONCURRENCY_CAP);
     reserved spend never silently exceeds the grant.
  3. Caps vs grants (charter C-4.4): a per-activation cap violation
     (budget_s=0) is refused independently of arbitration; a single
     activation larger than the grant is refused against the pool.
  4. Re-arbitration: changed demands produce changed grants within capacity.
  5. Epoch expiry is observable on the arbitrator's clock.
  6. Misconfiguration is loud: unregistered-loop demand, negative demand,
     and apply() for a substrate-unknown loop all raise explicitly.
  7. Minimums exceeding capacity are scaled AND recorded (never silent).

Exits nonzero on the first failed check. Prints N/N at the end.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "pylib")))

from swarm_engine.core.microcontroller.substrate import (
    MicrocontrollerSubstrate,
    R_ADMISSION_EXHAUSTED,
    R_CONCURRENCY_CAP,
    R_INVALID_BUDGET,
)
from swarm_engine.core.resource_arbitrator import ResourceArbitrator

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"PROOF FAILED at: {name} {detail}")


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def main():
    clock = FakeClock()
    substrate = MicrocontrollerSubstrate(clock=clock)
    for loop in ("acquisition", "execution", "distillation"):
        substrate.register_loop(loop, budget_s=1.0, max_concurrent=1)

    arb = ResourceArbitrator(total_budget_s=100.0, total_max_concurrent=4,
                             epoch_length_s=300.0, clock=clock)
    arb.register_loop("acquisition", weight=2.0,
                      minimum_budget_s=10.0, minimum_concurrent=1)
    arb.register_loop("execution", weight=1.0,
                      minimum_budget_s=10.0, minimum_concurrent=1)
    arb.register_loop("distillation", weight=1.0,
                      minimum_budget_s=5.0, minimum_concurrent=1)

    # --- genuine contention: demand 170s/9 slots vs 100s/4 slots ----------
    arb.set_demand("acquisition", budget_s=80.0, concurrent=4)
    arb.set_demand("execution", budget_s=80.0, concurrent=4)
    arb.set_demand("distillation", budget_s=10.0, concurrent=1)
    decision = arb.apply(substrate)
    grants = decision.grants

    total_b = sum(g.budget_s for g in grants.values())
    total_c = sum(g.max_concurrent for g in grants.values())
    check("grants within budget capacity", total_b <= 100.0, f"sum={total_b}")
    check("grants within concurrency capacity", total_c <= 4, f"sum={total_c}")
    check("acquisition minimum honored",
          grants["acquisition"].budget_s >= 10.0
          and grants["acquisition"].max_concurrent >= 1,
          f"{grants['acquisition']}")
    check("execution minimum honored",
          grants["execution"].budget_s >= 10.0
          and grants["execution"].max_concurrent >= 1,
          f"{grants['execution']}")
    check("distillation minimum honored",
          grants["distillation"].budget_s >= 5.0
          and grants["distillation"].max_concurrent >= 1,
          f"{grants['distillation']}")
    check("no grant exceeds demand",
          grants["acquisition"].budget_s <= 80.0
          and grants["execution"].budget_s <= 80.0
          and grants["distillation"].budget_s <= 10.0)
    check("higher weight wins the remainder under contention",
          grants["acquisition"].budget_s > grants["execution"].budget_s,
          f"acq={grants['acquisition'].budget_s} "
          f"exe={grants['execution'].budget_s}")
    check("grants applied to substrate pools",
          all(abs(substrate._loops[loop].budget_s - grants[loop].budget_s) < 1e-9
              and substrate._loops[loop].max_concurrent
              == grants[loop].max_concurrent
              for loop in grants))

    # --- enforcement: spawn against the real admission paths --------------
    # Budget axis, isolated: fresh pair, concurrency far above demand, so
    # only the budget ceiling can bind. Grant will be 50s.
    clock_b = FakeClock()
    sub_b = MicrocontrollerSubstrate(clock=clock_b)
    sub_b.register_loop("distillation", budget_s=1.0, max_concurrent=1)
    arb_b = ResourceArbitrator(total_budget_s=100.0, total_max_concurrent=100,
                               clock=clock_b)
    arb_b.register_loop("distillation", weight=1.0)
    arb_b.set_demand("distillation", budget_s=50.0, concurrent=100)
    gb = arb_b.apply(sub_b).grants["distillation"]
    check("isolated budget grant equals demand (no contention)",
          abs(gb.budget_s - 50.0) < 1e-6, f"grant={gb.budget_s}")
    spawned = []
    refusals = 0
    last_reason = None
    for _ in range(8):
        r = sub_b.spawn("distillation", purpose="budget probe", budget_s=10.0)
        if r.ok:
            spawned.append(r.mc.mc_id)
        else:
            refusals += 1
            last_reason = r.refusal.reason
    adm_b = sub_b._loops["distillation"]
    check("spawns stop at the grant (budget axis)",
          len(spawned) == 5 and refusals == 3,
          f"spawned={len(spawned)} refused={refusals}")
    check("refusal names the exhausted pool",
          last_reason == R_ADMISSION_EXHAUSTED, f"reason={last_reason}")
    check("refusals counted on the admission record",
          adm_b.total_refused == 3, f"total_refused={adm_b.total_refused}")
    check("reserved spend never exceeds the grant",
          adm_b.reserved_s <= gb.budget_s + 1e-9,
          f"reserved={adm_b.reserved_s} grant={gb.budget_s}")

    # Concurrency axis on the main contention pair: execution grant is
    # 28.75s / 1 slot. The second concurrent spawn must fail closed.
    r1 = substrate.spawn("execution", purpose="slot one", budget_s=1.0)
    r2 = substrate.spawn("execution", purpose="slot two", budget_s=1.0)
    check("first concurrent spawn admitted", r1.ok)
    check("second concurrent spawn refused: loser fails closed",
          not r2.ok and r2.refusal.reason == R_CONCURRENCY_CAP,
          f"reason={r2.refusal.reason if r2.refusal else None}")
    exe_adm = substrate._loops["execution"]
    active_exe = sum(1 for m in substrate._mcs.values()
                     if m.loop == "execution" and m.state == "active")
    check("active count never exceeds granted slots",
          active_exe <= grants["execution"].max_concurrent,
          f"active={active_exe}")

    # --- caps vs grants (charter C-4.4) -----------------------------------
    r_cap = substrate.spawn("distillation", purpose="zero cap", budget_s=0.0)
    check("per-activation cap violation refused independently of grants",
          not r_cap.ok and r_cap.refusal.reason == R_INVALID_BUDGET,
          f"reason={r_cap.refusal.reason if r_cap.refusal else None}")
    r_big = substrate.spawn("distillation", purpose="oversized activation",
                            budget_s=1000.0)
    check("single activation larger than the grant refused against the pool",
          not r_big.ok and r_big.refusal.reason == R_ADMISSION_EXHAUSTED,
          f"reason={r_big.refusal.reason if r_big.refusal else None}")

    # --- re-arbitration with changed demands -------------------------------
    for mc_id in list(substrate._mcs.keys()):
        substrate.retire(mc_id, outcome="resolved")
    arb.clear_demands()
    arb.set_demand("distillation", budget_s=90.0, concurrent=4)
    d2 = arb.apply(substrate)
    check("epoch advanced", d2.epoch_id == decision.epoch_id + 1)
    check("re-arbitration follows the new demand",
          abs(d2.grants["distillation"].budget_s - 90.0) < 1e-6
          and d2.grants["distillation"].max_concurrent == 4,
          f"{d2.grants['distillation']}")
    check("loops with no demand get no grant",
          "acquisition" not in d2.grants and "execution" not in d2.grants)
    check("decision ledger keeps history", len(arb.ledger) == 2)

    # --- epoch expiry -------------------------------------------------------
    check("no expiry before the deadline", arb.expired_loops() == [])
    clock.advance(301.0)
    check("expired loops observable after the deadline",
          arb.expired_loops() == ["distillation"],
          f"{arb.expired_loops()}")

    # --- misconfiguration is loud ------------------------------------------
    try:
        arb.set_demand("nope", budget_s=1.0)
        check("demand for unregistered loop raises", False)
    except ValueError:
        check("demand for unregistered loop raises", True)
    try:
        arb.set_demand("distillation", budget_s=-5.0)
        check("negative demand raises", False)
    except ValueError:
        check("negative demand raises", True)
    try:
        arb.register_loop("", weight=1.0)
        check("empty loop name raises", False)
    except ValueError:
        check("empty loop name raises", True)

    arb2 = ResourceArbitrator(total_budget_s=100.0, total_max_concurrent=4,
                              clock=clock)
    sub2 = MicrocontrollerSubstrate(clock=clock)
    sub2.register_loop("acquisition", budget_s=1.0, max_concurrent=1)
    arb2.register_loop("ghost", weight=1.0)
    arb2.set_demand("ghost", budget_s=10.0, concurrent=1)
    try:
        arb2.apply(sub2)
        check("apply for substrate-unknown loop raises", False)
    except KeyError:
        check("apply for substrate-unknown loop raises", True)

    # --- minimums exceeding capacity: scaled AND recorded -------------------
    arb3 = ResourceArbitrator(total_budget_s=10.0, total_max_concurrent=2,
                              clock=clock)
    arb3.register_loop("a", weight=1.0, minimum_budget_s=10.0,
                       minimum_concurrent=2)
    arb3.register_loop("b", weight=1.0, minimum_budget_s=10.0,
                       minimum_concurrent=2)
    arb3.set_demand("a", budget_s=50.0, concurrent=4)
    arb3.set_demand("b", budget_s=50.0, concurrent=4)
    d3 = arb3.arbitrate()
    check("over-minimum capacity flagged, not silent",
          d3.minimums_scaled and len(d3.notes) > 0,
          f"notes={d3.notes}")
    check("scaled grants stay within capacity",
          sum(g.budget_s for g in d3.grants.values()) <= 10.0 + 1e-9
          and sum(g.max_concurrent for g in d3.grants.values()) <= 2)

    print(f"\n{len(CHECKS)}/{len(CHECKS)} checks passed")


if __name__ == "__main__":
    main()
