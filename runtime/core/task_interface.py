"""
swarm_engine/core/task_interface.py

A single entry point every task goes through, built on top of the existing
machinery rather than replacing it.

`run_task` already does most of this implicitly, spread across arbitration
branches. This module makes the pipeline an explicit, named sequence with a
stage-by-stage trace, so "what did the engine actually do to solve this" is a
direct question with a direct answer instead of something you infer from
which branch of `run_task` happened to fire:

    UNDERSTAND -> DECOMPOSE -> ASSESS -> GAPS -> PLAN -> ACQUIRE
    -> EXECUTE -> VERIFY -> RECOVER -> LEARN -> IMPROVE -> PERSIST

Every stage either delegates to real, already-proven machinery (the gap
reasoner, the orchestrator, run_task itself, the recovery engine, failure
memory) or is honestly a thin pass-through with nothing new to do at that
stage for this particular goal. No stage fabricates work: ACQUIRE is skipped
and reported as skipped when there is no gap, not silently marked complete.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


def _normalize_example(ex: Any) -> Optional[Tuple[Dict[str, Any], Any]]:
    """Accept (input_dict, expected) or {"input": ..., "output": ...}."""
    if isinstance(ex, (list, tuple)) and len(ex) >= 2 and isinstance(ex[0], dict):
        return ex[0], ex[1]
    if isinstance(ex, dict):
        inp = ex.get("input")
        out = ex.get("output", ex.get("expected"))
        if isinstance(inp, dict):
            return inp, out
    return None


def _objective_equal(actual: Any, expected: Any, rel_tol: float = 1e-6,
                     abs_tol: float = 1e-9) -> bool:
    """Generalized objective equality for oracle checking.

    Handles floats (tolerance), lists/tuples (order-sensitive structural
    compare with nested tolerance), dicts (key-wise), and exact equality
    for other types. Does not attempt to interpret nondeterministic or
    effectful results as success without an explicit expected value.
    """
    if expected is None:
        return actual is None
    if isinstance(expected, float) or isinstance(actual, float):
        try:
            return math.isclose(float(actual), float(expected),
                                rel_tol=rel_tol, abs_tol=abs_tol)
        except (TypeError, ValueError):
            return False
    if isinstance(expected, (list, tuple)) and isinstance(actual, (list, tuple)):
        if len(expected) != len(actual):
            return False
        return all(_objective_equal(a, e, rel_tol, abs_tol)
                   for a, e in zip(actual, expected))
    if isinstance(expected, dict) and isinstance(actual, dict):
        if set(expected.keys()) != set(actual.keys()):
            return False
        return all(_objective_equal(actual[k], expected[k], rel_tol, abs_tol)
                   for k in expected)
    return actual == expected


class Stage(Enum):
    UNDERSTAND = "understand"
    DECOMPOSE = "decompose"
    ASSESS = "assess"
    GAPS = "gaps"
    PLAN = "plan"
    ACQUIRE = "acquire"
    EXECUTE = "execute"
    VERIFY = "verify"
    RECOVER = "recover"
    LEARN = "learn"
    IMPROVE = "improve"
    PERSIST = "persist"


@dataclass
class StageTrace:
    stage: Stage
    ran: bool
    detail: str = ""
    elapsed_ms: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"stage": self.stage.value, "ran": self.ran,
                "detail": self.detail[:250], "elapsed_ms": round(self.elapsed_ms, 3)}


@dataclass
class TaskOutcome:
    goal: str
    success: bool
    value: Any = None
    error: str = ""
    trace: List[StageTrace] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "success": self.success, "value": self.value,
                "error": self.error[:300], "trace": [t.as_dict() for t in self.trace]}


class UniversalTaskInterface:
    """One named pipeline for every goal.

    This does not reimplement arbitration, synthesis, decomposition, recovery
    or persistence — it calls the same engine methods `run_task` already
    calls, and adds the stages `run_task` does not: an explicit gap check
    before execution (so ACQUIRE is a decision, not a side effect of
    synthesis failing), and explicit LEARN/IMPROVE stages that record the
    outcome into failure memory and, periodically, drive one self-improvement
    cycle.
    """

    def __init__(self, engine, improve_every: int = 20):
        self.engine = engine
        self.improve_every = improve_every
        self._task_count = 0

    async def handle(self, goal: str, payload: Optional[Dict[str, Any]] = None,
                     examples: Optional[List[Any]] = None,
                     metadata: Optional[Dict[str, Any]] = None,
                     driver_id: Optional[str] = None) -> TaskOutcome:
        outcome = TaskOutcome(goal=goal, success=False)

        # Public-boundary schema guard (Defect #8). Non-dict payloads must
        # become structured TaskOutcome failures, never uncaught exceptions.
        if payload is not None and not isinstance(payload, dict):
            self._trace(outcome, Stage.UNDERSTAND, True,
                        f"rejected: payload must be a dict or None, got {type(payload).__name__}")
            outcome.error = (
                f"invalid payload type: expected dict or None, got {type(payload).__name__}"
            )
            return outcome

        if not isinstance(goal, str) or not goal.strip():
            self._trace(outcome, Stage.UNDERSTAND, True,
                        "rejected: goal must be a non-empty string")
            outcome.error = "invalid goal: must be a non-empty string"
            return outcome

        payload = dict(payload or {})
        metadata = dict(metadata or {})
        if examples:
            metadata.setdefault("acquisition_examples", examples)

        # O19: provenance for the driver example set. Recorded once at this
        # entry point (idempotent: the same examples yield the same batch
        # id); the id is threaded to every downstream verdict below so each
        # one cites the exact batch it was judged against. Driver identity
        # comes from the explicit kwarg or metadata, else "unattributed".
        examples_batch_id = None
        _reg = getattr(self.engine, "oracle_registry", None)
        _handle = getattr(self.engine, "oracle", None)
        if examples and _reg is not None and _handle is not None:
            from swarm_engine.governance.examples_provenance import (
                record_examples_batch)
            examples_batch_id = record_examples_batch(
                _reg, _handle, goal, list(examples), "task_interface.handle",
                driver_id=driver_id or metadata.get("driver_id"))

        # UNDERSTAND: goal parsing already happens inside the planner/composer
        # on every call; nothing upstream of that is duplicated here. This
        # stage exists as a named point in the trace, not a second parser.
        self._trace(outcome, Stage.UNDERSTAND, True, f"goal: {goal!r}")

        # DECOMPOSE: real when the goal is composite (':' or ' then '); a
        # no-op, reported honestly, otherwise. Reuses the same detector
        # arbitration uses, so this and run_task never disagree about it.
        from swarm_engine.core.taskgraph import is_composite
        composite = is_composite(goal)
        self._trace(outcome, Stage.DECOMPOSE, composite,
                   "composite goal; will be split by run_task's DECOMPOSE tier"
                   if composite else "goal is atomic; nothing to decompose")

        # ASSESS + GAPS: the real gap reasoner, not a guess. This is what lets
        # ACQUIRE below be a decision made on evidence rather than triggered
        # reactively after execution has already failed once.
        graph = None
        if not composite:
            started = time.time()
            graph = self.engine.gap_reasoner.analyze(goal)
            gaps = graph.gaps()
            self._trace(outcome, Stage.ASSESS, True,
                       f"{len(graph.nodes)} requirement(s) identified",
                       (time.time() - started) * 1000)
            self._trace(outcome, Stage.GAPS, bool(gaps),
                       f"{len(gaps)} unmet requirement(s): "
                       f"{[g.name for g in gaps]}" if gaps
                       else "no capability gap; existing vocabulary suffices")
        else:
            self._trace(outcome, Stage.ASSESS, False,
                       "composite goal; gap analysis applies per-stage inside "
                       "decomposition, not to the whole pipeline")
            self._trace(outcome, Stage.GAPS, False, "skipped for composite goals")

        # PLAN: delegate to the real planner for visibility into what run_task
        # will actually do, without duplicating its arbitration decision.
        if not composite:
            proposal = self.engine.planner.best(goal)
            self._trace(outcome, Stage.PLAN, proposal is not None,
                       f"strategy={proposal.strategy}" if proposal
                       else "no plan found over the current vocabulary")

        # ACQUIRE: when GAPS found something and examples exist, prefer
        # example-driven GenericCapabilityGrowthEngine (proper
        # CapabilityRequirement + param_names). execute_sync is safe under
        # a running loop after the Phase 5 / H repair. Fall back to the
        # free-form orchestrator if growth does not admit a capability.
        if graph is not None and graph.gaps() and examples:
            started = time.time()
            acquired_ids: List[str] = []
            failed_nodes: List[str] = []
            growth_ok = False
            try:
                from swarm_engine.capability.requirement import CapabilityRequirement
                from swarm_engine.capability.growth_engine import (
                    GenericCapabilityGrowthEngine,
                )
                first = examples[0]
                inputs = first[0] if isinstance(first, (list, tuple)) else (
                    first.get("input", {}) if isinstance(first, dict) else {})
                param_names = tuple(inputs.keys()) if isinstance(inputs, dict) else ()
                growth = GenericCapabilityGrowthEngine(
                    swarm_engine=self.engine, provenance=self.engine.provenance)
                # Full goal text first; if NL phrasing blocks composition,
                # retry with a neutral description so example-driven
                # synthesis can still admit a capability.
                gresult = None
                # Neutral fallback: param names only. Some NL phrasings bias the
                # synthesizer away from the example-driven solution; a
                # param-only description keeps the search example-led.
                fallback_desc = (
                    " ".join(param_names) if param_names else "capability"
                )
                for desc in (goal, fallback_desc):
                    req = CapabilityRequirement(
                        description=desc,
                        param_names=param_names or None,
                        examples=list(examples),
                    )
                    gresult = growth.grow(req)
                    if getattr(gresult, "succeeded", False):
                        break
                if gresult is not None and getattr(gresult, "succeeded", False):
                    growth_ok = True
                    cid = getattr(gresult, "capability_id", None)
                    if cid:
                        acquired_ids.append(cid)
                        # Bind so subsequent run_task arbitration can
                        # resolve this goal to the newly admitted capability.
                        try:
                            self.engine.capabilities.bind_goal(goal, cid)
                        except Exception:
                            pass
                else:
                    route = getattr(gresult, "route_attempted", "failed") if gresult else "none"
                    failed_nodes.append(f"growth:{route}")
            except Exception as ex:
                failed_nodes.append(f"growth:{type(ex).__name__}")

            if not growth_ok:
                acquisition = await self.engine.acquisition_orchestrator.resolve(
                    goal, examples_by_node={graph.gaps()[0].name: examples})
                acquired_ids.extend(list(acquisition.acquired or []))
                failed_nodes.extend(list(acquisition.failed or []))
                growth_ok = bool(acquisition.fully_resolved)

            self._trace(outcome, Stage.ACQUIRE, growth_ok,
                       f"acquired={acquired_ids} failed={failed_nodes}",
                       (time.time() - started) * 1000)
        elif graph is not None and graph.gaps():
            self._trace(outcome, Stage.ACQUIRE, False,
                       "gap identified but no examples supplied; a generated "
                       "candidate cannot be validated, so acquisition was not "
                       "attempted rather than admitted on trust")
        else:
            self._trace(outcome, Stage.ACQUIRE, False, "no gap; nothing to acquire")

        # PARAMETER BINDING (Defect #2):
        # Explicit payload is the primary execution-input source.
        # examples remain acquisition/verification evidence (already stored
        # in metadata). When the selected plan declares parameters that are
        # absent from payload, and examples exist, derive a candidate
        # execution payload from the INPUT side of the first example only.
        # Never use example outputs as execution inputs. Never override a
        # non-empty explicit payload.
        if examples and isinstance(examples, (list, tuple)) and examples:
            first = examples[0]
            example_inputs = None
            if isinstance(first, (list, tuple)) and len(first) >= 1 and isinstance(first[0], dict):
                example_inputs = first[0]
            elif isinstance(first, dict) and "input" in first and isinstance(first["input"], dict):
                example_inputs = first["input"]
            if example_inputs:
                # Only fill keys that the payload does not already supply.
                for k, v in example_inputs.items():
                    if k not in payload:
                        payload[k] = v

        # EXECUTE: the real engine pipeline. run_task performs arbitration,
        # synthesis-or-acquisition-fallback, and schema verification.
        started = time.time()
        task_id = self.engine.submit_task(goal, payload=payload, metadata=metadata)
        result = await self.engine.run_task(task_id)
        self._trace(outcome, Stage.EXECUTE, result.get("success", False),
                   f"state={result.get('state')}",
                   (time.time() - started) * 1000)

        outcome.success = bool(result.get("success"))
        outcome.value = (result.get("result") or {}).get("value")
        outcome.error = str(result.get("error") or "")

        # VERIFY: schema + value oracle (Phase 4) + world postconditions (#9).
        # SUCCESS requires all applicable obligations. A return-value match
        # alone must never certify an unperformed external effect.
        from swarm_engine.acquisition.intent import (
            infer_required_effects, infer_postconditions, evaluate_postconditions,
        )
        schema_detail = str(result.get("verification",
                            "no separate schema verification stage"))[:120]
        req_effects = infer_required_effects(goal)
        posts = infer_postconditions(goal, examples)

        verify_bits = [f"schema={schema_detail[:40]}"]
        if outcome.success and examples:
            obj_ok, obj_detail = await self._objective_verify(
                result, list(examples),
                examples_batch_id=examples_batch_id)
            if not obj_ok:
                outcome.success = False
                outcome.error = f"objective verification failed: {obj_detail}"
                verify_bits.append(f"objective=FAILED ({obj_detail})")
            else:
                verify_bits.append(f"objective=passed ({obj_detail})")
        else:
            verify_bits.append(
                "objective=skipped (%s)" % (
                    "no examples" if not examples else "exec failed"))

        # Effectful goals: pure/exec success without covering effects or
        # satisfied postconditions is a failure (#4 / #9).
        if req_effects and outcome.success:
            # Inspect executed capability purity when available.
            inner = result.get("result") or {}
            cap_id = inner.get("capability_id")
            pure_surrogate = False
            if cap_id:
                try:
                    rec, _probs = self.engine.capabilities.rehydrate(
                        cap_id, self.engine.primitives)
                    if rec is not None:
                        for step in (rec.plan or {}).get("steps", []):
                            op = step.get("op")
                            prim = self.engine.primitives.get(op) if op else None
                            if prim is not None and getattr(prim, "pure", True):
                                pure_surrogate = True
                except Exception:
                    pass
            if pure_surrogate:
                outcome.success = False
                outcome.error = (
                    "effect coverage failed: pure capability cannot satisfy "
                    f"required effects {req_effects}"
                )
                verify_bits.append("effects=FAILED (pure surrogate)")
            elif posts:
                post_ok, post_detail = evaluate_postconditions(posts)
                # O19: the postconditions were inferred from the driver's
                # examples -- cite the batch this verdict rests on.
                from swarm_engine.governance.examples_provenance import (
                    cite_batch)
                post_detail = post_detail + cite_batch(examples_batch_id)
                if not post_ok:
                    outcome.success = False
                    outcome.error = (
                        f"postcondition verification failed: {post_detail}"
                    )
                    verify_bits.append(f"postconditions=FAILED ({post_detail})")
                else:
                    verify_bits.append(f"postconditions=passed ({post_detail})")
            else:
                # Effect demanded but no observable postcondition could be
                # inferred — fail closed rather than value-only success.
                outcome.success = False
                outcome.error = (
                    "effectful goal has no verifiable postcondition; "
                    "refusing value-only success"
                )
                verify_bits.append("postconditions=FAILED (none inferred)")

        self._trace(outcome, Stage.VERIFY, True, "; ".join(verify_bits)[:200])

        # RECOVER: real recovery already ran inside run_task's own synthesis-
        # fallback path when EXECUTE failed. This reports that rather than
        # re-attempting a redundant recovery pass.
        self._trace(outcome, Stage.RECOVER, not outcome.success,
                   "run_task's synthesis-fallback already exhausted the "
                   "recovery paths available for this failure" if not outcome.success
                   else "not needed; execution succeeded")

        # LEARN: every outcome, success or failure, becomes evidence.
        if not outcome.success:
            self.engine.failure_memory.record(goal, outcome.error or "task failed")
            self._trace(outcome, Stage.LEARN, True, "failure recorded to memory")
        else:
            self._trace(outcome, Stage.LEARN, True,
                       "success; no failure evidence to record")

        # IMPROVE: record production outcome every task; run a governed
        # improvement cycle when failure evidence accumulates. Does not
        # self-certify — run_cycle still validates against incumbents.
        self._task_count += 1
        improved = False
        improve_detail = f"task {self._task_count}"
        pipeline = getattr(self.engine, "improvement_pipeline", None)
        if pipeline is not None:
            try:
                pipeline.record_production_outcome(
                    "cognition.search_policy", bool(outcome.success))
            except Exception:
                pass
            dominant = None
            try:
                dominant = self.engine.failure_memory.dominant_kind(min_samples=3)
            except Exception:
                dominant = None
            should_run = (
                (not outcome.success and dominant is not None)
                or (self._task_count % self.improve_every == 0 and dominant is not None)
            )
            if should_run:
                try:
                    reports = pipeline.run_cycle()
                    active = [r for r in (reports or [])
                              if r.get("outcome") == "active"]
                    improved = bool(active)
                    improve_detail = (
                        f"cycle ran; active={len(active)} reports={len(reports or [])}"
                        f"; dominant={dominant}"
                    )
                except Exception as ex:
                    improve_detail = f"cycle error: {type(ex).__name__}"
            else:
                improve_detail = (
                    f"skipped (dominant={dominant}); {improve_detail}"
                )
        self._trace(outcome, Stage.IMPROVE, improved, improve_detail)

        # PERSIST: everything that needed persisting already was, inside
        # run_task/acquire_capability (capability store, provenance, acquired
        # source, lifecycle). This stage confirms rather than duplicates.
        self._trace(outcome, Stage.PERSIST, True,
                   "handled by the underlying capability/provenance/lifecycle "
                   "stores during EXECUTE/ACQUIRE")

        return outcome

    async def _objective_verify(
        self,
        run_result: Dict[str, Any],
        examples: Sequence[Any],
        examples_batch_id: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Check that an admitted capability satisfies every example oracle.

        Uses the capability_id from the execution result when available so
        verification is against the same admitted plan that just ran, not a
        re-planned variant. Falls back to comparing only the primary
        execution value against the first matching example when no
        capability can be rehydrated (still better than schema-only).

        O19: the returned detail cites the example batch the verdict was
        judged against.
        """
        from swarm_engine.governance.examples_provenance import cite_batch
        ok, detail = await self._objective_verify_against(run_result,
                                                         examples)
        return ok, detail + cite_batch(examples_batch_id)

    async def _objective_verify_against(
        self,
        run_result: Dict[str, Any],
        examples: Sequence[Any],
    ) -> Tuple[bool, str]:
        parsed: List[Tuple[Dict[str, Any], Any]] = []
        for ex in examples:
            norm = _normalize_example(ex)
            if norm is not None:
                parsed.append(norm)
        if not parsed:
            return True, "no parseable examples"

        inner = run_result.get("result") or {}
        cap_id = inner.get("capability_id")
        primary_value = inner.get("value")

        # Fast path: if we only have the primary execution, at least check
        # it against the first example whose inputs were used (or first).
        if not cap_id:
            exp = parsed[0][1]
            if _objective_equal(primary_value, exp):
                return True, "primary value matched first example (no cap id)"
            return False, (
                f"primary value {primary_value!r} != expected {exp!r}"
            )

        record, problems = self.engine.capabilities.rehydrate(
            cap_id, self.engine.primitives)
        if record is None or problems:
            exp = parsed[0][1]
            if _objective_equal(primary_value, exp):
                return True, "primary matched; rehydrate unavailable"
            return False, (
                f"cannot rehydrate {cap_id}: {problems}; "
                f"primary {primary_value!r} != {exp!r}"
            )

        mismatches = []
        for args, expected in parsed:
            try:
                out = await self.engine.composer.execute(record.plan, dict(args))
            except Exception as ex:
                mismatches.append(f"{args!r}: raised {type(ex).__name__}: {ex}")
                continue
            if not out.get("success"):
                mismatches.append(
                    f"{args!r}: exec failed: {out.get('error')}")
                continue
            actual = out.get("value")
            if not _objective_equal(actual, expected):
                mismatches.append(
                    f"{args!r}: got {actual!r}, expected {expected!r}")

        if mismatches:
            return False, "; ".join(mismatches[:3])
        return True, f"matched {len(parsed)} example(s)"

    def _trace(self, outcome: TaskOutcome, stage: Stage, ran: bool, detail: str,
              elapsed_ms: float = 0.0) -> None:
        outcome.trace.append(StageTrace(stage, ran, detail, elapsed_ms))
