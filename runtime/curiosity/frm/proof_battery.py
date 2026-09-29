"""runtime/curiosity/frm/proof_battery.py

The CUR-P1C adversarial + causal battery, as callable checks.

Each check runs against the REAL FinancialResourceManager, the REAL
ResourceArbitrator instance it owns, and the REAL append-only ledger
(fake clock injected -- a supported API, not a mock). Checks return
(name, passed, detail); run_all() returns the full list. The unittest
suite (tests/test_frm_battery.py) asserts them; the proof driver
(proofs/cur_p1c_frm_proof.py) prints them and exits nonzero on the
first failure. One battery, two runners -- no duplication.

Battery (mandate section 3 + objective):
  B1  curiosity demand spike cannot breach primary's minimum
  B2  HARD_SHUTDOWN_RESOURCE -> zero allocation with demand present
  B3  SUSPENDED_SAFETY       -> zero allocation with demand present
  B4  BANNED_6M              -> zero allocation with demand present
  B5  WARNING_1 restricts curiosity per the standing policy fraction
  B6  epoch boundary recalls lent capacity (lending is epoch-bounded)
  B7  running grants are never shrunk mid-execution (non-preemption)
  B8  primary minimum above capacity is a loud configuration error
  B9  expensive-model request outside standing policy is refused
  B10 cost inputs keep provenance; UNPRICED handled without a crash
  B11 every grant matches the frozen grant shape exactly
  B12 end-to-end: demand -> FRM -> arbitrator -> grant -> real spending,
      with refusal past the grant (grant is a real ceiling)
  B13 expected-yield estimates are provisional and non-binding
  B14 unknown enforcement state is rejected loudly
  B15 the FRM owns a second arbitrator INSTANCE of the same class the
      Primary path uses (no second arbitrator class)
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Callable, List, Tuple

from swarm_engine.core.resource_arbitrator import ResourceArbitrator
from swarm_engine.curiosity.frm.evaluation import (
    CURIOSITY,
    PRIMARY,
    FinancialResourceManager,
)
from swarm_engine.curiosity.frm.grant import GRANT_KEYS
from swarm_engine.curiosity.frm.policy import (
    BANNED_6M,
    HARD_SHUTDOWN_RESOURCE,
    RUNNING,
    SUSPENDED_SAFETY,
    WARNING_1,
    CostInput,
    CostKind,
    DomainDemand,
    ExpensiveModelPolicy,
    ExpectedYield,
    FrmPolicy,
)
from swarm_engine.curiosity.frm.test_doubles import (
    GrantExhausted,
    GrantSpender,
    ScriptedDemandSource,
)

Check = Tuple[str, bool, str]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def make_policy(**overrides) -> FrmPolicy:
    kw = dict(
        total_budget_s=100.0,
        total_max_concurrent=4,
        primary_minimum_budget_s=30.0,
        primary_minimum_concurrent=1,
        primary_weight=1.0,
        curiosity_weight=1.0,
        epoch_s=300.0,
    )
    kw.update(overrides)
    return FrmPolicy(**kw)


def make_frm(policy=None, clock=None):
    clock = clock or FakeClock()
    policy = policy or make_policy()
    return FinancialResourceManager(policy, clock=clock), clock


def _demands(p_budget=0.0, p_conc=0, c_budget=0.0, c_conc=0,
             c_expensive: bool = False, c_model=None):
    primary = DomainDemand(domain=PRIMARY, budget_s=p_budget,
                           max_concurrent=p_conc)
    curiosity = DomainDemand(domain=CURIOSITY, budget_s=c_budget,
                             max_concurrent=c_conc,
                             requests_expensive_model=c_expensive,
                             expensive_model_name=c_model)
    return primary, curiosity


# -- battery ---------------------------------------------------------------

def b1_primary_minimum_under_spike() -> Check:
    name = "B1 primary minimum holds under curiosity demand spike"
    frm, _ = make_frm()
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=10000.0, c_conc=100)
    rnd = frm.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary,
        curiosity_demand=curiosity)
    pg, cg = rnd.grants[PRIMARY], rnd.grants[CURIOSITY]
    ok = (pg.budget_s >= 30.0 and pg.max_concurrent >= 1
          and cg.budget_s <= 100.0 - pg.budget_s + 1e-6
          and pg.budget_s + cg.budget_s <= 100.0 + 1e-6)
    return (name, ok,
            f"primary grant {pg.budget_s:.1f}s/{pg.max_concurrent} slots, "
            f"curiosity grant {cg.budget_s:.1f}s/{cg.max_concurrent} slots")


def b2_zero_allocation_hard_shutdown() -> Check:
    name = "B2 HARD_SHUTDOWN_RESOURCE -> zero curiosity allocation"
    frm, _ = make_frm()
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=50.0, c_conc=2)
    rnd = frm.evaluate_round(
        enforcement_state=HARD_SHUTDOWN_RESOURCE, primary_demand=primary,
        curiosity_demand=curiosity)
    cg, pg = rnd.grants[CURIOSITY], rnd.grants[PRIMARY]
    ok = (cg.budget_s == 0.0 and cg.max_concurrent == 0
          and pg.budget_s >= 30.0)
    return (name, ok,
            f"curiosity grant {cg.budget_s:.1f}s/{cg.max_concurrent} slots")


def b3_zero_allocation_suspended() -> Check:
    name = "B3 SUSPENDED_SAFETY -> zero curiosity allocation"
    frm, _ = make_frm()
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=50.0, c_conc=2)
    rnd = frm.evaluate_round(
        enforcement_state=SUSPENDED_SAFETY, primary_demand=primary,
        curiosity_demand=curiosity)
    cg = rnd.grants[CURIOSITY]
    ok = cg.budget_s == 0.0 and cg.max_concurrent == 0
    return (name, ok,
            f"curiosity grant {cg.budget_s:.1f}s/{cg.max_concurrent} slots")


def b4_zero_allocation_banned() -> Check:
    name = "B4 BANNED_6M -> zero curiosity allocation"
    frm, _ = make_frm()
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=50.0, c_conc=2)
    rnd = frm.evaluate_round(
        enforcement_state=BANNED_6M, primary_demand=primary,
        curiosity_demand=curiosity)
    cg = rnd.grants[CURIOSITY]
    ok = cg.budget_s == 0.0 and cg.max_concurrent == 0
    return (name, ok,
            f"curiosity grant {cg.budget_s:.1f}s/{cg.max_concurrent} slots")


def b5_warning_1_restriction() -> Check:
    name = "B5 WARNING_1 restricts curiosity to the standing fraction"
    frm, _ = make_frm()
    frac = frm.policy.warning_1_cap_fraction
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=60.0, c_conc=2)
    rnd = frm.evaluate_round(
        enforcement_state=WARNING_1, primary_demand=primary,
        curiosity_demand=curiosity)
    cg = rnd.grants[CURIOSITY]
    eff = rnd.curiosity_demand_effective
    ok = (eff.budget_s == 60.0 * frac
          and cg.budget_s <= 60.0 * frac + 1e-6
          and rnd.grants[PRIMARY].budget_s >= 30.0)
    return (name, ok,
            f"effective demand {eff.budget_s:.1f}s (frac {frac:g}), "
            f"grant {cg.budget_s:.1f}s")


def b6_epoch_recall() -> Check:
    name = "B6 lending is epoch-bounded: recalled at the boundary"
    frm, clock = make_frm()
    # Primary demands nothing: its whole minimum is lendable this epoch.
    primary, curiosity = _demands(p_budget=0.0, p_conc=0,
                                  c_budget=40.0, c_conc=2)
    r1 = frm.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary,
        curiosity_demand=curiosity)
    cg1 = r1.grants[CURIOSITY]
    lent_now = cg1.lent and cg1.lending.lent_budget_s > 0
    close = frm.advance_epoch()
    recalled = (close.recalled_lent_budget_s
                == cg1.lending.lent_budget_s)
    # Next epoch primary actually needs its minimum: no more loan.
    clock.advance(300.0)
    primary2, curiosity2 = _demands(p_budget=30.0, p_conc=1,
                                    c_budget=40.0, c_conc=2)
    r2 = frm.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary2,
        curiosity_demand=curiosity2)
    cg2 = r2.grants[CURIOSITY]
    no_loan = not cg2.lent and r2.grants[PRIMARY].budget_s >= 30.0
    ok = lent_now and recalled and no_loan
    return (name, ok,
            f"epoch1 lent={cg1.lending.lent_budget_s:.1f}s "
            f"(lent={cg1.lent}), recalled={close.recalled_lent_budget_s:.1f}s, "
            f"epoch2 lent={cg2.lent}, primary "
            f"{r2.grants[PRIMARY].budget_s:.1f}s")


def b7_non_preemption() -> Check:
    name = "B7 mid-epoch demand cannot shrink a running grant"
    frm, _ = make_frm()
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=20.0, c_conc=1)
    r1 = frm.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary,
        curiosity_demand=curiosity)
    g1 = {d: (g.grant_id, g.budget_s, g.max_concurrent)
          for d, g in r1.grants.items()}
    # New demand arrives mid-epoch trying to take curiosity's share.
    primary2, curiosity2 = _demands(p_budget=90.0, p_conc=4,
                                    c_budget=20.0, c_conc=1)
    r2 = frm.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary2,
        curiosity_demand=curiosity2)
    g2 = {d: (g.grant_id, g.budget_s, g.max_concurrent)
          for d, g in r2.grants.items()}
    unchanged = g1 == g2 and r2.mid_epoch_refusal
    # And there is no API that shrinks an issued grant: frozen dataclass.
    frozen = False
    try:
        r1.grants[CURIOSITY].budget_s = 0.0  # type: ignore[misc]
    except FrozenInstanceError:
        frozen = True
    ok = unchanged and frozen and len(frm.pending_demands) == 1
    return (name, ok,
            f"grants unchanged={unchanged}, frozen={frozen}, "
            f"pending recorded={len(frm.pending_demands)}")


def b8_minimum_above_capacity_loud() -> Check:
    name = "B8 primary minimum above capacity is a loud config error"
    try:
        make_policy(total_budget_s=100.0, primary_minimum_budget_s=120.0)
    except ValueError as e:
        return (name, True, f"ValueError: {e}")
    return (name, False, "no error raised")


def b9_expensive_model_refused() -> Check:
    name = "B9 unauthorized expensive-model request is refused"
    frm, _ = make_frm()  # default policy: expensive models disabled
    primary = DomainDemand(domain=PRIMARY, budget_s=30.0, max_concurrent=1)
    curiosity = DomainDemand(
        domain=CURIOSITY, budget_s=40.0, max_concurrent=2,
        requests_expensive_model=True, expensive_model_name="big-reasoner")
    rnd = frm.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary,
        curiosity_demand=curiosity)
    cg = rnd.grants[CURIOSITY]
    refused_note = any("expensive model" in n and "NOT authorized" in n
                       for n in rnd.notes)
    ok = cg.budget_s == 0.0 and cg.max_concurrent == 0 and refused_note
    # ...while an authorized model passes the gate.
    policy2 = make_policy(expensive_models=ExpensiveModelPolicy(
        enabled=True, allowed_models=frozenset({"big-reasoner"}),
        conditions={"note": "James standing auth 2026-09-29"}))
    frm2, _ = make_frm(policy2)
    rnd2 = frm2.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary,
        curiosity_demand=curiosity)
    ok = ok and rnd2.grants[CURIOSITY].budget_s > 0
    return (name, ok,
            f"refused grant {cg.budget_s:.1f}s, note={refused_note}, "
            f"authorized grant {rnd2.grants[CURIOSITY].budget_s:.1f}s")


def b10_cost_provenance() -> Check:
    name = "B10 cost inputs keep provenance; UNPRICED handled"
    frm, _ = make_frm()
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=20.0, c_conc=1)
    costs = [
        CostInput(kind=CostKind.MEASURED, value=0.42,
                  provenance="provider invoice #7"),
        CostInput(kind=CostKind.ESTIMATED, value=1.5,
                  provenance="provisional estimator v0 (not a billing fact)"),
        CostInput(kind=CostKind.UNPRICED, value=None,
                  provenance="no pricing infrastructure"),
    ]
    rnd = frm.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary,
        curiosity_demand=curiosity, cost_inputs=costs)
    kinds = [c.kind for c in rnd.cost_inputs]
    ok = (kinds == [CostKind.MEASURED, CostKind.ESTIMATED, CostKind.UNPRICED]
          and rnd.cost_inputs[0].provenance == "provider invoice #7"
          and rnd.cost_inputs[2].value is None)
    return (name, ok, f"kinds={kinds}")


def b11_grant_shape() -> Check:
    name = "B11 every grant matches the frozen grant shape"
    frm, _ = make_frm()
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=20.0, c_conc=1)
    rnd = frm.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary,
        curiosity_demand=curiosity)
    dicts = [g.as_dict() for g in rnd.grants.values()]
    ok = all(frozenset(d.keys()) == GRANT_KEYS for d in dicts)
    dims = all(set(d["dimensions"]) == {"budget_s", "max_concurrent"}
               and set(d["primary_minimum"]) == {"budget_s", "max_concurrent"}
               for d in dicts)
    return (name, ok and dims,
            f"keys={[sorted(d.keys()) for d in dicts]}")


def b12_end_to_end_spend() -> Check:
    name = "B12 grant is a real ceiling: spending past it is refused"
    frm, _ = make_frm()
    p_src = ScriptedDemandSource([DomainDemand(
        domain=PRIMARY, budget_s=30.0, max_concurrent=1)])
    c_src = ScriptedDemandSource([DomainDemand(
        domain=CURIOSITY, budget_s=40.0, max_concurrent=2)])
    rnd = frm.evaluate_round(
        enforcement_state=RUNNING,
        primary_demand=p_src.next_demand(),
        curiosity_demand=c_src.next_demand())
    spender = GrantSpender(rnd.grants[CURIOSITY])
    grant_b = rnd.grants[CURIOSITY].budget_s
    if grant_b <= 0:
        return (name, False, "curiosity grant was zero; cannot exercise spend")
    spender.spend_budget(grant_b - 1.0)
    spender.acquire_slot()
    refused_budget = refused_slot = False
    try:
        spender.spend_budget(2.0)
    except GrantExhausted:
        refused_budget = True
    try:
        for _ in range(10):
            spender.acquire_slot()
    except GrantExhausted:
        refused_slot = True
    ok = refused_budget and refused_slot and spender.spent_s == grant_b - 1.0
    return (name, ok,
            f"grant {grant_b:.1f}s, spent {spender.spent_s:.1f}s, "
            f"budget refusal={refused_budget}, slot refusal={refused_slot}")


def b13_yield_provisional() -> Check:
    name = "B13 expected-yield estimates are provisional and non-binding"
    frm, _ = make_frm()
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=20.0, c_conc=1)
    yields = {
        PRIMARY: ExpectedYield(value=0.1, basis="provisional heuristic"),
        CURIOSITY: ExpectedYield(value=999.0,
                                 basis="provisional heuristic: huge claim"),
    }
    rnd = frm.evaluate_round(
        enforcement_state=RUNNING, primary_demand=primary,
        curiosity_demand=curiosity, expected_yields=yields)
    prov = all(y.provisional for y in rnd.expected_yields.values())
    noted = any("PROVISIONAL" in n and "non-binding" in n
                for n in rnd.notes)
    minimum_holds = rnd.grants[PRIMARY].budget_s >= 30.0
    return (name, prov and noted and minimum_holds,
            f"provisional={prov}, noted={noted}, "
            f"primary {rnd.grants[PRIMARY].budget_s:.1f}s")


def b14_unknown_state_rejected() -> Check:
    name = "B14 unknown enforcement state is rejected loudly"
    frm, _ = make_frm()
    primary, curiosity = _demands(p_budget=30.0, p_conc=1,
                                  c_budget=20.0, c_conc=1)
    try:
        frm.evaluate_round(
            enforcement_state="SOME_NEW_STATE", primary_demand=primary,
            curiosity_demand=curiosity)
    except ValueError as e:
        return (name, True, f"ValueError: {e}")
    return (name, False, "no error raised")


def b15_shared_arbitrator_class() -> Check:
    name = "B15 FRM owns a second arbitrator INSTANCE, not a second class"
    frm, _ = make_frm()
    ok = (type(frm.arbitrator) is ResourceArbitrator
          and frm.arbitrator is not None)
    # It is its own instance: its epoch ledger is independent of any
    # Primary-path arbitrator (fresh epoch numbering).
    fresh = frm.arbitrator.epoch_id == 0
    return (name, ok and fresh,
            f"type is ResourceArbitrator={ok}, fresh instance={fresh}")


BATTERY: List[Callable[[], Check]] = [
    b1_primary_minimum_under_spike,
    b2_zero_allocation_hard_shutdown,
    b3_zero_allocation_suspended,
    b4_zero_allocation_banned,
    b5_warning_1_restriction,
    b6_epoch_recall,
    b7_non_preemption,
    b8_minimum_above_capacity_loud,
    b9_expensive_model_refused,
    b10_cost_provenance,
    b11_grant_shape,
    b12_end_to_end_spend,
    b13_yield_provisional,
    b14_unknown_state_rejected,
    b15_shared_arbitrator_class,
]


def run_all() -> List[Check]:
    """Run the full battery; each check is independent (fresh FRM)."""
    results: List[Check] = []
    for check_fn in BATTERY:
        try:
            results.append(check_fn())
        except Exception as e:  # noqa: BLE001 -- battery must not die silently
            results.append((check_fn.__name__, False,
                            f"EXCEPTION {type(e).__name__}: {e}"))
    return results
