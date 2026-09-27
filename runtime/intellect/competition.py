"""Behavioral epistemic loop: candidate programs compete as epistemic
hypotheses, discriminating experiments execute their actual plans, real
Evidence feeds the existing EvidenceArbiter, and only the
evidence-supported program is admitted.

This is the bridge the architecture was missing: synthesis produces
candidate programs (cognition Hypothesis) and records suppressed rivals
in the trace's ambiguity records, but nothing ever turned those into
epistemic Hypothesis objects, ran them against each other, or let
evidence decide. This module does that, generically:

- Competitors: the synthesis winner (if any) plus the suppressed rival
  programs rebuilt from the ambiguity records' Expr canonicals. No
  caller supplies which one should win.
- Discriminators: generated automatically from predictions -- probe
  inputs on which the competitors' Composer-executed plans disagree.
- World: a caller-supplied `world_fn` plays the environment. It is the
  data-generating process under test, not a winner selector: evidence
  comes from comparing each program's actual executed prediction
  against the world's observed output on the same probe input.
- Evidence: one real Evidence object per (hypothesis, probe), with
  supports = (prediction == observed), execution provenance, and the
  observed output in content. Decided by the existing EvidenceArbiter.
- Admission: only a SUPPORTED hypothesis's plan goes through the real
  AdmissionController path, with a smoke test built from a
  discriminating probe and the world's observed output. Anything else
  (refuted, under test, indistinguishable) fails closed: nothing is
  admitted.

Negative controls fall out of the arbiter's own thresholds: identical
programs yield no discriminator (no evidence -> PROPOSED -> no
admission); a world that contradicts every program refutes them all
(-> no admission).
"""
from __future__ import annotations

import itertools
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarm_engine.cognition.representations import Expr
from swarm_engine.intellect.epistemic import (
    Evidence,
    Experiment,
    Hypothesis,
    HypothesisState,
)
from swarm_engine.synthesis.admission import SmokeTest


def _close(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)),
                                                     abs(float(b)))
    except Exception:
        return a == b


def _probe_key(d: Dict[str, Any]) -> str:
    try:
        return repr(sorted((k, repr(v)) for k, v in d.items()))
    except Exception:
        return repr(sorted(d.keys()))


@dataclass
class Competitor:
    """One candidate program in the competition."""
    label: str
    plan: Dict[str, Any]
    expr_canonical: Optional[str]
    origin: str  # "synthesis_winner" or "suppressed_rival"


def _candidate_probes(examples: Sequence[Tuple[Dict[str, Any], Any]],
                      param_names: Sequence[str],
                      record_probes: List[Dict[str, Any]],
                      cap: int = 60) -> List[Dict[str, Any]]:
    """Bounded generic probe inputs: the ambiguity records'
    distinguishing probes plus midpoints between consecutive training
    values per numeric parameter and small offsets around them.
    Training rows themselves are excluded (every competitor fits
    them by construction)."""
    probes: List[Dict[str, Any]] = []
    seen = set()

    def add(d: Dict[str, Any]) -> None:
        k = _probe_key(d)
        if k not in seen:
            seen.add(k)
            probes.append(d)

    train_keys = {_probe_key(a) for a, _ in examples}
    for d in record_probes:
        if _probe_key(d) not in train_keys:
            add(dict(d))
    args_list = [a for a, _ in examples]
    if not args_list:
        return probes
    base = args_list[0]
    bool_params = [p for p in param_names
                   if all(isinstance(a.get(p), bool) for a in args_list)]
    for p in bool_params:
        for frac in (0.25, 0.5, 0.75):
            d = dict(base)
            d[p] = frac
            if _probe_key(d) not in train_keys:
                add(d)
    num_params = [p for p in param_names
                  if all(isinstance(a.get(p), (int, float))
                         and not isinstance(a.get(p), bool)
                         for a in args_list)]
    for p in num_params:
        vals = sorted({float(a[p]) for a in args_list})
        for x, y in zip(vals, vals[1:]):
            if y > x:
                for frac in (0.25, 0.5, 0.75):
                    d = dict(base)
                    d[p] = x + (y - x) * frac
                    if _probe_key(d) not in train_keys:
                        add(d)
        for v in vals:
            for delta in (-1.0, -0.5, 0.5, 1.0):
                d = dict(base)
                d[p] = v + delta
                if _probe_key(d) not in train_keys:
                    add(d)
        if len(probes) >= cap:
            break
    return probes[:cap]


def _find_discriminators(reference: Competitor,
                          others: List[Competitor],
                          probes: List[Dict[str, Any]],
                          predict: Callable[[Competitor, Dict[str, Any]],
                                             Tuple[bool, Any, Dict[str, Any]]],
                          max_per_pair: int = 3) -> List[Dict[str, Any]]:
    """Probe inputs on which each other competitor's executed prediction
    differs from the reference's. Generated automatically from
    predictions alone -- no caller input about who should win. Two
    behaviorally identical programs yield no discriminator."""
    ref_preds: Dict[str, Tuple[bool, Any]] = {}
    for probe in probes:
        ok, val, _ = predict(reference, probe)
        ref_preds[_probe_key(probe)] = (ok, val)
    out: List[Dict[str, Any]] = []
    for comp in others:
        found = 0
        for probe in probes:
            ok, val, _ = predict(comp, probe)
            rok, rval = ref_preds[_probe_key(probe)]
            if ok and rok and not _close(val, rval):
                out.append(dict(probe))
                found += 1
                if found >= max_per_pair:
                    break
    uniq: List[Dict[str, Any]] = []
    seen = set()
    for d in out:
        k = _probe_key(d)
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def run_competition(engine, goal: str,
                    examples: Sequence[Tuple[Dict[str, Any], Any]],
                    param_names: Sequence[str],
                    world_fn: Callable[..., Any],
                    question_id: Optional[str] = None,
                    max_rivals: int = 4,
                    max_discriminators_per_pair: int = 3,
                    ) -> Dict[str, Any]:
    """Run the full behavioral epistemic loop. Returns a report dict
    with question/hypothesis/experiment IDs, per-hypothesis verdicts,
    and the admission outcome."""
    t_start = time.time()
    question_id = question_id or "q_%s_%s" % (goal, uuid.uuid4().hex[:8])
    store = engine.intellect.epistemic
    arbiter = engine.intellect.arbiter
    report: Dict[str, Any] = {"question_id": question_id, "goal": goal}

    # ---- 1. synthesize: winner + suppressed rivals ----
    result = engine.cognition.propose_multi(goal, list(examples),
                                            tuple(param_names))
    report["solved"] = bool(result.solved)
    report["n_ambiguous_records"] = len(result.ambiguous)

    competitors: List[Competitor] = []
    if result.solved and result.plan:
        competitors.append(Competitor(
            label="winner", plan=result.plan, expr_canonical=None,
            origin="synthesis_winner"))
    seen_canon = set()
    record_probes: List[Dict[str, Any]] = []
    synthesizer = engine.cognition.reasoning.synthesizer
    for rec in result.ambiguous:
        canon = rec.get("conditional") or rec.get("candidate")
        probe = rec.get("distinguishing_probe")
        if probe:
            record_probes.append(dict(probe))
        if not canon or canon in seen_canon:
            continue
        seen_canon.add(canon)
        if len([c for c in competitors if c.origin == "suppressed_rival"]) \
                >= max_rivals:
            continue
        try:
            expr = Expr.from_dict(json.loads(canon))
            plan = synthesizer._build_general_plan(expr, tuple(param_names))
        except Exception:
            continue
        competitors.append(Competitor(
            label="rival_%d" % len(competitors), plan=plan,
            expr_canonical=canon, origin="suppressed_rival"))
    report["n_competitors"] = len(competitors)
    if not competitors:
        report["outcome"] = "no_competitors"
        return report

    # ---- 2. epistemic hypotheses ----
    hyps: List[Hypothesis] = []
    for i, comp in enumerate(competitors):
        hid = "hyp_%s_%d" % (question_id, i)
        hyp = Hypothesis(
            hypothesis_id=hid, question_id=question_id,
            statement="candidate program %s (%s) predicts the goal"
                      % (comp.label, comp.origin),
            specification={"label": comp.label, "origin": comp.origin,
                           "plan": comp.plan,
                           "expr_canonical": comp.expr_canonical},
            provenance={"goal": goal, "n_examples": len(examples),
                        "synthesis_solved": bool(result.solved)},
            state=HypothesisState.PROPOSED)
        store.save_hypothesis(hyp)
        hyps.append(hyp)
    for h in hyps:
        h.competing_with = [o.hypothesis_id for o in hyps
                            if o.hypothesis_id != h.hypothesis_id]
        store.save_hypothesis(h)

    # ---- 3. predictions on the probe pool ----
    probes = _candidate_probes(examples, param_names, record_probes)
    composer = engine.composer
    keep = []
    for comp in competitors:
        analysis = composer.analyze(comp.plan)
        keep.append(analysis.ok)
        if not analysis.ok:
            report.setdefault("dropped_invalid_plans", []).append(
                {"label": comp.label, "errors": analysis.errors})
    competitors = [c for c, k in zip(competitors, keep) if k]
    hyps = [h for h, k in zip(hyps, keep) if k]
    for h in hyps:  # refresh rival lists after any drop
        h.competing_with = [o.hypothesis_id for o in hyps
                            if o.hypothesis_id != h.hypothesis_id]
        store.save_hypothesis(h)
    if not competitors:
        report["outcome"] = "no_valid_competitors"
        return report

    def predict(comp: Competitor,
                probe: Dict[str, Any]) -> Tuple[bool, Any, Dict[str, Any]]:
        rr = composer.execute_sync(comp.plan, dict(probe), skip_check=True)
        prov = {"elapsed_ms": rr.get("elapsed_ms"),
                "primitive_calls": rr.get("primitive_calls"),
                "capability_id": rr.get("capability_id")}
        if rr.get("success"):
            return True, rr.get("value"), prov
        return False, None, dict(prov, error=str(rr.get("error")))

    # ---- 4. discriminators from predictions ----
    # Reference: the synthesis winner, else the first rival. For each
    # other competitor, keep up to N probes where its executed
    # prediction differs from the reference's. With no rivals at all,
    # fall back to confirmation probes (fresh inputs) for the winner.
    reference = competitors[0]
    discriminators: List[Dict[str, Any]] = []
    if len(competitors) > 1:
        discriminators = _find_discriminators(
            reference, competitors[1:], probes, predict,
            max_per_pair=max_discriminators_per_pair)
    else:
        discriminators = probes[:6]
    # Dedupe, keep bounded and deterministic.
    uniq: List[Dict[str, Any]] = []
    seen = set()
    for d in discriminators:
        k = _probe_key(d)
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    discriminators = uniq[:12]
    report["n_discriminators"] = len(discriminators)
    if not discriminators:
        report["outcome"] = "indistinguishable_on_probe_budget"
        report["admitted_capability_id"] = None
        report["verdicts"] = {
            h.hypothesis_id: {"state": h.state.value, "confidence": 0.5,
                              "reasoning": "no discriminating probe found; "
                                           "no evidence gathered"}
            for h in hyps}
        return report

    # ---- 5. experiments: execute plans, observe the world ----
    experiments: List[Experiment] = []
    for k, probe in enumerate(discriminators):
        try:
            observed = world_fn(**dict(probe))
        except Exception as e:
            continue  # world undefined here; not a valid experiment
        exp_id = "exp_%s_%d" % (question_id, k)
        preds: Dict[str, Any] = {}
        for comp, hyp in zip(competitors, hyps):
            ok, val, prov = predict(comp, probe)
            preds[hyp.hypothesis_id] = {
                "label": comp.label, "executed_ok": ok,
                "prediction": val, "provenance": prov}
        exp = Experiment(
            experiment_id=exp_id, question_id=question_id,
            hypothesis_ids=[h.hypothesis_id for h in hyps],
            design={"probe": dict(probe),
                    "discriminator": True,
                    "predictions": preds},
            executed=True,
            result={"observed": observed,
                    "world": getattr(world_fn, "__name__", "world_fn")})
        store.save_experiment(exp)
        experiments.append(exp)

        # ---- 6. real Evidence per (hypothesis, experiment) ----
        for comp, hyp in zip(competitors, hyps):
            p = preds[hyp.hypothesis_id]
            supports = bool(p["executed_ok"]) and _close(p["prediction"],
                                                          observed)
            ev = Evidence(
                evidence_id="ev_%s_%d" % (exp_id, len(experiments) * 100
                                          + hyps.index(hyp)),
                target_id=hyp.hypothesis_id, supports=supports,
                content={"experiment_id": exp_id, "probe": dict(probe),
                         "prediction": p["prediction"],
                         "executed_ok": p["executed_ok"],
                         "observed": observed,
                         "provenance": p["provenance"]},
                source="behavioral_experiment")
            store.save_evidence(ev)
            (hyp.supporting_evidence if supports
             else hyp.contradicting_evidence).append(ev.evidence_id)
    report["n_experiments"] = len(experiments)

    # ---- 7. arbitration by the existing EvidenceArbiter ----
    verdicts = []
    verdict_report = {}
    for hyp in hyps:
        evs = store.evidence_for(hyp.hypothesis_id)
        v = arbiter.decide(hyp.hypothesis_id, evs)
        verdicts.append(v)
        hyp.state = v.new_state
        hyp.confidence = v.new_confidence
        hyp.provenance = dict(hyp.provenance,
                              arbiter_reasoning=v.reasoning,
                              n_evidence=len(evs))
        store.save_hypothesis(hyp)
        verdict_report[hyp.hypothesis_id] = {
            "label": hyp.specification.get("label"),
            "state": v.new_state.value, "confidence": v.new_confidence,
            "reasoning": v.reasoning, "n_evidence": len(evs)}
    report["verdicts"] = verdict_report

    # ---- 8. admit ONLY the evidence-supported program ----
    admitted = None
    admit_detail: Dict[str, Any] = {}
    pick = arbiter.compare(verdicts)
    pick_v = next((v for v in verdicts if v.hypothesis_id == pick), None)
    if pick_v is not None and pick_v.new_state == HypothesisState.SUPPORTED:
        hyp = next(h for h in hyps if h.hypothesis_id == pick)
        comp = competitors[hyps.index(hyp)]
        if comp.origin == "synthesis_winner":
            # Smoke test from a discriminating probe + the world's own
            # observed output: the admission path independently
            # re-checks the behavior the evidence established.
            first_exp = experiments[0]
            smoke = SmokeTest(
                args=dict(first_exp.design["probe"]),
                expect=first_exp.result["observed"],
                name="epistemic_discrimination")
            ar = engine.admit_as_engine(goal, comp.plan, smoke=smoke,
                                        name="epistemic_%s" % hyp.hypothesis_id)
            admitted = ar.capability_id if ar.ok else None
            admit_detail = {"verdict": ar.verdict, "ok": ar.ok,
                            "errors": getattr(ar, "errors", None)}
        else:
            admit_detail = {
                "skipped": True,
                "reason": "evidence favors a suppressed rival; the "
                          "synthesis gate's poisoning stands -- fail "
                          "closed, nothing admitted"}
    else:
        admit_detail = {"skipped": True,
                        "reason": "no SUPPORTED hypothesis; fail closed"}
    report["admitted_capability_id"] = admitted
    report["admission"] = admit_detail
    report["outcome"] = ("admitted" if admitted
                         else "no_admission_fail_closed")
    # Phase 5: seal every artifact and link the tamper-evident lineage
    # goal -> question -> hypotheses -> experiments -> evidence, and
    # winner hypothesis -> capability. Integrity failures here are real
    # failures: the chain is the audit trail.
    if admitted:
        from swarm_engine.synthesis.integrity import seal_epistemic_chain
        report["sealed"] = seal_epistemic_chain(engine, report)
    report["elapsed_s"] = round(time.time() - t_start, 2)
    return report
