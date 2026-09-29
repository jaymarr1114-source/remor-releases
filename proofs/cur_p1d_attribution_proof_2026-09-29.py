#!/usr/bin/env python3
"""CUR-P1D proof battery — yield-attribution metering + dedicated causal gate.

Executes for real, end to end through the actual call path:
    grant → allocation → metered substrate call (REAL CPU work, REAL
    measured spend) → caller budget pays (REAL refusal on overrun) →
    expenditure recorded → work/result recorded → admission →
    attribution link recomputed from the ledgers.

Then the dedicated causal-necessity gate: every check removes or
alters a claimed causal expenditure (or admission) in an isolated
snapshot and recomputes from scratch. Quantity perturbations change
IDs not at all — an implementation that merely echoed ledger IDs
could not pass.

Exit 0 only if every check passes. Any failure → nonzero exit and
the failing check's before/after evidence.

Battery discipline: run me last, sequentially, on an unloaded
machine (1-min load <= 2.0).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback

WORKTREE = os.path.expanduser("~/workspace/worktrees/cur-p1d")
sys.path.insert(0, WORKTREE)

from runtime.curiosity.attribution.chain import (
    ChainLedger, aggregate_attribution, recompute_attribution)
from runtime.curiosity.attribution.gate import (
    CausalGate, Counterfactual, GateFailure)
from runtime.curiosity.attribution.grants import (
    AllocationLedger, AllocationRefused, Grant)
from runtime.curiosity.attribution.spend import ExpenditureLedger
from runtime.curiosity.attribution.testdoubles import (
    BudgetExceeded, EnforcementStub, InquiryDriver, TestAdmissionBoard,
    make_substrate)

EXEC = "test_executive"
RESULTS = []


def check(name):
    def deco(fn):
        try:
            detail = fn()
            RESULTS.append((name, True, detail))
            print(f"PASS  {name}\n      {detail}")
        except Exception as e:  # noqa: BLE001
            RESULTS.append((name, False, f"{e}\n{traceback.format_exc(limit=3)}"))
            print(f"FAIL  {name}\n      {e}")
    return deco


def make_grant(gid, budget_s=60.0, max_concurrent=4):
    return Grant(grant_id=gid, epoch_id="epoch-1", epoch_s=600,
                 dimensions={"budget_s": budget_s, "max_concurrent": max_concurrent},
                 primary_minimum={"budget_s": 1.0, "max_concurrent": 1},
                 lent=False)


def main():
    tmp = tempfile.mkdtemp(prefix="cur_p1d_proof_")
    alloc_db = os.path.join(tmp, "alloc.db")
    exp_db = os.path.join(tmp, "exp.db")
    chain_db = os.path.join(tmp, "chain.db")

    alloc_ledger = AllocationLedger(alloc_db)
    exp_ledger = ExpenditureLedger(exp_db)
    chain = ChainLedger(chain_db)
    board = TestAdmissionBoard(chain)
    enforcement = EnforcementStub("RUNNING")

    # shared infrastructure: one substrate instance for concurrent inquiries
    substrate_alpha = make_substrate("provider-alpha", declared_rate_per_cpu_s=1.0)
    substrate_beta = make_substrate("provider-beta", declared_rate_per_cpu_s=2.0)

    drvA = InquiryDriver("ctrl-A", EXEC, alloc_ledger, exp_ledger, chain,
                         board, substrate_alpha)
    drvB = InquiryDriver("ctrl-B", EXEC, alloc_ledger, exp_ledger, chain,
                         board, substrate_alpha)   # shares alpha: shared infra
    drvC = InquiryDriver("ctrl-C", EXEC, alloc_ledger, exp_ledger, chain,
                         board, substrate_beta)

    gate = CausalGate(alloc_db, exp_db, chain_db, EXEC)

    # ------------------------------------------------------------------
    # S0 — allocation: enforcement gate + real budgets
    # ------------------------------------------------------------------
    @check("S0a zero allocation while enforcement state active")
    def _():
        enforcement.set_state("SUSPENDED_SAFETY")
        try:
            drvA.allocate(make_grant("g-blocked"), enforcement)
        except AllocationRefused as e:
            enforcement.set_state("RUNNING")
            return f"refused as required: {e}"
        raise AssertionError("allocation proceeded under SUSPENDED_SAFETY")

    @check("S0b allocation proceeds when RUNNING; grant shape frozen")
    def _():
        a = drvA.allocate(make_grant("g-A", budget_s=60.0), enforcement)
        assert set(a.__dict__) >= {"grant_id", "epoch_id", "controller_id",
                                   "originating_executive", "budget_s",
                                   "max_concurrent", "lent"}
        return (f"grant g-A → ctrl-A: budget_s={a.budget_s} "
                f"exec={a.originating_executive}")

    @check("S0c budget really refuses overrun (A32)")
    def _():
        tiny = InquiryDriver("ctrl-T", EXEC, alloc_ledger, exp_ledger,
                             chain, board, substrate_alpha)
        tiny.allocate(make_grant("g-tiny", budget_s=0.0001), enforcement)
        w = tiny.work("inq-t")
        try:
            tiny.spend("inq-t", w.work_ref, b"x", iterations=20000)
        except BudgetExceeded as e:
            return f"refused as required: {e}"
        raise AssertionError("overspend was paid")

    @check("S0d cost-kind honesty: declared vs unpriced (§8)")
    def _():
        free = make_substrate("provider-free", declared_rate_per_cpu_s=0.0)
        drvF = InquiryDriver("ctrl-F", EXEC, alloc_ledger, exp_ledger,
                             chain, board, free)
        drvF.allocate(make_grant("g-F"), enforcement)
        w = drvF.work("inq-f")
        e = drvF.spend("inq-f", w.work_ref, b"free", iterations=500)
        assert e.cost_kind == "unpriced", e.cost_kind
        assert e.monetary_cost == 0.0
        w2 = drvA.work("inq-a0")
        e2 = drvA.spend("inq-a0", w2.work_ref, b"paid", iterations=500)
        assert e2.cost_kind == "declared", e2.cost_kind
        assert e2.monetary_cost > 0
        return (f"free call: kind={e.cost_kind} cost={e.monetary_cost}; "
                f"rated call: kind={e2.cost_kind} cost={e2.monetary_cost:.6f}")

    drvB.allocate(make_grant("g-B", budget_s=60.0), enforcement)
    drvC.allocate(make_grant("g-C", budget_s=60.0), enforcement)

    # ------------------------------------------------------------------
    # S1 — headline: full chain + collapse + restore
    # ------------------------------------------------------------------
    w1 = drvA.work("inq-A1")
    e1 = drvA.spend("inq-A1", w1.work_ref, b"headline-payload", iterations=3000)
    assert e1.cpu_seconds > 0 and e1.work_units == 3000, "spend must be real"
    r1 = drvA.result(w1.work_ref, "success", detail="headline result")
    ad1 = drvA.admit(r1.result_ref, "accepted", yield_value=10.0)
    link1 = drvA.link(w1.work_ref)

    @check("S1a headline chain: allocation→spend→work→result→admission→yield")
    def _():
        assert link1.expenditure_ids == [e1.expenditure_id]
        assert link1.attributed_yield == 10.0, link1.attributed_yield
        assert link1.total_cpu_seconds == round(e1.cpu_seconds, 6)
        assert link1.acceptance_record_id == ad1.admission_id
        assert link1.originating_executive == EXEC
        assert link1.controller_id == "ctrl-A"
        assert link1.inquiry_id == "inq-A1"
        assert link1.model_provider == "provider-alpha"
        assert link1.grant_id == "g-A"
        return (f"yield={link1.attributed_yield} spend={link1.total_cpu_seconds}s "
                f"cost={link1.total_monetary_cost:.6f} kind={link1.cost_kind}")

    @check("S1b HEADLINE: remove causal expenditure → attribution collapses")
    def _():
        c = gate.check_collapse_on_expenditure_removal(
            "headline-collapse", w1.work_ref, [e1.expenditure_id])
        return c.detail

    @check("S1c HEADLINE: fresh snapshot → attribution returns (restore)")
    def _():
        snap = Counterfactual(alloc_db, exp_db, chain_db, EXEC)
        try:
            restored = snap.link(w1.work_ref)
            assert restored.attributed_yield == 10.0, restored.attributed_yield
            assert restored.expenditure_ids == [e1.expenditure_id]
            assert restored.total_cpu_seconds == link1.total_cpu_seconds
            return (f"restored yield={restored.attributed_yield} "
                    f"spend={restored.total_cpu_seconds}s — collapse was "
                    f"caused by the removal, not by ledger rot")
        finally:
            snap.close()

    # ------------------------------------------------------------------
    # S2 — concurrency: shared substrate, no cross-contamination
    # ------------------------------------------------------------------
    w2 = drvB.work("inq-B1")
    e2 = drvB.spend("inq-B1", w2.work_ref, b"b-payload", iterations=3000)
    r2 = drvB.result(w2.work_ref, "success")
    ad2 = drvB.admit(r2.result_ref, "accepted", yield_value=7.0)
    link2_before = drvB.link(w2.work_ref)

    @check("S2a perturb A's spend → only A's attribution moves")
    def _():
        c = gate.check_quantity_perturbation(
            "concurrency-perturb-A", w1.work_ref, e1.expenditure_id,
            "cpu_seconds", 2.0)
        return c.detail

    @check("S2b control: B's link byte-identical after A's perturbation")
    def _():
        snap = Counterfactual(alloc_db, exp_db, chain_db, EXEC)
        try:
            before = snap.link(w2.work_ref)
            snap.alter_expenditure(e1.expenditure_id,
                                   cpu_seconds=e1.cpu_seconds * 2.0)
            after = snap.link(w2.work_ref)
            assert asdict_eq(before, after), "B moved when only A was perturbed"
            return (f"B unchanged: yield={after.attributed_yield} "
                    f"spend={after.total_cpu_seconds}s")
        finally:
            snap.close()

    # ------------------------------------------------------------------
    # S3 — retries: failed attempt's spend never folds into success
    # ------------------------------------------------------------------
    w3 = drvA.work("inq-A2", attempt_no=1)
    e3 = drvA.spend("inq-A2", w3.work_ref, b"attempt1", iterations=2000)
    r3 = drvA.result(w3.work_ref, "failed", detail="first attempt failed")
    drvA.admit(r3.result_ref, "rejected")
    w4 = drvA.work("inq-A2", attempt_no=2)
    e4 = drvA.spend("inq-A2", w4.work_ref, b"attempt2", iterations=2000)
    r4 = drvA.result(w4.work_ref, "success", detail="retry succeeded")
    ad4 = drvA.admit(r4.result_ref, "accepted", yield_value=5.0)

    @check("S3a retry: successful attempt bills only its own spend")
    def _():
        link4 = drvA.link(w4.work_ref)
        link3 = drvA.link(w3.work_ref)
        assert link4.expenditure_ids == [e4.expenditure_id], link4.expenditure_ids
        assert e3.expenditure_id not in link4.expenditure_ids
        assert link4.attributed_yield == 5.0
        assert link3.attributed_yield == 0.0  # failed: cost, zero yield (R1)
        assert link3.total_cpu_seconds > 0
        return (f"attempt2 yield={link4.attributed_yield} ids={link4.expenditure_ids}; "
                f"attempt1 yield={link3.attributed_yield} spend={link3.total_cpu_seconds}s")

    @check("S3b remove failed attempt's spend → success link unchanged")
    def _():
        c = gate.check_isolation(
            "retry-isolation", w4.work_ref,
            lambda snap: snap.remove_expenditure(e3.expenditure_id))
        return c.detail

    @check("S3c remove success spend → its yield collapses; failed link intact")
    def _():
        snap = Counterfactual(alloc_db, exp_db, chain_db, EXEC)
        try:
            snap.remove_expenditure(e4.expenditure_id)
            after4 = snap.link(w4.work_ref)
            after3 = snap.link(w3.work_ref)
            assert after4.attributed_yield == 0.0
            assert after3.total_cpu_seconds == round(e3.cpu_seconds, 6)
            return (f"attempt2 yield {5.0} → {after4.attributed_yield}; "
                    f"attempt1 spend intact at {after3.total_cpu_seconds}s")
        finally:
            snap.close()

    # ------------------------------------------------------------------
    # S4 — partial results: yield proportional to accepted portion
    # ------------------------------------------------------------------
    w5 = drvA.work("inq-A3")
    e5 = drvA.spend("inq-A3", w5.work_ref, b"partial", iterations=2000)
    r5 = drvA.result(w5.work_ref, "partial", detail="partially useful")
    ad5 = drvA.admit(r5.result_ref, "accepted", yield_value=8.0,
                     accepted_portion=0.6)

    @check("S4a partial: yield = 0.6 × 8.0 = 4.8")
    def _():
        link5 = drvA.link(w5.work_ref)
        assert link5.attributed_yield == 4.8, link5.attributed_yield
        return f"yield={link5.attributed_yield}"

    @check("S4b scale accepted_portion 0.6→0.3 → yield halves to 2.4")
    def _():
        c = gate.check_partial_yield_scaling(
            "partial-scale", w5.work_ref, ad5.admission_id, 0.3)
        return c.detail

    @check("S4c spend perturbation does NOT move partial yield")
    def _():
        c = gate.check_quantity_perturbation(
            "partial-spend-perturb", w5.work_ref, e5.expenditure_id,
            "cpu_seconds", 3.0)
        assert "yield_unchanged=True" in c.detail, c.detail
        return c.detail

    # ------------------------------------------------------------------
    # S5 — reused capabilities: second use never re-bills acquisition
    # ------------------------------------------------------------------
    w6 = drvA.work("inq-A4", kind="acquisition")
    e6 = drvA.spend("inq-A4", w6.work_ref, b"acquire-cap", iterations=4000)
    r6 = drvA.result(w6.work_ref, "success", detail="capability acquired")
    ad6 = drvA.admit(r6.result_ref, "accepted", yield_value=20.0)
    chain.record_acquisition("cap-1", w6.work_ref, [e6.expenditure_id])

    w7 = drvA.work("inq-A5", kind="reuse", acquired_capability_id="cap-1")
    e7 = drvA.spend("inq-A5", w7.work_ref, b"reuse-cap", iterations=1000)
    r7 = drvA.result(w7.work_ref, "success", detail="capability reused")
    ad7 = drvA.admit(r7.result_ref, "accepted", yield_value=3.0)

    @check("S5a reuse bills marginal spend only (R4 rule)")
    def _():
        link7 = drvA.link(w7.work_ref)
        link6 = drvA.link(w6.work_ref)
        assert link7.expenditure_ids == [e7.expenditure_id], link7.expenditure_ids
        assert e6.expenditure_id not in link7.expenditure_ids
        assert link7.attributed_yield == 3.0
        assert link6.expenditure_ids == [e6.expenditure_id]
        assert link6.attributed_yield == 20.0
        return (f"reuse yield={link7.attributed_yield} spend={link7.total_cpu_seconds}s "
                f"(acquisition spend {round(e6.cpu_seconds,6)}s NOT re-billed); "
                f"acquisition yield={link6.attributed_yield}")

    @check("S5b remove acquisition spend → acquirer collapses, reuser intact")
    def _():
        snap = Counterfactual(alloc_db, exp_db, chain_db, EXEC)
        try:
            snap.remove_expenditure(e6.expenditure_id)
            after6 = snap.link(w6.work_ref)
            after7 = snap.link(w7.work_ref)
            assert after6.attributed_yield == 0.0, after6.attributed_yield
            assert after7.attributed_yield == 3.0, after7.attributed_yield
            assert after7.expenditure_ids == [e7.expenditure_id]
            return (f"acquirer yield 20.0 → {after6.attributed_yield}; "
                    f"reuser yield intact at {after7.attributed_yield}")
        finally:
            snap.close()

    # ------------------------------------------------------------------
    # S6 — nested microcontrollers: aggregate exactly once
    # ------------------------------------------------------------------
    w8 = drvA.work("inq-A6")                       # parent
    e8 = drvA.spend("inq-A6", w8.work_ref, b"parent", iterations=1500)
    w9 = drvA.work("inq-A6", parent_work_ref=w8.work_ref)   # child
    e9 = drvA.spend("inq-A6", w9.work_ref, b"child", iterations=2500)
    r8 = drvA.result(w8.work_ref, "success", detail="parent done")
    ad8 = drvA.admit(r8.result_ref, "accepted", yield_value=12.0)

    @check("S6a nested: parent aggregate = own + child, exactly once")
    def _():
        agg = drvA.aggregate(w8.work_ref)
        leaf8 = drvA.link(w8.work_ref)
        leaf9 = drvA.link(w9.work_ref)
        assert sorted(agg.expenditure_ids) == sorted(
            [e8.expenditure_id, e9.expenditure_id]), agg.expenditure_ids
        expect = round(leaf8.total_cpu_seconds + leaf9.total_cpu_seconds, 6)
        assert agg.total_cpu_seconds == expect, (agg.total_cpu_seconds, expect)
        # exactly-once: aggregate equals the sum of the leaves, no double count
        assert abs(agg.total_cpu_seconds
                   - round(e8.cpu_seconds + e9.cpu_seconds, 6)) < 2e-6
        return (f"aggregate spend={agg.total_cpu_seconds}s = parent "
                f"{round(e8.cpu_seconds,6)}s + child {round(e9.cpu_seconds,6)}s")

    @check("S6b remove child spend → parent drops by EXACTLY that amount")
    def _():
        c = gate.check_nested_aggregation_delta(
            "nested-delta", w8.work_ref, e9.expenditure_id,
            round(e9.cpu_seconds, 6))
        return c.detail

    # ------------------------------------------------------------------
    # S7 — multi-model/provider: provenance carried, no conflation
    # ------------------------------------------------------------------
    w10 = drvC.work("inq-C1")
    e10 = drvC.spend("inq-C1", w10.work_ref, b"beta-work", iterations=2000)
    r10 = drvC.result(w10.work_ref, "success")
    ad10 = drvC.admit(r10.result_ref, "accepted", yield_value=6.0)

    @check("S7a attribution carries model/provider in provenance")
    def _():
        link10 = drvC.link(w10.work_ref)
        assert link10.model_provider == "provider-beta", link10.model_provider
        assert link1.model_provider == "provider-alpha"
        assert link10.total_monetary_cost > 0
        return (f"C: provider={link10.model_provider} cost={link10.total_monetary_cost:.6f}; "
                f"A: provider={link1.model_provider} cost={link1.total_monetary_cost:.6f}")

    @check("S7b perturb beta's monetary cost → only beta's attribution moves")
    def _():
        snap = Counterfactual(alloc_db, exp_db, chain_db, EXEC)
        try:
            before10 = snap.link(w10.work_ref)
            before1 = snap.link(w1.work_ref)
            snap.alter_expenditure(e10.expenditure_id,
                                   monetary_cost=e10.monetary_cost * 5.0)
            after10 = snap.link(w10.work_ref)
            after1 = snap.link(w1.work_ref)
            # expectation from the full-precision recorded value, not
            # the rounded link total (rounding-boundary discipline)
            expected10 = round(e10.monetary_cost * 5.0, 6)
            assert abs(after10.total_monetary_cost - expected10) < 1e-9, (
                after10.total_monetary_cost, expected10)
            assert asdict_eq(before1, after1), "alpha moved on beta's perturbation"
            return (f"beta cost {before10.total_monetary_cost:.6f} → "
                    f"{after10.total_monetary_cost:.6f}; alpha unchanged")
        finally:
            snap.close()

    # ------------------------------------------------------------------
    # S8 — adversarial: re-attribution + verdict flip
    # ------------------------------------------------------------------
    @check("S8a adversarial: spend re-pointed at another work moves with it")
    def _():
        snap = Counterfactual(alloc_db, exp_db, chain_db, EXEC)
        try:
            snap.alter_expenditure(e1.expenditure_id, work_ref=w2.work_ref)
            after1 = snap.link(w1.work_ref)
            after2 = snap.link(w2.work_ref)
            assert after1.attributed_yield == 0.0  # w1's chain now broken
            assert e1.expenditure_id in after2.expenditure_ids
            return (f"attribution follows the ledger rows: w1 yield → "
                    f"{after1.attributed_yield}, w2 ids now include e1")
        finally:
            snap.close()

    @check("S8b adversarial: verdict flipped accepted→rejected → yield 0")
    def _():
        snap = Counterfactual(alloc_db, exp_db, chain_db, EXEC)
        try:
            snap.alter_admission(ad1.admission_id, verdict="rejected",
                                 yield_value=0.0)
            after = snap.link(w1.work_ref)
            assert after.attributed_yield == 0.0
            assert after.acceptance_record_id is None
            assert after.total_cpu_seconds == round(e1.cpu_seconds, 6)
            return ("yield is admission-driven: flip the verdict and the "
                    "yield goes to 0 while spend is untouched")
        finally:
            snap.close()

    @check("S8c R1: unaccepted work accrues cost, zero yield")
    def _():
        w11 = drvB.work("inq-B2")
        e11 = drvB.spend("inq-B2", w11.work_ref, b"unaccepted", iterations=1000)
        r11 = drvB.result(w11.work_ref, "success")
        link11 = drvB.link(w11.work_ref)   # no admission at all
        assert link11.attributed_yield == 0.0
        assert link11.acceptance_record_id is None
        assert link11.total_cpu_seconds == round(e11.cpu_seconds, 6)
        assert link11.total_cpu_seconds > 0
        return (f"no admission → yield={link11.attributed_yield}, "
                f"spend={link11.total_cpu_seconds}s still metered")

    # ------------------------------------------------------------------
    # summary
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    for name, ok, _ in RESULTS:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"  {passed}/{total} checks passed")
    print("=" * 70)
    alloc_ledger.close(); exp_ledger.close(); chain.close()
    return 0 if passed == total else 1


def asdict_eq(a, b) -> bool:
    from dataclasses import asdict
    da, db = asdict(a), asdict(b)
    da.pop("computed_at", None); db.pop("computed_at", None)
    return da == db


if __name__ == "__main__":
    sys.exit(main())
