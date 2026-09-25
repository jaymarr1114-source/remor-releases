"""
swarm_engine/synthesis/composer.py

The grammar over the primitive vocabulary.

A Plan is data, not code. That is the whole point: a plan can be type-checked,
effect-checked, hashed, stored in the KnowledgeBase, reloaded, diffed against a
candidate revision and rolled back — none of which is possible with a blob of
generated Python that has to be exec'd to find out what it does. The
VerificationPipeline's AST gate exists because the old synthesizer emitted
source; this composer removes the need to emit source at all.

Plan shape
----------
    {
      "name":   "weekly_report",
      "params": {"rows": "list", "threshold": "num"},
      "steps":  [ <step>, ... ],
      "output": <ref>
    }

Step forms
----------
    {"id": "s1", "op": "computation.mean", "args": {...}}
    {"id": "s2", "control": "if",       "cond": <ref>, "then": [...], "else": [...]}
    {"id": "s3", "control": "foreach",  "items": <ref>, "as": "row", "body": [...], "yield": <ref>}
    {"id": "s4", "control": "while",    "cond": [...], "body": [...], "max_iterations": 100}
    {"id": "s5", "control": "try",      "body": [...], "catch": [...]}
    {"id": "s6", "control": "parallel", "branches": {"a": [...], "b": [...]}}
    {"id": "s7", "control": "let",      "bindings": {"k": <ref>}}

Reference forms (anywhere an argument value is expected)
--------------------------------------------------------
    5                       literal
    {"$step":  "s1"}        result of an earlier step
    {"$param": "rows"}      a plan parameter
    {"$var":   "row"}       a loop/let variable
    {"$lambda": {"params": ["x"], "steps": [...], "output": <ref>}}
                            a nested plan compiled to a callable, so higher-order
                            primitives (map/filter/reduce/parallel_map) compose
    {"$list": [<ref>, ...]} / {"$dict": {k: <ref>}}   structural literals
    {"$tuple": [<ref>, ...]}  tuple literal: builds a real Python tuple from
                            the resolved elements, so a plan can assemble a
                            fixed-arity tuple (e.g. pairing sub-results) the
                            same way $list builds a list. The static checker
                            has no tuple kind in its type system, so a $tuple
                            ref types as ANY; element refs are still checked.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarm_engine.primitives.core import (
    ANY, BOOL, CALLABLE, DICT, LIST, NUM, STR, Effect, ExecContext, Kind,
    PrimitiveRegistry, TypeSpec, infer,
)
from swarm_engine.services.run_control import (
    RunStopped,
    checkpoint,
    current as _current_run_control,
    set_current as _set_current_run_control,
)

CONTROL_OPS = {"if", "foreach", "while", "try", "parallel", "let"}
_KINDS = {"int": Kind.INT, "float": Kind.FLOAT, "num": Kind.NUM, "str": Kind.STR,
          "bool": Kind.BOOL, "list": Kind.LIST, "dict": Kind.DICT, "any": Kind.ANY,
          "callable": Kind.CALLABLE, "bytes": Kind.BYTES, "none": Kind.NONE}


def _spec(name: str) -> TypeSpec:
    return TypeSpec(_KINDS.get(str(name).split("[")[0].lower(), Kind.ANY))


class PlanError(Exception):
    """Raised when a plan is structurally invalid, ill-typed, or over-privileged."""


# ---------------------------------------------------------------------------
# STATIC ANALYSIS
# ---------------------------------------------------------------------------

@dataclass
class PlanAnalysis:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    output_type: TypeSpec = ANY
    effects: set = field(default_factory=set)
    primitives_used: List[str] = field(default_factory=list)
    step_types: Dict[str, TypeSpec] = field(default_factory=dict)
    depth: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "output_type": str(self.output_type),
                "effects": sorted(e.value for e in self.effects),
                "primitives_used": self.primitives_used, "depth": self.depth}


class PlanChecker:
    """Walks a plan without executing it: resolves every reference, infers the
    type of every step, and collects the union of effects the plan will demand.
    A plan that fails here never reaches the executor."""

    def __init__(self, registry: PrimitiveRegistry, max_steps: int = 2000):
        self.reg = registry
        self.max_steps = max_steps

    def check(self, plan: Dict[str, Any]) -> PlanAnalysis:
        a = PlanAnalysis(ok=True)
        if not isinstance(plan, dict):
            return PlanAnalysis(False, ["plan must be an object"])
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            return PlanAnalysis(False, ["plan.steps must be a non-empty list"])

        scope: Dict[str, TypeSpec] = {}
        params = {k: _spec(v) for k, v in (plan.get("params") or {}).items()}
        self._walk(steps, scope, params, a, depth=0)

        out_ref = plan.get("output")
        if out_ref is None:
            last = steps[-1]
            out_ref = {"$step": last.get("id")} if last.get("id") else None
        if out_ref is not None:
            a.output_type = self._ref_type(out_ref, scope, params, a)
        a.ok = not a.errors
        return a

    # -- internals ----------------------------------------------------------
    def _walk(self, steps, scope, params, a: PlanAnalysis, depth: int) -> None:
        a.depth = max(a.depth, depth)
        if depth > 12:
            a.errors.append("plan nesting exceeds depth 12")
            return
        for step in steps:
            if not isinstance(step, dict):
                a.errors.append(f"step must be an object, got {type(step).__name__}")
                continue
            sid = step.get("id")
            if not sid:
                a.errors.append("every step needs an 'id'")
                continue
            if sid in scope:
                a.errors.append(f"duplicate step id {sid!r}")

            control = step.get("control")
            if control:
                self._walk_control(step, control, sid, scope, params, a, depth)
                continue

            op = step.get("op")
            if not op:
                a.errors.append(f"step {sid!r} has neither 'op' nor 'control'")
                continue
            prim = self.reg.get(op)
            if prim is None:
                a.errors.append(f"step {sid!r}: unknown primitive {op!r}")
                scope[sid] = ANY
                continue
            if op not in a.primitives_used:
                a.primitives_used.append(op)
            a.effects.update(e for e in prim.effects if e is not Effect.PURE)

            args = step.get("args") or {}
            if not isinstance(args, dict):
                a.errors.append(f"step {sid!r}: args must be an object")
                scope[sid] = prim.output
                continue

            for pname, pspec in prim.inputs.items():
                if pname not in args:
                    if not pspec.optional:
                        a.errors.append(
                            f"step {sid!r} ({op}): missing required argument {pname!r}: {pspec}")
                    continue
                got = self._ref_type(args[pname], scope, params, a, context=f"{sid}.{pname}")
                if not pspec.accepts(got) and got.kind is not Kind.ANY:
                    a.errors.append(
                        f"step {sid!r} ({op}): argument {pname!r} expects {pspec}, "
                        f"plan supplies {got}")
            extra = set(args) - set(prim.inputs)
            if extra and not prim.variadic:
                a.warnings.append(f"step {sid!r} ({op}): ignoring unexpected args {sorted(extra)}")
            scope[sid] = prim.output

    def _walk_control(self, step, control, sid, scope, params, a, depth) -> None:
        if control not in CONTROL_OPS:
            a.errors.append(f"step {sid!r}: unknown control {control!r}")
            scope[sid] = ANY
            return

        if control == "if":
            if "cond" not in step:
                a.errors.append(f"step {sid!r}: 'if' requires 'cond'")
            else:
                self._ref_type(step["cond"], scope, params, a, context=f"{sid}.cond")
            inner_a = {}
            branch_types = []
            for key in ("then", "else"):
                body = step.get(key) or []
                sub = dict(scope)
                self._walk(body, sub, params, a, depth + 1)
                if body:
                    branch_types.append(sub.get(body[-1].get("id"), ANY))
                scope.update({k: v for k, v in sub.items() if k not in scope})
            scope[sid] = branch_types[0] if len(set(map(str, branch_types))) == 1 and branch_types else ANY

        elif control == "foreach":
            items_t = self._ref_type(step.get("items"), scope, params, a, context=f"{sid}.items")
            if items_t.kind not in (Kind.LIST, Kind.ANY, Kind.DICT):
                a.errors.append(f"step {sid!r}: 'foreach' needs a list, got {items_t}")
            elem = items_t.args[0] if (items_t.kind is Kind.LIST and items_t.args) else ANY
            sub = dict(scope)
            sub[step.get("as", "item")] = elem
            sub["_index"] = TypeSpec(Kind.INT)
            self._walk(step.get("body") or [], sub, params, a, depth + 1)
            if "yield" in step:
                y = self._ref_type(step["yield"], sub, params, a, context=f"{sid}.yield")
                scope[sid] = LIST(y)
            else:
                scope[sid] = LIST(ANY)

        elif control == "while":
            sub = dict(scope)
            self._walk(step.get("cond") or [], sub, params, a, depth + 1)
            self._walk(step.get("body") or [], sub, params, a, depth + 1)
            if not step.get("max_iterations"):
                a.warnings.append(f"step {sid!r}: 'while' without max_iterations "
                                  f"defaults to 1000 and may spin")
            scope[sid] = ANY

        elif control == "try":
            sub = dict(scope)
            self._walk(step.get("body") or [], sub, params, a, depth + 1)
            sub["_error"] = STR
            self._walk(step.get("catch") or [], sub, params, a, depth + 1)
            scope[sid] = ANY

        elif control == "parallel":
            branches = step.get("branches") or {}
            if not isinstance(branches, dict):
                a.errors.append(f"step {sid!r}: 'parallel' branches must be a mapping")
                branches = {}
            for _, body in branches.items():
                sub = dict(scope)
                self._walk(body or [], sub, params, a, depth + 1)
            a.effects.add(Effect.SPAWN)
            scope[sid] = DICT()

        elif control == "let":
            for name, ref in (step.get("bindings") or {}).items():
                scope[name] = self._ref_type(ref, scope, params, a, context=f"{sid}.{name}")
            scope[sid] = ANY

    def _ref_type(self, ref, scope, params, a: PlanAnalysis, context: str = "") -> TypeSpec:
        if isinstance(ref, dict) and len(ref) == 1:
            key, val = next(iter(ref.items()))
            if key == "$step":
                if val not in scope:
                    a.errors.append(f"{context or 'reference'}: step {val!r} is not defined "
                                    f"before use")
                    return ANY
                return scope[val]
            if key == "$param":
                if val not in params:
                    a.errors.append(f"{context or 'reference'}: unknown plan parameter {val!r}")
                    return ANY
                return params[val]
            if key == "$var":
                if val not in scope:
                    a.errors.append(f"{context or 'reference'}: unbound variable {val!r}")
                    return ANY
                return scope[val]
            if key == "$lambda":
                sub_params = {p: ANY for p in (val.get("params") or [])}
                sub_scope = dict(scope)
                sub_scope.update(sub_params)
                self._walk(val.get("steps") or [], sub_scope, params, a, a.depth + 1)
                return CALLABLE
            if key == "$partial":
                # Registry-derived partial application → CALLABLE (M+16)
                op = (val or {}).get("op")
                if not op or self.reg.get(op) is None:
                    a.errors.append(
                        f"{context or 'reference'}: $partial op {op!r} not in registry")
                return CALLABLE
            if key == "$list":
                ts = [self._ref_type(v, scope, params, a, context) for v in val]
                return LIST(ts[0]) if ts and len({str(t) for t in ts}) == 1 else LIST(ANY)
            if key == "$dict":
                for v in val.values():
                    self._ref_type(v, scope, params, a, context)
                return DICT()
            if key == "$tuple":
                # Tuple assembly for composed multi-part results. The type
                # system has no tuple kind, so this is ANY by necessity --
                # but every element ref is still type-checked above, so a
                # dangling $step/$param inside the tuple is still an error.
                for v in val:
                    self._ref_type(v, scope, params, a, context)
                return ANY
        return infer(ref)


# ---------------------------------------------------------------------------
# EXECUTION
# ---------------------------------------------------------------------------

class PlanExecutor:
    """Interprets a checked plan. Never uses exec/eval — a plan is walked, not
    compiled, so nothing the synthesizer produces can escape the registry."""

    def __init__(self, registry: PrimitiveRegistry):
        self.reg = registry

    async def run(self, plan: Dict[str, Any], args: Optional[Dict[str, Any]] = None,
                  ctx: Optional[ExecContext] = None) -> Dict[str, Any]:
        # Cooperation point: a stop requested before/while a plan executes
        # takes effect here. No-op when no control is installed.
        checkpoint("execute:plan")
        ctx = ctx or ExecContext(self.reg.governor, self.reg)
        env: Dict[str, Any] = {"__params__": dict(args or {})}
        started = _now()
        try:
            steps = plan.get("steps") or []
            if steps and any(s.get("control") for s in steps):
                await self._exec_steps(steps, env, ctx)
            else:
                await self._exec_steps_lazy(plan, env, ctx)
        except RunStopped:
            # Cooperative stop: must propagate, never be reported as a
            # normal execution failure.
            raise
        except Exception as exc:  # surfaced, never swallowed
            return {"success": False, "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": _ms(started), "trace": ctx.trace[-25:],
                    "primitive_calls": len(ctx.trace)}

        out_ref = plan.get("output")
        if out_ref is None:
            steps = plan.get("steps") or []
            out_ref = {"$step": steps[-1].get("id")} if steps else None
        try:
            value = await self._resolve(out_ref, env, ctx) if out_ref is not None else None
        except RunStopped:
            # Cooperative stop: must propagate, never be reported as a
            # normal execution failure.
            raise
        except Exception as exc:
            return {"success": False, "error": f"output resolution failed: {exc}",
                    "elapsed_ms": _ms(started)}
        return {"success": True, "value": value, "elapsed_ms": _ms(started),
                "primitive_calls": len(ctx.trace)}

    # -- steps --------------------------------------------------------------
    async def _exec_steps_lazy(self, plan, env, ctx) -> None:
        """Evaluate only steps demanded by the output, short-circuiting if_else.

        Linear execution of synthesized if_else plans evaluates both
        branches, which is illegal when branches have incompatible
        input types (type/shape dispatch). Lazy + short-circuit is the
        generic fix; control-flow plans still use _exec_steps.
        """
        steps = plan.get("steps") or []
        by_id = {s.get("id"): s for s in steps if s.get("id")}

        async def resolve(ref):
            return await self._resolve(ref, env, ctx)

        async def ensure(sid):
            if sid in env:
                return env[sid]
            # Cooperation point: one per evaluated step (each step runs at
            # most once thanks to env caching). No-op without a control.
            checkpoint("execute:step")
            step = by_id.get(sid)
            if step is None:
                raise PlanError(f"reference to undefined step {sid!r}")
            op = step.get("op")
            raw = step.get("args") or {}
            if op == "if_else":
                cond = await resolve_or_step(raw.get("condition"))
                branch = raw.get("then") if cond else raw.get("otherwise")
                env[sid] = await resolve_or_step(branch)
                return env[sid]
            kwargs = {}
            for k, v in raw.items():
                kwargs[k] = await resolve_or_step(v)
            env[sid] = await self.reg.invoke(op, ctx, **kwargs)
            return env[sid]

        async def resolve_or_step(ref):
            if isinstance(ref, dict) and len(ref) == 1:
                key, val = next(iter(ref.items()))
                if key in ("$step", "$var") and val in by_id and val not in env:
                    return await ensure(val)
            return await resolve(ref)

        out_ref = plan.get("output")
        if isinstance(out_ref, dict) and "$step" in out_ref:
            await ensure(out_ref["$step"])
        elif steps:
            await ensure(steps[-1]["id"])

    async def _exec_steps(self, steps, env, ctx) -> None:
        # Cooperation point (throttled): plans with many steps or
        # control-flow loops stay stoppable mid-execution. No-op when no
        # control is installed.
        for idx, step in enumerate(steps):
            if idx % 64 == 0:
                checkpoint("execute:steps")
            sid = step.get("id")
            control = step.get("control")
            if control:
                env[sid] = await self._exec_control(step, control, env, ctx)
                continue
            op = step["op"]
            raw = step.get("args") or {}
            kwargs = {}
            for k, v in raw.items():
                kwargs[k] = await self._resolve(v, env, ctx)
            env[sid] = await self.reg.invoke(op, ctx, **kwargs)

    async def _exec_control(self, step, control, env, ctx):
        if control == "if":
            cond = await self._resolve(step["cond"], env, ctx)
            body = step.get("then" if cond else "else") or []
            await self._exec_steps(body, env, ctx)
            return env.get(body[-1]["id"]) if body else None

        if control == "foreach":
            items = await self._resolve(step.get("items"), env, ctx)
            var = step.get("as", "item")
            body = step.get("body") or []
            out = []
            seq = items.items() if isinstance(items, dict) else enumerate(items)
            for idx, item in seq:
                env[var] = item
                env["_index"] = idx
                await self._exec_steps(body, env, ctx)
                if "yield" in step:
                    out.append(await self._resolve(step["yield"], env, ctx))
                elif body:
                    out.append(env.get(body[-1]["id"]))
            return out

        if control == "while":
            cond_steps = step.get("cond") or []
            body = step.get("body") or []
            limit = int(step.get("max_iterations", 1000))
            iterations = 0
            while iterations < limit:
                await self._exec_steps(cond_steps, env, ctx)
                if not cond_steps or not env.get(cond_steps[-1]["id"]):
                    break
                await self._exec_steps(body, env, ctx)
                iterations += 1
            return {"iterations": iterations, "completed": iterations < limit}

        if control == "try":
            try:
                await self._exec_steps(step.get("body") or [], env, ctx)
                return {"ok": True}
            except Exception as exc:
                env["_error"] = f"{type(exc).__name__}: {exc}"
                await self._exec_steps(step.get("catch") or [], env, ctx)
                return {"ok": False, "error": env["_error"]}

        if control == "parallel":
            branches = step.get("branches") or {}

            async def branch(name, body):
                local = dict(env)
                await self._exec_steps(body, local, ctx.child())
                return name, (local.get(body[-1]["id"]) if body else None)

            results = await asyncio.gather(*(branch(n, b) for n, b in branches.items()))
            merged = dict(results)
            env.update({f"{step['id']}.{k}": v for k, v in merged.items()})
            return merged

        if control == "let":
            for name, ref in (step.get("bindings") or {}).items():
                env[name] = await self._resolve(ref, env, ctx)
            return None

        raise PlanError(f"unknown control {control!r}")

    # -- references ---------------------------------------------------------
    async def _resolve(self, ref, env, ctx):
        if isinstance(ref, dict) and len(ref) == 1:
            key, val = next(iter(ref.items()))
            if key == "$step" or key == "$var":
                if val not in env:
                    raise PlanError(f"reference to undefined {key[1:]} {val!r}")
                return env[val]
            if key == "$param":
                params = env["__params__"]
                if val not in params:
                    raise PlanError(f"missing plan parameter {val!r}")
                return params[val]
            if key == "$lambda":
                return self._make_callable(val, env, ctx)
            if key == "$partial":
                return self._make_partial(val, env, ctx)
            if key == "$list":
                return [await self._resolve(v, env, ctx) for v in val]
            if key == "$dict":
                return {k: await self._resolve(v, env, ctx) for k, v in val.items()}
            if key == "$tuple":
                return tuple([await self._resolve(v, env, ctx) for v in val])
        if isinstance(ref, list):
            return [await self._resolve(v, env, ctx) for v in ref]
        return ref

    def _make_callable(self, spec, env, ctx) -> Callable:
        """Compile a sub-plan into a Python callable so higher-order primitives
        (map, filter, reduce, parallel_map, memoize) can drive it. The returned
        function is sync-looking but returns a coroutine when the body needs
        one; the pure-path fast case keeps map/filter usable everywhere."""
        names = spec.get("params") or []
        steps = spec.get("steps") or []
        out_ref = spec.get("output") or ({"$step": steps[-1]["id"]} if steps else None)
        pure = self._is_pure(steps)

        def sync_call(*call_args):
            local = dict(env)
            for n, v in zip(names, call_args):
                local[n] = v
            coro = self._run_lambda(steps, out_ref, local, ctx)
            return _drive(coro)

        async def async_call(*call_args):
            local = dict(env)
            for n, v in zip(names, call_args):
                local[n] = v
            return await self._run_lambda(steps, out_ref, local, ctx)

        return sync_call if pure else async_call


    def _make_partial(self, spec, env, ctx) -> Callable:
        """Compile a registry-derived partial application into a CALLABLE.

        Spec form (serializable, no arbitrary code):
            {"op": <registered pure prim>, "bound": {arg: value, ...},
             "free": [<arg name>, ...]}

        Free parameters are supplied positionally by higher-order callers
        (e.g. map.fn). Bound arguments stay fixed. Only pure registered
        primitives are allowed — no eval/exec, no $lambda translation.
        """
        if not isinstance(spec, dict):
            raise PlanError("$partial spec must be a dict")
        op = spec.get("op")
        if not op or not isinstance(op, str):
            raise PlanError("$partial requires string op")
        prim = self.reg.get(op)
        if prim is None:
            raise PlanError(f"$partial op {op!r} is not registered")
        if not getattr(prim, "pure", False):
            raise PlanError(f"$partial op {op!r} is not pure")
        bound = dict(spec.get("bound") or {})
        free = list(spec.get("free") or [])
        if not free:
            raise PlanError("$partial requires at least one free parameter")
        # Reject unknown argument names
        known = set(prim.inputs.keys())
        for k in list(bound.keys()) + free:
            if k not in known:
                raise PlanError(
                    f"$partial argument {k!r} not in signature of {op!r}")
        # All required inputs must be either bound or free
        required = [k for k, v in prim.inputs.items()
                    if not getattr(v, "optional", False)]
        covered = set(bound.keys()) | set(free)
        missing = [k for k in required if k not in covered]
        if missing:
            raise PlanError(
                f"$partial op {op!r} missing required args {missing}")

        def sync_call(*call_args):
            if len(call_args) < len(free):
                raise PlanError(
                    f"$partial {op!r} expected {len(free)} free args, "
                    f"got {len(call_args)}")
            kwargs = dict(bound)
            for name, val in zip(free, call_args):
                kwargs[name] = val
            # Invoke registered primitive only
            if prim.needs_ctx:
                return prim.fn(ctx=ctx, **kwargs)
            return prim.fn(**kwargs)

        return sync_call

    async def _run_lambda(self, steps, out_ref, local, ctx):
        await self._exec_steps(steps, local, ctx)
        return await self._resolve(out_ref, local, ctx) if out_ref is not None else None

    def _is_pure(self, steps) -> bool:
        for s in steps:
            if s.get("control"):
                for key in ("then", "else", "body", "catch", "cond"):
                    if not self._is_pure(s.get(key) or []):
                        return False
                for b in (s.get("branches") or {}).values():
                    if not self._is_pure(b or []):
                        return False
                continue
            prim = self.reg.get(s.get("op", ""))
            if prim is None or not prim.pure or prim.is_async:
                return False
        return True


def _drive(coro):
    """Run an already-pure coroutine to completion without a loop. Pure plans
    never await anything real, so the coroutine completes on first send."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise PlanError("lambda body awaited a real coroutine in a synchronous slot; "
                    "use an async-aware primitive such as concurrency.parallel_map")


def _now() -> float:
    import time
    return time.time()


def _ms(started: float) -> float:
    import time
    return round((time.time() - started) * 1000, 3)


# ---------------------------------------------------------------------------
# COMPOSER  (check + admit + execute + hash)
# ---------------------------------------------------------------------------

class Composer:
    """Front door for plan-based capabilities: analyse, gate on declared
    effects, execute, and produce a stable identity for storage."""

    def __init__(self, registry: PrimitiveRegistry):
        self.reg = registry
        self.checker = PlanChecker(registry)
        self.executor = PlanExecutor(registry)

    def analyze(self, plan: Dict[str, Any]) -> PlanAnalysis:
        return self.checker.check(plan)

    def plan_hash(self, plan: Dict[str, Any]) -> str:
        # Batch 9: unified identity. Execution attribution MUST match the
        # stored identity from synthesis/capability_store.plan_fingerprint.
        # Previously this used a different exclusion set (_strip_volatile),
        # causing the same logical plan to receive different cap_ IDs at
        # execution vs storage. Now delegates to the single canonical
        # fingerprint function.
        from swarm_engine.synthesis.capability_store import plan_fingerprint
        return plan_fingerprint(plan)

    def permitted(self, plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Is every effect this plan demands grantable right now? Checked up
        front so a capability is never half-executed before being denied.

        This is the static half of the permission model: arguments aren't bound
        yet, so it asks whether each effect has a grant behind it at all. The
        governor still enforces the specific target at execution, so a plan
        admitted here can be denied later on a path outside its grant.
        """
        a = self.analyze(plan)
        missing = []
        for eff in a.effects:
            ok, why = self.reg.governor.grants_any(eff)
            if not ok:
                missing.append(f"{eff.value}: {why}")
        return (not missing), missing

    async def execute(self, plan: Dict[str, Any], args: Optional[Dict[str, Any]] = None,
                      ctx: Optional[ExecContext] = None,
                      skip_check: bool = False) -> Dict[str, Any]:
        if not skip_check:
            a = self.analyze(plan)
            if not a.ok:
                return {"success": False, "error": "plan failed static check",
                        "analysis": a.as_dict()}
        result = await self.executor.run(plan, args, ctx)
        result["capability_id"] = self.plan_hash(plan)
        return result

    def execute_sync(self, plan, args=None, ctx=None, skip_check=False):
        """Run execute() from sync code, including under a running event loop.

        asyncio.run() cannot be nested inside an already-running loop (the
        H / perimeter defect when GenericCapabilityGrowthEngine.grow is
        invoked from UniversalTaskInterface). When a loop is running, the
        work is done in a worker thread with its own loop so the caller's
        loop is never nested or blocked unsafely.

        Cooperative-preemption note: contextvars do not cross the thread
        boundary on their own, so the current RunControl (if any) is
        installed explicitly in the worker thread. A stop requested on the
        calling thread therefore still takes effect at the next checkpoint
        inside the worker.
        """
        parent_control = _current_run_control()

        def _run_in_fresh_loop():
            prev = _current_run_control()
            if parent_control is not None:
                _set_current_run_control(parent_control)
            try:
                return asyncio.run(self.execute(plan, args, ctx, skip_check))
            finally:
                _set_current_run_control(prev)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return _run_in_fresh_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(_run_in_fresh_loop).result()

