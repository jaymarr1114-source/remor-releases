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
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from swarm_engine.primitives.core import (
    ANY, BOOL, DICT, LIST, NUM, STR, Effect, PrimitiveRegistry, TypeSpec, Kind,
)
from swarm_engine.synthesis.semantic_frames import (
    IntentFrame, parse_frame,
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
# GOAL PARSING (NL semantic layer: frame-derived, keyword hints retired)
# ---------------------------------------------------------------------------

@dataclass
class Goal:
    """A normalised request. `raw` is what the caller said; everything else is
    what the semantic frame layer extracted from it.

    `frame` is the IntentFrame this goal was derived from (None only for
    goals built by hand, e.g. in tests). `actionable` is False when the
    frame is AMBIGUOUS or UNKNOWN: the goal carries no reliable meaning,
    so NL entry points (the task-interface UNDERSTAND stage) must fail
    closed instead of misrouting. Programmatic callers that pass purpose
    fragments (codegen purposes like "random number generator") still get
    the legacy keyword-hint extraction for verbs/nouns/wants_type, so
    their behavior is unchanged -- the flag is what distinguishes "no
    grammatical parse" from "a real request".
    """
    raw: str
    verbs: List[str] = field(default_factory=list)
    nouns: List[str] = field(default_factory=list)
    numbers: List[float] = field(default_factory=list)
    quoted: List[str] = field(default_factory=list)
    wants_type: TypeSpec = ANY
    actionable: bool = True
    frame: Optional[IntentFrame] = None

    @property
    def text(self) -> str:
        return self.raw.lower()


# Legacy keyword-hint tables. Kept ONLY as the fallback for non-actionable
# frames (purpose fragments and other unparseable text): for those, the old
# bag-of-words extraction is the best available signal and must not change
# behavior. Actionable frames never consult these.
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


def _legacy_hints(g: Goal, raw: str) -> None:
    """The pre-semantic keyword-hint extraction, verbatim.

    Used only when the frame is AMBIGUOUS/UNKNOWN, so programmatic callers
    passing fragments see exactly the old values.
    """
    low = raw.lower()
    words = re.findall(r"[a-z_]+", low)
    g.verbs = [w for w in words if w in _VERB_HINTS]
    g.nouns = [w for w in words if w not in _VERB_HINTS and len(w) > 3]
    for pat, spec in _OUTPUT_HINTS:
        if pat.search(low):
            g.wants_type = spec
            break


def _frame_verbs(frame: IntentFrame, raw: str) -> List[str]:
    """The frame's predicate: the verb-class head of the request.

    For imperative mood the grammar already established verb-first
    structure, so the head verb is the first word token (after a polite
    'please'). Other moods have no predicate head to report.
    """
    if frame.mood != "imperative":
        return []
    toks = re.findall(r"[A-Za-z]+", raw)
    if not toks:
        return []
    head = toks[0].lower()
    if head == "please" and len(toks) > 1:
        head = toks[1].lower()
    return [head]


def _frame_nouns(frame: IntentFrame) -> List[str]:
    """Entity keywords: content words drawn from the frame's entities.

    Unlike the old bag-of-words noun list, these come from the parsed
    meaning (purpose, prompt, question, goal_text, language, ...), not
    from raw text -- scaffolding words ("what", "able", "terms") never
    appear because the grammar never put them in a slot.
    """
    out: List[str] = []
    seen: set = set()
    for value in (frame.entities or {}).values():
        if not isinstance(value, str):
            continue
        for w in re.findall(r"[a-z]+", value.lower()):
            if len(w) > 3 and w not in seen:
                seen.add(w)
                out.append(w)
    return out


def parse_goal(raw: str) -> Goal:
    """Parse a goal through the semantic frame layer.

    The IntentFrame is the single source of meaning: verbs come from the
    frame's predicate (verb-class head), nouns from the frame's entity
    keywords, wants_type from the frame's output_type; numbers and quoted
    spans use the pre-existing structural extractors.

    AMBIGUOUS/UNKNOWN frames yield a Goal with actionable=False: downstream
    NL entry points fail closed on the flag instead of misrouting. The
    frame-derived verbs/nouns are cleared first and then _legacy_hints
    repopulates them with the legacy keyword-hint extraction, so
    programmatic fragment callers (codegen purposes) behave exactly as
    before.
    """
    g = Goal(raw=raw)
    # Structural extractors (unchanged): quoted spans and numbers are
    # surface facts, not interpretations.
    g.quoted = re.findall(r"[\"']([^\"']+)[\"']", raw)
    g.numbers = [float(n) for n in re.findall(r"-?\d+\.?\d*", raw)]
    frame = parse_frame(raw)
    g.frame = frame
    if not frame.actionable:
        g.actionable = False
        g.verbs = []
        g.nouns = []
        _legacy_hints(g, raw)
        return g
    g.verbs = _frame_verbs(frame, raw)
    g.nouns = _frame_nouns(frame)
    g.wants_type = frame.output_type
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
    # Head-action words this template's reading can realize. Defaults to the
    # keyword tokens; the aggregation builders ALSO bind an aggregation word
    # from the goal text (e.g. "sum of values above 5" -> computation.sum),
    # so the composition templates extend this with _AGGREGATION_HEAD_WORDS.
    # The stuffing detector consults it: a template whose reading realizes
    # neither the matched keywords' action nor any head word is excluded.
    head_words: FrozenSet[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.head_words:
            toks = set()
            for k in self.keywords:
                toks.update(re.findall(r"[a-z]+", k.lower()))
            self.head_words = frozenset(toks)

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


# Head-action vocabulary bound by the aggregation builders (build_aggregate
# and build_filter_aggregate scan the goal text for these words to pick the
# aggregation op). A goal like "sum of values above 5" is headed by "sum":
# filter_aggregate matches on "above", but its reading genuinely realizes
# the head action via the aggregation word -- so these words count as
# realized head actions for the aggregation templates, and the stuffing
# detector must not exclude such a composition reading.
_AGGREGATION_HEAD_WORDS = frozenset({
    "average", "mean", "total", "sum", "count",
    "maximum", "max", "minimum", "min", "product",
})


def _hint_toksets(hint_words: set) -> List[frozenset]:
    """Tokenise hint words once per search for the steering check below."""
    out = []
    for w in hint_words:
        toks = frozenset(re.findall(r"[a-z]+", w.lower()))
        if toks:
            out.append(toks)
    return out


def _hint_steers(hint_toksets: List[frozenset], prim_name: str) -> bool:
    """Token-boundary hint steering (the same boundary the template tier got
    for Defect #1, applied to primitive names).

    A hint steers a primitive only when every token of the hint appears as a
    whole token of the primitive's snake_case name. Raw substring matching
    (``w in p.name``) let unrelated primitives jump the queue: the hint
    "value" promoted "values", "sum" promoted "cumsum", "record" promoted
    "profile_records", "name" promoted "rename". Steering is ordering-only,
    so a false steer is enough to emit the wrong plan -- _search returns on
    first success and the correct primitive is never tried.
    """
    name_toks = set(re.findall(r"[a-z]+", prim_name.lower()))
    return any(h <= name_toks for h in hint_toksets)


def _first_available(reg: PrimitiveRegistry, *candidates: str) -> Optional[str]:
    for c in candidates:
        if c in reg:
            return c
    return None


# Delexical ("light") verbs: grammatical predicates that carry no action
# semantics of their own ("get the average", "set the value"). They are
# carved out of the head-verb contradiction check below so genuine phrasing
# keeps its template; only "get"/"set" are bare primitive names today, the
# rest future-proof the set if primitives are registered under them.
_LIGHT_VERBS = frozenset({
    "get", "set", "take", "make", "do", "have", "give", "put", "use",
})


def _head_word(raw: str) -> str:
    """The goal's predicate head: first word token (after a polite 'please').

    Mirrors _frame_verbs so the detector and the frame layer agree on what
    the goal's action word is.
    """
    toks = re.findall(r"[A-Za-z]+", raw)
    if not toks:
        return ""
    head = toks[0].lower()
    if head == "please" and len(toks) > 1:
        head = toks[1].lower()
    return head


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

    def _template_contradicts_head(self, tmpl: Template, goal: Goal) -> bool:
        """Stuffing detector: does this template's keyword reading contradict
        the goal's own predicate?

        A template match is a deliberate reading of intent -- but keyword
        stuffing ("negate the number sum total", "reverse the list of values
        sum total average") can make a template fire on stray nouns while the
        goal's action word names a *different* primitive entirely. When the
        head verb is itself a registered primitive name (a real action, not a
        light verb like "get") and NONE of the template's matched keywords
        covers it, the template is misreading the goal: it is skipped so it
        cannot outrank the search tier on tiering alone.

        Why not a score bar: a genuine single-keyword match ("compute the
        average of the list" -> aggregate 0.477) and a stuffed single-keyword
        match ("negate the number sum" -> aggregate 0.477) score IDENTICALLY --
        no threshold separates them. The head verb is the only signal that
        does. A non-contradicted template still outranks search absolutely;
        that asymmetry is by design (deliberate reading > blind walk).
        """
        head = _head_word(goal.raw)
        if not head or head in _LIGHT_VERBS:
            return False
        if head not in set(self.reg.names()):
            return False
        low = goal.text.lower()
        matched = [k for k in tmpl.keywords
                   if re.search(r"(?<![a-z])" + re.escape(k.lower()) + r"(?![a-z])",
                                low)]
        # Covered: the template fired on the head action itself (equality or
        # either-way containment, e.g. head "sort" vs keyword "sort"), or the
        # head word is one the template's build can realize even without it
        # being a matched keyword -- the composition templates bind an
        # aggregation word from the goal text ("sum of values above 5":
        # filter_aggregate matches "above", but its plan genuinely computes
        # the head action "sum"). Only when NEITHER holds is the reading a
        # contradiction: the head names a real primitive the plan will not
        # perform, which is exactly the stuffing signature.
        if any(head == k or head in k or k in head for k in matched):
            return False
        if head in tmpl.head_words:
            return False
        return True

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
        # must not block a lower-scoring but valid template. A template whose
        # keyword reading contradicts the goal's head verb (stuffing
        # detector) is skipped the same way: its intent reading is invalid,
        # so it must not outrank the search tier.
        for best_score, best in scored:
            if self._template_contradicts_head(best, goal):
                continue
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

        _agg_kw = ("average", "mean", "median", "sum", "total", "count",
                   "product", "max", "min", "maximum", "minimum")
        self.add(Template("aggregate", _agg_kw,
                          [], build_aggregate, weight=1.4,
                          head_words=frozenset(_agg_kw) | _AGGREGATION_HEAD_WORDS),
                 _engine_builtin=True)

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

        _fagg_kw = ("filter", "above", "below", "over", "under",
                    "greater", "more than", "at least")
        self.add(Template("filter_aggregate", _fagg_kw,
                          [], build_filter_aggregate, weight=1.2,
                          head_words=frozenset(_fagg_kw) | _AGGREGATION_HEAD_WORDS),
                 _engine_builtin=True)

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

        _aou_kw = ("unique", "distinct", "dedup",
                   "product", "sum", "total", "max", "min", "maximum", "minimum",
                   "average", "mean")
        self.add(Template(
            "aggregate_of_unique",
            # Requires hits from both families; score only rises when both
            # unique-signal and aggregate-signal tokens are present.
            _aou_kw,
            [], build_aggregate_of_unique, weight=2.0,
            head_words=frozenset(_aou_kw) | _AGGREGATION_HEAD_WORDS),
            _engine_builtin=True)

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
        # Prefer primitives whose name echoes the goal's own words --
        # token-boundary only (see _hint_steers): a hint steers when its
        # tokens are whole tokens of the primitive's name, never on a raw
        # substring. Steering is ordering-only, never filtering.
        hint_toksets = _hint_toksets(hint_words)
        # TIE-BREAKING RULE: promotion is the FINAL tie-breaker. It reorders
        # only candidates already tied on hint-steering AND arity; it never
        # overrides hint-steering or the simplicity (fewer-inputs)
        # preference. A promoted (ReviewBoard-admitted, verdict-bound)
        # primitive earns priority over a built-in in ties; an unpromoted
        # capability never gains priority. Promotion stays the gate --
        # discovery only orders among the trusted. DEFECT-2's name-first
        # _bind is untouched.
        reg = self.reg

        def _promoted_rank(p) -> int:
            is_prom = getattr(reg, "is_promoted", None)
            try:
                return 0 if (is_prom is not None and is_prom(p.name)) else 1
            except Exception:
                return 1  # fail closed: no priority on any doubt

        candidates.sort(key=lambda p: (
            0 if _hint_steers(hint_toksets, p.name) else 1,
            len(p.inputs),
            _promoted_rank(p),
        ))

        for prim in candidates[: self.max_branch]:
            args: Dict[str, Any] = {}
            prereq: List[Tuple[str, Dict[str, Any]]] = []
            ok = True
            for aname, aspec in prim.inputs.items():
                bound = self._bind(aname, aspec, available)
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
    def _bind(aname: str, spec: TypeSpec,
              available: Dict[str, TypeSpec]) -> Optional[Dict[str, Any]]:
        # Name-first binding (2026-09-27, M1 shakedown DEFECT-2): a
        # primitive's input names are part of its contract. Purely
        # type-directed binding collapsed every same-typed input onto the
        # first type-compatible parameter (std->mean, clip->mean), so the
        # composed plan executed "successfully" while computing the wrong
        # function. Prefer the same-named parameter when its type fits;
        # fall back to type-only binding when no name matches.
        if aname in available and spec.accepts(available[aname]):
            return {"$param": aname}
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
