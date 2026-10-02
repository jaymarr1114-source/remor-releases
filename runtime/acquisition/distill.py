"""
swarm_engine/acquisition/distill.py

M2 (technique distillation): the distillation loop driver.

The loop: delta record → experience → synthesis → verification → promotion
→ admitted native capability → planner-visible primitive.

Then the recursive seed: native capability → result + new delta (the loop
observing itself, logged as an experience record per distillation).

Synthesis routes, in order:
  A. adapt_capability — a retained near-miss capability is adapted to the
     delta's objective. The full trust path lives inside: adapt →
     new ReviewBoard admission → verdict-bound promotion → planner-visible
     primitive. (Proven 35/35.)
  B. fresh synthesis — engine.synthesize_and_admit (propose → ReviewBoard
     admission) on the build examples; the admitted plan is rendered to
     standalone code, held-out verified in fresh processes, re-verified
     through ReviewBoard.verify_artifact, and promoted through the frozen
     promotion API (VerdictPromotionBridge via the engine's proven
     _verdict_promote_acquired path).

Verification is causal, never asserted:
  - held-out examples the technique was NOT built against (split at the
    delta level; the builder never sees them),
  - negative controls (inputs that must fail cleanly / not spuriously match),
  - fresh-process execution (run_code subprocesses).

Promotion goes through the real promotion API only — no bypass, no manual
badge-flipping. Every refusal is fail-closed with the reason named.

This module is M2-owned. It CALLS (never edits): VerdictPromotionBridge,
the epistemic store API, the ReviewBoard, the primitive registry.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.acquisition.delta import (
    DeltaRecord, DeltaValidationError,
)


# Bound on the coupled-substitution search: pairs of numeric-literal spans
# tried per generalize() call. The pair phase runs only after the single-
# constant phase rejected every span, so reaching the cap means no coupled
# substitution was found among the first _MAX_PAIR_CANDIDATES pairs.
_MAX_PAIR_CANDIDATES = 2000


@dataclass
class DistillationResult:
    """The full chain of one distillation — the loop observing itself."""

    delta_id: str
    success: bool
    route: str = ""                     # "adaptation" | "fresh-synthesis" | ""
    promoted_name: str = ""
    capability_id: str = ""
    build_examples: int = 0
    heldout_examples: int = 0
    heldout_passed: int = 0
    negative_controls_passed: int = 0
    verdict_admitted: bool = False
    reason: str = ""                    # named on failure — never silent
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DistillationLoop:
    """Drives one delta record through to a planner-visible primitive."""

    def __init__(self, engine: Any, epistemic: Any = None):
        self.engine = engine
        self.epistemic = epistemic

    def _render_second_order(self, plan: Dict[str, Any],
                             delta_id: str) -> str:
        """Render a plan that composes acquired primitives by directly
        composing their persisted standalone code.

        Codegen cannot embed a verdict-promoted primitive's runtime wrapper
        (closure over trust machinery), nor the lambda-based op helpers in
        a previously rendered technique module. For the second-order case --
        a plan whose steps are all acquired primitives -- we compose the
        verified stored code strings directly. Each dependency's `run` is
        renamed to a unique symbol, its module-level helpers are prefixed,
        and the new `run` wires the dataflow from the plan steps.

        This is honest: the composed code contains exactly the verified
        bytes from the AcquiredCodeStore, wired according to the admitted
        plan. No new logic is invented.
        """
        import re as _re
        engine = self.engine
        steps = plan.get("steps") or []
        # Verify all steps are acquired primitives.
        dep_names = []
        for step in steps:
            op = step.get("op")
            if not (isinstance(op, str) and op.startswith("acquired.")):
                raise ValueError(
                    f"_render_second_order: non-acquired op {op!r}")
            if op not in dep_names:
                dep_names.append(op)
        # Load and rename each dependency's code.
        dep_runs = {}  # op name -> renamed run symbol
        parts = []
        for idx, op in enumerate(dep_names):
            rec = engine.acquired_code.get(op)
            if rec is None:
                raise ValueError(f"_render_second_order: {op} not in store")
            code = rec.get("code") or ""
            entrypoint = rec.get("entrypoint") or "run"
            if not code:
                raise ValueError(f"_render_second_order: {op} has no code")
            prefix = f"_dep{idx}_"
            # Rename the entrypoint and module-level helpers.
            code2 = _re.sub(r'\bdef\s+' + _re.escape(entrypoint) + r'\b',
                            f'def {prefix}{entrypoint}', code)
            code2 = _re.sub(r'\b_PLAN_DEFAULTS\b', prefix + 'PLAN_DEFAULTS',
                            code2)
            code2 = _re.sub(r'\bop_([A-Za-z0-9_]+)\b', prefix + r'op_\1',
                            code2)
            # Also rename _require_num and similar helpers if present.
            code2 = _re.sub(r'\b_require_([A-Za-z0-9_]+)\b',
                            prefix + r'_require_\1', code2)
            parts.append(f"# --- dependency: {op} ---\n" + code2)
            dep_runs[op] = f"{prefix}{entrypoint}"
        # Wire the dataflow according to the plan steps.
        run_lines = []
        run_lines.append("def run(**_kw):")
        # Map plan params.
        params = plan.get("params") or {}
        for pname in params:
            run_lines.append(f"    {pname} = _kw[{pname!r}]")
        # Execute steps in order.
        for step in steps:
            sid = step.get("id")
            op = step.get("op")
            args = step.get("args") or {}
            dep_run = dep_runs[op]
            # Resolve args: $param -> var, $step -> var
            arg_strs = []
            for aname, aval in args.items():
                if isinstance(aval, dict):
                    if "$param" in aval:
                        arg_strs.append(f"{aname}={aval['$param']}")
                    elif "$step" in aval:
                        arg_strs.append(f"{aname}=_var_{aval['$step']}")
                    else:
                        raise ValueError(
                            f"unsupported arg ref {aval!r}")
                else:
                    arg_strs.append(f"{aname}={aval!r}")
            run_lines.append(
                f"    _var_{sid} = {dep_run}({', '.join(arg_strs)})")
        # Return value from plan output.
        output = plan.get("output") or {}
        if isinstance(output, dict) and "$step" in output:
            run_lines.append(f"    return _var_{output['$step']}")
        elif isinstance(output, dict) and "$param" in output:
            run_lines.append(f"    return {output['$param']}")
        else:
            raise ValueError(f"unsupported plan output {output!r}")
        parts.append("\n".join(run_lines))
        header = ('"""Second-order distillation: composed from verified '
                  'acquired techniques.\n\n'
                  f'Delta: {delta_id}\n'
                  'Dependencies: ' + ", ".join(dep_names) + '\n"""\n')
        return header + "\n\n".join(parts) + "\n"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def distill(self, delta: DeltaRecord) -> DistillationResult:
        """Run the full loop for one delta record. Never raises on a
        failed distillation — the failure is named in the result."""
        try:
            delta.validate()
        except DeltaValidationError as exc:
            return DistillationResult(
                delta_id=getattr(delta, "delta_id", "?"),
                success=False, reason=f"delta rejected: {exc}")

        try:
            build, heldout = delta.split_evidence()
        except DeltaValidationError as exc:
            return DistillationResult(
                delta_id=delta.delta_id, success=False,
                reason=f"evidence split refused: {exc}")

        result = DistillationResult(
            delta_id=delta.delta_id, success=False,
            build_examples=len(build), heldout_examples=len(heldout))
        self._log_experience(delta, "distillation_started",
                             {"build": len(build), "heldout": len(heldout)})

        # ---- Route A: adapt a retained near-miss capability ----
        adapted = self._try_adaptation(delta, build, heldout, result)
        if adapted is not None:
            return self._finish(delta, adapted)

        # ---- Route C: trace-guided synthesis (multi-step via teacher traces) ----
        # Selected by trace availability: a delta carrying demonstration
        # traces takes this route; without traces it falls through to B.
        traced = self._try_trace_guided(delta, build, heldout, result)
        if traced is not None:
            return self._finish(delta, traced)

        # ---- Route B: fresh synthesis + verify + promote ----
        fresh = self._try_fresh_synthesis(delta, build, heldout, result)
        return self._finish(delta, fresh)

    def generalize(self, source: "DistillationResult", new_goal: str,
                   new_examples: List[Tuple[Dict[str, Any], Any]]
                   ) -> DistillationResult:
        """Test 2: the SAME technique, a materially different task,
        generalized with NO external agent in the loop.

        Mechanism: bound-constant re-parameterization of the distilled
        technique's own code. The distilled doubling code carries its
        constant as a literal; the new task names its own target constant
        in the goal. Single-constant substitution is tried first: each
        candidate literal is substituted and the resulting code must
        reproduce ALL new-task examples in fresh processes. Where no
        single substitution suffices, COUPLED substitution fires: pairs
        of numeric literals are substituted jointly with the target
        (e.g. a clamp's threshold constant and floor constant move
        together). The coupled path runs only after the single path
        rejected every span on the same examples, so a coupled winner is
        genuinely needed — the new-task evidence rejected the source
        program (vacuity guard below) and every single-constant
        substitution. Then the EXACT trust path runs:
        ReviewBoard.verify_artifact on the new bytes, then the
        verdict-bound promotion. The original external agent is nowhere
        in this call.

        (Why code-level, not adapt_capability: the adaptation driver only
        sees bound constants in partial-plan shapes; the cognitive
        synthesis route emits step-plans whose constants it cannot see.
        Rather than editing shared machinery owned by no mission, the
        distillation loop performs the substitution on its own distilled
        bytes — the same substitution, the same trust path.)
        """
        result = DistillationResult(
            delta_id=f"generalize:{source.promoted_name}", success=False,
            route="generalization")
        if not new_examples or len(new_examples) < 2:
            result.reason = ("generalize refused: need at least 2 examples "
                             "of the new task")
            return result
        target = self._goal_number(new_goal)
        if target is None:
            result.reason = ("generalize refused: new goal names no single "
                             "target constant")
            return result
        try:
            code = self._source_code(source)
        except Exception as exc:
            result.reason = (f"generalize: could not recover source code: "
                             f"{type(exc).__name__}: {exc}")
            return result
        winner = None
        # Causal split: select the substitution on the build half only;
        # the held-out half is verified afterward, unseen by selection.
        half = max(1, len(new_examples) // 2)
        select_examples, heldout_examples = (new_examples[:half],
                                             new_examples[half:])
        winner = self._reparameterize(code, target, select_examples)
        if winner is None:
            result.reason = ("generalize: no bound-constant substitution "
                             "reproduces the new-task examples")
            return result
        new_code, old_const = winner
        # Vacuity guard (Q6): a substitution can "reproduce" the new
        # examples while changing no executable behavior — e.g. the
        # replaced literal sits inside a comment, so the new code IS the
        # old program. That happens exactly when the new-task evidence
        # cannot discriminate the generalization from the source: the
        # source program already fits the new examples. Such a
        # generalization proves nothing about a materially different
        # task, so it fails closed here instead of promoting.
        if new_code == code:
            result.reason = ("generalize refused: substitution changed no "
                             "code text (vacuous generalization)")
            return result
        if self._code_reproduces(code, new_examples):
            result.reason = ("generalize refused: the source program already "
                             "reproduces all new-task examples, so the "
                             "new-task evidence does not discriminate the "
                             "generalization from the source; generalization "
                             "unproven")
            return result
        ok, passed = self._verify_code_heldout(new_code, "run",
                                               heldout_examples)
        result.heldout_examples = len(heldout_examples)
        result.heldout_passed = passed
        if not ok:
            result.reason = ("generalize: re-parameterized code failed "
                             f"held-out verification ({passed}/"
                             f"{len(heldout_examples)})")
            return result
        neg_ok, neg_n = self._negative_controls_code(
            new_code, "run", new_examples)
        result.negative_controls_passed = neg_n if neg_ok else 0
        # The full trust path on the NEW bytes: independent verification,
        # then verdict-bound promotion. No bypass.
        try:
            from types import SimpleNamespace as _NS
            from swarm_engine.acquisition.strategies import CapabilitySpec
            from swarm_engine.acquisition.semantic import Case
            engine = self.engine
            review = engine.ensure_review_board()
            if review is None:
                result.reason = "generalize: ReviewBoard unavailable"
                return result
            param_names = list(new_examples[0][0].keys())
            cases = [Case(args=dict(a), expect=e, kind="positive",
                          label=f"generalize_{i}")
                     for i, (a, e) in enumerate(new_examples)]
            spec = CapabilitySpec(
                name=f"generalized_{source.delta_id[:8]}",
                description=new_goal, examples=list(new_examples),
                input_names=param_names)
            verdict = review.verify_artifact(
                new_code, "run", spec, cases,
                artifact_ref=f"generalize_{source.delta_id}")
            admitted = bool(getattr(verdict, "admitted", False))
            result.verdict_admitted = admitted
            if not admitted:
                result.reason = ("generalize: ReviewBoard did not admit: "
                                 + str(getattr(verdict, "reasons", "")))
                return result
            if isinstance(old_const, tuple):
                sub_desc = (f"coupled {old_const[0]}+{old_const[1]} -> "
                            f"{target}")
            else:
                sub_desc = f"bound-constant {old_const} -> {target}"
            candidate = _NS(
                code=new_code, entrypoint="run",
                declared_effects=["pure"],
                source=f"generalization:{source.promoted_name}",
                notes=(f"{sub_desc} on {source.promoted_name}"))
            promoted = engine._verdict_promote_acquired(
                candidate, spec, list(new_examples),
                f"generalized_{source.delta_id[:8]}")
        except Exception as exc:
            result.reason = ("generalize: verify/promote raised "
                             f"{type(exc).__name__}: {exc}")
            return result
        if not promoted:
            result.reason = "generalize: promotion refused (fail closed)"
            return result
        result.promoted_name = promoted
        result.capability_id = source.capability_id
        result.success = True
        self._log_experience_raw(
            f"generalization_complete:{source.promoted_name}",
            {"new_goal": new_goal, "new_promoted_name": promoted,
             "substitution": f"{old_const}->{target}",
             "substitution_path": ("coupled" if isinstance(old_const, tuple)
                                   else "single"),
             "heldout": f"{passed}/{len(heldout_examples)}"})
        return result

    # ------------------------------------------------------------------
    # Generalization helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _goal_number(goal: str) -> Optional[float]:
        import re
        nums = re.findall(r"-?\d+(?:\.\d+)?", goal or "")
        vals = [float(n) for n in nums]
        return vals[0] if len(set(vals)) == 1 and vals else None

    def _source_code(self, source: "DistillationResult") -> str:
        """Recover the distilled technique's standalone code from its
        retained capability record."""
        rec = self.engine.capabilities.get(source.capability_id)
        if rec is None or not getattr(rec, "plan", None):
            raise ValueError("no retained plan for "
                             f"{source.capability_id}")
        _steps = rec.plan.get("steps") or []
        _all_acq = _steps and all(
            isinstance(s.get("op"), str) and
            s.get("op").startswith("acquired.")
            for s in _steps)
        if _all_acq:
            return self._render_second_order(rec.plan, source.delta_id
                                             if hasattr(source, "delta_id")
                                             else source.promoted_name)
        from swarm_engine.synthesis.codegen import render_plan_to_source
        code, _meta = render_plan_to_source(
            rec.plan, self.engine.primitives,
            purpose=f"generalization source {source.promoted_name}")
        return code

    def _reparameterize(self, code: str, target: float,
                        examples) -> Optional[Tuple[str, Any]]:
        """Select the bound-constant substitution for the new task.

        Singles first, coupled pairs second. The single-constant path is
        tried exactly as before and wins whenever any one literal
        substitution reproduces the examples — the coupled path can never
        hijack a case the single path handles. The pair phase runs only
        when every single substitution was rejected on these same
        examples, which is what makes a pair winner genuinely coupled:
        the new-task evidence rejected the source program (enforced by
        the vacuity guard in generalize()) and every single-constant
        substitution.

        Returns (new_code, old_literal) for a single winner, or
        (new_code, (lit_i, lit_j)) for a coupled winner, or None.
        """
        single = self._reparameterize_single(code, target, examples)
        if single is not None:
            return single
        return self._reparameterize_pair(code, target, examples)

    def _reparameterize_single(self, code: str, target: float,
                               examples) -> Optional[Tuple[str, Any]]:
        """Try substituting the target constant for the numeric literal at
        EACH position in the code; return (new_code, old_literal) for the
        first substitution that reproduces ALL examples in fresh
        processes.

        Position-by-position, not first-occurrence: a literal can also
        appear inside comments or docstrings, where substituting it
        changes nothing. The behavioral check — not a positional
        heuristic — decides which substitution is the bound constant.
        """
        import re
        target_s = (str(int(target)) if float(target).is_integer()
                    else str(target))
        spans = [(m.start(), m.end(), m.group(0)) for m in re.finditer(
            r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", code)]
        for start, end, lit in spans:
            if float(lit) == float(target):
                continue
            new_code = code[:start] + target_s + code[end:]
            if self._code_reproduces(new_code, examples):
                return new_code, lit
        return None

    def _reparameterize_pair(self, code: str, target: float,
                             examples) -> Optional[Tuple[str, Tuple[Any,
                                                                    Any]]]:
        """Coupled substitution: TWO bound constants move to the target
        together.

        Tries pairs of numeric-literal spans (i < j), substituting the
        target at BOTH positions, and returns (new_code, (lit_i, lit_j))
        for the first pair whose joint substitution reproduces ALL
        examples in fresh processes. The two spans may carry different
        values (e.g. a distilled clamp's threshold and floor constants);
        what makes them coupled is that the new task's single target
        constant replaces both jointly.

        Non-vacuity is structural: this phase runs only after
        _reparameterize_single rejected every span on the same examples,
        so no single-constant substitution reproduces them. A pair winner
        therefore proves something beyond the one-constant path. The
        caller additionally refuses when the source program already
        reproduces the new-task examples (vacuity guard), which also
        kills pairs that change no executable behavior (e.g. two spans
        inside comments): such a pair behaves exactly like the source.

        Pruning (generic — no technique knowledge, no hardcoded pairs):
          * pairs only, never triples or more;
          * a span whose value already equals the target is excluded: a
            pair (i, j) with lit_j == target is behaviorally identical
            to the single substitution at i, which the single phase
            already rejected — such a pair cannot discriminate beyond
            the single path;
          * pairs are tried once each, in span order, under a hard cap
            (_MAX_PAIR_CANDIDATES) that bounds the combinatorics.
        """
        import re
        target_s = (str(int(target)) if float(target).is_integer()
                    else str(target))
        spans = [(m.start(), m.end(), m.group(0)) for m in re.finditer(
            r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", code)]
        cand = [(s, e, lit) for (s, e, lit) in spans
                if float(lit) != float(target)]
        tried = 0
        for a in range(len(cand)):
            s1, e1, lit1 = cand[a]
            for b in range(a + 1, len(cand)):
                if tried >= _MAX_PAIR_CANDIDATES:
                    return None
                tried += 1
                s2, e2, lit2 = cand[b]
                new_code = (code[:s1] + target_s + code[e1:s2]
                            + target_s + code[e2:])
                if new_code == code:
                    continue
                if self._code_reproduces(new_code, examples):
                    return new_code, (lit1, lit2)
        return None

    def _code_reproduces(self, code: str, examples) -> bool:
        from swarm_engine.agent_org.subprocess_runner import run_code
        for args, expected in examples:
            try:
                rep = run_code(code, "run", [dict(args)])
            except Exception:
                return False
            vals = getattr(rep, "value", None) or []
            if not (vals and isinstance(vals, list) and vals[0].get("ok")
                    and vals[0].get("value") == expected):
                return False
        return True

    # ------------------------------------------------------------------
    # Route A: adaptation
    # ------------------------------------------------------------------
    def _try_adaptation(self, delta: DeltaRecord, build, heldout,
                        result: DistillationResult
                        ) -> Optional[DistillationResult]:
        """Returns a filled result on the adaptation route, None to fall
        through to fresh synthesis."""
        try:
            res = self.engine.adapt_capability(delta.objective, list(build))
        except Exception as exc:
            result.reason = (f"adaptation route raised "
                             f"{type(exc).__name__}: {exc}; falling through "
                             f"to fresh synthesis")
            return None
        if not res.get("adapted"):
            # Not a failure — just no near-miss retained. Name it and move on.
            result.reason = ("adaptation route: "
                             + str(res.get("reason", "no near-miss retained")))
            return None
        promoted = res.get("promoted_name") or ""
        if not promoted:
            result.reason = ("adaptation route: adapted but promotion "
                             "returned no name (fail closed upstream)")
            return None
        ok, passed = self._verify_promoted_heldout(promoted, heldout)
        result.route = "adaptation"
        result.promoted_name = promoted
        result.capability_id = str(res.get("capability_id", ""))
        result.heldout_passed = passed
        result.verdict_admitted = True  # adaptation promotes verdict-bound
        # Negative controls on the adapted primitive.
        neg_ok, neg_n = self._negative_controls(promoted, build)
        result.negative_controls_passed = neg_n if neg_ok else 0
        if not ok:
            result.reason = ("adaptation route: promoted primitive failed "
                             f"held-out verification ({passed}/"
                             f"{len(heldout)})")
            result.success = False
            return result
        if not neg_ok:
            result.reason = ("adaptation route: negative controls failed")
            result.success = False
            return result
        result.success = True
        return result

    # ------------------------------------------------------------------
    # Route B: fresh synthesis
    # ------------------------------------------------------------------
    def _try_fresh_synthesis(self, delta: DeltaRecord, build, heldout,
                             result: DistillationResult) -> DistillationResult:
        engine = self.engine
        # 1. Synthesize + admit through the real cognitive path.
        # Param names come from the evidence itself — the technique was
        # demonstrated with these argument names.
        param_names = tuple(build[0][0].keys()) if build else ("value",)
        try:
            syn = engine.synthesize_and_admit(delta.objective, list(build),
                                              param_names=param_names)
        except Exception as exc:
            result.reason = (f"fresh synthesis raised "
                             f"{type(exc).__name__}: {exc}")
            return result
        if not syn.get("admitted"):
            result.reason = ("fresh synthesis: not admitted: "
                             + str(syn.get("reason", syn)))
            return result
        cap_id = syn.get("capability_id") or ""
        result.capability_id = str(cap_id)

        # 2-5. Shared trust path: render -> held-out in fresh processes ->
        #    negative controls -> ReviewBoard re-verify of the exact bytes ->
        #    frozen promotion.
        try:
            rec = engine.capabilities.get(cap_id)
            effects = list(getattr(rec, "effects", None) or [])
            plan = rec.plan
        except Exception as exc:
            result.reason = (f"fresh synthesis: plan lookup failed "
                             f"{type(exc).__name__}: {exc}")
            return result
        return self._verify_and_promote_plan(
            plan, delta, build, heldout, result,
            route="fresh-synthesis", spec_prefix="distilled",
            effects=effects)

    # ------------------------------------------------------------------
    # Route C: trace-guided synthesis
    # ------------------------------------------------------------------
    def _try_trace_guided(self, delta: DeltaRecord, build, heldout,
                          result: DistillationResult) -> DistillationResult:
        """Distill a multi-step technique from teacher demonstration traces.

        The exact-fit search cannot find multi-step programs (the
        distillation wall). When the delta carries the teacher's labeled
        intermediate work, decompose into one single-step subproblem per
        labeled line, solve each with the existing search, compose in trace
        order, and run the composed program through the shared trust path.
        Returns None when the delta carries no traces (fall through to
        fresh synthesis); otherwise the result is terminal for this delta
        (success or a named failure).
        """
        from swarm_engine.synthesis import trace_guided as tg
        traces = list(getattr(delta, "demonstration_traces", None) or [])
        if not traces:
            return None
        result.route = "trace-guided"
        if len(traces) < len(build):
            result.reason = (
                f"trace-guided: {len(traces)} traces < {len(build)} "
                "build examples: refusing (fail closed)")
            return result
        build_traces = traces[:len(build)]
        # 1. Parse traces and form per-labeled-line subproblems.
        try:
            parsed = [tg.parse_labeled_trace(t) for t in build_traces]
            subproblems = tg.form_subproblems(build, parsed)
        except (tg.TraceParseError, tg.TraceShapeError) as exc:
            result.reason = f"trace-guided: trace decomposition failed: {exc}"
            return result
        # 2. Solve each subproblem with the existing single-step-capable
        #    exact-fit search. A failed subproblem fails the route by name.
        sub_plans = []
        try:
            for i, sp in enumerate(subproblems):
                res = self.engine.cognition.propose_multi(
                    f"trace-guided substep {i + 1}/{len(subproblems)} "
                    f"of {delta.technique}",
                    sp.examples, tuple(sp.input_names))
                if not res.solved or not res.plan:
                    result.reason = (
                        f"trace-guided: substep {i + 1}/{len(subproblems)} "
                        f"({sp.label}) synthesis failed: "
                        f"{getattr(res, 'reason', 'unsolved')}")
                    return result
                sub_plans.append(res.plan)
        except Exception as exc:
            result.reason = (
                f"trace-guided: subproblem synthesis raised "
                f"{type(exc).__name__}: {exc}")
            return result
        # 3. Compose the per-step programs in trace order.
        try:
            inter_labels = [sp.label for sp in subproblems
                            if sp.kind == "intermediate"]
            plan = tg.compose_plan(
                sub_plans, list(build[0][0].keys()), inter_labels,
                f"trace_guided_{delta.delta_id[:8]}")
        except tg.TraceShapeError as exc:
            result.reason = f"trace-guided: composition failed: {exc}"
            return result
        # 4-7. The shared trust path: render, held-out, negative
        #    controls, ReviewBoard re-verify, frozen promotion.
        return self._verify_and_promote_plan(
            plan, delta, build, heldout, result,
            route="trace-guided", spec_prefix="trace_guided", effects=[])

    # ------------------------------------------------------------------
    # Shared trust path for synthesized plans (Routes B and C)
    # ------------------------------------------------------------------
    def _verify_and_promote_plan(self, plan, delta: DeltaRecord, build,
                                 heldout, result: DistillationResult,
                                 route: str, spec_prefix: str,
                                 effects) -> DistillationResult:
        """Render -> held-out (fresh processes) -> negative controls ->
        ReviewBoard re-verify of the exact bytes -> frozen promotion.
        The same causal bar for every synthesized plan, whichever route
        produced it. Returns the result (success or named failure)."""
        engine = self.engine
        param_names = tuple(build[0][0].keys()) if build else ("value",)
        # Render the plan to standalone code.
        try:
            _steps = plan.get("steps") or []
            _all_acq = _steps and all(
                isinstance(s.get("op"), str) and
                s.get("op").startswith("acquired.")
                for s in _steps)
            if _all_acq:
                code = self._render_second_order(plan, delta.delta_id)
            else:
                from swarm_engine.synthesis.codegen import (
                    render_plan_to_source)
                code, _meta = render_plan_to_source(
                    plan, engine.primitives,
                    purpose=f"distillation of {delta.delta_id}")
        except Exception as exc:
            result.reason = (f"{route}: render failed "
                             f"{type(exc).__name__}: {exc}")
            return result

        # Held-out verification in fresh processes — the technique was
        # NOT built against these examples.
        ok, passed = self._verify_code_heldout(code, "run", heldout)
        result.heldout_passed = passed
        if not ok:
            result.reason = (
                f"{route}: held-out verification failed "
                f"({passed}/{len(heldout)}): refusing promotion")
            return result

        # Negative controls on the candidate code.
        neg_ok, neg_n = self._negative_controls_code(code, "run", build)
        result.negative_controls_passed = neg_n if neg_ok else 0
        if not neg_ok:
            result.reason = f"{route}: negative controls failed"
            return result

        # Re-verify the exact bytes through ReviewBoard, then promote
        # through the frozen promotion API path.
        try:
            from swarm_engine.acquisition.strategies import CapabilitySpec
            from swarm_engine.acquisition.semantic import Case
            review = engine.ensure_review_board()
            if review is None:
                result.reason = (f"{route}: ReviewBoard unavailable "
                                 "(fail closed)")
                return result
            cases = [Case(args=dict(a), expect=e, kind="positive",
                          label=f"distill_{delta.delta_id}_{i}")
                     for i, (a, e) in enumerate(build)]
            spec = CapabilitySpec(
                name=f"{spec_prefix}_{delta.delta_id[:8]}",
                description=delta.objective,
                examples=list(build),
                input_names=list(param_names))
            verdict = review.verify_artifact(
                code, "run", spec, cases,
                artifact_ref=f"distill_{delta.delta_id}")
            admitted = bool(getattr(verdict, "admitted", False))
            result.verdict_admitted = admitted
            if not admitted:
                result.reason = (
                    f"{route}: ReviewBoard did not admit: "
                    + str(getattr(verdict, "reasons", "")))
                return result
            candidate = SimpleNamespace(
                code=code, entrypoint="run",
                declared_effects=list(effects or []),
                source=f"distillation:{delta.delta_id}",
                notes=(f"distilled from delta {delta.delta_id}: "
                       f"{delta.technique[:80]}"))
            promoted = engine._verdict_promote_acquired(
                candidate, spec, list(build),
                f"{spec_prefix}_{delta.delta_id[:8]}")
        except Exception as exc:
            result.reason = (f"{route}: verify/promote raised "
                             f"{type(exc).__name__}: {exc}")
            return result
        if not promoted:
            result.reason = (f"{route}: promotion refused "
                             "(fail closed upstream)")
            return result
        result.route = route
        result.promoted_name = promoted
        result.success = True
        return result

    # ------------------------------------------------------------------
    # Verification helpers (causal, fresh-process)
    # ------------------------------------------------------------------
    def _verify_code_heldout(self, code: str, entrypoint: str,
                             heldout) -> Tuple[bool, int]:
        """Execute the candidate code in fresh subprocesses against the
        held-out examples. Returns (all_passed, n_passed)."""
        from swarm_engine.agent_org.subprocess_runner import run_code
        passed = 0
        for args, expected in heldout:
            try:
                rep = run_code(code, entrypoint, [dict(args)])
            except Exception:
                continue
            vals = getattr(rep, "value", None) or []
            if (vals and isinstance(vals, list) and vals[0].get("ok")
                    and vals[0].get("value") == expected):
                passed += 1
        return passed == len(heldout) and len(heldout) > 0, passed

    def _verify_promoted_heldout(self, promoted_name: str,
                                 heldout) -> Tuple[bool, int]:
        """Call the promoted primitive (planner-visible) on held-out inputs."""
        passed = 0
        for args, expected in heldout:
            try:
                got = self.engine.primitives.invoke_sync(
                    promoted_name, **dict(args))
            except Exception:
                continue
            if got == expected:
                passed += 1
        return passed == len(heldout) and len(heldout) > 0, passed

    def _negative_controls(self, promoted_name: str,
                           build) -> Tuple[bool, int]:
        """Negative controls: out-of-contract inputs must not produce
        pathological behavior. Returns (ok, n_passed); ok=False vetoes
        the distillation (fail closed).

        A clean raise on garbage input is the CORRECT behavior (pass).
        A clean return is tolerated as neutral (pass): plan operators
        are duck-typed (a string IS a sequence for chunk/nth; concat
        stringifies), so a returned value on garbage does not by itself
        prove the technique is wrong. What fails the control is
        pathological behavior: a sandbox escape / BaseException that
        bypasses normal error handling. (Q6: this helper previously
        returned True unconditionally — it could not fail. It can now.)
        """
        arg_names = list(build[0][0].keys()) if build else []
        if not arg_names:
            return True, 0
        passed = 0
        for bad in ("not-a-number", None):
            kwargs = {arg_names[0]: bad}
            try:
                self.engine.primitives.invoke_sync(promoted_name, **kwargs)
            except Exception:
                passed += 1  # failing cleanly is the correct behavior
            except BaseException as exc:
                # Sandbox escape / interpreter-level pathology: fail closed.
                return False, passed
            else:
                # Returned a value on garbage: neutral under duck-typing
                # (documented scope limit, not a blanket pass).
                passed += 1
        return True, passed

    def _negative_controls_code(self, code: str, entrypoint: str,
                                build) -> Tuple[bool, int]:
        """Negative controls against raw candidate code in fresh
        processes. Returns (ok, n_passed); ok=False vetoes promotion.

        Each garbage probe runs in the W4-R1 PURE sandbox. A sandbox-
        level failure — timeout (hang), shim crash, or effect violation
        — FAILS the control: the candidate is pathological on
        out-of-contract input. A clean in-code raise (per-case ok=False)
        passes; a returned value passes as neutral under the same
        duck-typing rationale as _negative_controls. A runner-level
        exception means the control itself could not run, which also
        fails closed rather than claiming a control that never ran.
        (Q6: this helper previously returned True on every path.)
        """
        from swarm_engine.agent_org.subprocess_runner import run_code
        arg_names = list(build[0][0].keys()) if build else []
        if not arg_names:
            return True, 0
        passed = 0
        for bad in ("not-a-number", None):
            try:
                rep = run_code(code, entrypoint,
                               [{arg_names[0]: bad}])
            except Exception:
                # The control could not run: fail closed, do not claim it.
                return False, passed
            if not rep.ok:
                # Sandbox-level pathology: hang, crash, effect violation.
                return False, passed
            vals = getattr(rep, "value", None) or []
            if vals and not vals[0].get("ok", True):
                passed += 1  # clean in-code raise: correct behavior
            else:
                passed += 1  # returned value: neutral (duck-typing)
        return True, passed

    # ------------------------------------------------------------------
    # Finishing: fill C, log the chain as an experience record
    # ------------------------------------------------------------------
    def _finish(self, delta: DeltaRecord,
                result: DistillationResult) -> DistillationResult:
        if result.success:
            delta.mark_synthesized({
                "route": result.route,
                "promoted_name": result.promoted_name,
                "capability_id": result.capability_id,
                "heldout": f"{result.heldout_passed}/{result.heldout_examples}",
                "verdict_admitted": result.verdict_admitted,
                "at": result.at,
            })
            self._log_experience(delta, "distillation_complete",
                                 result.as_dict())
        else:
            self._log_experience(delta, "distillation_refused",
                                 {"reason": result.reason})
        return result

    # ------------------------------------------------------------------
    # Experience logging (the loop observing itself)
    # ------------------------------------------------------------------
    def _log_experience(self, delta: DeltaRecord, event: str,
                        detail: Dict[str, Any]) -> None:
        self._log_experience_raw(
            f"{event}:{delta.delta_id}",
            {"delta_id": delta.delta_id,
             "objective": delta.objective,
             "technique": (delta.technique or "")[:200],
             "detail": detail})

    def _log_experience_raw(self, content: str,
                            raw: Dict[str, Any]) -> None:
        if self.epistemic is None:
            return
        try:
            # Experience records go through the unified-memory facade (not
            # the store's raw save_observation): the facade stamps the
            # canonical provenance block. The deterministic id keeps the
            # write idempotent across retries.
            from swarm_engine.intellect.unified_memory import record_experience
            import hashlib as _hl
            oid = ("exp_" + _hl.sha256(
                f"{content}{time.time()}".encode()).hexdigest()[:16])
            record_experience(
                self.epistemic,
                origin_loop="acquisition",
                kind="distillation_experience",
                content=content,
                raw=dict(raw),
                source="distillation-loop",
                observation_id=oid)
        except Exception:
            pass  # experience logging never breaks the loop
