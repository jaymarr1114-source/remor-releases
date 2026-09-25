"""
swarm_engine/synthesis/planner.py

Goal -> Plan. This is the piece that was missing: the engine had a vocabulary
(primitives) and a grammar (composer) but nothing that wrote sentences.

Two planning strategies, tried in order:

1. TemplatePlanner  - a goal that matches a known intent shape is filled from a
                      parameterised template. Fast, deterministic, and the
                      overwhelming majority of real requests land here.
2. BackwardPlanner  - no template matched: search the registry backwards from
                      the desired output type, chaining primitives whose output
                      can fill the next slot, until every input is satisfied by
                      a plan parameter. This is what lets the engine build
                      capabilities nobody enumerated in advance.

Neither emits Python. Both emit Plan dicts, which the PlanChecker type-checks
and effect-checks before anything runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from swarm_engine.primitives.core import (
    ANY, BOOL, DICT, LIST, NUM, STR, Effect, PrimitiveRegistry, TypeSpec, Kind,
)

_KIND_NAMES = {Kind.INT: "int", Kind.FLOAT: "float", Kind.NUM: "num",
               Kind.STR: "str", Kind.BOOL: "bool", Kind.LIST: "list",
               Kind.DICT: "dict", Kind.ANY: "any", Kind.BYTES: "bytes",
               Kind.CALLABLE: "callable", Kind.NONE: "none"}


def _pname(spec: TypeSpec) -> str:
    return _KIND_NAMES.get(spec.kind, "any")


@dataclass
class PlanProposal:
    """A candidate plan plus why the planner believes in it."""
    plan: Dict[str, Any]
    strategy: str
    confidence: float
    rationale: str = ""
    ops_used: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"plan": self.plan, "strategy": self.strategy,
                "confidence": self.confidence, "rationale": self.rationale,
                "ops_used": self.ops_used}


# ---------------------------------------------------------------------------
# GOAL PARSING
# ---------------------------------------------------------------------------

@dataclass
class Goal:
    """A normalised request. `raw` is what the caller said; everything else is
    what the planner managed to extract from it."""
    raw: str
    verbs: List[str] = field(default_factory=list)
    nouns: List[str] = field(default_factory=list)
    numbers: List[float] = field(default_factory=list)
    quoted: List[str] = field(default_factory=list)
    wants_type: TypeSpec = ANY

    @property
    def text(self) -> str:
        return self.raw.lower()


_VERB_HINTS = {
    "sum", "add", "total", "average", "mean", "median", "count", "max", "min",
    "sort", "filter", "group", "map", "reduce", "join", "merge", "split",
    "read", "write", "fetch", "get", "download", "search", "find", "parse",
    "format", "convert", "translate", "summarize", "extract", "classify",
    "validate", "normalize", "deduplicate", "unique", "reverse", "hash",
    "encode", "decode", "compress", "compare", "diff", "rank", "sample",
}

_OUTPUT_HINTS = [
    (re.compile(r"\b(list|array|rows|items|records|collection)\b"), LIST(ANY)),
    (re.compile(r"\b(dict|object|map|table|record|json)\b"), DICT(STR, ANY)),
    (re.compile(r"\b(count|number|total|sum|average|mean|score|amount)\b"), NUM),
    (re.compile(r"\b(text|string|name|label|message|report|summary)\b"), STR),
    (re.compile(r"\b(is|has|whether|true|false|valid|check)\b"), BOOL),
]


def parse_goal(raw: str) -> Goal:
    g = Goal(raw=raw)
    low = raw.lower()
    g.quoted = re.findall(r"[\"']([^\"']+)[\"']", raw)
    g.numbers = [float(n) for n in re.findall(r"-?\d+\.?\d*", raw)]
    words = re.findall(r"[a-z_]+", low)
    g.verbs = [w for w in words if w in _VERB_HINTS]
    g.nouns = [w for w in words if w not in _VERB_HINTS and len(w) > 3]
    for pat, spec in _OUTPUT_HINTS:
        if pat.search(low):
            g.wants_type = spec
            break
    return g


# ---------------------------------------------------------------------------
# TEMPLATE PLANNER
# ---------------------------------------------------------------------------

@dataclass
class Template:
    """An intent shape. `match` scores a goal 0..1; `build` returns a plan."""
    name: str
    keywords: Sequence[str]
    requires: Sequence[str]          # primitive names that must exist
    build: Any                       # Callable[[Goal, PrimitiveRegistry], dict]
    weight: float = 1.0

    def score(self, goal: Goal) -> float:
        # Token-boundary matching prevents false positives such as
        # "min" inside "determine" or "count" inside "counter" (Defect #1).
        # Word-boundary regex keeps legitimate phrases ("min of", "count the")
        # while rejecting embedded stems.
        import re
        low = goal.text.lower()
        hits = 0
        for k in self.keywords:
            kl = k.lower()
            if re.search(r"(?<![a-z])" + re.escape(kl) + r"(?![a-z])", low):
                hits += 1
        if not hits:
            return 0.0
        return min(1.0, (hits / len(self.keywords)) * self.weight + 0.35)


def _first_available(reg: PrimitiveRegistry, *candidates: str) -> Optional[str]:
    for c in candidates:
        if c in reg:
            return c
    return None


class TemplatePlanner:
    """Known intent shapes. Cheap, deterministic, and covers the common cases
    without search."""

    def __init__(self, registry: PrimitiveRegistry,
                 oracle_registry=None, engine_oracle=None):
        self.reg = registry
        self.templates: List[Template] = []
        # O20: when a registry is present, every template's build callable
        # is a registered oracle (definition digest + producer); the
        # winning template's bytes are re-verified at plan time and the
        # match is recorded. Without a registry this is exactly the legacy
        # planner (classified as unbound).
        self.oracle_registry = oracle_registry
        self.engine_oracle = engine_oracle
        self._template_bindings: Dict[str, Dict[str, Any]] = {}
        self._install_builtin()

    def add(self, tpl: Template, producer_id: Optional[str] = None,
            token: Optional[str] = None,
            _engine_builtin: bool = False) -> None:
        if self.oracle_registry is None:
            self.templates.append(tpl)
            return
        from swarm_engine.governance.binding_helpers import (
            authenticated_registrar)
        from swarm_engine.governance.oracle_binding import OracleBindingError
        if tpl.name in self._template_bindings:
            raise OracleBindingError(
                f"planner template {tpl.name!r} is already registered -- a "
                "second template claiming the same name is refused")
        oracle_name = f"planner_template.{tpl.name}"
        if _engine_builtin:
            # Engine-pinned builtin: registered at install under the engine
            # identity (the trusted boot path, same as O18 builtins).
            register, engine_producer, _note = authenticated_registrar(
                self.oracle_registry, self.engine_oracle)
            oracle_id, version = register(
                oracle_name, tpl.build,
                input_contract="(goal, registry) -> plan",
                output_contract="plan dict or None",
                source="planner builtin template")
            producer = engine_producer
        else:
            if not producer_id or not token:
                raise OracleBindingError(
                    f"planner template {tpl.name!r}: bound mode requires an "
                    "authenticated producer (producer_id + token) -- "
                    "anonymous template installs are refused")
            if not self.oracle_registry.authenticate(producer_id, token):
                raise OracleBindingError(
                    f"planner template {tpl.name!r}: producer authentication "
                    "failed -- forged or missing identity refused")
            oracle_id, version = self.oracle_registry.register_oracle(
                producer_id, token, oracle_name, tpl.build,
                input_contract="(goal, registry) -> plan",
                output_contract="plan dict or None",
                source="planner template add")
            producer = producer_id
        self._template_bindings[tpl.name] = {
            "oracle_id": oracle_id, "version": version,
            "producer_id": producer}
        self.templates.append(tpl)

    def _invoke_build(self, tpl: Template, goal: Goal) -> Optional[dict]:
        """Invoke a template's build callable, verifying + recording it in
        bound mode (the O1 re-digest pattern); legacy direct call when no
        registry is present."""
        binding = self._template_bindings.get(tpl.name)
        if self.oracle_registry is None or binding is None:
            return tpl.build(goal, self.reg)
        from swarm_engine.governance.binding_helpers import (
            BoundCallable, verify_live_callable)
        verify_live_callable(self.oracle_registry, binding["oracle_id"],
                             binding["version"], tpl.build,
                             what="planner template %r" % (tpl.name,))
        if self.engine_oracle is None:
            return tpl.build(goal, self.reg)
        bound = BoundCallable(
            self.oracle_registry, self.engine_oracle, tpl.build,
            binding["oracle_id"], binding["version"],
            binding["producer_id"],
            what="planner template %r" % (tpl.name,))
        return bound(goal, self.reg)

    def plan(self, goal: Goal) -> Optional[PlanProposal]:
        scored = []
        for t in self.templates:
            s = t.score(goal)
            if s <= 0:
                continue
            if any(r not in self.reg for r in t.requires):
                continue
            scored.append((s, t))
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        # Try builders in score order; a high-scoring template whose build()
        # returns None (e.g. composition template missing a required signal)
        # must not block a lower-scoring but valid template.
        for best_score, best in scored:
            # O20: the winning template's build callable is re-verified
            # against its registration (and the match recorded) in bound
            # mode; legacy direct call otherwise.
            plan = self._invoke_build(best, goal)
            if plan is None:
                continue
            ops = _collect_ops(plan)
            return PlanProposal(plan=plan, strategy=f"template:{best.name}",
                                confidence=best_score,
                                rationale=f"matched intent template {best.name!r}",
                                ops_used=ops)
        return None

    # -- builtin templates --------------------------------------------------
    def _install_builtin(self) -> None:
        reg = self.reg

        # --- aggregate a numeric list -------------------------------------
        def build_aggregate(goal: Goal, r: PrimitiveRegistry):
            import re
            low = goal.text.lower()
            choice = None
            for word, ops in (("average", ("computation.mean",)),
                              ("mean", ("computation.mean",)),
                              ("median", ("computation.median",)),
                              ("product", ("computation.product", "product")),
                              ("total", ("computation.sum",)),
                              ("sum", ("computation.sum",)),
                              ("count", ("data.length", "computation.count")),
                              ("maximum", ("computation.max",)),
                              ("max", ("computation.max",)),
                              ("minimum", ("computation.min",)),
                              ("min", ("computation.min",)),
                              ("spread", ("computation.stddev",)),
                              ("deviation", ("computation.stddev",))):
                # Token-boundary match (Defect #1).
                matched = bool(re.search(
                    r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low))
                if matched:
                    choice = _first_available(r, *ops)
                    if choice:
                        break
            if not choice:
                choice = _first_available(r, "computation.sum", "computation.mean")
            if not choice:
                return None
            arg = "values" if "values" in (r.get(choice).inputs if r.get(choice) else {}) else \
                  next(iter(r.get(choice).inputs))
            return {
                "name": "aggregate",
                "params": {"values": "list"},
                "steps": [{"id": "s1", "op": choice, "args": {arg: {"$param": "values"}}}],
                "output": {"$step": "s1"},
            }

        self.add(Template("aggregate",
                          ["average", "mean", "median", "sum", "total", "count",
                           "product", "max", "min", "maximum", "minimum"],
                          [], build_aggregate, weight=1.4), _engine_builtin=True)

        # --- filter then aggregate ----------------------------------------
        def build_filter_aggregate(goal: Goal, r: PrimitiveRegistry):
            filt = _first_available(r, "data.filter")
            if not filt:
                return None
            agg = None
            low = goal.text
            for word, op in (("average", "computation.mean"), ("mean", "computation.mean"),
                             ("total", "computation.sum"), ("sum", "computation.sum"),
                             ("count", "data.length"), ("max", "computation.max"),
                             ("min", "computation.min")):
                if word in low and op in r:
                    agg = op
                    break
            if not agg:
                return None
            gt = _first_available(r, "logic.greater_than", "logic.gt")
            if not gt:
                return None
            threshold = goal.numbers[0] if goal.numbers else 0
            agg_prim = r.get(agg)
            agg_arg = "values" if "values" in agg_prim.inputs else next(iter(agg_prim.inputs))
            filt_prim = r.get(filt)
            fin = list(filt_prim.inputs)
            coll_arg, fn_arg = fin[0], (fin[1] if len(fin) > 1 else "fn")
            return {
                "name": "filter_then_aggregate",
                "params": {"values": "list", "threshold": "num"},
                "steps": [
                    {"id": "kept", "op": filt, "args": {
                        coll_arg: {"$param": "values"},
                        fn_arg: {"$lambda": {
                            "params": ["x"],
                            "steps": [{"id": "c", "op": gt, "args": {
                                _in(r, gt, 0): {"$var": "x"},
                                _in(r, gt, 1): {"$param": "threshold"}}}],
                            "output": {"$step": "c"}}}}},
                    {"id": "out", "op": agg, "args": {agg_arg: {"$step": "kept"}}},
                ],
                "output": {"$step": "out"},
                "defaults": {"threshold": threshold},
            }

        self.add(Template("filter_aggregate",
                          ["filter", "above", "below", "over", "under",
                           "greater", "more than", "at least"],
                          [], build_filter_aggregate, weight=1.2), _engine_builtin=True)

        # --- sort / rank ---------------------------------------------------
        def build_sort(goal: Goal, r: PrimitiveRegistry):
            op = _first_available(r, "data.sort", "data.sort_by")
            if not op:
                return None
            desc = any(w in goal.text for w in ("descending", "desc", "highest",
                                                "largest", "top", "biggest"))
            prim = r.get(op)
            args: Dict[str, Any] = {next(iter(prim.inputs)): {"$param": "values"}}
            if "reverse" in prim.inputs:
                args["reverse"] = desc
            return {
                "name": "sort_values",
                "params": {"values": "list"},
                "steps": [{"id": "s1", "op": op, "args": args}],
                "output": {"$step": "s1"},
            }

        # "order" removed as a bare keyword — it false-matches the English
        # connector "in order to". Prefer "sort" / "rank" / "arrange" /
        # "order by" style phrases.
        self.add(Template("sort", ["sort", "rank", "arrange", "order by"],
                          [], build_sort, weight=1.3), _engine_builtin=True)

        # --- deduplicate ---------------------------------------------------
        def build_unique(goal: Goal, r: PrimitiveRegistry):
            op = _first_available(r, "data.unique", "data.deduplicate", "data.distinct")
            if not op:
                return None
            return {
                "name": "deduplicate",
                "params": {"values": "list"},
                "steps": [{"id": "s1", "op": op,
                           "args": {next(iter(r.get(op).inputs)): {"$param": "values"}}}],
                "output": {"$step": "s1"},
            }

        self.add(Template("unique", ["unique", "dedup", "deduplicate",
                                     "distinct", "duplicates"],
                          [], build_unique, weight=1.4), _engine_builtin=True)

        # --- aggregate of unique (product/sum/min/max of unique values) -----
        # Defect #1 semantic composition: "product of the unique values"
        # must not be satisfied by unique alone. This template fires only
        # when BOTH a unique-signal and an aggregate-signal are present.
        def build_aggregate_of_unique(goal: Goal, r: PrimitiveRegistry):
            import re
            low = goal.text.lower()
            # Require an explicit unique-family signal; do not fire on
            # aggregate-only goals (e.g. "sum of a list of numbers").
            uniq_signals = ("unique", "distinct", "dedup", "deduplicate", "duplicates")
            if not any(re.search(r"(?<![a-z])" + re.escape(u) + r"(?![a-z])", low)
                       for u in uniq_signals):
                return None
            uniq_op = _first_available(r, "data.unique", "data.deduplicate", "data.distinct")
            if not uniq_op:
                return None
            agg_choice = None
            for word, ops in (("product", ("computation.product", "product")),
                              ("sum", ("computation.sum", "sum")),
                              ("total", ("computation.sum", "sum")),
                              ("maximum", ("computation.max", "max")),
                              ("max", ("computation.max", "max")),
                              ("minimum", ("computation.min", "min")),
                              ("min", ("computation.min", "min")),
                              ("average", ("computation.mean", "mean")),
                              ("mean", ("computation.mean", "mean"))):
                if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low):
                    agg_choice = _first_available(r, *ops)
                    if agg_choice:
                        break
            if not agg_choice:
                return None
            uniq_arg = next(iter(r.get(uniq_op).inputs))
            agg_arg = "values" if "values" in (r.get(agg_choice).inputs or {}) else \
                      next(iter(r.get(agg_choice).inputs))
            return {
                "name": "aggregate_of_unique",
                "params": {"values": "list"},
                "steps": [
                    {"id": "s1", "op": uniq_op,
                     "args": {uniq_arg: {"$param": "values"}}},
                    {"id": "s2", "op": agg_choice,
                     "args": {agg_arg: {"$step": "s1"}}},
                ],
                "output": {"$step": "s2"},
            }

        self.add(Template(
            "aggregate_of_unique",
            # Requires hits from both families; score only rises when both
            # unique-signal and aggregate-signal tokens are present.
            ["unique", "distinct", "dedup",
             "product", "sum", "total", "max", "min", "maximum", "minimum",
             "average", "mean"],
            [], build_aggregate_of_unique, weight=2.0), _engine_builtin=True)

        # --- group and count ------------------------------------------------
        def build_group(goal: Goal, r: PrimitiveRegistry):
            op = _first_available(r, "data.group_by", "data.group")
            if not op:
                return None
            get = _first_available(r, "data.get_field", "data.get", "data.field")
            key = goal.quoted[0] if goal.quoted else "key"
            prim = r.get(op)
            names = list(prim.inputs)
            coll_arg = names[0]
            fn_arg = names[1] if len(names) > 1 else "key_fn"
            if get:
                keyfn = {"$lambda": {
                    "params": ["row"],
                    "steps": [{"id": "k", "op": get, "args": {
                        _in(r, get, 0): {"$var": "row"},
                        _in(r, get, 1): key}}],
                    "output": {"$step": "k"}}}
            else:
                return None
            return {
                "name": "group_rows",
                "params": {"rows": "list"},
                "steps": [{"id": "s1", "op": op,
                           "args": {coll_arg: {"$param": "rows"}, fn_arg: keyfn}}],
                "output": {"$step": "s1"},
            }

        self.add(Template("group", ["group", "bucket", "partition", "categorize"],
                          [], build_group, weight=1.2), _engine_builtin=True)

        # --- map a transform over a list -----------------------------------
        def build_map(goal: Goal, r: PrimitiveRegistry):
            mp = _first_available(r, "data.map")
            if not mp:
                return None
            low = goal.text
            inner = None
            for word, cands in (("upper", ("text.upper", "text.uppercase")),
                                ("lower", ("text.lower", "text.lowercase")),
                                ("trim", ("text.strip", "text.trim")),
                                ("double", ("computation.multiply",)),
                                ("square", ("computation.multiply",)),
                                ("abs", ("computation.abs",)),
                                ("round", ("computation.round",)),
                                ("negate", ("computation.negate",))):
                if word in low:
                    inner = _first_available(r, *cands)
                    if inner:
                        break
            if not inner:
                return None
            ip = r.get(inner)
            iargs: Dict[str, Any] = {_in(r, inner, 0): {"$var": "x"}}
            if len(ip.inputs) > 1 and ("double" in low or "square" in low):
                second = _in(r, inner, 1)
                iargs[second] = 2 if "double" in low else {"$var": "x"}
            names = list(r.get(mp).inputs)
            return {
                "name": "map_transform",
                "params": {"values": "list"},
                "steps": [{"id": "s1", "op": mp, "args": {
                    names[0]: {"$param": "values"},
                    (names[1] if len(names) > 1 else "fn"): {"$lambda": {
                        "params": ["x"],
                        "steps": [{"id": "t", "op": inner, "args": iargs}],
                        "output": {"$step": "t"}}}}}],
                "output": {"$step": "s1"},
            }

        self.add(Template("map", ["map", "transform", "convert each",
                                  "uppercase", "lowercase", "double", "square",
                                  "round", "trim"],
                          [], build_map, weight=1.1), _engine_builtin=True)

        # --- read a file ---------------------------------------------------
        def build_read_file(goal: Goal, r: PrimitiveRegistry):
            op = _first_available(r, "filesystem.read_text", "filesystem.read_file",
                                  "filesystem.read")
            if not op:
                return None
            return {
                "name": "read_file",
                "params": {"path": "str"},
                "steps": [{"id": "s1", "op": op,
                           "args": {_in(r, op, 0): {"$param": "path"}}}],
                "output": {"$step": "s1"},
            }

        self.add(Template("read_file", ["read file", "load file", "open file",
                                        "read the file", "file contents"],
                          [], build_read_file, weight=1.5), _engine_builtin=True)

        # --- json round trip -----------------------------------------------
        def build_parse_json(goal: Goal, r: PrimitiveRegistry):
            op = _first_available(r, "data.from_json", "data.deserialize",
                                  "data.parse_json")
            if not op:
                return None
            return {
                "name": "parse_json",
                "params": {"text": "str"},
                "steps": [{"id": "s1", "op": op,
                           "args": {_in(r, op, 0): {"$param": "text"}}}],
                "output": {"$step": "s1"},
            }

        self.add(Template("parse_json", ["parse json", "decode json", "json to",
                                         "deserialize"],
                          [], build_parse_json, weight=1.5), _engine_builtin=True)

        # --- word / token count --------------------------------------------
        def build_word_count(goal: Goal, r: PrimitiveRegistry):
            tok = _first_available(r, "text.tokenize", "text.split_words", "text.words")
            cnt = _first_available(r, "data.length", "data.count", "computation.count")
            if not (tok and cnt):
                return None
            return {
                "name": "word_count",
                "params": {"text": "str"},
                "steps": [
                    {"id": "t", "op": tok, "args": {_in(r, tok, 0): {"$param": "text"}}},
                    {"id": "n", "op": cnt, "args": {_in(r, cnt, 0): {"$step": "t"}}},
                ],
                "output": {"$step": "n"},
            }

        self.add(Template("word_count", ["word count", "count words",
                                         "how many words", "number of words"],
                          [], build_word_count, weight=1.6), _engine_builtin=True)


def _in(reg: PrimitiveRegistry, op: str, idx: int) -> str:
    """Name of the idx-th declared input of a primitive."""
    p = reg.get(op)
    if not p:
        return "value"
    names = list(p.inputs)
    return names[idx] if idx < len(names) else names[-1]


# ---------------------------------------------------------------------------
# BACKWARD PLANNER
# ---------------------------------------------------------------------------

class BackwardPlanner:
    """Goal-directed search over the registry.

    Starts from the type the goal wants and works backwards: which primitives
    produce that type? For each, can its inputs be satisfied by a plan
    parameter, a literal, or the output of another primitive? Depth-limited so
    the search stays bounded; effect-limited so a search for `num` never
    wanders into the network.
    """

    def __init__(self, registry: PrimitiveRegistry, max_depth: int = 3,
                 max_branch: int = 6):
        self.reg = registry
        self.max_depth = max_depth
        self.max_branch = max_branch

    def plan(self, goal: Goal, available: Optional[Dict[str, TypeSpec]] = None,
             allow_effects: bool = False) -> Optional[PlanProposal]:
        available = available or {"input": ANY}
        target = goal.wants_type
        self._counter = 0

        chain = self._search(target, available, depth=0, allow_effects=allow_effects,
                             hint_words=set(goal.verbs) | set(goal.nouns))
        if not chain:
            return None

        steps: List[Dict[str, Any]] = []
        for i, (op, args) in enumerate(chain):
            steps.append({"id": f"s{i+1}", "op": op, "args": args})
        plan = {
            "name": "synthesized",
            "params": {k: _pname(v) for k, v in available.items()},
            "steps": steps,
            "output": {"$step": steps[-1]["id"]},
        }
        return PlanProposal(plan=plan, strategy="backward_search",
                            confidence=0.55,
                            rationale=f"composed {len(steps)} primitive(s) to reach {target}",
                            ops_used=[op for op, _ in chain])

    def _search(self, target: TypeSpec, available: Dict[str, TypeSpec],
                depth: int, allow_effects: bool,
                hint_words: set) -> Optional[List[Tuple[str, Dict[str, Any]]]]:
        if depth > self.max_depth:
            return None
        self._counter += 1
        if self._counter > 400:
            return None

        candidates = self.reg.producing(target)
        if not allow_effects:
            candidates = [p for p in candidates if p.pure]
        # prefer primitives whose name echoes the goal's own words
        candidates.sort(key=lambda p: (
            0 if any(w in p.name for w in hint_words) else 1,
            len(p.inputs),
        ))

        for prim in candidates[: self.max_branch]:
            args: Dict[str, Any] = {}
            prereq: List[Tuple[str, Dict[str, Any]]] = []
            ok = True
            for aname, aspec in prim.inputs.items():
                bound = self._bind(aspec, available)
                if bound is not None:
                    args[aname] = bound
                    continue
                if aspec.optional:
                    continue
                sub = self._search(aspec, available, depth + 1, allow_effects, hint_words)
                if not sub:
                    ok = False
                    break
                prereq.extend(sub)
                args[aname] = {"$step": f"s{len(prereq)}"}
            if ok:
                return prereq + [(prim.name, args)]
        return None

    @staticmethod
    def _bind(spec: TypeSpec, available: Dict[str, TypeSpec]) -> Optional[Dict[str, Any]]:
        for pname, pspec in available.items():
            if spec.accepts(pspec):
                return {"$param": pname}
        return None


# ---------------------------------------------------------------------------
# COMBINED PLANNER
# ---------------------------------------------------------------------------

def _collect_ops(plan: Dict[str, Any]) -> List[str]:
    out: List[str] = []

    def walk(steps):
        for s in steps or []:
            if "op" in s:
                out.append(s["op"])
            for k in ("then", "else", "body", "catch"):
                if isinstance(s.get(k), list):
                    walk(s[k])
            if isinstance(s.get("branches"), dict):
                for b in s["branches"].values():
                    walk(b)
            for v in (s.get("args") or {}).values():
                if isinstance(v, dict) and "$lambda" in v:
                    walk(v["$lambda"].get("steps"))

    walk(plan.get("steps"))
    return out


class Planner:
    """Template first, backward search as fallback. Returns every viable
    proposal ranked, so the admission layer can pick or the improvement loop
    can try the next one after a failure."""

    def __init__(self, registry: PrimitiveRegistry,
                 oracle_registry=None, engine_oracle=None):
        self.reg = registry
        self.templates = TemplatePlanner(
            registry, oracle_registry=oracle_registry,
            engine_oracle=engine_oracle)
        self.backward = BackwardPlanner(registry)

    # Strategies are ranked by tier first, confidence second. A template match
    # is a deliberate reading of the goal's intent; backward search is a blind
    # type-directed walk that will happily return `negate` for "average" simply
    # because the signature fits. Their confidence numbers are not measured on
    # the same scale, so comparing them directly lets a plausible-looking
    # search result displace a correct intent match. Tier keeps search as what
    # it is: the fallback for goals no template understands.
    _TIER = {"template": 0, "backward_search": 1}

    @classmethod
    def _rank(cls, p: PlanProposal) -> Tuple[int, float]:
        tier = cls._TIER.get(p.strategy.split(":", 1)[0], 2)
        return (tier, -p.confidence)

    def propose(self, goal_text: str, params: Optional[Dict[str, TypeSpec]] = None,
                allow_effects: bool = False, limit: int = 3) -> List[PlanProposal]:
        goal = parse_goal(goal_text)
        out: List[PlanProposal] = []

        t = self.templates.plan(goal)
        if t:
            out.append(t)

        b = self.backward.plan(goal, params, allow_effects=allow_effects)
        if b and all(b.ops_used != p.ops_used for p in out):
            out.append(b)

        out.sort(key=self._rank)
        return out[:limit]

    def best(self, goal_text: str, **kw) -> Optional[PlanProposal]:
        props = self.propose(goal_text, **kw)
        return props[0] if props else None
