"""
swarm_engine/primitives/families_meta.py

The reflexive half of the vocabulary — primitives whose subject matter is the
engine itself. This is what closes the loop the original V6 left open: the
synthesizer could write a capability, but nothing could evaluate one, diagnose
why it failed, propose a revision, test the revision against the incumbent, and
retain the winner. Those steps are primitives here, which means an improvement
cycle is itself a composable plan rather than hardcoded engine machinery.

Families in this module:
    capability   agent        testing      optimization
    improvement  contracts    explanation  metalearning   version   compose
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from typing import Any, Callable, Dict, List, Optional

from swarm_engine.primitives.core import (
    ANY, BOOL, CALLABLE, DICT, FLOAT, INT, LIST, NONE, NUM, OPT, STR, TUPLE,
    UNION, Effect, ExecContext, Primitive, PrimitiveRegistry, infer,
)


def _p(reg, name, family, fn, inputs, output, effects=(Effect.PURE,),
       needs_ctx=False, doc=""):
    reg.register(Primitive(name=name, family=family, fn=fn, inputs=inputs,
                           output=output, effects=effects, needs_ctx=needs_ctx,
                           doc=doc), overwrite=True)


def _composer(ctx: ExecContext):
    from swarm_engine.synthesis.composer import Composer
    comp = ctx.scratch.get("_composer")
    if comp is None:
        comp = Composer(ctx.registry)
        ctx.scratch["_composer"] = comp
    return comp


# ===========================================================================
# 17. CAPABILITY  (discover / inspect / invoke / admit the vocabulary itself)
# ===========================================================================

def register_capability(reg: PrimitiveRegistry) -> None:
    F = "capability"

    def list_primitives(ctx, family=None):
        if family:
            return [p.name for p in ctx.registry.family(family)]
        return ctx.registry.names()

    def list_families(ctx):
        return {f: len(n) for f, n in ctx.registry.families().items()}

    def describe_primitive(ctx, name):
        p = ctx.registry.get(name)
        if p is None:
            return {"found": False, "name": name}
        return {"found": True, "name": p.name, "family": p.family,
                "signature": p.signature(), "doc": p.doc, "pure": p.pure,
                "effects": [e.value for e in p.effects],
                "inputs": {k: str(v) for k, v in p.inputs.items()},
                "output": str(p.output)}

    def find_primitives(ctx, term):
        return [{"name": p.name, "family": p.family, "doc": p.doc}
                for p in ctx.registry.search(term)]

    def primitives_producing(ctx, type_name):
        from swarm_engine.synthesis.composer import _spec
        return [p.name for p in ctx.registry.producing(_spec(type_name))]

    def primitives_consuming(ctx, type_name):
        from swarm_engine.synthesis.composer import _spec
        return [p.name for p in ctx.registry.consuming(_spec(type_name))]

    async def invoke_primitive(ctx, name, args):
        return await ctx.registry.invoke(name, ctx, **(args or {}))

    def analyze_plan(ctx, plan):
        return _composer(ctx).analyze(plan).as_dict()

    async def run_plan(ctx, plan, args=None):
        if ctx.depth > 6:
            raise RuntimeError("plan recursion depth exceeded")
        return await _composer(ctx).execute(plan, args or {}, ctx.child())

    def plan_identity(ctx, plan):
        return _composer(ctx).plan_hash(plan)

    def plan_permitted(ctx, plan):
        ok, missing = _composer(ctx).permitted(plan)
        return {"permitted": ok, "missing_grants": missing}

    def store_capability(ctx, plan, spec=None):
        if ctx.kb is None:
            raise RuntimeError("no knowledge base bound")
        cid = _composer(ctx).plan_hash(plan)
        ctx.kb.store_capability(cid, spec or {"name": plan.get("name", "unnamed"),
                                              "params": plan.get("params", {})},
                                json.dumps(plan))
        return {"capability_id": cid, "stored": True}

    def load_capability(ctx, capability_id):
        if ctx.kb is None:
            raise RuntimeError("no knowledge base bound")
        rec = ctx.kb.get_capability(capability_id)
        if not rec:
            return {"found": False, "capability_id": capability_id}
        return {"found": True, "capability_id": capability_id,
                "plan": json.loads(rec["code"]), "spec": rec["spec"],
                "use_count": rec["use_count"], "success_count": rec["success_count"],
                "fail_count": rec["fail_count"]}

    def record_outcome(ctx, capability_id, success):
        if ctx.kb is None:
            raise RuntimeError("no knowledge base bound")
        ctx.kb.record_use(capability_id, bool(success))
        return {"capability_id": capability_id, "recorded": True}

    def admit_capability(ctx, plan, min_checks=True):
        """The admission gate: a plan enters the registry only if it type-checks
        and every effect it demands is already granted."""
        comp = _composer(ctx)
        analysis = comp.analyze(plan)
        permitted, missing = comp.permitted(plan)
        admitted = analysis.ok and (permitted or not min_checks)
        out = {"admitted": admitted, "capability_id": comp.plan_hash(plan),
               "analysis": analysis.as_dict(), "missing_grants": missing}
        if admitted and ctx.kb is not None:
            store_capability(ctx, plan)
        return out

    for name, fn, inp, out, eff, doc in [
        ("list_primitives", list_primitives, {"family": OPT(STR)}, LIST(STR), (Effect.PURE,), "Every primitive, optionally by family."),
        ("list_families", list_families, {}, DICT(STR, INT), (Effect.PURE,), "Family names and sizes."),
        ("describe_primitive", describe_primitive, {"name": STR}, DICT(), (Effect.PURE,), "Signature, effects and doc for one primitive."),
        ("find_primitives", find_primitives, {"term": STR}, LIST(DICT()), (Effect.PURE,), "Search the vocabulary."),
        ("primitives_producing", primitives_producing, {"type_name": STR}, LIST(STR), (Effect.PURE,), "What can produce this type."),
        ("primitives_consuming", primitives_consuming, {"type_name": STR}, LIST(STR), (Effect.PURE,), "What can consume this type."),
        ("analyze_plan", analyze_plan, {"plan": DICT()}, DICT(), (Effect.PURE,), "Static check of a plan."),
        ("plan_identity", plan_identity, {"plan": DICT()}, STR, (Effect.PURE,), "Stable hash of a plan."),
        ("plan_permitted", plan_permitted, {"plan": DICT()}, DICT(), (Effect.PURE,), "Are the plan's effects granted?"),
        ("invoke_primitive", invoke_primitive, {"name": STR, "args": DICT()}, ANY, (Effect.PURE,), "Call a primitive by name."),
        ("run_plan", run_plan, {"plan": DICT(), "args": OPT(DICT())}, DICT(), (Effect.PURE,), "Execute a plan as a sub-capability."),
        ("store_capability", store_capability, {"plan": DICT(), "spec": OPT(DICT())}, DICT(), (Effect.MEMORY,), "Persist a plan."),
        ("load_capability", load_capability, {"capability_id": STR}, DICT(), (Effect.MEMORY,), "Rehydrate a stored plan."),
        ("record_outcome", record_outcome, {"capability_id": STR, "success": BOOL}, DICT(), (Effect.MEMORY,), "Log a use for success tracking."),
        ("admit_capability", admit_capability, {"plan": DICT(), "min_checks": OPT(BOOL)}, DICT(), (Effect.MUTATE_SELF,), "Gate and register a plan."),
    ]:
        _p(reg, name, F, fn, inp, out, effects=eff, needs_ctx=True, doc=doc)


# ===========================================================================
# 18. TESTING / SIMULATION
# ===========================================================================

def register_testing(reg: PrimitiveRegistry) -> None:
    F = "testing"

    async def run_case(ctx, plan, args=None, expected=None, comparator=None):
        comp = _composer(ctx)
        r = await comp.execute(plan, args or {}, ctx.child())
        passed = r.get("success", False)
        if passed and expected is not None:
            passed = comparator(r["value"], expected) if comparator else r["value"] == expected
        return {"passed": passed, "args": args, "expected": expected,
                "actual": r.get("value"), "error": r.get("error"),
                "elapsed_ms": r.get("elapsed_ms")}

    async def run_suite(ctx, plan, cases):
        results = []
        for case in cases:
            results.append(await run_case(ctx, plan, case.get("args"),
                                          case.get("expected"),
                                          case.get("comparator")))
        passed = sum(1 for r in results if r["passed"])
        return {"total": len(results), "passed": passed, "failed": len(results) - passed,
                "pass_rate": round(passed / len(results), 4) if results else 0.0,
                "results": results,
                "failures": [r for r in results if not r["passed"]][:10]}

    def generate_cases(schema, count=10, seed=0):
        """Property-style input generation from a {param: kind} schema."""
        import random as _r
        rng = _r.Random(seed)
        gen = {
            "int": lambda: rng.randint(-1000, 1000),
            "num": lambda: rng.uniform(-1000, 1000),
            "float": lambda: rng.uniform(-1000, 1000),
            "str": lambda: "".join(rng.choice("abcdefghij ") for _ in range(rng.randint(0, 20))),
            "bool": lambda: rng.choice([True, False]),
            "list": lambda: [rng.randint(0, 100) for _ in range(rng.randint(0, 8))],
            "dict": lambda: {"k": rng.randint(0, 100)},
        }
        return [{"args": {p: gen.get(k, lambda: None)() for p, k in schema.items()}}
                for _ in range(int(count))]

    def edge_cases(schema):
        """The inputs that actually break things, enumerated deliberately."""
        edges = {"int": [0, -1, 1, 2 ** 31], "num": [0.0, -1e9, 1e9], "float": [0.0, -0.0, 1e-9],
                 "str": ["", " ", "a" * 1000, "ünïcodé", "\n"], "bool": [True, False],
                 "list": [[], [0], list(range(500))], "dict": [{}, {"k": None}]}
        cases = []
        for p, k in schema.items():
            for v in edges.get(k, [None]):
                cases.append({"args": {p: v}})
        return cases

    async def property_check(ctx, plan, schema, prop, count=25, seed=0):
        """Assert an invariant holds across generated inputs; report the first
        counterexample rather than just a pass rate."""
        comp = _composer(ctx)
        for case in generate_cases(schema, count, seed):
            r = await comp.execute(plan, case["args"], ctx.child())
            if not r.get("success"):
                return {"holds": False, "counterexample": case["args"], "error": r.get("error")}
            if not prop(r["value"]):
                return {"holds": False, "counterexample": case["args"], "value": r["value"]}
        return {"holds": True, "checked": int(count)}

    def mock_provider(ctx, name, response):
        providers = ctx.scratch.setdefault("_providers", {})

        async def _mock(**kwargs):
            return response
        providers[name] = _mock
        ctx.providers = providers
        return {"provider": name, "mocked": True}

    def assert_equal(actual, expected, message=""):
        if actual != expected:
            raise AssertionError(message or f"expected {expected!r}, got {actual!r}")
        return True

    def assert_close(actual, expected, tolerance=1e-6):
        if abs(actual - expected) > tolerance:
            raise AssertionError(f"{actual} differs from {expected} by more than {tolerance}")
        return True

    for name, fn, inp, out, ctxflag, eff, doc in [
        ("run_case", run_case, {"plan": DICT(), "args": OPT(DICT()), "expected": OPT(ANY), "comparator": OPT(CALLABLE)}, DICT(), True, (Effect.PURE,), "Execute one test case against a plan."),
        ("run_suite", run_suite, {"plan": DICT(), "cases": LIST(DICT())}, DICT(), True, (Effect.PURE,), "Run a case list and report a pass rate."),
        ("property_check", property_check, {"plan": DICT(), "schema": DICT(), "prop": CALLABLE, "count": OPT(INT), "seed": OPT(INT)}, DICT(), True, (Effect.PURE,), "Search for a counterexample to an invariant."),
        ("mock_provider", mock_provider, {"name": STR, "response": ANY}, DICT(), True, (Effect.MEMORY,), "Substitute a fake external provider."),
        ("generate_cases", generate_cases, {"schema": DICT(), "count": OPT(INT), "seed": OPT(INT)}, LIST(DICT()), False, (Effect.PURE,), "Random inputs from a parameter schema."),
        ("edge_cases", edge_cases, {"schema": DICT()}, LIST(DICT()), False, (Effect.PURE,), "Boundary inputs from a parameter schema."),
        ("assert_equal", assert_equal, {"actual": ANY, "expected": ANY, "message": OPT(STR)}, BOOL, False, (Effect.PURE,), "Raise unless two values match."),
        ("assert_close", assert_close, {"actual": NUM, "expected": NUM, "tolerance": OPT(NUM)}, BOOL, False, (Effect.PURE,), "Raise unless two numbers match within tolerance."),
    ]:
        _p(reg, name, F, fn, inp, out, effects=eff, needs_ctx=ctxflag, doc=doc)


# ===========================================================================
# 19. OPTIMIZATION
# ===========================================================================

def register_optimization(reg: PrimitiveRegistry) -> None:
    F = "optimization"

    async def benchmark(ctx, plan, args=None, runs=5):
        comp = _composer(ctx)
        times, ok = [], 0
        for _ in range(int(runs)):
            r = await comp.execute(plan, args or {}, ctx.child())
            times.append(r.get("elapsed_ms", 0.0))
            ok += 1 if r.get("success") else 0
        times.sort()
        return {"runs": int(runs), "successes": ok,
                "min_ms": times[0], "median_ms": times[len(times) // 2],
                "max_ms": times[-1], "mean_ms": round(sum(times) / len(times), 4)}

    async def compare_plans(ctx, candidates, cases, weight_correctness=0.8):
        """Score competing plans on correctness first, speed second. This is the
        arbiter the improvement loop calls to decide whether a candidate is
        genuinely better than the incumbent."""
        from swarm_engine.primitives.families_meta import _composer as _c
        scored = []
        for label, plan in candidates.items():
            comp = _c(ctx)
            passed, elapsed = 0, 0.0
            for case in cases:
                r = await comp.execute(plan, case.get("args") or {}, ctx.child())
                elapsed += r.get("elapsed_ms", 0.0)
                if r.get("success") and (case.get("expected") is None
                                         or r.get("value") == case["expected"]):
                    passed += 1
            rate = passed / len(cases) if cases else 0.0
            speed = 1.0 / (1.0 + elapsed)
            scored.append({"label": label, "pass_rate": round(rate, 4),
                           "total_ms": round(elapsed, 3),
                           "score": round(weight_correctness * rate
                                          + (1 - weight_correctness) * speed, 5)})
        scored.sort(key=lambda s: -s["score"])
        return {"ranking": scored, "best": scored[0]["label"] if scored else None}

    def grid_search(space):
        keys = list(space)
        combos = [{}]
        for k in keys:
            combos = [dict(c, **{k: v}) for c in combos for v in space[k]]
        return combos

    async def tune(ctx, plan_factory, space, cases, limit=32):
        combos = grid_search(space)[: int(limit)]
        candidates = {json.dumps(c, sort_keys=True): plan_factory(c) for c in combos}
        return await compare_plans(ctx, candidates, cases)

    def pareto_frontier(points, objectives):
        """Non-dominated set. `objectives` maps field -> 'min'|'max'."""
        def dominates(a, b):
            better_any = False
            for f, direction in objectives.items():
                av, bv = a.get(f, 0), b.get(f, 0)
                if direction == "min":
                    if av > bv:
                        return False
                    if av < bv:
                        better_any = True
                else:
                    if av < bv:
                        return False
                    if av > bv:
                        better_any = True
            return better_any
        return [p for p in points if not any(dominates(q, p) for q in points if q is not p)]

    def hill_climb(objective, start, step=1.0, iterations=100):
        """Coordinate-wise local search over a numeric dict."""
        current = dict(start)
        best = objective(current)
        for _ in range(int(iterations)):
            improved = False
            for k in current:
                for delta in (step, -step):
                    trial = dict(current)
                    trial[k] = trial[k] + delta
                    score = objective(trial)
                    if score > best:
                        current, best, improved = trial, score, True
            if not improved:
                break
        return {"best_params": current, "score": best}

    for name, fn, inp, out, ctxflag, doc in [
        ("benchmark", benchmark, {"plan": DICT(), "args": OPT(DICT()), "runs": OPT(INT)}, DICT(), True, "Timing distribution for a plan."),
        ("compare_plans", compare_plans, {"candidates": DICT(), "cases": LIST(DICT()), "weight_correctness": OPT(NUM)}, DICT(), True, "Rank competing plans on correctness then speed."),
        ("tune", tune, {"plan_factory": CALLABLE, "space": DICT(), "cases": LIST(DICT()), "limit": OPT(INT)}, DICT(), True, "Grid-search plan parameters."),
        ("grid_search", grid_search, {"space": DICT()}, LIST(DICT()), False, "Cartesian product of a parameter space."),
        ("pareto_frontier", pareto_frontier, {"points": LIST(DICT()), "objectives": DICT()}, LIST(DICT()), False, "Non-dominated solutions."),
        ("hill_climb", hill_climb, {"objective": CALLABLE, "start": DICT(), "step": OPT(NUM), "iterations": OPT(INT)}, DICT(), False, "Local search over numeric parameters."),
    ]:
        _p(reg, name, F, fn, inp, out, needs_ctx=ctxflag, doc=doc)


# ===========================================================================
# 20. IMPROVEMENT  (the loop the original engine was missing)
# ===========================================================================

def register_improvement(reg: PrimitiveRegistry) -> None:
    F = "improvement"

    def diagnose(failures):
        """Cluster failing cases into a named fault, so a repair can target the
        actual cause instead of retrying blindly."""
        if not failures:
            return {"fault": "none", "detail": "no failures to diagnose"}
        errors = [f.get("error") or "" for f in failures]
        joined = " ".join(errors).lower()
        rules = [
            ("type_mismatch", ("expects", "got", "typeerror")),
            ("missing_argument", ("missing required argument",)),
            ("permission", ("denied", "no grant")),
            ("division_by_zero", ("division by zero", "modulo by zero")),
            ("empty_input", ("empty sequence", "empty collection", "index out of range")),
            ("unknown_primitive", ("unknown primitive",)),
            ("null_handling", ("nonetype", "is not subscriptable")),
            ("timeout", ("timed out", "timeout")),
        ]
        for fault, needles in rules:
            if any(n in joined for n in needles):
                return {"fault": fault, "count": len(failures),
                        "sample_error": errors[0][:300],
                        "sample_args": failures[0].get("args")}
        wrong_value = [f for f in failures if not f.get("error")]
        if wrong_value:
            return {"fault": "wrong_result", "count": len(wrong_value),
                    "expected": wrong_value[0].get("expected"),
                    "actual": wrong_value[0].get("actual")}
        return {"fault": "unclassified", "count": len(failures), "sample_error": errors[0][:300]}

    def propose_repairs(plan, diagnosis):
        """Fault-directed plan rewrites. Each returned candidate is a complete
        plan, so it can be checked and benchmarked exactly like the incumbent."""
        fault = diagnosis.get("fault")
        out: Dict[str, Any] = {}

        if fault in ("empty_input", "null_handling"):
            for i, step in enumerate(plan.get("steps", [])):
                if step.get("op") in ("mean", "median", "mode", "min", "max", "first", "last"):
                    guarded = copy.deepcopy(plan)
                    guarded["steps"] = _guard_empty(guarded["steps"], i)
                    out[f"guard_empty@{step['id']}"] = guarded
        if fault == "division_by_zero":
            for i, step in enumerate(plan.get("steps", [])):
                if step.get("op") in ("divide", "modulo"):
                    guarded = copy.deepcopy(plan)
                    guarded["steps"] = _guard_zero(guarded["steps"], i)
                    out[f"guard_zero@{step['id']}"] = guarded
        if fault == "type_mismatch":
            for i, step in enumerate(plan.get("steps", [])):
                for arg in (step.get("args") or {}):
                    coerced = copy.deepcopy(plan)
                    coerced["steps"] = _insert_cast(coerced["steps"], i, arg)
                    out[f"cast@{step.get('id')}.{arg}"] = coerced
        if fault in ("wrong_result", "unclassified", "timeout"):
            wrapped = copy.deepcopy(plan)
            wrapped["steps"] = [{"id": "_try", "control": "try",
                                 "body": wrapped["steps"],
                                 "catch": [{"id": "_fallback", "op": "identity",
                                            "args": {"value": None}}]}]
            wrapped["output"] = plan.get("output")
            out["wrap_try"] = wrapped
        return {"candidates": out, "count": len(out), "fault": fault}

    def _guard_empty(steps, index):
        target = steps[index]
        arg_name = "values" if "values" in (target.get("args") or {}) else "items"
        source = (target.get("args") or {}).get(arg_name)
        guard = {"id": f"{target['id']}_guard", "op": "is_empty", "args": {"value": source}}
        branch = {"id": target["id"], "control": "if",
                  "cond": {"$step": guard["id"]},
                  "then": [{"id": f"{target['id']}_zero", "op": "identity", "args": {"value": 0}}],
                  "else": [dict(target, id=f"{target['id']}_real")]}
        return steps[:index] + [guard, branch] + steps[index + 1:]

    def _guard_zero(steps, index):
        target = steps[index]
        divisor = (target.get("args") or {}).get("b")
        guard = {"id": f"{target['id']}_nz", "op": "not_equals",
                 "args": {"a": divisor, "b": 0}}
        branch = {"id": target["id"], "control": "if",
                  "cond": {"$step": guard["id"]},
                  "then": [dict(target, id=f"{target['id']}_real")],
                  "else": [{"id": f"{target['id']}_inf", "op": "identity", "args": {"value": 0}}]}
        return steps[:index] + [guard, branch] + steps[index + 1:]

    def _insert_cast(steps, index, arg_name):
        target = steps[index]
        source = (target.get("args") or {}).get(arg_name)
        cast = {"id": f"{target['id']}_{arg_name}_cast", "op": "cast",
                "args": {"value": source, "kind": "float"}}
        fixed = copy.deepcopy(target)
        fixed.setdefault("args", {})[arg_name] = {"$step": cast["id"]}
        return steps[:index] + [cast, fixed] + steps[index + 1:]

    async def improve(ctx, plan, cases, max_rounds=3):
        """Evaluate -> diagnose -> propose -> test -> retain. Returns the best
        plan found together with the full audit of what was tried, so a human
        can see why the engine changed its own behaviour."""
        from swarm_engine.primitives.families_meta import _composer as _c
        comp = _c(ctx)
        history = []
        current = plan

        for round_index in range(int(max_rounds)):
            suite = await ctx.registry.invoke("run_suite", ctx, plan=current, cases=cases)
            history.append({"round": round_index, "pass_rate": suite["pass_rate"],
                            "failed": suite["failed"]})
            if suite["failed"] == 0:
                return {"improved": round_index > 0, "rounds": round_index + 1,
                        "plan": current, "pass_rate": 1.0, "history": history,
                        "capability_id": comp.plan_hash(current)}

            diagnosis = diagnose(suite["failures"])
            proposals = propose_repairs(current, diagnosis)
            history[-1]["diagnosis"] = diagnosis
            if not proposals["candidates"]:
                history[-1]["note"] = "no repair strategy matched this fault"
                break

            viable = {}
            for label, cand in proposals["candidates"].items():
                if comp.analyze(cand).ok:
                    viable[label] = cand
            if not viable:
                history[-1]["note"] = "all proposed repairs failed static checking"
                break

            viable["__incumbent__"] = current
            ranking = await ctx.registry.invoke("compare_plans", ctx,
                                                candidates=viable, cases=cases)
            best_label = ranking["best"]
            history[-1]["tried"] = ranking["ranking"][:5]
            if best_label == "__incumbent__":
                history[-1]["note"] = "no candidate beat the incumbent"
                break
            current = viable[best_label]
            history[-1]["adopted"] = best_label

        final = await ctx.registry.invoke("run_suite", ctx, plan=current, cases=cases)
        return {"improved": current is not plan, "rounds": len(history), "plan": current,
                "pass_rate": final["pass_rate"], "history": history,
                "capability_id": comp.plan_hash(current)}

    def learn_from_outcome(ctx, capability_id, success, note=""):
        """Fold a single execution into the engine's memory of what works."""
        if ctx.kb is None:
            return {"recorded": False, "reason": "no knowledge base"}
        ctx.kb.record_use(capability_id, bool(success))
        ctx.kb.log_generation(capability_id=capability_id, prompt=note,
                              body_plan=capability_id, passed=bool(success),
                              feedback=[note or ("ok" if success else "failed")])
        return {"recorded": True, "capability_id": capability_id}

    def generalize_plan(plan, literals):
        """Turn a one-off plan into a reusable one by lifting literals into
        parameters — how a specific solution becomes a general capability."""
        text = json.dumps(plan)
        params = dict(plan.get("params") or {})
        for pname, literal in literals.items():
            text = text.replace(json.dumps(literal), json.dumps({"$param": pname}))
            params[pname] = str(infer(literal)).split("[")[0]
        out = json.loads(text)
        out["params"] = params
        out["name"] = plan.get("name", "plan") + "_general"
        return out

    def transfer_plan(plan, substitutions):
        """Retarget a working plan onto a new domain by swapping primitives or
        field names it depends on."""
        text = json.dumps(plan)
        for old, new in substitutions.items():
            text = text.replace(json.dumps(old), json.dumps(new))
        return json.loads(text)

    for name, fn, inp, out, ctxflag, eff, doc in [
        ("diagnose", diagnose, {"failures": LIST(DICT())}, DICT(), False, (Effect.PURE,), "Classify a batch of failures into a named fault."),
        ("propose_repairs", propose_repairs, {"plan": DICT(), "diagnosis": DICT()}, DICT(), False, (Effect.PURE,), "Generate fault-directed plan revisions."),
        ("improve", improve, {"plan": DICT(), "cases": LIST(DICT()), "max_rounds": OPT(INT)}, DICT(), True, (Effect.MUTATE_SELF,), "Run the full evaluate/diagnose/repair/retain loop."),
        ("learn_from_outcome", learn_from_outcome, {"capability_id": STR, "success": BOOL, "note": OPT(STR)}, DICT(), True, (Effect.MEMORY,), "Record an execution outcome for future arbitration."),
        ("generalize_plan", generalize_plan, {"plan": DICT(), "literals": DICT()}, DICT(), False, (Effect.PURE,), "Lift literals into parameters."),
        ("transfer_plan", transfer_plan, {"plan": DICT(), "substitutions": DICT()}, DICT(), False, (Effect.PURE,), "Retarget a plan onto a new domain."),
    ]:
        _p(reg, name, F, fn, inp, out, effects=eff, needs_ctx=ctxflag, doc=doc)


# ===========================================================================
# 21. CONTRACTS
# ===========================================================================

def register_contracts(reg: PrimitiveRegistry) -> None:
    F = "contracts"

    def define_contract(name, requires=None, ensures=None, invariants=None):
        return {"name": name, "requires": requires or [], "ensures": ensures or [],
                "invariants": invariants or []}

    def check_precondition(contract, args):
        failed = [c["description"] for c in contract.get("requires", [])
                  if not c["check"](args)]
        return {"satisfied": not failed, "violations": failed}

    def check_postcondition(contract, args, result):
        failed = [c["description"] for c in contract.get("ensures", [])
                  if not c["check"](args, result)]
        return {"satisfied": not failed, "violations": failed}

    async def verify_contract(ctx, plan, contract, cases):
        comp = _composer(ctx)
        violations = []
        for case in cases:
            args = case.get("args") or {}
            pre = check_precondition(contract, args)
            if not pre["satisfied"]:
                continue  # input outside the contract's domain; not a violation
            r = await comp.execute(plan, args, ctx.child())
            if not r.get("success"):
                violations.append({"args": args, "kind": "error", "detail": r.get("error")})
                continue
            post = check_postcondition(contract, args, r["value"])
            if not post["satisfied"]:
                violations.append({"args": args, "kind": "postcondition",
                                   "detail": post["violations"], "value": r["value"]})
        return {"contract": contract.get("name"), "cases": len(cases),
                "holds": not violations, "violations": violations[:10]}

    def cases_from_contract(contract, schema, count=20, seed=0):
        from swarm_engine.primitives.families_meta import register_testing  # noqa
        import random as _r
        rng = _r.Random(seed)
        gen = {"int": lambda: rng.randint(-100, 100), "num": lambda: rng.uniform(-100, 100),
               "float": lambda: rng.uniform(-100, 100), "str": lambda: "x" * rng.randint(0, 10),
               "bool": lambda: rng.choice([True, False]),
               "list": lambda: [rng.randint(0, 50) for _ in range(rng.randint(0, 6))],
               "dict": lambda: {}}
        cases = []
        while len(cases) < int(count) * 5 and len([c for c in cases if c]) < int(count):
            args = {p: gen.get(k, lambda: None)() for p, k in schema.items()}
            if check_precondition(contract, args)["satisfied"]:
                cases.append({"args": args})
            if len(cases) >= int(count):
                break
        return cases[: int(count)]

    def strengthen(contract, additional_requires):
        out = copy.deepcopy(contract)
        out["requires"] = list(out.get("requires", [])) + list(additional_requires)
        return out

    def weaken(contract, drop_descriptions):
        out = copy.deepcopy(contract)
        out["requires"] = [c for c in out.get("requires", [])
                           if c["description"] not in drop_descriptions]
        return out

    for name, fn, inp, out, ctxflag, doc in [
        ("define_contract", define_contract, {"name": STR, "requires": OPT(LIST()), "ensures": OPT(LIST()), "invariants": OPT(LIST())}, DICT(), False, "Build a pre/post condition contract."),
        ("check_precondition", check_precondition, {"contract": DICT(), "args": DICT()}, DICT(), False, "Do the inputs satisfy the contract's domain?"),
        ("check_postcondition", check_postcondition, {"contract": DICT(), "args": DICT(), "result": ANY}, DICT(), False, "Does the result satisfy the contract's guarantees?"),
        ("verify_contract", verify_contract, {"plan": DICT(), "contract": DICT(), "cases": LIST(DICT())}, DICT(), True, "Check a plan against a contract over many inputs."),
        ("cases_from_contract", cases_from_contract, {"contract": DICT(), "schema": DICT(), "count": OPT(INT), "seed": OPT(INT)}, LIST(DICT()), False, "Generate inputs inside a contract's domain."),
        ("strengthen", strengthen, {"contract": DICT(), "additional_requires": LIST()}, DICT(), False, "Narrow a contract's domain."),
        ("weaken", weaken, {"contract": DICT(), "drop_descriptions": LIST(STR)}, DICT(), False, "Widen a contract's domain."),
    ]:
        _p(reg, name, F, fn, inp, out, needs_ctx=ctxflag, doc=doc)


# ===========================================================================
# 22. EXPLANATION
# ===========================================================================

def register_explanation(reg: PrimitiveRegistry) -> None:
    F = "explanation"

    def explain_plan(ctx, plan):
        """Narrate a plan in terms of what each step does — so a synthesized
        capability is auditable by a person, not just by the type checker."""
        lines = []
        params = plan.get("params") or {}
        if params:
            lines.append("Takes " + ", ".join(f"{k} ({v})" for k, v in params.items()) + ".")
        for step in plan.get("steps") or []:
            lines.append("  " + _describe_step(ctx, step, 0))
        out = plan.get("output")
        if isinstance(out, dict) and "$step" in out:
            lines.append(f"Returns the result of step {out['$step']!r}.")
        return "\n".join(lines)

    def _describe_step(ctx, step, indent):
        pad = "  " * indent
        if step.get("control"):
            c = step["control"]
            if c == "if":
                return f"{pad}[{step['id']}] branch on a condition"
            if c == "foreach":
                return f"{pad}[{step['id']}] for each element, run {len(step.get('body') or [])} step(s)"
            if c == "while":
                return f"{pad}[{step['id']}] repeat while a condition holds"
            if c == "try":
                return f"{pad}[{step['id']}] attempt {len(step.get('body') or [])} step(s), recover on failure"
            if c == "parallel":
                return f"{pad}[{step['id']}] run {len(step.get('branches') or {})} branches concurrently"
            return f"{pad}[{step['id']}] {c}"
        prim = ctx.registry.get(step.get("op", ""))
        doc = prim.doc if prim else "unknown primitive"
        return f"{pad}[{step['id']}] {step.get('op')}: {doc}"

    def trace_causality(trace, step_id=None):
        """Which primitive calls contributed to a result, in order."""
        if step_id is None:
            return [e["primitive"] for e in trace]
        chain, seen = [], False
        for e in reversed(trace):
            chain.append(e["primitive"])
            if e["primitive"] == step_id:
                seen = True
                break
        return list(reversed(chain)) if seen else []

    def attribution(trace):
        """Where the time actually went — the honest version of a profile."""
        totals: Dict[str, float] = {}
        for e in trace:
            totals[e["primitive"]] = totals.get(e["primitive"], 0.0) + e["ms"]
        total = sum(totals.values()) or 1.0
        return sorted(({"primitive": k, "ms": round(v, 3),
                        "share": round(v / total, 4)} for k, v in totals.items()),
                      key=lambda d: -d["ms"])

    def counterfactual(ctx, plan, removed_step_id):
        """What the plan would look like without a given step — the basis for
        'was this step load-bearing?'"""
        pruned = copy.deepcopy(plan)
        pruned["steps"] = [s for s in pruned.get("steps", []) if s.get("id") != removed_step_id]
        analysis = _composer(ctx).analyze(pruned)
        return {"removed": removed_step_id, "still_valid": analysis.ok,
                "errors": analysis.errors[:5], "plan": pruned}

    def explain_decision(decision, factors):
        ordered = sorted(factors.items(), key=lambda kv: -abs(kv[1]))
        return {"decision": decision,
                "primary_factor": ordered[0][0] if ordered else None,
                "factors": [{"name": k, "weight": v} for k, v in ordered]}

    def summarize_analysis(analysis):
        parts = [f"{len(analysis.get('primitives_used', []))} primitives"]
        if analysis.get("effects"):
            parts.append("requires " + ", ".join(analysis["effects"]))
        else:
            parts.append("side-effect free")
        parts.append(f"returns {analysis.get('output_type', 'any')}")
        status = "valid" if analysis.get("ok") else f"invalid ({len(analysis.get('errors', []))} errors)"
        return f"Plan is {status}: " + "; ".join(parts) + "."

    for name, fn, inp, out, ctxflag, doc in [
        ("explain_plan", explain_plan, {"plan": DICT()}, STR, True, "Narrate a plan step by step."),
        ("counterfactual", counterfactual, {"plan": DICT(), "removed_step_id": STR}, DICT(), True, "Would the plan still hold without this step?"),
        ("trace_causality", trace_causality, {"trace": LIST(DICT()), "step_id": OPT(STR)}, LIST(STR), False, "Ordered chain of contributing calls."),
        ("attribution", attribution, {"trace": LIST(DICT())}, LIST(DICT()), False, "Time attributed per primitive."),
        ("explain_decision", explain_decision, {"decision": ANY, "factors": DICT()}, DICT(), False, "Rank the factors behind a choice."),
        ("summarize_analysis", summarize_analysis, {"analysis": DICT()}, STR, False, "One-line verdict on a plan analysis."),
    ]:
        _p(reg, name, F, fn, inp, out, needs_ctx=ctxflag, doc=doc)


# ===========================================================================
# 23. METALEARNING
# ===========================================================================

def register_metalearning(reg: PrimitiveRegistry) -> None:
    F = "metalearning"

    def record_experience(ctx, domain, plan_id, outcome, features=None):
        store = ctx.scratch.setdefault("_experience", [])
        store.append({"domain": domain, "plan_id": plan_id, "outcome": bool(outcome),
                      "features": features or {}, "at": time.time()})
        return {"recorded": len(store)}

    def recall_experience(ctx, domain=None, limit=100):
        store = ctx.scratch.get("_experience", [])
        rows = [e for e in store if domain is None or e["domain"] == domain]
        return rows[-int(limit):]

    def best_known(ctx, domain):
        """Which plan has actually worked most often in this domain — the basis
        for biasing future synthesis toward what already succeeds."""
        rows = recall_experience(ctx, domain, 10_000)
        tally: Dict[str, Dict[str, int]] = {}
        for e in rows:
            t = tally.setdefault(e["plan_id"], {"wins": 0, "tries": 0})
            t["tries"] += 1
            t["wins"] += 1 if e["outcome"] else 0
        ranked = sorted(({"plan_id": k, "rate": v["wins"] / v["tries"], **v}
                         for k, v in tally.items()),
                        key=lambda d: (-d["rate"], -d["tries"]))
        return {"domain": domain, "candidates": ranked[:10],
                "best": ranked[0]["plan_id"] if ranked else None}

    def which_primitives_help(ctx, domain):
        """Correlate primitive usage with success — a crude but real prior over
        the vocabulary for the given domain."""
        rows = recall_experience(ctx, domain, 10_000)
        stats: Dict[str, Dict[str, int]] = {}
        for e in rows:
            for prim in (e.get("features") or {}).get("primitives", []):
                s = stats.setdefault(prim, {"wins": 0, "tries": 0})
                s["tries"] += 1
                s["wins"] += 1 if e["outcome"] else 0
        return sorted(({"primitive": k, "success_rate": round(v["wins"] / v["tries"], 3), **v}
                       for k, v in stats.items() if v["tries"] >= 2),
                      key=lambda d: -d["success_rate"])

    def curriculum(cases, difficulty_key="difficulty"):
        return sorted(cases, key=lambda c: c.get(difficulty_key, 0))

    def few_shot_adapt(base_plan, examples, literal_map):
        """Specialize a known-good plan to a new instance from a couple of
        examples, rather than synthesizing from scratch."""
        text = json.dumps(base_plan)
        for old, new in literal_map.items():
            text = text.replace(json.dumps(old), json.dumps(new))
        out = json.loads(text)
        out["name"] = base_plan.get("name", "plan") + "_adapted"
        out.setdefault("provenance", {})["adapted_from"] = base_plan.get("name")
        out["provenance"]["examples"] = len(examples)
        return out

    def active_query(candidates, uncertainty_key="score"):
        """Which case would be most informative to test next: the one the engine
        is least sure about."""
        if not candidates:
            return None
        return min(candidates, key=lambda c: abs(c.get(uncertainty_key, 0.5) - 0.5))

    for name, fn, inp, out, ctxflag, eff, doc in [
        ("record_experience", record_experience, {"domain": STR, "plan_id": STR, "outcome": BOOL, "features": OPT(DICT())}, DICT(), True, (Effect.MEMORY,), "Log a plan's result in a domain."),
        ("recall_experience", recall_experience, {"domain": OPT(STR), "limit": OPT(INT)}, LIST(DICT()), True, (Effect.MEMORY,), "Retrieve past outcomes."),
        ("best_known", best_known, {"domain": STR}, DICT(), True, (Effect.MEMORY,), "Highest-performing plan for a domain."),
        ("which_primitives_help", which_primitives_help, {"domain": STR}, LIST(DICT()), True, (Effect.MEMORY,), "Primitives correlated with success."),
        ("curriculum", curriculum, {"cases": LIST(DICT()), "difficulty_key": OPT(STR)}, LIST(DICT()), False, (Effect.PURE,), "Order cases easiest-first."),
        ("few_shot_adapt", few_shot_adapt, {"base_plan": DICT(), "examples": LIST(), "literal_map": DICT()}, DICT(), False, (Effect.PURE,), "Specialize a known plan from examples."),
        ("active_query", active_query, {"candidates": LIST(DICT()), "uncertainty_key": OPT(STR)}, ANY, False, (Effect.PURE,), "Pick the most informative next test."),
    ]:
        _p(reg, name, F, fn, inp, out, effects=eff, needs_ctx=ctxflag, doc=doc)


# ===========================================================================
# 24. VERSION  (plan-level evolution and rollback)
# ===========================================================================

def register_version(reg: PrimitiveRegistry) -> None:
    F = "version"

    def diff_plans(a, b):
        ids_a = {s.get("id"): s for s in a.get("steps", [])}
        ids_b = {s.get("id"): s for s in b.get("steps", [])}
        return {
            "added": sorted(set(ids_b) - set(ids_a)),
            "removed": sorted(set(ids_a) - set(ids_b)),
            "changed": sorted(k for k in set(ids_a) & set(ids_b) if ids_a[k] != ids_b[k]),
            "params_changed": (a.get("params") or {}) != (b.get("params") or {}),
        }

    def snapshot(ctx, plan, label=""):
        history = ctx.scratch.setdefault("_versions", [])
        entry = {"index": len(history), "label": label or f"v{len(history)}",
                 "plan": copy.deepcopy(plan), "at": time.time()}
        history.append(entry)
        return {"index": entry["index"], "label": entry["label"], "total": len(history)}

    def list_versions(ctx):
        return [{"index": e["index"], "label": e["label"], "at": e["at"]}
                for e in ctx.scratch.get("_versions", [])]

    def rollback(ctx, index=None, label=None):
        history = ctx.scratch.get("_versions", [])
        if not history:
            return {"rolled_back": False, "reason": "no snapshots"}
        entry = None
        if label is not None:
            entry = next((e for e in reversed(history) if e["label"] == label), None)
        elif index is not None:
            entry = history[int(index)] if -len(history) <= index < len(history) else None
        else:
            entry = history[-1]
        if entry is None:
            return {"rolled_back": False, "reason": "version not found"}
        return {"rolled_back": True, "label": entry["label"], "plan": entry["plan"]}

    def tag_version(ctx, index, tag):
        history = ctx.scratch.get("_versions", [])
        if not (-len(history) <= int(index) < len(history)):
            return {"tagged": False}
        history[int(index)]["label"] = tag
        return {"tagged": True, "index": int(index), "label": tag}

    def merge_plans(base, incoming, prefer="incoming"):
        """Union two plans' steps by id; conflicting ids resolve by preference.
        Used when two improvement branches both produced viable revisions."""
        out = copy.deepcopy(base)
        by_id = {s.get("id"): i for i, s in enumerate(out.get("steps", []))}
        for step in incoming.get("steps", []):
            sid = step.get("id")
            if sid in by_id:
                if prefer == "incoming":
                    out["steps"][by_id[sid]] = copy.deepcopy(step)
            else:
                out["steps"].append(copy.deepcopy(step))
        out["params"] = {**(base.get("params") or {}), **(incoming.get("params") or {})}
        return out

    for name, fn, inp, out, ctxflag, eff, doc in [
        ("snapshot", snapshot, {"plan": DICT(), "label": OPT(STR)}, DICT(), True, (Effect.MEMORY,), "Record a recoverable version of a plan."),
        ("list_versions", list_versions, {}, LIST(DICT()), True, (Effect.MEMORY,), "Enumerate snapshots."),
        ("rollback", rollback, {"index": OPT(INT), "label": OPT(STR)}, DICT(), True, (Effect.MEMORY,), "Restore a previous plan version."),
        ("tag_version", tag_version, {"index": INT, "tag": STR}, DICT(), True, (Effect.MEMORY,), "Name a snapshot."),
        ("diff_plans", diff_plans, {"a": DICT(), "b": DICT()}, DICT(), False, (Effect.PURE,), "Step-level difference between two plans."),
        ("merge_plans", merge_plans, {"base": DICT(), "incoming": DICT(), "prefer": OPT(STR)}, DICT(), False, (Effect.PURE,), "Combine two plan revisions."),
    ]:
        _p(reg, name, F, fn, inp, out, effects=eff, needs_ctx=ctxflag, doc=doc)


# ===========================================================================
# 25. COMPOSE  (meta-primitives: build plans out of plans)
# ===========================================================================

def register_compose(reg: PrimitiveRegistry) -> None:
    F = "compose"

    def step(id, op, args=None):
        return {"id": id, "op": op, "args": args or {}}

    def plan(steps, name="plan", params=None, output=None):
        p = {"name": name, "steps": steps, "params": params or {}}
        if output is not None:
            p["output"] = output
        return p

    def ref(step_id):
        return {"$step": step_id}

    def param(name):
        return {"$param": name}

    def lambda_(params, steps, output):
        return {"$lambda": {"params": params, "steps": steps, "output": output}}

    def pipeline(ops, input_ref, name="pipeline"):
        """Thread a value through a list of single-input primitives. The most
        common composition shape, expressed once."""
        steps, prev = [], input_ref
        for i, op in enumerate(ops):
            op_name, arg_name = (op if isinstance(op, (list, tuple)) else (op, "value"))
            sid = f"s{i}"
            steps.append({"id": sid, "op": op_name, "args": {arg_name: prev}})
            prev = {"$step": sid}
        return plan(steps, name, output=prev)

    def sequence(plans, name="sequence"):
        """Concatenate plans, renaming steps so ids never collide."""
        steps, last = [], None
        for i, sub in enumerate(plans):
            mapping = {s["id"]: f"p{i}_{s['id']}" for s in sub.get("steps", [])}
            text = json.dumps(sub.get("steps", []))
            for old, new in mapping.items():
                text = text.replace(json.dumps({"$step": old}), json.dumps({"$step": new}))
            renamed = json.loads(text)
            for s, orig in zip(renamed, sub.get("steps", [])):
                s["id"] = mapping[orig["id"]]
            steps.extend(renamed)
            last = steps[-1]["id"] if steps else None
        merged_params = {}
        for sub in plans:
            merged_params.update(sub.get("params") or {})
        return plan(steps, name, merged_params, {"$step": last} if last else None)

    def branch(condition_ref, then_steps, else_steps, id="branch"):
        return {"id": id, "control": "if", "cond": condition_ref,
                "then": then_steps, "else": else_steps}

    def loop_over(items_ref, body_steps, yield_ref, as_="item", id="loop"):
        return {"id": id, "control": "foreach", "items": items_ref, "as": as_,
                "body": body_steps, "yield": yield_ref}

    def guarded(body_steps, fallback_steps, id="guarded"):
        return {"id": id, "control": "try", "body": body_steps, "catch": fallback_steps}

    def parallel_branches(branches, id="parallel"):
        return {"id": id, "control": "parallel", "branches": branches}

    def bind_params(plan_obj, bindings):
        """Partially apply a plan: fix some parameters to literals, leaving a
        narrower capability behind."""
        text = json.dumps(plan_obj)
        params = dict(plan_obj.get("params") or {})
        for name, value in bindings.items():
            text = text.replace(json.dumps({"$param": name}), json.dumps(value))
            params.pop(name, None)
        out = json.loads(text)
        out["params"] = params
        return out

    for name, fn, inp, out, doc in [
        ("step", step, {"id": STR, "op": STR, "args": OPT(DICT())}, DICT(), "Build one plan step."),
        ("plan", plan, {"steps": LIST(DICT()), "name": OPT(STR), "params": OPT(DICT()), "output": OPT(ANY)}, DICT(), "Assemble steps into a plan."),
        ("ref", ref, {"step_id": STR}, DICT(), "Reference an earlier step's result."),
        ("param", param, {"name": STR}, DICT(), "Reference a plan parameter."),
        ("lambda", lambda_, {"params": LIST(STR), "steps": LIST(DICT()), "output": ANY}, DICT(), "Build an inline callable sub-plan."),
        ("pipeline", pipeline, {"ops": LIST(), "input_ref": ANY, "name": OPT(STR)}, DICT(), "Chain single-input primitives over a value."),
        ("sequence", sequence, {"plans": LIST(DICT()), "name": OPT(STR)}, DICT(), "Concatenate plans with id remapping."),
        ("branch", branch, {"condition_ref": ANY, "then_steps": LIST(DICT()), "else_steps": LIST(DICT()), "id": OPT(STR)}, DICT(), "Conditional control step."),
        ("loop_over", loop_over, {"items_ref": ANY, "body_steps": LIST(DICT()), "yield_ref": ANY, "as_": OPT(STR), "id": OPT(STR)}, DICT(), "Iteration control step."),
        ("guarded", guarded, {"body_steps": LIST(DICT()), "fallback_steps": LIST(DICT()), "id": OPT(STR)}, DICT(), "Try/recover control step."),
        ("parallel_branches", parallel_branches, {"branches": DICT(), "id": OPT(STR)}, DICT(), "Concurrent control step."),
        ("bind_params", bind_params, {"plan_obj": DICT(), "bindings": DICT()}, DICT(), "Partially apply a plan's parameters."),
    ]:
        _p(reg, name, F, fn, inp, out, doc=doc)


# ===========================================================================
# 26. AGENT
# ===========================================================================

def register_agent(reg: PrimitiveRegistry) -> None:
    F = "agent"
    SP = (Effect.SPAWN,)

    def _bus(ctx):
        return ctx.scratch.setdefault("_agent_bus", {"agents": {}, "messages": [], "next_id": 1})

    def spawn_agent(ctx, role, capabilities=None):
        bus = _bus(ctx)
        aid = f"agent_{bus['next_id']}"
        bus["next_id"] += 1
        bus["agents"][aid] = {"id": aid, "role": role, "status": "idle",
                              "capabilities": list(capabilities or []), "completed": 0}
        return {"agent_id": aid, "role": role}

    def list_agents(ctx, role=None):
        bus = _bus(ctx)
        return [a for a in bus["agents"].values() if role is None or a["role"] == role]

    def inspect_agent(ctx, agent_id):
        return _bus(ctx)["agents"].get(agent_id, {"found": False, "agent_id": agent_id})

    def find_capable_agent(ctx, capability):
        bus = _bus(ctx)
        for a in bus["agents"].values():
            if capability in a["capabilities"] and a["status"] == "idle":
                return a
        return None

    async def assign_task(ctx, agent_id, plan, args=None):
        bus = _bus(ctx)
        agent = bus["agents"].get(agent_id)
        if agent is None:
            raise KeyError(f"unknown agent {agent_id!r}")
        agent["status"] = "working"
        try:
            result = await _composer(ctx).execute(plan, args or {}, ctx.child())
        finally:
            agent["status"] = "idle"
            agent["completed"] += 1
        return {"agent_id": agent_id, **result}

    async def delegate(ctx, capability, plan, args=None):
        """Route work to a capable agent, spawning one if none is free."""
        agent = find_capable_agent(ctx, capability)
        if agent is None:
            agent = _bus(ctx)["agents"][spawn_agent(ctx, capability, [capability])["agent_id"]]
        return await assign_task(ctx, agent["id"], plan, args)

    def send_message(ctx, to, content, sender="engine"):
        bus = _bus(ctx)
        msg = {"id": len(bus["messages"]), "to": to, "from": sender,
               "content": content, "at": time.time(), "read": False}
        bus["messages"].append(msg)
        return {"message_id": msg["id"], "delivered": True}

    def receive_messages(ctx, agent_id, mark_read=True):
        bus = _bus(ctx)
        inbox = [m for m in bus["messages"] if m["to"] in (agent_id, "*") and not m["read"]]
        if mark_read:
            for m in inbox:
                m["read"] = True
        return inbox

    def broadcast(ctx, content, sender="engine"):
        return send_message(ctx, "*", content, sender)

    async def coordinate(ctx, assignments):
        """Run several agent/plan pairs concurrently and collect the results —
        the multi-agent shape that isn't just a loop."""
        async def one(a):
            return await assign_task(ctx, a["agent_id"], a["plan"], a.get("args"))
        results = await asyncio.gather(*(one(a) for a in assignments), return_exceptions=True)
        return {"completed": sum(1 for r in results if not isinstance(r, Exception)),
                "results": [r if not isinstance(r, Exception) else {"success": False,
                                                                    "error": str(r)}
                            for r in results]}

    def vote(votes, threshold=0.5):
        tally: Dict[str, int] = {}
        for v in votes:
            key = json.dumps(v, sort_keys=True, default=str)
            tally[key] = tally.get(key, 0) + 1
        if not tally:
            return {"decided": False}
        best, count = max(tally.items(), key=lambda kv: kv[1])
        share = count / len(votes)
        return {"decided": share >= threshold, "winner": json.loads(best),
                "share": round(share, 3), "votes": len(votes)}

    def terminate_agent(ctx, agent_id):
        bus = _bus(ctx)
        return {"agent_id": agent_id, "terminated": bus["agents"].pop(agent_id, None) is not None}

    for name, fn, inp, out, doc in [
        ("spawn_agent", spawn_agent, {"role": STR, "capabilities": OPT(LIST(STR))}, DICT(), "Create a worker with declared capabilities."),
        ("list_agents", list_agents, {"role": OPT(STR)}, LIST(DICT()), "Enumerate live agents."),
        ("inspect_agent", inspect_agent, {"agent_id": STR}, DICT(), "Status and history of one agent."),
        ("assign_task", assign_task, {"agent_id": STR, "plan": DICT(), "args": OPT(DICT())}, DICT(), "Hand a plan to a specific agent."),
        ("delegate", delegate, {"capability": STR, "plan": DICT(), "args": OPT(DICT())}, DICT(), "Route a plan to any capable agent."),
        ("coordinate", coordinate, {"assignments": LIST(DICT())}, DICT(), "Run several agent tasks concurrently."),
        ("send_message", send_message, {"to": STR, "content": ANY, "sender": OPT(STR)}, DICT(), "Post a message to an agent."),
        ("receive_messages", receive_messages, {"agent_id": STR, "mark_read": OPT(BOOL)}, LIST(DICT()), "Drain an agent's inbox."),
        ("broadcast", broadcast, {"content": ANY, "sender": OPT(STR)}, DICT(), "Message every agent."),
        ("terminate_agent", terminate_agent, {"agent_id": STR}, DICT(), "Remove an agent."),
    ]:
        _p(reg, name, F, fn, inp, out, effects=SP, needs_ctx=True, doc=doc)

    _p(reg, "vote", F, vote, {"votes": LIST(), "threshold": OPT(NUM)}, DICT(),
       doc="Majority decision over agent proposals.")


def register_all_meta(reg: PrimitiveRegistry) -> None:
    register_capability(reg)
    register_testing(reg)
    register_optimization(reg)
    register_improvement(reg)
    register_contracts(reg)
    register_explanation(reg)
    register_metalearning(reg)
    register_version(reg)
    register_compose(reg)
    register_agent(reg)
