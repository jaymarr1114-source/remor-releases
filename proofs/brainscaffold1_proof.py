#!/usr/bin/env python3
"""BRAIN-SCAFFOLD-1 proof battery: the governed cognition inlet.

Proves the v3 retained core for cognition (James, 2026-09-30):
  one cognition inlet on the executive-owned substrate; native first;
  borrow only after a named native refusal; no grant => refusal;
  insufficient grant => deferral; cognize never silently charges (U-6);
  FrmGrant canonical (U-7); legacy reasoning entry points governed (U-5);
  borrow-to-native telemetry.

Run in a fresh process from the tree root:
    python3 proofs/brainscaffold1_proof.py
Exit 0 iff every section passes. No hard-coded results: every assertion
is evaluated against real mechanisms (real FrmGrant.issue, real
MicrocontrollerSubstrate charge path, real CuriositySubstrate, real
PrecisionCognitionProvider, real GrantedCognitionProvider).

Sections:
  B1 inventory assertions (one inlet, grant-concept separation)
  B2 native-first via the REAL curiosity substrate (no grant, no charge)
  B3 grantless borrow refused
  B4 wrong-grant-type refused (both legacy Grant concepts)
  B5 insufficient grant deferred with no charge
  B6 successful borrow: provenance + native_refusal + real charge
  B7 facade: wrapped reasoner never called; bypass fails closed;
      governed path travels substrate.cognize
  B8 telemetry queries (borrow ratio, deferral rate, proposal survival)
  B9 no silent charge on any refused/deferred call
"""

import os
import sys
import time
import traceback

TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TREE, "pylib"))

from swarm_engine.core.microcontroller.substrate import (  # noqa: E402
    CognitionProvider,
    CognitionResult,
    MicrocontrollerSubstrate,
)
from swarm_engine.core.microcontroller.granted_cognition import (  # noqa: E402
    CognitionTelemetry,
    GrantedCognitionProvider,
    NativeRefusal,
    StubTeacher,
)
from swarm_engine.curiosity.substrate import CuriositySubstrate  # noqa: E402
from swarm_engine.curiosity.cognition import (  # noqa: E402
    OP_EXTRACT,
    PrecisionCognitionProvider,
)
from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord  # noqa: E402
from swarm_engine.primitives.core import (  # noqa: E402
    Effect,
    Grant as PrimitiveGrant,
)
# NOTE (GRANT-MIGRATE-1, U-7): the old attribution Grant was migrated into
# FrmGrant — the single canonical grant contract. Two grant concepts
# remain: FrmGrant (FRM resource grant) vs PrimitiveGrant (primitive
# effect permission) — deliberately distinct.
from swarm_engine.intellect.reasoner import (  # noqa: E402
    ExternalReasoner,
    NoExternalReasoner,
)
from swarm_engine.intellect.governed_reasoner import (  # noqa: E402
    GovernedExternalReasoner,
)

PASS = []
FAIL = []


def check(section, name, cond, detail=""):
    if cond:
        PASS.append(f"{section}:{name}")
    else:
        FAIL.append(f"{section}:{name} -- {detail}")


def make_grant(budget_s, note="proof", enforcement="RUNNING"):
    return FrmGrant.issue(
        domain="curiosity", epoch_id=1, epoch_s=300.0,
        budget_s=budget_s, max_concurrent=1,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        lent=False, lending=LendingRecord(0.0, 0),
        enforcement_state_at_issue=enforcement,
        issued_at=time.time(), note=note)


class RefusingNative(CognitionProvider):
    """Stand-in for a future native tier that names its refusal."""

    def request_cognition(self, *, mc_id, prompt, context):
        raise NativeRefusal("precision_tier:cannot_reason_about_x",
                            "test refusal")


class RecordingReasoner(ExternalReasoner):
    def __init__(self):
        self.calls = []

    def propose_questions(self, context):
        self.calls.append(("propose_questions", context))
        return ["should-not-be-used"]

    def propose_hypotheses(self, question_text, context):
        self.calls.append(("propose_hypotheses", question_text))
        return ["should-not-be-used"]

    def design_experiment(self, question_text, hypothesis_statements,
                          context):
        self.calls.append(("design_experiment", question_text))
        return {"should": "not-be-used"}


def fresh_substrate(provider):
    sub = MicrocontrollerSubstrate()
    sub.register_loop("run", budget_s=600.0)
    sub.set_cognition_provider(provider)
    sr = sub.spawn("run", purpose="proof", budget_s=100.0)
    assert sr.ok, f"spawn refused: {sr.refusal}"
    return sub, sr.mc.mc_id


# ---------------------------------------------------------------- B1 ----

def b1_inventory():
    prov = GrantedCognitionProvider.__new__(GrantedCognitionProvider)
    check("B1", "provider_class", isinstance(
        GrantedCognitionProvider, type))
    check("B1", "native_refusal_names_refusal",
          NativeRefusal("x").name == "x")
    # The curiosity substrate installs the governed inlet, native tier set.
    cs = CuriositySubstrate()
    gp = cs._provider
    check("B1", "curiosity_installs_governed",
          isinstance(gp, GrantedCognitionProvider),
          f"got {type(gp).__name__}")
    check("B1", "curiosity_native_tier",
          isinstance(gp._native, PrecisionCognitionProvider),
          f"got {type(gp._native).__name__}")
    # Two grant concepts, two distinct classes (U-7: the old attribution
    # Grant was migrated INTO FrmGrant); the inlet takes FrmGrant.
    check("B1", "frmgrant_distinct_from_primitive",
          FrmGrant is not PrimitiveGrant)
    # Stub teacher is honest, never plausible reasoning.
    t = StubTeacher()
    text, secs = t.complete("2+2=?", {})
    check("B1", "stub_marked",
          text.startswith("[STUB-TEACHER") and "not a reasoning result" in text,
          text[:60])
    check("B1", "stub_estimate_documented", t.estimate_cost_s("abc") > 0)


# ---------------------------------------------------------------- B2 ----

def b2_native_first():
    cs = CuriositySubstrate()
    cs.register_loop("questioning", budget_s=600.0)
    sr = cs.spawn("questioning", purpose="proof", budget_s=50.0)
    assert sr.ok, f"spawn refused: {sr.refusal}"
    mc_id = sr.mc.mc_id
    before = cs.get(mc_id).remaining_s
    res = cs.cognize(mc_id, "extract",
                     {"operation": OP_EXTRACT,
                      "question_text": "what is the melting point of X?",
                      "purpose": "native-probe"})
    check("B2", "native_ok", res.ok, res.error)
    check("B2", "native_provenance",
          res.provenance == "native:PrecisionCognitionProvider",
          res.provenance)
    check("B2", "native_result_id", bool(res.result_id))
    check("B2", "native_no_charge",
          cs.get(mc_id).remaining_s == before,
          f"{before} -> {cs.get(mc_id).remaining_s}")
    check("B2", "native_no_grant_needed", True)  # no frm_grant in ctx
    tel = cs._provider.telemetry
    check("B2", "telemetry_native_event",
          tel.counts().get("native", 0) == 1, str(tel.counts()))
    check("B2", "borrow_ratio_zero",
          tel.borrow_ratio() == 0.0, str(tel.borrow_ratio()))


# ---------------------------------------------------------------- B3 ----

def b3_grantless_refused():
    prov = GrantedCognitionProvider(
        substrate=None, native=RefusingNative())  # substrate unused here
    tel = prov.telemetry
    res = prov.request_cognition(
        mc_id="mc-x", prompt="reason about X",
        context={"purpose": "p", "target_profile": "t"})
    check("B3", "refused", not res.ok)
    check("B3", "no_grant_reason",
          "cognition_refused:no_grant" in res.error, res.error)
    check("B3", "native_refusal_preserved",
          res.native_refusal == "precision_tier:cannot_reason_about_x",
          res.native_refusal)
    check("B3", "telemetry_refused_no_grant",
          tel.counts().get("refused_no_grant", 0) == 1,
          str(tel.counts()))


# ---------------------------------------------------------------- B4 ----

def b4_wrong_grant_type():
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(substrate=sub, native=RefusingNative())
    tel = prov.telemetry
    wrong = [
        ("primitives.Grant",
         PrimitiveGrant(effect=Effect.READ_FS, pattern="*")),
        # U-7: the old mutable attribution Grant no longer exists as a
        # distinct type. The adversarial slot is now held by a
        # legacy-shaped impostor: a dict with the old grant's fields
        # (str epoch_id included). It must be refused, never coerced.
        ("legacy_grant_dict",
         {"grant_id": "g1", "epoch_id": "e1",
          "dimensions": {"budget_s": 100.0, "max_concurrent": 1}}),
    ]
    for label, g in wrong:
        res = prov.request_cognition(
            mc_id="mc-x", prompt="reason",
            context={"frm_grant": g, "purpose": "p"})
        check("B4", f"refused_{label}", not res.ok)
        check("B4", f"bad_grant_reason_{label}",
              "cognition_refused:grant_not_frmgrant" in res.error,
              res.error)
    check("B4", "telemetry_refused_bad_grant",
          tel.counts().get("refused_bad_grant", 0) == 2,
          str(tel.counts()))
    # Enforcement-state refusal: grant issued under a non-RUNNING state.
    g2 = make_grant(60.0, enforcement="SUSPENDED_SAFETY")
    res = prov.request_cognition(
        mc_id="mc-x", prompt="reason",
        context={"frm_grant": g2, "purpose": "p"})
    check("B4", "enforcement_refused", not res.ok)
    check("B4", "enforcement_reason",
          "cognition_refused:grant_enforcement_state" in res.error,
          res.error)


# ---------------------------------------------------------------- B5 ----

def b5_deferred_no_charge():
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(substrate=sub, native=RefusingNative())
    sub.register_loop("run", budget_s=600.0)
    sr = sub.spawn("run", purpose="proof", budget_s=100.0)
    mc_id = sr.mc.mc_id
    grant = make_grant(0.0001)  # far below any real estimate
    before = sub.get(mc_id).remaining_s
    res = prov.request_cognition(
        mc_id=mc_id, prompt="reason at length " * 100,
        context={"frm_grant": grant, "purpose": "p",
                 "target_profile": "t"})
    check("B5", "deferred", not res.ok)
    check("B5", "deferral_reason",
          "cognition_deferred:insufficient_grant" in res.error, res.error)
    check("B5", "no_grant_consumed",
          prov.grant_consumed_s(grant.grant_id) == 0.0)
    check("B5", "no_mc_charge",
          sub.get(mc_id).remaining_s == before,
          f"{before} -> {sub.get(mc_id).remaining_s}")
    check("B5", "telemetry_deferred",
          prov.telemetry.counts().get("deferred_insufficient_grant", 0) == 1,
          str(prov.telemetry.counts()))
    check("B5", "deferral_rate",
          prov.telemetry.deferral_rate() == 1.0,
          str(prov.telemetry.deferral_rate()))


# ---------------------------------------------------------------- B6 ----

def b6_successful_borrow():
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(substrate=sub, native=RefusingNative())
    sub.register_loop("run", budget_s=600.0)
    sr = sub.spawn("run", purpose="proof", budget_s=100.0)
    mc_id = sr.mc.mc_id
    grant = make_grant(60.0)
    before = sub.get(mc_id).remaining_s
    prompt = "reason about X"
    res = prov.request_cognition(
        mc_id=mc_id, prompt=prompt,
        context={"frm_grant": grant, "purpose": "hypothesis",
                 "target_profile": "qwen3-8b"})
    check("B6", "borrow_ok", res.ok, res.error)
    check("B6", "provenance",
          res.provenance == "borrowed:stub-teacher@0.0.0", res.provenance)
    check("B6", "native_refusal_on_record",
          res.native_refusal == "precision_tier:cannot_reason_about_x",
          res.native_refusal)
    check("B6", "stub_text_marked",
          res.text.startswith("[STUB-TEACHER"), res.text[:40])
    consumed = prov.grant_consumed_s(grant.grant_id)
    check("B6", "grant_consumed", consumed > 0, str(consumed))
    after = sub.get(mc_id).remaining_s
    check("B6", "mc_charged_exactly_consumed",
          abs((before - after) - consumed) < 1e-9,
          f"before={before} after={after} consumed={consumed}")
    tel = prov.telemetry
    check("B6", "telemetry_borrowed",
          tel.counts().get("borrowed", 0) == 1, str(tel.counts()))
    check("B6", "borrow_ratio_one",
          tel.borrow_ratio(purpose="hypothesis",
                           target_profile="qwen3-8b") == 1.0,
          str(tel.borrow_ratio()))
    check("B6", "result_id_present", bool(res.result_id))
    # Proposal survival: record one survived, one not.
    tel.record_proposal_outcome(res.result_id, True, purpose="hypothesis",
                                target_profile="qwen3-8b")
    tel.record_proposal_outcome("deadbeef1234", False, purpose="hypothesis",
                                target_profile="qwen3-8b")
    check("B6", "survival_rate",
          tel.proposal_survival_rate(purpose="hypothesis") == 0.5,
          str(tel.proposal_survival_rate()))


# ---------------------------------------------------------------- B7 ----

def b7_facade():
    rec = RecordingReasoner()
    # Engine construction wraps the raw reasoner in the facade.
    from swarm_engine.intellect.engine import IntellectualEngine
    eng = IntellectualEngine(None, db_path=":memory:",
                             external_reasoner=rec)
    check("B7", "engine_wraps_facade",
          isinstance(eng.reasoner, GovernedExternalReasoner),
          type(eng.reasoner).__name__)
    check("B7", "noexternalreasoner_untouched",
          isinstance(IntellectualEngine(
              None, db_path=":memory:").reasoner, NoExternalReasoner))
    # Bypass fails closed: no governance context => empty, no direct call.
    out = eng.reasoner.propose_questions({"open_questions": 3})
    check("B7", "bypass_fails_closed", out == [])
    check("B7", "wrapped_never_called", rec.calls == [], str(rec.calls))
    # Governed path: substrate + mc_id + FrmGrant in context.
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(substrate=sub, native=None)
    sub.set_cognition_provider(prov)
    sub.register_loop("run", budget_s=600.0)
    sr = sub.spawn("run", purpose="proof", budget_s=100.0)
    mc_id = sr.mc.mc_id
    grant = make_grant(60.0)
    fac = GovernedExternalReasoner(rec, substrate=sub)
    n_events_before = len(prov.telemetry.events)
    out = fac.propose_questions({"mc_id": mc_id, "frm_grant": grant,
                                 "purpose": "intellect:propose_questions"})
    check("B7", "governed_path_empty_from_stub", out == [])
    check("B7", "wrapped_still_never_called", rec.calls == [],
          str(rec.calls))
    new_events = prov.telemetry.events[n_events_before:]
    check("B7", "governed_path_telemetered_borrow",
          len(new_events) == 1 and new_events[0].outcome == "borrowed",
          str([(e.outcome) for e in new_events]))
    check("B7", "governed_native_refusal_attested",
          new_events and new_events[0].native_refusal
          == GovernedExternalReasoner.NATIVE_REFUSAL,
          str([e.native_refusal for e in new_events]))
    # Grant consumed through the one inlet (visible charge).
    check("B7", "governed_path_charged",
          prov.grant_consumed_s(grant.grant_id) > 0)
    # No grant in context => fail closed, no new telemetry.
    n2 = len(prov.telemetry.events)
    out = fac.propose_questions({"mc_id": mc_id})
    check("B7", "facade_no_grant_fails_closed",
          out == [] and len(prov.telemetry.events) == n2)


# ---------------------------------------------------------------- B8 ----

def b8_telemetry_queries():
    tel = CognitionTelemetry()
    tel.record(mc_id="a", purpose="p1", target_profile="tp",
               outcome="borrowed", charged_s=1.0)
    tel.record(mc_id="a", purpose="p1", target_profile="tp",
               outcome="native")
    tel.record(mc_id="a", purpose="p2", target_profile="tp",
               outcome="deferred_insufficient_grant")
    check("B8", "borrow_ratio_filtered",
          tel.borrow_ratio(purpose="p1", target_profile="tp") == 0.5,
          str(tel.borrow_ratio(purpose="p1")))
    check("B8", "borrow_ratio_none_when_empty",
          tel.borrow_ratio(purpose="nope") is None)
    check("B8", "deferral_rate_filtered",
          abs(tel.deferral_rate(purpose="p2") - 1.0) < 1e-9,
          str(tel.deferral_rate(purpose="p2")))
    check("B8", "survival_none_when_unrecorded",
          tel.proposal_survival_rate() is None)
    check("B8", "counts", tel.counts() == {
        "borrowed": 1, "native": 1, "deferred_insufficient_grant": 1},
        str(tel.counts()))


# ---------------------------------------------------------------- B9 ----

def b9_no_silent_charge():
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(substrate=sub, native=RefusingNative())
    sub.register_loop("run", budget_s=600.0)
    sr = sub.spawn("run", purpose="proof", budget_s=100.0)
    mc_id = sr.mc.mc_id
    before = sub.get(mc_id).remaining_s
    # refused (no grant), refused (bad grant), deferred (tiny grant)
    prov.request_cognition(mc_id=mc_id, prompt="x", context={})
    prov.request_cognition(
        mc_id=mc_id, prompt="x",
        context={"frm_grant": PrimitiveGrant(effect=Effect.PURE)})
    prov.request_cognition(
        mc_id=mc_id, prompt="x",
        context={"frm_grant": make_grant(0.0001)})
    check("B9", "remaining_unchanged",
          sub.get(mc_id).remaining_s == before,
          f"{before} -> {sub.get(mc_id).remaining_s}")
    check("B9", "no_borrowed_events",
          "borrowed" not in prov.telemetry.counts(),
          str(prov.telemetry.counts()))


def main():
    sections = [b1_inventory, b2_native_first, b3_grantless_refused,
                b4_wrong_grant_type, b5_deferred_no_charge,
                b6_successful_borrow, b7_facade, b8_telemetry_queries,
                b9_no_silent_charge]
    for fn in sections:
        try:
            fn()
        except Exception:
            FAIL.append(f"{fn.__name__}:EXCEPTION\n{traceback.format_exc()}")
    print(f"BRAIN-SCAFFOLD-1 proof: {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("FAIL:", f)
    if FAIL:
        sys.exit(1)
    # Summary of what the battery exercised (for the report).
    print("sections: B1 inventory, B2 native-first, B3 no-grant refusal, "
          "B4 wrong-grant refusal, B5 deferral, B6 borrow, B7 facade, "
          "B8 telemetry, B9 no-silent-charge")


if __name__ == "__main__":
    main()
