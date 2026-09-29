"""CUR-P1D — Yield-attribution metering + dedicated causal gate (Phase 1, mission 4 of 5).

This package implements the yield-attribution machinery that is the FRM
amendment's open implementation boundary (§18) and the implementation
plan's Phase-1 chunk 1d — explicitly "the plan's biggest technical risk".

What it is:
    The causal accounting chain from the FRM amendment §7:

        resource allocation → actual expenditure → specific work →
        produced result → Primary evaluation → acceptance/admission →
        accepted capability yield

    Plus the dedicated causal-necessity gate from the implementation
    plan: altering or removing the claimed causal expenditure must change
    the attribution appropriately — a ledger with matching IDs is
    necessary but not sufficient, otherwise the FRM receives a
    misleading economic signal.

What it is NOT (Phase 2+):
    Not the Curiosity Executive, not the Run Controller, not any loop.
    Those sides are driven here by test doubles that really spend: real
    substrate calls with real measured expenditure, real budgets, real
    budget-exceeded refusals.

    The acceptance/admission side is driven by a test double
    (TestAdmissionBoard) that stands in for Primary Acceptance, which
    arrives in Phase 4+. It is labeled as such everywhere; it does not
    pretend to be the real authority.

Frozen interfaces honored (CUR-P1A block, do not renegotiate):
    * FRM grant: {grant_id, epoch_id, epoch_s (default 300),
      dimensions: {budget_s, max_concurrent}, primary_minimum:
      {budget_s, max_concurrent}, lent: bool}. Zero allocation while any
      enforcement state is active. Lending epoch-bounded; grants
      non-preemptive.
    * Enforcement states: RUNNING | HARD_SHUTDOWN_RESOURCE | WARNING_1 |
      SUSPENDED_SAFETY | BANNED_6M. This package only reads the frozen
      state vocabulary through the EnforcementStatusProvider inlet;
      enforcement machinery itself is CUR-P1B's.
    * Attribution link: {grant_id, expenditure_ids: [], work_ref,
      result_ref, acceptance_record_id: null|uuid, attributed_yield:
      float}.

Charter grounding:
    * C-4.5 — shared-substrate spend is metered at the call site by the
      substrate interface and attributed to the calling controller's
      budget. The substrate enforces nothing and holds no budget; it
      reports spend. (spend.py)
    * A31/A32 — the substrate interface meters (reports); the calling
      controller's budget pays. (grants.py + spend.py)
    * FRM amendment §6 — yield = accepted capability yield.
      Truth/generation/execution alone are not yield. Unaccepted work
      accrues cost but zero yield. (chain.py)
    * FRM amendment §7 — attribution preserves: resource expenditure;
      monetary expenditure where available; originating executive;
      originating Run Controller; originating inquiry/work unit;
      model/provider; acceptance/admission record; causal relationship
      between expenditure and accepted result. (chain.py)
    * FRM amendment §8 — monetary cost provenance is distinguished:
      measured | declared | estimated | unpriced. (spend.py)
    * FRM amendment §18 — this machinery is the open implementation
      boundary; attribution must survive concurrent inquiries, multiple
      models/providers, shared infrastructure, retries, failed attempts,
      partial results, reused capabilities, nested microcontrollers,
      and prove causal NECESSITY. (gate.py + proof battery)

Modules:
    grants.py      — Grant record (frozen shape) + allocation-site
                     metering (grant → controller) with the zero-
                     allocation-under-enforcement rule.
    spend.py       — Expenditure-site metering per C-4.5: the metered
                     substrate interface, SpendReport, Expenditure
                     records with monetary-cost provenance.
    chain.py       — The full chain: work units, results, evaluation,
                     admission link, AttributionLink, and the yield
                     rules (incl. reuse non-rebilling, partial yield).
    gate.py        — The dedicated causal-necessity gate: counterfactual
                     execution (remove/alter expenditure → recompute
                     from a ledger snapshot → assert the attribution
                     moves appropriately).
    testdoubles.py — Test doubles that really spend: RealSpendSubstrate
                     (real CPU work, real wall-clock measurement),
                     CallerBudget (a real paying budget per A32),
                     TestAdmissionBoard (stands in for Primary
                     Acceptance, honestly labeled).

The proof battery lives at proofs/cur_p1d_attribution_proof_2026-09-29.py
(the mission's one new proof file) and executes every causal check for
real against this package.
"""
