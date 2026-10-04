#!/usr/bin/env python3
"""CREATIVITY-RUNCTRL-1 proof battery: the Creativity Run Controller.

Battery A — the controller lifecycle (predicates, budget wiring, kill,
records) through the real executive/FRM paths in fresh processes.
Battery B — exact refinement turn counts via the executive's public
step() interface with an explicitly constructed _RunContext.

No mocks of the machinery under test. Fixtures are real sqlite stores
via public APIs; the FRM grant path is the real issue_run_grant.
"""

import os
import sys
import tempfile
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WT_ROOT = os.environ.get("WT_ROOT", os.path.abspath(
    os.path.join(SCRIPT_DIR, "..", "..")))
sys.path.insert(0, WT_ROOT)
sys.path.insert(0, os.path.join(WT_ROOT, "pylib"))  # swarm_engine import root

from swarm_engine.primitives.core import (  # noqa: E402
    NUM,
    Effect,
    PrimitiveRegistry,
)
from runtime.services.evidence import EvidenceStore  # noqa: E402
from runtime.core.executive.detector import ScanReport  # noqa: E402
from runtime.core.microcontroller.substrate import (  # noqa: E402
    CognitionResult,
)
import runtime.creativity.executive as exec_mod  # noqa: E402
from runtime.creativity.executive import (  # noqa: E402
    CreativityExecutiveController,
    SearchBounds,
    classify_ambiguity,
)
from runtime.creativity.intent import register_intent  # noqa: E402
from runtime.creativity.ledger import LedgerEntry  # noqa: E402
from runtime.creativity.budget import (  # noqa: E402
    AuthorizationRecord,
    BudgetEnvelope,
    BudgetHalted,
    BudgetRefused,
    COMPUTE,
    CostBound,
    CreativityBudget,
    MONETARY,
)
from runtime.creativity.run_controller import (  # noqa: E402
    CreativityRunController,
    RunControllerRefused,
    DORMANT,
    REFUSED,
    COMPLETED,
    KILLED,
)
from runtime.creativity.stages import CreativeStage  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} :: {detail}")


# ---------------------------------------------------------------------------
# fixtures: real machinery, scratch stores
# ---------------------------------------------------------------------------
def make_registry():
    reg = PrimitiveRegistry()

    @reg.define("add", "arithmetic", {"a": NUM, "b": NUM}, NUM,
                effects=(Effect.PURE,))
    def _add(a, b):
        return a + b

    @reg.define("mul", "arithmetic", {"a": NUM, "b": NUM}, NUM,
                effects=(Effect.PURE,))
    def _mul(a, b):
        return a * b

    @reg.define("sub", "arithmetic", {"a": NUM, "b": NUM}, NUM,
                effects=(Effect.PURE,))
    def _sub(a, b):
        return a - b

    return reg


def clean_scan():
    def scan():
        return ScanReport(scanned_at=time.time(), presentations=[],
                          source_absent=[], observed={}, notes=[])
    return scan


def make_exec(tmp, scan=None, cognition=None, stop_event=None):
    reg = make_registry()
    ev = stop_event or threading.Event()
    ex = CreativityExecutiveController(
        store_dir=tmp,
        primitives=reg,
        boundary_scan=scan or clean_scan(),
        stop_event=ev,
        cognition=cognition,
        search_bounds=SearchBounds(max_composition_size=2, max_candidates=10),
        repo_root=WT_ROOT,
        admission_db_path=os.path.join(tmp, "adm.db"))
    evs = EvidenceStore(os.path.join(tmp, "ev.db"))
    for pid in ("add", "mul", "sub"):
        e = evs.add_entry(
            "observation",
            f"gate crossing: primitive '{pid}' admitted (runctrl1 fixture)",
            source="gate:RUNCTRL1")
        evs.verify_entry(e["id"], "gate:RUNCTRL1")
        ex.ledger.add_entry(
            LedgerEntry(pid, "evidence", e["id"], "gate:RUNCTRL1@runctrl1"))
    return ex, ev


def make_envelope(compute_bound):
    return BudgetEnvelope({
        COMPUTE: CostBound(dimension=COMPUTE, unit="s", bound=compute_bound,
                           measurement="runctrl1 battery wall-clock",
                           provenance="runctrl1 battery fixture"),
        MONETARY: CostBound(dimension=MONETARY, unit="USD", bound=None,
                            measurement="unpriced",
                            provenance="runctrl1 battery fixture"),
    })


def make_budget(tmp, compute_bound):
    return CreativityBudget(make_envelope(compute_bound),
                            spend_db_path=os.path.join(tmp, "spend.db"))


def make_controller(tmp, bound=2, compute_bound=60.0, scan=None,
                    cognition=None, work_id="w1"):
    ex, ev = make_exec(tmp, scan=scan, cognition=cognition)
    budget = make_budget(tmp, compute_bound)
    ctl = CreativityRunController(
        executive=ex, budget=budget, refinement_bound=bound,
        work_id=work_id, kill_event=ev)
    return ctl, ex, ev, budget


def fresh_tmp():
    return tempfile.mkdtemp(prefix="runctrl1_")


INTENT = register_intent("tester", "make me a song")


# ---------------------------------------------------------------------------
# Battery A — controller lifecycle
# ---------------------------------------------------------------------------
def a_construction():
    for bad, label in [(0, "zero"), (-3, "negative"), ("3", "non-int"),
                       (None, "none")]:
        try:
            CreativityRunController(
                executive=object(), budget=object(),
                refinement_bound=bad, work_id="w", kill_event=object())
            check(f"a_construction bound {label} refused", False,
                  "no refusal raised")
        except RunControllerRefused as exc:
            check(f"a_construction bound {label} refused", True)
        except Exception as exc:  # noqa: BLE001
            check(f"a_construction bound {label} refused", False,
                  f"wrong exception: {type(exc).__name__}: {exc}")

    tmp = fresh_tmp()
    ex, ev = make_exec(tmp)
    budget = make_budget(tmp, 60.0)
    for kw, label in [
            (dict(executive="nope"), "bad executive"),
            (dict(budget="nope"), "bad budget"),
            (dict(kill_event="nope"), "bad kill_event"),
            (dict(work_id="  "), "empty work_id")]:
        args = dict(executive=ex, budget=budget, refinement_bound=2,
                    work_id="w", kill_event=ev)
        args.update(kw)
        try:
            CreativityRunController(**args)
            check(f"a_construction {label} refused", False, "no refusal")
        except RunControllerRefused:
            check(f"a_construction {label} refused", True)


def a_dormant_no_intent():
    tmp = fresh_tmp()
    scan_calls = []
    orig = clean_scan()

    def counting_scan():
        scan_calls.append(1)
        return orig()

    ctl, ex, ev, budget = make_controller(tmp, scan=counting_scan)
    before_threads = len(threading.enumerate())
    rec = ctl.run(None, estimated_compute_s=5.0)
    after_threads = len(threading.enumerate())
    check("a_dormant_no_intent status dormant", rec.status == DORMANT,
          rec.status)
    check("a_dormant_no_intent intent predicate unmet with reason",
          any(p.name == "intent_present" and not p.met and p.reason
              for p in rec.predicates),
          str([(p.name, p.met) for p in rec.predicates]))
    check("a_dormant_no_intent executive untouched (scan never called)",
          scan_calls == [], f"scan called {len(scan_calls)}x")
    check("a_dormant_no_intent no threads created",
          after_threads == before_threads,
          f"{before_threads} -> {after_threads}")
    s = budget.ledger.summary("w1")
    check("a_dormant_no_intent zero spend accounted",
          s["granted_compute_s"] == 0 and s["spent_compute_s"] == 0, str(s))


def a_dormant_kill_active():
    tmp = fresh_tmp()
    scan_calls = []
    orig = clean_scan()

    def counting_scan():
        scan_calls.append(1)
        return orig()

    ctl, ex, ev, budget = make_controller(tmp, scan=counting_scan)
    ev.set()  # kill BEFORE run: predicates must refuse
    rec = ctl.run(INTENT, estimated_compute_s=5.0)
    check("a_dormant_kill_active status dormant", rec.status == DORMANT,
          rec.status)
    check("a_dormant_kill_active kill predicate unmet",
          any(p.name == "no_kill_active" and not p.met
              for p in rec.predicates))
    check("a_dormant_kill_active executive untouched",
          scan_calls == [], f"scan called {len(scan_calls)}x")


def a_dormant_estimate_negative():
    tmp = fresh_tmp()
    ctl, ex, ev, budget = make_controller(tmp)
    rec = ctl.run(INTENT, estimated_compute_s=-1.0)
    check("a_dormant_estimate_negative status dormant",
          rec.status == DORMANT, rec.status)
    check("a_dormant_estimate_negative budget predicate names the reason",
          any(p.name == "budget_granted" and not p.met and ">=" in p.reason
              for p in rec.predicates),
          str([(p.name, p.met, p.reason) for p in rec.predicates
               if p.name == "budget_granted"]))


def a_refused_budget():
    tmp = fresh_tmp()
    scan_calls = []
    orig = clean_scan()

    def counting_scan():
        scan_calls.append(1)
        return orig()

    ctl, ex, ev, budget = make_controller(tmp, compute_bound=0.05,
                                         scan=counting_scan)
    rec = ctl.run(INTENT, estimated_compute_s=5.0)
    # Per the mandate, budget-refused is a dormant predicate outcome:
    # "predicates unmet (no intent / budget refused / kill active) ->
    # controller dormant". REFUSED is reserved for post-grant commission
    # refusal (a_commission_refused).
    check("a_refused_budget status dormant (predicate unmet)",
          rec.status == DORMANT, rec.status)
    check("a_refused_budget exact reason names the bound",
          "0.05" in rec.error and "authorization" in rec.error, rec.error)
    check("a_refused_budget executive untouched (scan never called)",
          scan_calls == [], f"scan called {len(scan_calls)}x")
    check("a_refused_budget no grant issued", not rec.grant_issued)


def a_active_run():
    tmp = fresh_tmp()
    ctl, ex, ev, budget = make_controller(tmp, bound=2)
    rec = ctl.run(INTENT, estimated_compute_s=5.0)
    check("a_active_run status completed", rec.status == COMPLETED,
          f"{rec.status} :: {rec.error}")
    check("a_active_run all predicates met",
          all(p.met for p in rec.predicates),
          str([(p.name, p.met) for p in rec.predicates]))
    check("a_active_run real executive path (admission outcome)",
          rec.outcome_status == "completed" and "admitted" in rec.outcome_detail,
          f"{rec.outcome_status} :: {rec.outcome_detail[:80]}")
    check("a_active_run grant issued", rec.grant_issued and rec.grant_size_s == 5.0,
          f"{rec.grant_issued} {rec.grant_size_s}")
    check("a_active_run spend recorded from measured actuals",
          rec.spend_s > 0, f"spend_s={rec.spend_s}")
    check("a_active_run no ceiling stop", rec.ceiling_stop is None)
    check("a_active_run kill not seen", not rec.kill_seen)


def a_bound_one_terminates():
    # Bound=1: the bound structurally binds (plateau needs 3+ history
    # entries, unreachable) — the run must terminate at release.
    tmp = fresh_tmp()
    ctl, ex, ev, budget = make_controller(tmp, bound=1)
    rec = ctl.run(INTENT, estimated_compute_s=5.0)
    check("a_bound_one_terminates completed (never loops forever)",
          rec.status == COMPLETED, f"{rec.status} :: {rec.error}")
    check("a_bound_one_terminates bound recorded", rec.refinement_bound == 1)


def a_kill_via_scan():
    # Deterministic kill: the boundary scan (FIRST thing commission does)
    # sets the shared event — predicates already passed, run() then stops
    # cold at its first poll.
    tmp = fresh_tmp()

    def evil_scan():
        ev_holder[0].set()
        return ScanReport(scanned_at=time.time(), presentations=[],
                          source_absent=[], observed={}, notes=[])

    ev_holder = []
    ex, ev = make_exec(tmp, scan=evil_scan)
    ev_holder.append(ev)
    budget = make_budget(tmp, 60.0)
    ctl = CreativityRunController(
        executive=ex, budget=budget, refinement_bound=5,
        work_id="wkill", kill_event=ev)
    rec = ctl.run(INTENT, estimated_compute_s=5.0)
    check("a_kill_via_scan status killed", rec.status == KILLED,
          f"{rec.status} :: {rec.error}")
    check("a_kill_via_scan outcome killed cold",
          rec.outcome_status == "killed" and "cold" in rec.outcome_detail,
          f"{rec.outcome_status} :: {rec.outcome_detail[:60]}")
    check("a_kill_via_scan kill seen", rec.kill_seen)


def a_kill_midrun_cognition():
    # Deterministic mid-run kill: the cognition provider (called during
    # VARIATION, after real generation work) sets the shared event.
    tmp = fresh_tmp()

    class KillCognition:
        def __init__(self, event):
            self._event = event

        def request_cognition(self, *, mc_id, prompt, context):
            self._event.set()
            return CognitionResult(ok=True, text="[]",
                                   provenance="runctrl1-test")

    ev = threading.Event()
    ex, _ = make_exec(tmp, cognition=KillCognition(ev), stop_event=ev)
    budget = make_budget(tmp, 60.0)
    ctl = CreativityRunController(
        executive=ex, budget=budget, refinement_bound=5,
        work_id="wkill2", kill_event=ev)
    rec = ctl.run(INTENT, estimated_compute_s=5.0)
    check("a_kill_midrun_cognition status killed", rec.status == KILLED,
          f"{rec.status} :: {rec.error}")
    check("a_kill_midrun_cognition kill seen with record",
          rec.kill_seen and rec.run_id, f"kill_seen={rec.kill_seen}")


def a_ceiling_midwork():
    # Envelope 0.1s; pre-spend 0.08 on the work_id through the real
    # record_spend; the run's closing actuals push cumulative spend over
    # the ceiling -> CeilingStop with preservation; afterwards the work
    # is halted and totals are frozen.
    tmp = fresh_tmp()
    ctl, ex, ev, budget = make_controller(tmp, bound=1, compute_bound=0.1,
                                         work_id="wceil")
    budget.record_spend("wceil", 0.08)
    rec = ctl.run(INTENT, estimated_compute_s=0.01)
    check("a_ceiling_midwork ceiling stop recorded",
          rec.ceiling_stop is not None,
          f"status={rec.status}")
    check("a_ceiling_midwork preservation keys present",
          rec.ceiling_stop is not None and
          "consumption_records" in rec.ceiling_stop.get("preservation_keys", []),
          str((rec.ceiling_stop or {}).get("preservation_keys")))
    before = budget.ledger.summary("wceil")["spent_compute_s"]
    try:
        budget.request_budget(work_id="wceil", estimated_compute_s=0.01)
        check("a_ceiling_midwork halted afterwards", False,
              "request_budget did not raise BudgetHalted")
    except BudgetHalted:
        check("a_ceiling_midwork halted afterwards", True)
    try:
        budget.record_spend("wceil", 0.01)
        check("a_ceiling_midwork no further spend", False,
              "record_spend did not raise BudgetHalted")
    except BudgetHalted:
        check("a_ceiling_midwork no further spend", True)
    after = budget.ledger.summary("wceil")["spent_compute_s"]
    check("a_ceiling_midwork totals frozen", before == after,
          f"{before} -> {after}")


def a_auth_expiry():
    # Authorization expands the bound; after expiry the ceiling logic
    # must honor the dropped effective bound.
    tmp = fresh_tmp()
    budget = make_budget(tmp, 1.0)
    now = time.time()
    budget.authorize_expansion(AuthorizationRecord(
        authorized_by="runctrl1-test", dimension=COMPUTE, additional=10.0,
        scope="wexp", issued_at=now, expires_at=now + 0.2))
    grant = budget.request_budget(work_id="wexp", estimated_compute_s=5.0)
    check("a_auth_expiry granted inside expanded bound", grant is not None)
    time.sleep(0.35)  # let the authorization expire
    stop = budget.record_spend("wexp", 2.0)
    check("a_auth_expiry ceiling honors dropped bound",
          stop is not None and "1.00s" in stop.reason,
          getattr(stop, "reason", None))


def a_no_threads():
    tmp = fresh_tmp()
    ctl, ex, ev, budget = make_controller(tmp, bound=1)
    before = set(t.name for t in threading.enumerate())
    ctl.run(INTENT, estimated_compute_s=5.0)
    time.sleep(0.05)
    after = set(t.name for t in threading.enumerate())
    check("a_no_threads controller creates no threads",
          after <= before, f"new threads: {sorted(after - before)}")


def a_new_run_after_kill():
    # No resumption: after KILLED, run() starts a NEW run_id. With the
    # kill still set the new call is dormant.
    tmp = fresh_tmp()

    def evil_scan():
        ev_holder[0].set()
        return ScanReport(scanned_at=time.time(), presentations=[],
                          source_absent=[], observed={}, notes=[])

    ev_holder = []
    ex, ev = make_exec(tmp, scan=evil_scan)
    ev_holder.append(ev)
    budget = make_budget(tmp, 60.0)
    ctl = CreativityRunController(
        executive=ex, budget=budget, refinement_bound=2,
        work_id="wkr", kill_event=ev)
    rec1 = ctl.run(INTENT, estimated_compute_s=5.0)
    rec2 = ctl.run(INTENT, estimated_compute_s=5.0)
    check("a_new_run_after_kill first run killed", rec1.status == KILLED,
          rec1.status)
    check("a_new_run_after_kill second call is a NEW run (no resumption)",
          rec2.run_id != rec1.run_id, f"{rec1.run_id} vs {rec2.run_id}")
    check("a_new_run_after_kill kill still set -> dormant",
          rec2.status == DORMANT, rec2.status)


def a_commission_refused():
    # boundary_scan returns garbage -> commission raises ExecutiveRefused
    # AFTER the budget grant: status REFUSED, grant noted unspent.
    tmp = fresh_tmp()

    def garbage_scan():
        return "not-a-scan-report"

    ctl, ex, ev, budget = make_controller(tmp, scan=garbage_scan)
    rec = ctl.run(INTENT, estimated_compute_s=5.0)
    check("a_commission_refused status refused", rec.status == REFUSED,
          rec.status)
    check("a_commission_refused exact reason",
          "ScanReport" in rec.error, rec.error)
    check("a_commission_refused grant issued but unspent",
          rec.grant_issued and rec.outcome_status is None)


# ---------------------------------------------------------------------------
# Battery B — exact refinement turn counts (public step() interface)
# ---------------------------------------------------------------------------
def drive_bound(bound, isolate_plateau):
    """Drive executive.step() with an explicitly constructed _RunContext.

    Returns (loop_backs, routed). The context is built from public pieces
    (classify_ambiguity is public; _RunContext is a plain dataclass); the
    mc spawn/retire bookkeeping of run() is omitted because no step
    handler reads ctx.mc — verified by reading the handlers.
    """
    tmp = fresh_tmp()
    ex, ev = make_exec(tmp)
    intent = register_intent("tester", "make me a song")
    ctx = exec_mod._RunContext(
        executive=ex,
        intent=intent,
        ambiguity=exec_mod.classify_ambiguity(intent),
        current_stage=CreativeStage.INTENT,
        refinement_bound=bound,
        constraints=())
    loopbacks = 0
    critiques = 0
    while True:
        step = ex.step(ctx)  # public directly-drivable interface
        assert step.advanced, f"stage refused: {step.stage} {step.detail}"
        if step.stage == CreativeStage.CRITIQUE:
            critiques += 1
            if isolate_plateau:
                # Isolate the bound logic: a strictly increasing history
                # makes the v1 plateau check (non-improvement over the
                # last two) provably unable to fire — any stop is the
                # bound's doing. Disclosed, not hidden.
                ctx.value_history = list(range(1, critiques + 1))
        if step.stage == CreativeStage.RELEASE:
            return loopbacks, "release"
        if step.stage == CreativeStage.REFINEMENT:
            if step.next_stage == CreativeStage.VARIATION:
                loopbacks += 1
            elif step.next_stage != CreativeStage.RELEASE:
                raise AssertionError(
                    f"unexpected refinement routing {step.next_stage}")
        assert step.next_stage is not None, "stage with no next"
        ctx.current_stage = step.next_stage


def b_exact_counts():
    for bound in (1, 2, 3):
        loopbacks, routed = drive_bound(bound, isolate_plateau=True)
        check(f"b_exact_count bound={bound} -> exactly {bound} turns",
              loopbacks == bound and routed == "release",
              f"loopbacks={loopbacks} routed={routed}")


def b_plateau_early():
    # Bound=10 with REAL values (no isolation): values are capped by the
    # criteria count, so the plateau must fire before the bound binds.
    # _refinement_continue has exactly two False paths (bound, plateau) —
    # loopbacks < bound therefore proves the plateau fired (elimination).
    loopbacks, routed = drive_bound(10, isolate_plateau=False)
    check("b_plateau_early adaptive stop fires inside the bound",
          loopbacks < 10 and routed == "release",
          f"loopbacks={loopbacks} routed={routed}")
    check("b_plateau_early at least one iteration ran", loopbacks >= 1,
          f"loopbacks={loopbacks}")


# ---------------------------------------------------------------------------
def main():
    a_construction()
    a_dormant_no_intent()
    a_dormant_kill_active()
    a_dormant_estimate_negative()
    a_refused_budget()
    a_active_run()
    a_bound_one_terminates()
    a_kill_via_scan()
    a_kill_midrun_cognition()
    a_ceiling_midwork()
    a_auth_expiry()
    a_no_threads()
    a_new_run_after_kill()
    a_commission_refused()
    b_exact_counts()
    b_plateau_early()
    print(f"[SUMMARY] PASS={PASS} FAIL={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
