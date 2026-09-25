"""
swarm_engine/primitives/core.py

The primitive substrate. Three things live here:

1. TypeSpec  - a structural type language used to declare what every primitive
               consumes and produces, so a composition can be checked *before*
               it executes rather than blowing up mid-plan.
2. Permission/Governor - privileged operations (filesystem, network, process,
               credentials) are declared on the primitive itself. The Governor
               decides, per call, whether the operation is allowed. A primitive
               cannot silently acquire a capability it didn't declare.
3. Primitive / PrimitiveRegistry - the actual callable units and the registry
               the synthesizer draws from.

The registry is the vocabulary. The composer (synthesis/composer.py) is the
grammar. Nothing in this file knows about Forge, agents, or tasks.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# TYPE SYSTEM
# ---------------------------------------------------------------------------

class Kind(Enum):
    ANY = "any"
    INT = "int"
    FLOAT = "float"
    NUM = "num"          # int or float
    BOOL = "bool"
    STR = "str"
    BYTES = "bytes"
    NONE = "none"
    LIST = "list"
    DICT = "dict"
    TUPLE = "tuple"
    CALLABLE = "callable"
    UNION = "union"


@dataclass(frozen=True)
class TypeSpec:
    kind: Kind
    args: Tuple["TypeSpec", ...] = ()
    optional: bool = False

    def __str__(self) -> str:
        s = self.kind.value
        if self.args:
            s += "[" + ", ".join(str(a) for a in self.args) + "]"
        if self.optional:
            s += "?"
        return s

    __repr__ = __str__

    # -- compatibility ------------------------------------------------------
    def accepts(self, other: "TypeSpec") -> bool:
        """Can a value of type `other` be fed to a slot of type `self`?"""
        if self.kind is Kind.ANY or other.kind is Kind.ANY:
            return True
        if other.kind is Kind.NONE:
            return self.optional or self.kind is Kind.NONE
        if self.kind is Kind.UNION:
            return any(a.accepts(other) for a in self.args)
        if other.kind is Kind.UNION:
            return all(self.accepts(a) for a in other.args)
        if self.kind is Kind.NUM and other.kind in (Kind.INT, Kind.FLOAT, Kind.NUM):
            return True
        if self.kind is Kind.FLOAT and other.kind is Kind.INT:
            return True  # widening
        if self.kind is not other.kind:
            return False
        if not self.args:
            return True
        if not other.args:
            return True  # unparameterised container satisfies parameterised slot
        if len(self.args) != len(other.args):
            return False
        return all(a.accepts(b) for a, b in zip(self.args, other.args))


# shorthand constructors
ANY = TypeSpec(Kind.ANY)
INT = TypeSpec(Kind.INT)
FLOAT = TypeSpec(Kind.FLOAT)
NUM = TypeSpec(Kind.NUM)
BOOL = TypeSpec(Kind.BOOL)
STR = TypeSpec(Kind.STR)
BYTES = TypeSpec(Kind.BYTES)
NONE = TypeSpec(Kind.NONE)
CALLABLE = TypeSpec(Kind.CALLABLE)


def LIST(t: TypeSpec = ANY) -> TypeSpec:
    return TypeSpec(Kind.LIST, (t,))


def DICT(k: TypeSpec = STR, v: TypeSpec = ANY) -> TypeSpec:
    return TypeSpec(Kind.DICT, (k, v))


def TUPLE(*ts: TypeSpec) -> TypeSpec:
    return TypeSpec(Kind.TUPLE, tuple(ts))


def UNION(*ts: TypeSpec) -> TypeSpec:
    return TypeSpec(Kind.UNION, tuple(ts))


def OPT(t: TypeSpec) -> TypeSpec:
    return TypeSpec(t.kind, t.args, optional=True)


def infer(value: Any) -> TypeSpec:
    """Structural type of a runtime value."""
    if value is None:
        return NONE
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, int):
        return INT
    if isinstance(value, float):
        return FLOAT
    if isinstance(value, str):
        return STR
    if isinstance(value, (bytes, bytearray)):
        return BYTES
    if isinstance(value, tuple):
        return TUPLE(*(infer(v) for v in value[:4]))
    if isinstance(value, list):
        return LIST(infer(value[0])) if value else LIST(ANY)
    if isinstance(value, dict):
        if not value:
            return DICT(STR, ANY)
        k, v = next(iter(value.items()))
        return DICT(infer(k), infer(v))
    if callable(value):
        return CALLABLE
    return ANY


def coerce(value: Any, target: TypeSpec) -> Tuple[bool, Any]:
    """Best-effort coercion. Never lossy-coerces silently into a narrower
    numeric type when it would change the value."""
    if target.accepts(infer(value)):
        return True, value
    try:
        if target.kind is Kind.FLOAT:
            return True, float(value)
        if target.kind is Kind.INT:
            iv = int(value)
            if isinstance(value, float) and iv != value:
                return False, value
            return True, iv
        if target.kind is Kind.STR:
            return True, str(value)
        if target.kind is Kind.BOOL:
            if isinstance(value, str):
                return True, value.strip().lower() in ("true", "yes", "1", "on")
            return True, bool(value)
        if target.kind is Kind.BYTES and isinstance(value, str):
            return True, value.encode()
        if target.kind is Kind.LIST and isinstance(value, (tuple, set)):
            return True, list(value)
    except (TypeError, ValueError):
        pass
    return False, value


def unify(a: TypeSpec, b: TypeSpec) -> Optional[TypeSpec]:
    if a.accepts(b):
        return a
    if b.accepts(a):
        return b
    if {a.kind, b.kind} <= {Kind.INT, Kind.FLOAT, Kind.NUM}:
        return NUM
    return None


# ---------------------------------------------------------------------------
# GOVERNANCE
# ---------------------------------------------------------------------------

class Effect(Enum):
    """What a primitive touches beyond its own arguments. PURE primitives are
    freely composable and cacheable; everything else is gated."""
    PURE = "pure"
    READ_FS = "read_fs"
    WRITE_FS = "write_fs"
    NETWORK = "network"
    PROCESS = "process"
    CREDENTIAL = "credential"
    CLOCK = "clock"
    RANDOM = "random"
    MEMORY = "memory"          # engine knowledge base
    SPAWN = "spawn"            # agents / threads
    MUTATE_SELF = "mutate_self"  # modifies the capability registry


#: effects that never need a grant
FREE_EFFECTS = {Effect.PURE, Effect.CLOCK, Effect.RANDOM}


@dataclass
class Grant:
    effect: Effect
    pattern: str = "*"          # glob-ish target restriction
    note: str = ""

    def covers(self, effect: Effect, target: str) -> bool:
        if self.effect is not effect:
            return False
        if self.pattern == "*":
            return True
        if self.pattern.endswith("*"):
            return str(target).startswith(self.pattern[:-1])
        return str(target) == self.pattern


@dataclass
class AuditEntry:
    effect: Effect
    target: str
    primitive: str
    allowed: bool
    reason: str
    at: float = field(default_factory=time.time)


class PermissionError_(RuntimeError):
    """Raised when a primitive attempts an ungranted effect."""


class Governor:
    """Deny-by-default gate for every non-free effect.

    The engine holds one Governor. Grants are added explicitly by the operator
    (or by an admission decision); a synthesized capability can never widen its
    own permissions because `grant` is not exposed to primitives.

    ORACLE BINDING (2026-09-25): when constructed with an oracle registry,
    every grant is issued through the tamper-evident registry as a chained,
    attributed grant record (granted_by, scope, timestamp). The in-memory
    grant list is rehydrated from the registry at construction, so grants
    survive restart and revocation is a recorded event, not a silent list
    mutation. Issuing a grant requires the issuer to hold the 'grant'
    decision class -- an untrusted caller cannot mint its own authority.
    Without a registry the Governor keeps its legacy runtime-only behavior
    (documented as unbound).
    """

    def __init__(self, grants: Optional[Sequence[Grant]] = None, audit_limit: int = 5000,
                 oracle_registry: Optional[Any] = None,
                 engine_oracle: Optional[Any] = None):
        self.grants: List[Grant] = list(grants or [])
        self.audit: List[AuditEntry] = []
        self._audit_limit = audit_limit
        self.oracle_registry = oracle_registry
        self.engine_oracle = engine_oracle
        if oracle_registry is not None:
            self._rehydrate_grants()

    def _rehydrate_grants(self) -> None:
        """Rebuild the runtime grant list from the registry's live grants.

        Fail-closed: the grant chain is audited before any row is trusted.
        A corrupt or unauditable chain means the persisted grants are
        discarded (the engine starts with no rehydrated grants) and the
        corruption is recorded on the Governor for the operator.
        """
        self.grant_chain_corrupt: bool = False
        seen = {(g.effect, g.pattern) for g in self.grants}
        try:
            ok, why = self.oracle_registry.audit_chain("ob_grants")
            if not ok:
                self.grant_chain_corrupt = True
                return
            cur = self.oracle_registry._conn.cursor()
            cur.execute("SELECT * FROM ob_grants ORDER BY seq ASC")
            heads: Dict[str, Any] = {}
            for row in cur.fetchall():
                heads[row["grant_id"]] = row
            for row in heads.values():
                if row["revoked"]:
                    continue
                effect = Effect(row["effect"])
                if (effect, row["pattern"]) not in seen:
                    self.grants.append(Grant(
                        effect, row["pattern"],
                        note=f"rehydrated from oracle registry "
                             f"(granted_by={row['granted_by']})"))
                    seen.add((effect, row["pattern"]))
        except Exception:
            self.grant_chain_corrupt = True

    def grant(self, effect: Effect, pattern: str = "*", note: str = "",
              authority: Optional[Any] = None) -> Grant:
        if self.oracle_registry is not None:
            # Grants are authority-issued and recorded. The issuer must hold
            # the 'grant' decision class; by default the engine's own handle
            # is the issuer (the Governor is the engine's instrument).
            handle = authority or self.engine_oracle
            if handle is None:
                raise PermissionError_(
                    "grant refused: no oracle authority available")
            grant_id = handle.issue_grant(effect.value, pattern,
                                          scope=note or "")
            note = (note + f" [oracle_grant={grant_id}]").strip()
        g = Grant(effect, pattern, note)
        self.grants.append(g)
        return g

    def revoke(self, effect: Effect, pattern: str = "*") -> int:
        before = len(self.grants)
        self.grants = [g for g in self.grants if not (g.effect is effect and g.pattern == pattern)]
        removed = before - len(self.grants)
        if self.oracle_registry is not None and removed:
            # Record the revocation in the registry as well so the grant
            # history stays complete (revocation is an event, not a deletion).
            try:
                import sqlite3 as _sqlite3
                cur = self.oracle_registry._conn.cursor()
                cur.execute("SELECT grant_id FROM ob_grants WHERE effect=? "
                            "AND pattern=? AND revoked=0 ORDER BY seq DESC",
                            (effect.value, pattern))
                handle = self.engine_oracle
                for row in cur.fetchall():
                    if handle is not None:
                        handle.revoke_grant(row["grant_id"])
            except Exception:
                pass
        return removed

    def allows(self, effect: Effect, target: str = "") -> Tuple[bool, str]:
        if effect in FREE_EFFECTS:
            return True, "unrestricted effect"
        for g in self.grants:
            if g.covers(effect, target):
                return True, f"granted by {g.effect.value}:{g.pattern}"
        return False, f"no grant for {effect.value} on {target!r}"

    def grants_any(self, effect: Effect) -> Tuple[bool, str]:
        """Is this effect grantable at all, for any target?

        Admission checks a plan before its arguments are bound, so the concrete
        target (which path, which URL) genuinely isn't known yet. Asking
        allows(effect, "*") there is the wrong question: "*" is a literal
        target that no scoped grant covers, so a grant like READ_FS on
        '/tmp/ws/*' would fail admission and every scoped grant would be
        unusable — leaving all-or-nothing '*' grants as the only ones that
        work, which is the opposite of least privilege.

        So admission asks only whether some grant for this effect exists. The
        per-target decision is still enforced at execution by check(), once the
        real target is known. A plan that needs an effect with no grant behind
        it is refused up front; a plan whose effect is granted somewhere is
        admitted and then held to the actual pattern at run time.
        """
        if effect in FREE_EFFECTS:
            return True, "unrestricted effect"
        for g in self.grants:
            if g.effect is effect:
                return True, f"grantable under {g.effect.value}:{g.pattern}"
        return False, f"no grant for {effect.value} on any target"

    def check(self, effect: Effect, target: str = "", primitive: str = "") -> None:
        ok, reason = self.allows(effect, target)
        self._record(AuditEntry(effect, str(target), primitive, ok, reason))
        if not ok:
            raise PermissionError_(
                f"{primitive or 'operation'} denied: {effect.value} on {target!r} - {reason}"
            )

    def _record(self, entry: AuditEntry) -> None:
        self.audit.append(entry)
        if len(self.audit) > self._audit_limit:
            del self.audit[: len(self.audit) // 2]

    def denials(self) -> List[AuditEntry]:
        return [e for e in self.audit if not e.allowed]

    def summary(self) -> Dict[str, Any]:
        return {
            "grants": [(g.effect.value, g.pattern) for g in self.grants],
            "audited": len(self.audit),
            "denied": len(self.denials()),
        }


# ---------------------------------------------------------------------------
# PRIMITIVES
# ---------------------------------------------------------------------------

@dataclass
class Primitive:
    name: str
    family: str
    fn: Callable[..., Any]
    inputs: Dict[str, TypeSpec]
    output: TypeSpec
    effects: Tuple[Effect, ...] = (Effect.PURE,)
    doc: str = ""
    variadic: bool = False        # last declared input absorbs extra positionals
    pure: bool = True
    needs_ctx: bool = False       # fn receives ctx=ExecContext as first arg
    is_async: bool = False

    def __post_init__(self):
        self.pure = all(e is Effect.PURE for e in self.effects)
        self.is_async = asyncio.iscoroutinefunction(self.fn)
        if not self.doc:
            self.doc = (inspect.getdoc(self.fn) or "").split("\n")[0]

    # -- signature checking -------------------------------------------------
    def check_args(self, kwargs: Dict[str, Any]) -> Tuple[bool, str]:
        for name, spec in self.inputs.items():
            if name not in kwargs:
                if spec.optional:
                    continue
                return False, f"{self.name}: missing required argument {name!r}"
            got = infer(kwargs[name])
            if not spec.accepts(got):
                ok, _ = coerce(kwargs[name], spec)
                if not ok:
                    return False, f"{self.name}: argument {name!r} expects {spec}, got {got}"
        if not self.variadic:
            extra = set(kwargs) - set(self.inputs)
            if extra:
                return False, f"{self.name}: unexpected argument(s) {sorted(extra)}"
        return True, "ok"

    def coerce_args(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(kwargs)
        for name, spec in self.inputs.items():
            if name in out:
                ok, v = coerce(out[name], spec)
                if ok:
                    out[name] = v
        return out

    def signature(self) -> str:
        params = ", ".join(f"{n}: {t}" for n, t in self.inputs.items())
        eff = "" if self.pure else "  !" + ",".join(e.value for e in self.effects)
        return f"{self.name}({params}) -> {self.output}{eff}"


@dataclass
class ExecContext:
    """Everything a non-pure primitive is allowed to reach. Passed explicitly;
    primitives never import the engine."""
    governor: Governor
    registry: "PrimitiveRegistry"
    kb: Any = None
    providers: Any = None
    scratch: Dict[str, Any] = field(default_factory=dict)
    depth: int = 0
    budget: int = 10_000          # remaining primitive invocations
    trace: List[Dict[str, Any]] = field(default_factory=list)

    def child(self) -> "ExecContext":
        c = ExecContext(self.governor, self.registry, self.kb, self.providers,
                        dict(self.scratch), self.depth + 1, self.budget, self.trace)
        return c

    def spend(self, n: int = 1) -> None:
        self.budget -= n
        if self.budget <= 0:
            raise RuntimeError("primitive invocation budget exhausted")


class PrimitiveRegistry:
    """The engine's vocabulary. Every capability the synthesizer can build is a
    composition of entries in here."""

    def __init__(self, governor: Optional[Governor] = None):
        self.governor = governor or Governor()
        self._prims: Dict[str, Primitive] = {}
        self._by_family: Dict[str, List[str]] = {}

    # -- registration -------------------------------------------------------
    def register(self, prim: Primitive, overwrite: bool = False) -> Primitive:
        if prim.name in self._prims and not overwrite:
            raise ValueError(f"primitive {prim.name!r} already registered")
        self._prims[prim.name] = prim
        self._by_family.setdefault(prim.family, [])
        if prim.name not in self._by_family[prim.family]:
            self._by_family[prim.family].append(prim.name)
        return prim

    def define(self, name: str, family: str, inputs: Dict[str, TypeSpec],
               output: TypeSpec, effects: Tuple[Effect, ...] = (Effect.PURE,),
               variadic: bool = False, needs_ctx: bool = False):
        """Decorator form."""
        def deco(fn):
            self.register(Primitive(name=name, family=family, fn=fn, inputs=inputs,
                                    output=output, effects=effects,
                                    variadic=variadic, needs_ctx=needs_ctx))
            return fn
        return deco

    # -- removal ------------------------------------------------------------
    def unregister(self, name: str) -> bool:
        """Remove a primitive by name. Returns True if one was removed.

        Generic lifecycle support: acquisition rollback (a graph that fails
        to resolve must not leave partially acquired prerequisites behind)
        and any future capability-retirement path use this rather than
        reaching into the registry's internals. Built-in families are not
        protected here — callers decide what may be removed.
        """
        prim = self._prims.pop(name, None)
        if prim is None:
            return False
        fam = self._by_family.get(prim.family)
        if fam and name in fam:
            fam.remove(name)
            if not fam:
                del self._by_family[prim.family]
        return True

    # -- lookup -------------------------------------------------------------
    def resolve(self, name: str) -> Optional[str]:
        """Resolve a reference to a registered primitive name.

        Accepts both the bare key ('mean') and a qualified 'family.name'
        reference ('computation.mean'). Planners and stored plans are written
        against qualified names so a plan stays unambiguous if two families
        ever expose the same bare name; the registry itself is keyed bare.
        Returns None if the reference names nothing, or if a qualified
        reference disagrees with the family the primitive actually lives in.
        """
        if name in self._prims:
            return name
        if "." in name:
            family, _, bare = name.rpartition(".")
            prim = self._prims.get(bare)
            if prim is not None and prim.family == family:
                return bare
        return None

    def get(self, name: str) -> Optional[Primitive]:
        key = self.resolve(name)
        return self._prims.get(key) if key else None

    def __contains__(self, name: str) -> bool:
        return self.resolve(name) is not None

    def __len__(self) -> int:
        return len(self._prims)

    def names(self) -> List[str]:
        return sorted(self._prims)

    def families(self) -> Dict[str, List[str]]:
        return {f: sorted(v) for f, v in sorted(self._by_family.items())}

    def family(self, name: str) -> List[Primitive]:
        return [self._prims[n] for n in self._by_family.get(name, [])]

    def search(self, term: str) -> List[Primitive]:
        t = term.lower()
        return [p for p in self._prims.values()
                if t in p.name.lower() or t in p.doc.lower() or t in p.family.lower()]

    def producing(self, spec: TypeSpec) -> List[Primitive]:
        """Primitives whose output can fill a slot of type `spec` — this is what
        makes goal-directed composition possible."""
        return [p for p in self._prims.values() if spec.accepts(p.output)]

    def consuming(self, spec: TypeSpec) -> List[Primitive]:
        return [p for p in self._prims.values()
                if any(t.accepts(spec) for t in p.inputs.values())]

    def effects_of(self, names: Sequence[str]) -> set:
        out = set()
        for n in names:
            p = self.get(n)
            if p:
                out.update(e for e in p.effects if e is not Effect.PURE)
        return out

    # -- invocation ---------------------------------------------------------
    # `name` and `ctx` are positional-only. Eleven primitives — including
    # param, plan, pipeline and sequence — declare an argument of their own
    # called `name`, and one keyed on `ctx` would collide the same way. With
    # ordinary parameters, invoke(prim, ctx, name="x") raises "got multiple
    # values for argument 'name'" and those primitives are uncallable by
    # keyword. The `/` sends every caller keyword into **kwargs where it
    # belongs, so a primitive's argument names are its own business.
    async def invoke(self, name: str, ctx: Optional[ExecContext] = None, /, **kwargs) -> Any:
        prim = self.get(name)
        if prim is None:
            raise KeyError(f"unknown primitive {name!r}")
        ctx = ctx or ExecContext(self.governor, self)
        ctx.spend()

        ok, why = prim.check_args(kwargs)
        if not ok:
            raise TypeError(why)
        kwargs = prim.coerce_args(kwargs)

        for eff in prim.effects:
            if eff not in FREE_EFFECTS:
                target = kwargs.get("path") or kwargs.get("url") or kwargs.get("key") or ""
                ctx.governor.check(eff, str(target), prim.name)

        started = time.time()
        if prim.needs_ctx:
            result = prim.fn(ctx, **kwargs)
        else:
            result = prim.fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        ctx.trace.append({"primitive": name, "ms": round((time.time() - started) * 1000, 3)})
        return result

    def invoke_sync(self, name: str, ctx: Optional[ExecContext] = None, /, **kwargs) -> Any:
        return _run_sync(self.invoke(name, ctx, **kwargs))

    def describe(self) -> str:
        lines = []
        for fam, names in self.families().items():
            lines.append(f"[{fam}] ({len(names)})")
            for n in names:
                lines.append("   " + self._prims[n].signature())
        return "\n".join(lines)


def _run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("invoke_sync called from inside a running event loop; use await invoke()")
