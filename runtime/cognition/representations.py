"""
swarm_engine/cognition/representations.py

Data structures shared by synthesis (E), reasoning (G), and learning (H).

Scope, stated once here rather than scattered: every structure below
represents *plan structure* — sequences of primitive names and how their
outputs chain into inputs — never natural language meaning. A Concept is a
verified op-sequence shape, not a semantic notion. This is a real but bounded
representation: it can capture "these two solutions have the same shape" and
nothing about why that shape is useful in the world.

All three stores persist to SQLite, the same discipline used everywhere else
in the engine, so learning survives restart rather than resetting every
process.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Expr:
    """A node in a general composition expression tree.

    Three leaf/node kinds, not two: a primitive application (op + children),
    a reference to one of the problem's input parameters (param), or — a
    fixed LITERAL constant (literal). Without a literal leaf, no composition
    can ever construct something like `modulo(n, 9)`: 9 cannot be derived
    from any parameter or any primitive's output on a single-parameter
    problem, so there is structurally no way to obtain it as an operand.
    Confirmed directly while trying to synthesize a digital-root formula:
    `constant(value)` is a real registered primitive, but it takes the
    constant as an INPUT — it cannot manufacture one from nothing.

    A fourth node kind, op == "$lambda" with lambda_params + a single
    ("body", ...) child, is a callable literal: the search's way to build
    the function arguments higher-order primitives (filter/partition/map)
    require. See is_lambda/size/ops_used/as_dict for the accounting.
    """
    op: Optional[str] = None
    param: Optional[str] = None
    literal: Any = None
    is_literal: bool = False
    children: Tuple[Tuple[str, "Expr"], ...] = ()
    # Lambda binder (Q11, 2026-09-27): a node with op == "$lambda" is a
    # callable literal. lambda_params names the bound parameters and the
    # single child ("body", body_expr) is the function body. This is the
    # search-side counterpart of the plan language's {"$lambda": ...}
    # node that composer._resolve and codegen._render_lambda already
    # support -- it lets the enumerative search construct the callable
    # arguments higher-order primitives (filter/partition/map) require,
    # which no primitive's output type can supply.
    lambda_params: Tuple[str, ...] = ()

    def is_lambda(self) -> bool:
        return self.op == "$lambda"

    def is_leaf(self) -> bool:
        return self.op is None

    def size(self) -> int:
        if self.is_leaf():
            return 0
        if self.is_lambda():
            # A binder is not a primitive application: its cost is exactly
            # its body's cost. The body's size counts in full toward the
            # candidate's total (minimality); the binding itself is free.
            return sum(child.size() for _, child in self.children)
        return 1 + sum(child.size() for _, child in self.children)

    def ops_used(self) -> Tuple[str, ...]:
        if self.is_leaf():
            return ()
        if self.is_lambda():
            # "$lambda" is plan-language structure, not a primitive: bias,
            # provenance, and concept identity record the body's real
            # primitives, never a pseudo-op the registry cannot resolve.
            out: List[str] = []
            for _, child in self.children:
                out.extend(child.ops_used())
            return tuple(out)
        out: List[str] = [self.op]
        for _, child in self.children:
            out.extend(child.ops_used())
        return tuple(out)

    def as_dict(self) -> Dict[str, Any]:
        if self.is_leaf():
            if self.is_literal:
                return {"literal": self.literal}
            return {"param": self.param}
        if self.is_lambda():
            return {"$lambda": {"params": list(self.lambda_params),
                                "body": dict(self.children)["body"].as_dict()}}
        return {"op": self.op,
                "children": {name: child.as_dict() for name, child in self.children}}

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Expr":
        if "$lambda" in data:
            spec = data["$lambda"]
            return Expr(op="$lambda",
                        lambda_params=tuple(spec.get("params") or ()),
                        children=(("body", Expr.from_dict(spec["body"])),))
        if "literal" in data:
            return Expr(literal=data["literal"], is_literal=True)
        if "param" in data:
            return Expr(param=data["param"])
        return Expr(op=data["op"],
                    children=tuple((name, Expr.from_dict(child))
                                   for name, child in data["children"].items()))

    def canonical(self) -> str:
        """A stable string identity for this tree — what a Concept's
        primary key and equality are based on."""
        return json.dumps(self.as_dict(), sort_keys=True)


def evaluate_expr(expr: "Expr", args: Dict[str, Any], registry: Any) -> Any:
    """Evaluate an Expr tree against a set of parameter bindings, using
    `registry` to resolve primitive calls. Pulled out as a standalone
    function (originally private to GeneralSynthesizer) so any other
    mechanism that needs to evaluate a verified Expr — primitive promotion
    is the first one — reuses the exact same evaluation logic rather than
    a second copy that could silently diverge from it."""
    if expr.is_leaf():
        return expr.literal if expr.is_literal else args[expr.param]
    if expr.is_lambda():
        # Compile a verified lambda body to a real Python callable over
        # its bound parameters. This is the SAME compilation the
        # synthesis search uses to test candidate predicates (no
        # divergent copy): primitive promotion, the identifiability
        # gate's rival evaluation, and any other consumer of a verified
        # Expr reuse exactly this logic.
        params = expr.lambda_params
        children = dict(expr.children)
        if "body" not in children:
            raise ValueError("$lambda node has no 'body' child")

        def _call(*call_args: Any) -> Any:
            if len(call_args) != len(params):
                raise TypeError(
                    f"$lambda takes {len(params)} argument(s), "
                    f"got {len(call_args)}")
            return evaluate_expr(children["body"],
                                 dict(zip(params, call_args)), registry)

        return _call
    prim = registry.get(expr.op)
    # Short-circuit if_else so ill-typed unused branches are not evaluated.
    # Required for type/shape dispatch: the other branch is often illegal
    # on this input (e.g. sum of a scalar).
    if expr.op == "if_else":
        ch = dict(expr.children)
        cond = evaluate_expr(ch["condition"], args, registry)
        branch = "then" if cond else "otherwise"
        return evaluate_expr(ch[branch], args, registry)
    kwargs = {name: evaluate_expr(child, args, registry) for name, child in expr.children}
    return prim.fn(**kwargs)


@dataclass
class Concept:
    """A verified, reusable expression-tree shape.

    Formed only when the SAME tree structure independently solves two or
    more DISTINCT goals (exact structural match, not fuzzy similarity) —
    this is the honest scope of induction here: it recognizes literal
    recurrence, not an inferred general principle. Generalized from the
    prior linear-sequence-only version: a Concept can now be any Expr shape,
    including nested and multi-argument ones.
    """
    concept_id: str
    expr: Expr
    support: int = 1
    example_goals: List[str] = field(default_factory=list)

    @property
    def op_sequence(self) -> Tuple[str, ...]:
        """Flattened view, kept for callers (SearchBias, older tests) that
        only need "which ops were involved", not tree structure."""
        return self.expr.ops_used()

    def as_dict(self) -> Dict[str, Any]:
        return {"concept_id": self.concept_id, "expr": self.expr.as_dict(),
                "op_sequence": list(self.op_sequence),
                "support": self.support, "example_goals": self.example_goals}


@dataclass
class Hypothesis:
    """A candidate plan — the existing Composer DSL shape, nothing new — plus
    provenance of how the cognitive layer arrived at it. `expr` carries the
    general tree structure when synthesis produced one (item 1); older
    single/two-parameter shapes and analogical/deductive hypotheses may leave
    it unset, and `op_sequence` (the flattened view) remains the identity
    used by SearchBias, CaseMemory, and concept lookup either way."""
    plan: Dict[str, Any]
    op_sequence: Tuple[str, ...]
    origin: str                       # "analogical" | "deductive" | "synthesis"
    derivation: str
    expr: Optional["Expr"] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"plan": self.plan, "op_sequence": list(self.op_sequence),
                "origin": self.origin, "derivation": self.derivation,
                "expr": self.expr.as_dict() if self.expr else None}


@dataclass
class CaseMemoryEntry:
    goal: str
    op_sequence: Tuple[str, ...]
    plan: Dict[str, Any]
    expr: Optional[Expr] = None
    examples: Optional[List[Tuple[Dict[str, Any], Any]]] = None
    param_names: Optional[Tuple[str, ...]] = None
    at: float = field(default_factory=time.time)
    # Governing semantic law this case was verified from, when the case
    # came from grounding: {"predicate":..., "semantic_id":...}.
    # Lets later machinery (e.g. reification) attribute which laws
    # justify a recurring computation, so evidence-driven invalidation
    # can reach exactly the promotions that depended on a contradicted
    # law. None for cases with no governing law (cognition syntheses).
    law: Optional[Dict[str, str]] = None


class ConceptGraph:
    """Persisted store of verified expression-tree shapes, keyed on tree
    structure rather than a flattened op list — `add(abs(a), b)` and
    `add(a, abs(b))` share the same two ops but are different concepts, and
    conflating them (as the earlier flat-tuple keying would) would silently
    lose exactly the structural distinction a general representation exists
    to preserve."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS concepts (
                concept_id TEXT PRIMARY KEY, canonical TEXT UNIQUE, expr TEXT,
                support INTEGER, example_goals TEXT)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def find(self, expr: Expr) -> Optional[Concept]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM concepts WHERE canonical=?",
                (expr.canonical(),)).fetchone()
        return self._row(row) if row else None

    def reinforce(self, expr: Expr, goal: str) -> Concept:
        existing = self.find(expr)
        if existing:
            existing.support += 1
            if goal not in existing.example_goals:
                existing.example_goals.append(goal)
        else:
            existing = Concept(concept_id=f"concept_{abs(hash(expr.canonical()))%10**10}",
                               expr=expr, support=1, example_goals=[goal])
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO concepts
                (concept_id, canonical, expr, support, example_goals)
                VALUES (?,?,?,?,?)""",
                (existing.concept_id, expr.canonical(),
                 json.dumps(expr.as_dict()), existing.support,
                 json.dumps(existing.example_goals)))
        return existing

    def all(self) -> List[Concept]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM concepts").fetchall()
        return [self._row(r) for r in rows]

    def _row(self, row) -> Concept:
        return Concept(concept_id=row["concept_id"],
                       expr=Expr.from_dict(json.loads(row["expr"])),
                       support=row["support"],
                       example_goals=json.loads(row["example_goals"]))


class CaseMemory:
    """Persisted store of verified (goal, op_sequence, plan, expr, examples)
    records. `examples` is what makes this usable as a REGRESSION SET for
    self-improvement: without the original input/output pairs, "does the
    candidate policy still solve what the incumbent already proved it could"
    can only be answered by re-executing the stored plan (which tests
    nothing about the search policy — execution doesn't involve search at
    all) rather than by re-running the search itself under the candidate
    configuration, which is the actual question a policy change needs
    answered."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS case_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT, op_sequence TEXT,
                plan TEXT, expr TEXT, examples TEXT, param_names TEXT, at REAL)""")
            # Additive migration: cases remembered before law attribution
            # existed simply carry law=NULL (treated as "no governing law").
            cols = {r["name"]
                    for r in conn.execute("PRAGMA table_info(case_memory)")}
            if "law" not in cols:
                conn.execute("ALTER TABLE case_memory ADD COLUMN law TEXT")
            conn.execute("""CREATE TABLE IF NOT EXISTS failed_adaptations (
                case_id INTEGER, target_goal TEXT, PRIMARY KEY (case_id, target_goal))""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def remember(self, goal: str, op_sequence: Tuple[str, ...],
                plan: Dict[str, Any], expr: Optional[Expr] = None,
                examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None,
                param_names: Optional[Sequence[str]] = None,
                law: Optional[Dict[str, str]] = None) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT INTO case_memory
                (goal, op_sequence, plan, expr, examples, param_names, at, law)
                VALUES (?,?,?,?,?,?,?,?)""",
                (goal, json.dumps(list(op_sequence)), json.dumps(plan),
                 json.dumps(expr.as_dict()) if expr else None,
                 json.dumps([[a, b] for a, b in examples], default=str)
                 if examples else None,
                 json.dumps(list(param_names)) if param_names else None,
                 time.time(),
                 json.dumps(law) if law else None))

    def all(self) -> List[Tuple[int, CaseMemoryEntry]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM case_memory ORDER BY id").fetchall()
        out = []
        for r in rows:
            expr = Expr.from_dict(json.loads(r["expr"])) if r["expr"] else None
            examples = ([(a, b) for a, b in json.loads(r["examples"])]
                       if r["examples"] else None)
            param_names = (tuple(json.loads(r["param_names"]))
                          if r["param_names"] else None)
            # "law" not in r.keys() only for DB files created before the
            # law-attribution migration and never reopened via __init__.
            law_raw = r["law"] if "law" in r.keys() else None
            out.append((r["id"], CaseMemoryEntry(
                goal=r["goal"], op_sequence=tuple(json.loads(r["op_sequence"])),
                plan=json.loads(r["plan"]), expr=expr, examples=examples,
                param_names=param_names, at=r["at"],
                law=json.loads(law_raw) if law_raw else None)))
        return out

    def mark_adaptation_failed(self, case_id: int, target_goal: str) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR IGNORE INTO failed_adaptations
                (case_id, target_goal) VALUES (?,?)""", (case_id, target_goal))

    def adaptation_already_failed(self, case_id: int, target_goal: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM failed_adaptations WHERE case_id=? AND target_goal=?",
                (case_id, target_goal)).fetchone()
        return row is not None


class SearchBias:
    """Persisted, evidence-weighted search ordering.

    Laplace-smoothed so an untried op scores neutral (0.5) rather than zero —
    the same "absence of evidence is not evidence of failure" principle used
    for role reliability and provenance confidence elsewhere in the engine.
    """

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        # In-memory score cache. Found directly by profiling a deep
        # search: score_op() opened a fresh sqlite connection and ran a
        # SELECT per call (102k calls / 30s in one stress search), yet
        # scores cannot change mid-search -- record() is the only writer
        # and it is never called from inside the search loop. The cache
        # is cleared by record(), so every read still reflects all
        # evidence recorded by this process.
        self._score_cache: Dict[str, float] = {}
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS search_bias_op (
                op TEXT PRIMARY KEY, hits INTEGER, tries INTEGER)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS search_bias_pair (
                op_a TEXT, op_b TEXT, hits INTEGER, tries INTEGER,
                PRIMARY KEY (op_a, op_b))""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def score_op(self, op: str) -> float:
        try:
            return self._score_cache[op]
        except KeyError:
            pass
        with self._conn() as conn:
            row = conn.execute("SELECT hits, tries FROM search_bias_op WHERE op=?",
                              (op,)).fetchone()
        hits, tries = (row["hits"], row["tries"]) if row else (0, 0)
        score = (hits + 1) / (tries + 2)
        self._score_cache[op] = score
        return score

    def record(self, op_sequence: Tuple[str, ...], success: bool) -> None:
        with self._conn() as conn:
            for op in op_sequence:
                row = conn.execute("SELECT hits, tries FROM search_bias_op WHERE op=?",
                                  (op,)).fetchone()
                hits, tries = (row["hits"], row["tries"]) if row else (0, 0)
                conn.execute("""INSERT OR REPLACE INTO search_bias_op
                    (op, hits, tries) VALUES (?,?,?)""",
                    (op, hits + (1 if success else 0), tries + 1))
            for a, b in zip(op_sequence, op_sequence[1:]):
                row = conn.execute(
                    "SELECT hits, tries FROM search_bias_pair WHERE op_a=? AND op_b=?",
                    (a, b)).fetchone()
                hits, tries = (row["hits"], row["tries"]) if row else (0, 0)
                conn.execute("""INSERT OR REPLACE INTO search_bias_pair
                    (op_a, op_b, hits, tries) VALUES (?,?,?,?)""",
                    (a, b, hits + (1 if success else 0), tries + 1))
        # Scores changed: drop the cache so later reads see the update.
        self._score_cache.clear()

    def stats(self, op: str) -> Tuple[int, int]:
        with self._conn() as conn:
            row = conn.execute("SELECT hits, tries FROM search_bias_op WHERE op=?",
                              (op,)).fetchone()
        return (row["hits"], row["tries"]) if row else (0, 0)

    def snapshot_into(self, other: "SearchBias") -> None:
        """Copy all accumulated op/pair statistics into another SearchBias.

        Required for fair policy comparison, not optional: a candidate
        policy tested from a stone-cold bias against an incumbent with
        accumulated learning is not a comparison of the policy NUMBERS at
        all — it is a comparison of "no learning" against "months of
        learning," and a perfectly good (or better) candidate configuration
        can look like a regression purely because a primitive ranked outside
        the per-level pool at neutral bias never gets tried regardless of
        total candidate budget. Seeding the candidate from the same
        knowledge the incumbent has isolates what is actually being tested
        to the configuration itself.
        """
        with self._conn() as src, other._conn() as dst:
            for row in src.execute("SELECT op, hits, tries FROM search_bias_op"):
                dst.execute("""INSERT OR REPLACE INTO search_bias_op
                    (op, hits, tries) VALUES (?,?,?)""",
                    (row["op"], row["hits"], row["tries"]))
            for row in src.execute(
                    "SELECT op_a, op_b, hits, tries FROM search_bias_pair"):
                dst.execute("""INSERT OR REPLACE INTO search_bias_pair
                    (op_a, op_b, hits, tries) VALUES (?,?,?,?)""",
                    (row["op_a"], row["op_b"], row["hits"], row["tries"]))


class ExhaustedSearchMemory:
    """Records which searches ran their full candidate budget without
    finding anything.

    Originally built to do exactly one thing: stop literally re-running an
    identical exhausted search (item 2's "don't repeat a proven-fruitless
    attempt"). That version stored only a one-way hash — enough to recognize
    "I've seen this exact search before," but nothing else, because nothing
    else was needed for that purpose.

    It turned out to be a hidden ceiling once self-improvement needed to
    reason about *why* searches were failing: a hash cannot be replayed, so
    there was no way to test whether a different search policy would have
    succeeded where the recorded one didn't. This is the fix — the actual
    examples, parameter names, and (when available) an expressiveness
    verdict are now stored alongside the hash, so a failure is genuine
    evidence something can learn from, not just a fingerprint to avoid
    repeating. The original dedup behaviour (`was_exhausted`) is unchanged.
    """

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS exhausted_searches (
                signature TEXT PRIMARY KEY, candidates_tried INTEGER,
                param_names TEXT, examples TEXT, expressible_by_type INTEGER,
                goal TEXT, at REAL)""")
            existing_cols = {row[1] for row in
                            conn.execute("PRAGMA table_info(exhausted_searches)")}
            if "goal" not in existing_cols:
                conn.execute("ALTER TABLE exhausted_searches ADD COLUMN goal TEXT")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, signature: str, candidates_tried: int,
              param_names: Optional[Sequence[str]] = None,
              examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None,
              expressible_by_type: Optional[bool] = None,
              goal: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO exhausted_searches
                (signature, candidates_tried, param_names, examples,
                 expressible_by_type, goal, at) VALUES (?,?,?,?,?,?,?)""",
                (signature, candidates_tried,
                 json.dumps(list(param_names)) if param_names else None,
                 json.dumps([[a, b] for a, b in examples], default=str)
                 if examples else None,
                 (1 if expressible_by_type else 0)
                 if expressible_by_type is not None else None,
                 goal, time.time()))

    def was_exhausted(self, signature: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM exhausted_searches WHERE signature=?",
                (signature,)).fetchone()
        return row is not None

    def clear(self) -> int:
        """Invalidate every recorded exhaustion. Required when the search
        policy itself changes: a record saying "this search failed" is only
        evidence about the POLICY that was active when it failed. Once that
        policy is replaced, every prior exhaustion record is stale — it
        would otherwise block retrying a goal the NEW policy can now solve,
        which is exactly what happened the first time this interaction was
        tested: a genuinely improved policy activated correctly, and the
        very goal that motivated the improvement still failed, because the
        dedup skip fired before synthesis ever got the chance to use the new
        policy. Returns how many records were cleared."""
        with self._conn() as conn:
            count = conn.execute("SELECT COUNT(*) c FROM exhausted_searches").fetchone()["c"]
            conn.execute("DELETE FROM exhausted_searches")
        return count

    def replayable(self, only_expressible: bool = True) -> List[Dict[str, Any]]:
        """Every exhausted search that stored enough to be replayed —
        the evidence an improvement observer actually reasons over."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM exhausted_searches WHERE examples IS NOT NULL"
            ).fetchall()
        out = []
        for r in rows:
            if only_expressible and r["expressible_by_type"] == 0:
                continue
            out.append({
                "signature": r["signature"],
                "candidates_tried": r["candidates_tried"],
                "param_names": tuple(json.loads(r["param_names"])),
                "examples": [(a, b) for a, b in json.loads(r["examples"])],
                "expressible_by_type": (bool(r["expressible_by_type"])
                                        if r["expressible_by_type"] is not None
                                        else None),
                "goal": r["goal"] if "goal" in r.keys() else None,
                "at": r["at"],
            })
        return out
