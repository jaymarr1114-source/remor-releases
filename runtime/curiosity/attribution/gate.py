"""CUR-P1D — the dedicated causal-necessity gate.

The implementation plan (Phase 1, chunk 1d) requires: the gate's test
is causal-necessity, not ledger hygiene. Changing or removing the
claimed causal expenditure must change the attribution appropriately
— a ledger with matching IDs is necessary but not sufficient.
Otherwise the FRM receives a misleading economic signal.

How the gate works:
    1. Snapshot — copy the three ledger DB files into an isolated
       temp dir. Every counterfactual runs against a fresh snapshot;
       the mission ledgers are never mutated.
    2. Counterfactual — remove or alter expenditure/admission rows in
       the snapshot (the ONLY production-adjacent code paths that
       mutate ledger rows, and they are explicitly labeled
       counterfactual-only).
    3. Recompute — recompute_attribution / aggregate_attribution run
       from scratch against the snapshot. They derive every quantity
       from the rows present NOW; they have no memory of prior
       results.
    4. Assert — the attribution moved APPROPRIATELY:
         * remove the causal expenditure → the link collapses:
           expenditure_ids empty, totals zero, attributed_yield 0.
         * alter a quantity (cpu_seconds ×k) → the totals move by
           the expected delta while IDs are UNCHANGED. An
           implementation that merely echoed ledger IDs could not
           produce this: the check is quantity-causal, not
           ID-hygienic.
         * alter/remove an unrelated expenditure → the link is
           byte-identical (control: no cross-contamination).
         * remove the admission → yield collapses (yield is
           admission-driven, FRM §6).
         * alter accepted_portion → yield scales proportionally
           (R3).

    5. Restore — each check takes a fresh snapshot, so "restore" is
       structural: the next check sees the unmutated ledgers. The
       headline counterfactual test explicitly demonstrates
       collapse-then-return.

Causal semantics of collapse: attribution is the link
expenditure → yield. A work unit with no recorded expenditure has a
broken causal chain: there is nothing to attribute yield to, so
attributed_yield is 0 even if an admission record still references
its result. The admission record itself is untouched (it is the
authority's fact); the LINK is void.

If any check passes only by ledger-ID matching, the gate FAILS —
the quantity-perturbation checks are designed so ID-echo cannot
pass them.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from .chain import (ChainLedger, AttributionLink, aggregate_attribution,
                    recompute_attribution)
from .grants import AllocationLedger
from .spend import ExpenditureLedger


class GateFailure(Exception):
    """A causal-necessity check failed. The gate is red."""


class GateCheck:
    """One executed check: name, expectation, observed before/after,
    verdict. The proof battery collects these as evidence."""

    def __init__(self, name: str):
        self.name = name
        self.before: Optional[Dict[str, Any]] = None
        self.after: Optional[Dict[str, Any]] = None
        self.expectation: str = ""
        self.passed: bool = False
        self.detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "expectation": self.expectation,
                "before": self.before, "after": self.after,
                "passed": self.passed, "detail": self.detail}


class Counterfactual:
    """An isolated snapshot of the three ledgers plus counterfactual
    mutation operations. Each check gets a fresh Counterfactual."""

    def __init__(self, alloc_db: str, exp_db: str, chain_db: str,
                 originating_executive: str):
        self._tmp = tempfile.mkdtemp(prefix="cur_p1d_gate_")
        self._paths = {}
        for label, src in (("alloc", alloc_db), ("exp", exp_db),
                           ("chain", chain_db)):
            dst = os.path.join(self._tmp, f"{label}.db")
            shutil.copy2(src, dst)
            self._paths[label] = dst
        self.alloc = AllocationLedger(self._paths["alloc"])
        self.exp = ExpenditureLedger(self._paths["exp"])
        self.chain = ChainLedger(self._paths["chain"])
        self.originating_executive = originating_executive

    # -- observation --------------------------------------------------
    def link(self, work_ref: str) -> AttributionLink:
        return recompute_attribution(work_ref, self.chain, self.exp,
                                     self.originating_executive)

    def aggregate(self, work_ref: str) -> AttributionLink:
        return aggregate_attribution(work_ref, self.chain, self.exp,
                                     self.originating_executive)

    # -- counterfactual mutations (snapshot only) ----------------------
    def remove_expenditure(self, expenditure_id: str) -> None:
        self.exp.delete(expenditure_id)

    def alter_expenditure(self, expenditure_id: str, **fields: Any) -> None:
        allowed = {"cpu_seconds", "work_units", "bytes_processed",
                   "monetary_cost", "cost_kind", "model_provider",
                   "inquiry_id", "work_ref"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"counterfactual may not alter {bad}")
        conn = sqlite3.connect(self._paths["exp"])
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE expenditures SET {sets} WHERE expenditure_id=?",
                     (*fields.values(), expenditure_id))
        conn.commit()
        conn.close()

    def remove_admission(self, admission_id: str) -> None:
        conn = sqlite3.connect(self._paths["chain"])
        conn.execute("DELETE FROM admissions WHERE admission_id=?",
                     (admission_id,))
        conn.commit()
        conn.close()

    def alter_admission(self, admission_id: str, **fields: Any) -> None:
        allowed = {"verdict", "yield_value", "accepted_portion"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"counterfactual may not alter {bad}")
        conn = sqlite3.connect(self._paths["chain"])
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE admissions SET {sets} WHERE admission_id=?",
                     (*fields.values(), admission_id))
        conn.commit()
        conn.close()

    def close(self) -> None:
        for ledger in (self.alloc, self.exp, self.chain):
            ledger.close()
        shutil.rmtree(self._tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

class CausalGate:
    """Executes the causal-necessity battery. Each method runs one
    check against a fresh snapshot and returns a GateCheck. Any
    failure raises GateFailure — the gate is fail-closed."""

    def __init__(self, alloc_db: str, exp_db: str, chain_db: str,
                 originating_executive: str):
        self._dbs = (alloc_db, exp_db, chain_db)
        self._exec = originating_executive
        self.checks: List[GateCheck] = []

    def _snap(self) -> Counterfactual:
        return Counterfactual(*self._dbs, self._exec)

    def _record(self, check: GateCheck) -> GateCheck:
        self.checks.append(check)
        if not check.passed:
            raise GateFailure(f"gate check FAILED: {check.name}: {check.detail}")
        return check

    # -- primitives ------------------------------------------------------
    def check_collapse_on_expenditure_removal(
            self, name: str, work_ref: str,
            expenditure_ids: List[str]) -> GateCheck:
        """Remove the claimed causal expenditures → the link collapses:
        no expenditure_ids, zero totals, zero attributed yield."""
        check = GateCheck(name)
        snap = self._snap()
        try:
            before = snap.link(work_ref)
            check.before = asdict(before)
            check.expectation = (
                "removing all causal expenditures voids the link: "
                "expenditure_ids=[], totals 0, attributed_yield 0")
            for eid in expenditure_ids:
                snap.remove_expenditure(eid)
            after = snap.link(work_ref)
            check.after = asdict(after)
            ok = (after.expenditure_ids == []
                  and after.total_cpu_seconds == 0.0
                  and after.total_monetary_cost == 0.0
                  and after.attributed_yield == 0.0)
            check.passed = ok
            check.detail = (
                f"before yield={before.attributed_yield} "
                f"spend={before.total_cpu_seconds}s; after "
                f"yield={after.attributed_yield} spend={after.total_cpu_seconds}s")
        finally:
            snap.close()
        return self._record(check)

    def check_quantity_perturbation(self, name: str, work_ref: str,
                                    expenditure_id: str,
                                    field: str, factor: float) -> GateCheck:
        """Scale one expenditure quantity by `factor` → the matching
        total moves by the expected delta, the ID set is UNCHANGED,
        and attributed_yield does NOT move (yield is admission-driven,
        not spend-driven). ID-echo cannot pass this check."""
        check = GateCheck(name)
        snap = self._snap()
        try:
            before = snap.link(work_ref)
            check.before = asdict(before)
            # Full-precision stored values from the snapshot: the
            # expectation must be computed from what the recompute
            # actually sums (stored doubles), not from the rounded
            # link totals — otherwise rounding boundaries produce
            # false failures.
            stored = {e.expenditure_id: getattr(e, field)
                      for e in snap.exp.for_work(work_ref)}
            old_val = stored[expenditure_id]
            true_sum = sum(stored.values())
            snap.alter_expenditure(expenditure_id, **{field: old_val * factor})
            after = snap.link(work_ref)
            check.after = asdict(after)
            total_field = {"cpu_seconds": "total_cpu_seconds",
                           "monetary_cost": "total_monetary_cost"}[field]
            expected = round(true_sum - old_val + old_val * factor, 6)
            totals_ok = abs(getattr(after, total_field) - expected) < 1e-9
            ids_ok = after.expenditure_ids == before.expenditure_ids
            yield_ok = after.attributed_yield == before.attributed_yield
            check.expectation = (
                f"{total_field} moves by {old_val}*{factor - 1} "
                f"(IDs unchanged, yield unchanged)")
            check.passed = totals_ok and ids_ok and yield_ok
            check.detail = (
                f"{total_field}: {getattr(before, total_field)} → "
                f"{getattr(after, total_field)} (expected {expected}); "
                f"ids_unchanged={ids_ok} yield_unchanged={yield_ok}")
        finally:
            snap.close()
        return self._record(check)

    def check_isolation(self, name: str, untouched_work_ref: str,
                        mutate) -> GateCheck:
        """Apply a counterfactual mutation; an unrelated work unit's
        link must be byte-identical afterwards (control)."""
        check = GateCheck(name)
        snap = self._snap()
        try:
            before = asdict(snap.link(untouched_work_ref))
            check.before = before
            mutate(snap)
            after = asdict(snap.link(untouched_work_ref))
            check.after = after
            check.expectation = "unrelated link byte-identical after mutation"
            # computed_at is observation metadata (when the link was
            # recomputed), not attribution content — excluded.
            before.pop("computed_at", None)
            after.pop("computed_at", None)
            check.passed = before == after
            check.detail = f"identical={before == after}"
        finally:
            snap.close()
        return self._record(check)

    def check_yield_collapse_on_admission_removal(
            self, name: str, work_ref: str, admission_id: str) -> GateCheck:
        """Remove the admission → yield collapses to 0 while the cost
        side is untouched (R1: yield is admission-driven)."""
        check = GateCheck(name)
        snap = self._snap()
        try:
            before = snap.link(work_ref)
            check.before = asdict(before)
            check.expectation = (
                "admission removed → attributed_yield 0, spend totals unchanged")
            snap.remove_admission(admission_id)
            after = snap.link(work_ref)
            check.after = asdict(after)
            check.passed = (
                after.attributed_yield == 0.0
                and after.acceptance_record_id is None
                and after.total_cpu_seconds == before.total_cpu_seconds
                and after.expenditure_ids == before.expenditure_ids)
            check.detail = (
                f"yield {before.attributed_yield} → {after.attributed_yield}; "
                f"spend {before.total_cpu_seconds} → {after.total_cpu_seconds}")
        finally:
            snap.close()
        return self._record(check)

    def check_partial_yield_scaling(self, name: str, work_ref: str,
                                    admission_id: str,
                                    new_portion: float) -> GateCheck:
        """Alter accepted_portion → yield scales proportionally (R3).
        Quantity-causal: the recompute multiplies; ID-echo could not
        produce the scaled value."""
        check = GateCheck(name)
        snap = self._snap()
        try:
            before = snap.link(work_ref)
            check.before = asdict(before)
            # recover the admitted full yield from the snapshot's
            # admission row BEFORE altering it
            conn = sqlite3.connect(snap._paths["chain"])
            row = conn.execute(
                "SELECT yield_value, accepted_portion FROM admissions "
                "WHERE admission_id=?", (admission_id,)).fetchone()
            conn.close()
            old_yield, old_portion = row[0], row[1]
            snap.alter_admission(admission_id, accepted_portion=new_portion)
            after = snap.link(work_ref)
            check.after = asdict(after)
            expected = round(old_yield * new_portion, 6)
            check.expectation = (
                f"yield = yield_value × accepted_portion = {old_yield} × "
                f"{new_portion} = {expected} (was {old_yield} × "
                f"{old_portion})")
            check.passed = abs(after.attributed_yield - expected) < 1e-6
            check.detail = (
                f"yield {before.attributed_yield} → {after.attributed_yield} "
                f"(expected {expected})")
        finally:
            snap.close()
        return self._record(check)

    def check_nested_aggregation_delta(
            self, name: str, parent_ref: str, child_expenditure_id: str,
            child_cpu: float) -> GateCheck:
        """R5: remove one child's expenditure → the parent aggregate
        drops by EXACTLY that expenditure's amount, and the child's ID
        leaves the aggregate set. Exactly-once aggregation."""
        check = GateCheck(name)
        snap = self._snap()
        try:
            before = snap.aggregate(parent_ref)
            check.before = asdict(before)
            check.expectation = (
                f"parent aggregate total_cpu_seconds drops by exactly "
                f"{child_cpu}; child expenditure leaves the ID set")
            snap.remove_expenditure(child_expenditure_id)
            after = snap.aggregate(parent_ref)
            check.after = asdict(after)
            expected = round(before.total_cpu_seconds - child_cpu, 6)
            totals_ok = abs(after.total_cpu_seconds - expected) < 2e-6
            ids_ok = child_expenditure_id not in after.expenditure_ids
            check.passed = totals_ok and ids_ok
            check.detail = (
                f"aggregate {before.total_cpu_seconds} → "
                f"{after.total_cpu_seconds} (expected {expected}); "
                f"child id removed={ids_ok}")
        finally:
            snap.close()
        return self._record(check)
