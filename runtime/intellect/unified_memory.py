"""Unified memory: epistemic + capability + planner registry as one process.

James's developmental vision (2026-09-27): unified memory across
perception/learning/action -- the epistemic store, the capability store,
and the planner's primitive registry queried as ONE process, not three
separate components with three separate vocabularies.

Two mechanisms live here:

1. ``UnifiedMemory`` -- one query interface spanning all three stores.
   The stores stay authoritative: no duplicated state, no shadow copies,
   no writes to the capability store or the registry (both are
   read-only from this layer). The layer unifies access and resolves
   cross-references (capability <-> distillation experience record <->
   epistemic delta) from the linkage the loop itself writes.

2. Planner-attempt Z-check -- the honest replacement for the lexical
   ``_objective_overlap`` heuristic in the ingestion gate. Instead of
   asking "do any words overlap?", the system asks the planner to
   ATTEMPT the objective as a bounded sandboxed dry-run (PURE policy:
   zero granted effect capabilities). What the planner can actually
   reach -- verified by executing its plan against demonstrated
   evidence in a fresh subprocess -- becomes the Z side of the delta.

   The failure Q5 demonstrated was one-directional and lexical:
   ``image_generate`` vs "generate a quarterly revenue summary" shares
   the word "generate", so the heuristic reported overlap and a real
   composition gap was MISSED. No lexical retuning fixes this (it just
   moves errors around). The planner's blind backward search proposes a
   ``deserialize`` plan for that objective -- a type-fit coincidence --
   and executing it against the demonstrated evidence shows it does not
   reproduce the demonstrated behavior: genuine gap, honestly reported.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4


# ---------------------------------------------------------------------------
# Small text utilities (lexical RETRIEVAL only -- the Z-check below is where
# the behavioral rigor lives; retrieval just finds candidate records)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> List[str]:
    return [t for t in "".join(
        c.lower() if c.isalnum() else " " for c in (text or "")
    ).split() if len(t) >= 3]


def _overlap_score(query_tokens: Sequence[str], *fields: str) -> float:
    """Fraction of query tokens appearing in any of the fields."""
    if not query_tokens:
        return 0.0
    hay = " ".join(fields).lower()
    hits = sum(1 for t in query_tokens if t in hay)
    return hits / len(query_tokens)


def _json_norm(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except Exception:
        return str(value)


def _values_equal(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return _json_norm(a) == _json_norm(b)


# ---------------------------------------------------------------------------
# Planner-attempt Z-check
# ---------------------------------------------------------------------------

#: Classifications returned by attempt_z. Only "verified_reach" sets
#: reached=True -- every other outcome is an honestly characterized gap.
Z_NO_PROPOSAL = "no_proposal"
Z_RENDER_FAILED = "render_failed"
Z_EXECUTION_FAILED = "execution_failed"
Z_OUTPUT_MISMATCH = "output_mismatch"
Z_PROPOSAL_UNVERIFIED = "proposal_unverified"
Z_VERIFIED_REACH = "verified_reach"


@dataclass
class ZCheckResult:
    """The characterized outcome of a planner-attempt Z-check."""
    objective: str
    reached: bool
    classification: str
    strategy: str = ""
    ops_used: List[str] = field(default_factory=list)
    confidence: float = 0.0
    attempted: List[str] = field(default_factory=list)
    stopped_at: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "objective": self.objective,
            "reached": self.reached,
            "classification": self.classification,
            "strategy": self.strategy,
            "ops_used": list(self.ops_used),
            "confidence": self.confidence,
            "attempted": list(self.attempted),
            "stopped_at": self.stopped_at,
            "detail": dict(self.detail),
        }


def evidence_from_demo_actions(
    actions: Sequence[Any],
) -> List[Tuple[Dict[str, Any], Any]]:
    """Convert observed demo actions to (inputs, expected) evidence pairs.

    Each action must have carried observed inputs AND outputs (the
    ingestion gate's sufficient-evidence bar). The expected value is the
    action's single output value when it has exactly one, else the whole
    outputs dict -- documented, not guessed.
    """
    pairs = []
    for a in actions:
        inputs = dict(getattr(a, "inputs", {}) or {})
        outputs = dict(getattr(a, "outputs", {}) or {})
        if not inputs or not outputs:
            continue
        expected = next(iter(outputs.values())) if len(outputs) == 1 else outputs
        pairs.append((inputs, expected))
    return pairs


# ---------------------------------------------------------------------------
# Distillation experience (James's delta schema: X objective, Y what the
# external agent did, Z what REMOR could already do, gap Y-Z, T technique,
# E evidence, D dependencies, V verification, C resulting capability)
# ---------------------------------------------------------------------------

def record_distillation_experience(epistemic: "EpistemicStore",
                                   objective: str, delta_id: str,
                                   Y: Any = None, Z: Any = None,
                                   T: Any = None, E: Any = None,
                                   D: Any = None, V: Any = None,
                                   C: Any = None) -> str:
    """Append an M2-style experience record to the epistemic store.

    V10-P1: this is the acquisition loop's experience write path, cut over
    to the unified write path (record_experience). The record lands in the
    epistemic store via the frozen EpistemicStore API, stamped with
    provenance (origin loop, timestamp, causal chain). Returns the
    observation id.
    """
    raw = {"objective": objective, "delta_id": delta_id,
           "Y": Y, "Z": Z, "T": T, "E": E, "D": D, "V": V,
           "synthesized_capability_id": C}
    return record_experience(
        epistemic,
        origin_loop="acquisition",
        kind="distillation",
        content=f"distillation experience: {objective} (technique: {T})",
        raw=raw,
        causal_chain=[delta_id],
        source="distillation-loop")


def _get_planner(registry: Any) -> Any:
    from swarm_engine.synthesis.planner import Planner
    return Planner(registry)


def _get_registry() -> Any:
    from swarm_engine.primitives import build_registry
    return build_registry()


def attempt_z(
    objective: str,
    evidence: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None,
    planner: Any = None,
    registry: Any = None,
    epistemic: Any = None,
    record_attempt: bool = True,
) -> ZCheckResult:
    """Attempt the objective with the real planner; characterize what Z can do.

    The planner proposes (PURE only -- ``allow_effects=False``); the best
    proposal is rendered to source and executed in a fresh PURE subprocess
    against the demonstrated evidence. ``reached`` is True ONLY when every
    evidence example reproduces exactly. A failed attempt is a
    characterized result (what was tried, where it stopped), never a
    silent verdict.

    This function never raises on a failed check -- the failure is named
    in the result (the M2 pattern).

    When ``epistemic`` is provided and ``record_attempt`` is true, the
    attempt is persisted as an observation (source="z-check") through the
    frozen epistemic API, and prior attempts on the same objective are
    surfaced in ``detail["prior_attempts"]`` -- the loop observing its
    own history. Prior attempts are advisory: the attempt always
    re-runs (the world may have changed); history never substitutes for
    the attempt.
    """
    ev_list = list(evidence or [])
    result = ZCheckResult(objective=objective, reached=False,
                          classification=Z_NO_PROPOSAL)
    try:
        return _attempt_z_inner(objective, ev_list, planner, registry,
                                epistemic if record_attempt else None, result)
    except Exception as exc:  # never raise: characterize
        result.classification = Z_EXECUTION_FAILED
        result.stopped_at = "attempt"
        result.detail["error"] = f"{type(exc).__name__}: {exc}"
        return result


def _attempt_z_inner(objective, ev_list, planner, registry, epistemic,
                     result: ZCheckResult) -> ZCheckResult:
    if registry is None:
        registry = _get_registry()
    if planner is None:
        planner = _get_planner(registry)

    # Prior attempts: the loop's own history on this objective, advisory.
    if epistemic is not None:
        try:
            result.detail["prior_attempts"] = [
                {"observation_id": o.observation_id,
                 "classification": (o.raw or {}).get("classification"),
                 "at": o.at}
                for o in _prior_attempt_observations(epistemic, objective)
            ]
        except Exception:
            result.detail["prior_attempts"] = []

    # 1. The planner attempts. PURE only: the dry-run must not need effects.
    try:
        proposals = planner.propose(objective, allow_effects=False, limit=3)
    except Exception as exc:
        result.classification = Z_EXECUTION_FAILED
        result.stopped_at = "proposal"
        result.detail["error"] = f"planner.propose raised {type(exc).__name__}: {exc}"
        result.attempted = ["template", "backward_search"]
        _persist_attempt(epistemic, result)
        return result

    strategies = [getattr(p, "strategy", "?") for p in (proposals or [])]
    result.attempted = strategies or ["template", "backward_search"]
    if not proposals:
        result.classification = Z_NO_PROPOSAL
        result.stopped_at = "proposal"
        result.detail["reason"] = ("neither template nor backward search "
                                   "produced a proposal")
        _persist_attempt(epistemic, result)
        return result

    best = proposals[0]
    result.strategy = getattr(best, "strategy", "")
    result.ops_used = list(getattr(best, "ops_used", []) or [])
    result.confidence = float(getattr(best, "confidence", 0.0) or 0.0)

    # 2. Render the plan to standalone source (fail closed on bad shapes).
    try:
        from swarm_engine.synthesis.codegen import (
            render_plan_to_source, CodegenError)
        source, _meta = render_plan_to_source(best.plan, registry,
                                              purpose=f"z-check: {objective[:80]}")
    except Exception as exc:
        result.classification = Z_RENDER_FAILED
        result.stopped_at = "render"
        result.detail["error"] = f"{type(exc).__name__}: {exc}"
        _persist_attempt(epistemic, result)
        return result

    # 3. Without evidence the plan is a claim, not a demonstrated ability.
    if not ev_list:
        result.classification = Z_PROPOSAL_UNVERIFIED
        result.stopped_at = "verification"
        result.detail["reason"] = (
            "planner proposed a plan but no demonstrated evidence was "
            "provided to verify it against; a proposal is not proof of "
            "ability")
        _persist_attempt(epistemic, result)
        return result

    # 4. Execute against every evidence example in fresh PURE subprocesses.
    from swarm_engine.agent_org.subprocess_runner import run_code
    mismatches = []
    for idx, (inputs, expected) in enumerate(ev_list):
        try:
            rep = run_code(source, "run", [dict(inputs)])
        except Exception as exc:
            result.classification = Z_EXECUTION_FAILED
            result.stopped_at = "execution"
            result.detail["error"] = (f"example {idx}: run_code raised "
                                      f"{type(exc).__name__}: {exc}")
            _persist_attempt(epistemic, result)
            return result
        vals = getattr(rep, "value", None) or []
        if not vals or not vals[0].get("ok"):
            result.classification = Z_EXECUTION_FAILED
            result.stopped_at = "execution"
            result.detail["error"] = (
                f"example {idx}: subprocess failed: "
                f"{(vals[0].get('error') if vals else getattr(rep, 'error', ''))}")
            _persist_attempt(epistemic, result)
            return result
        actual = vals[0].get("value")
        if not _values_equal(actual, expected):
            mismatches.append({"example": idx, "inputs": inputs,
                               "expected": expected, "actual": actual})
    if mismatches:
        result.classification = Z_OUTPUT_MISMATCH
        result.stopped_at = "verification"
        result.detail["mismatches"] = mismatches
        result.detail["reason"] = (
            "the planner's plan executed but did not reproduce the "
            "demonstrated behavior")
        _persist_attempt(epistemic, result)
        return result

    result.reached = True
    result.classification = Z_VERIFIED_REACH
    result.stopped_at = "verification"
    result.detail["examples_verified"] = len(ev_list)
    _persist_attempt(epistemic, result)
    return result


def _prior_attempt_observations(epistemic: Any,
                                objective: str) -> List[Any]:
    out = []
    for o in epistemic.all_observations():
        if getattr(o, "source", "") != "z-check":
            continue
        if (o.raw or {}).get("objective") == objective:
            out.append(o)
    return out


def _persist_attempt(epistemic: Any, result: ZCheckResult) -> None:
    if epistemic is None:
        return
    try:
        # V10-P1: the z-check attempt log is cut over to the unified write
        # path (origin loop "planning"). Source stays "z-check" so the
        # prior-attempt scan keeps working; raw keeps its legacy keys.
        record_experience(
            epistemic,
            origin_loop="planning",
            kind="z_check_attempt",
            content=(f"z-check on {result.objective!r}: "
                     f"{result.classification} "
                     f"(strategy={result.strategy or 'none'}, "
                     f"ops={result.ops_used}, reached={result.reached})"),
            raw={"type": "z_check_attempt", "objective": result.objective,
                 **result.as_dict()},
            causal_chain=[result.objective],
            source="z-check")
    except Exception:
        pass  # attempt logging never breaks the check


# ---------------------------------------------------------------------------
# UnifiedMemory: one query layer over the three stores
# ---------------------------------------------------------------------------

@dataclass
class UnifiedAnswer:
    question: str
    epistemic_hits: List[Dict[str, Any]] = field(default_factory=list)
    capability_hits: List[Dict[str, Any]] = field(default_factory=list)
    primitive_hits: List[Dict[str, Any]] = field(default_factory=list)
    links: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"question": self.question,
                "epistemic_hits": self.epistemic_hits,
                "capability_hits": self.capability_hits,
                "primitive_hits": self.primitive_hits,
                "links": self.links}


class UnifiedMemory:
    """One query interface over epistemic / capability / planner vocabularies.

    All three stores stay authoritative; this layer holds no records of
    its own (the z-check attempt log lives in the epistemic store via
    the frozen API). The capability store and the primitive registry
    are read-only from here -- never edited, never badge-flipped.
    """

    def __init__(self, epistemic: Any, capabilities: Any, registry: Any,
                 planner: Any = None):
        self.epistemic = epistemic
        self.capabilities = capabilities
        self.registry = registry
        self.planner = planner

    # -- unified query ----------------------------------------------------
    def query(self, question: str, limit: int = 10) -> UnifiedAnswer:
        toks = _tokens(question)
        ans = UnifiedAnswer(question=question)
        ans.epistemic_hits = self._search_epistemic(toks, limit)[:limit]
        ans.capability_hits = self._search_capabilities(toks, limit)[:limit]
        ans.primitive_hits = self._search_primitives(toks, limit)[:limit]
        ans.links = self._resolve_links(ans)
        return ans

    def _search_epistemic(self, toks: Sequence[str],
                          limit: int) -> List[Dict[str, Any]]:
        hits = []
        try:
            observations = self.epistemic.all_observations()
        except Exception:
            observations = []
        for o in observations:
            score = _overlap_score(toks, o.content or "", o.source or "")
            if score > 0:
                hits.append({"kind": "observation",
                             "id": o.observation_id,
                             "source": o.source,
                             "summary": (o.content or "")[:200],
                             "score": round(score, 3)})
        try:
            hyps = self.epistemic.all_hypotheses()
        except Exception:
            hyps = []
        for h in hyps:
            score = _overlap_score(toks, getattr(h, "statement", "") or "")
            if score > 0:
                hits.append({"kind": "hypothesis",
                             "id": getattr(h, "hypothesis_id", "?"),
                             "source": "hypothesis",
                             "summary": (getattr(h, "statement", "") or "")[:200],
                             "score": round(score, 3)})
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    def _search_capabilities(self, toks: Sequence[str],
                             limit: int) -> List[Dict[str, Any]]:
        hits = []
        try:
            records = self.capabilities.list(status="active", limit=200)
        except Exception:
            records = []
        for r in records:
            score = _overlap_score(
                toks, getattr(r, "name", "") or "",
                getattr(r, "goal", "") or "",
                getattr(r, "capability_id", "") or "")
            if score > 0:
                hits.append({"kind": "capability",
                             "id": getattr(r, "capability_id", "?"),
                             "source": "capability_store",
                             "summary": (f"{getattr(r, 'name', '?')}: "
                                         f"{getattr(r, 'goal', '') or ''}")[:200],
                             "score": round(score, 3),
                             "status": getattr(r, "status", "?")})
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    def _search_primitives(self, toks: Sequence[str],
                           limit: int) -> List[Dict[str, Any]]:
        hits = []
        try:
            names = self.registry.names()
        except Exception:
            names = []
        get = getattr(self.registry, "get", None)
        for n in names:
            prim = get(n) if callable(get) else None
            doc = getattr(prim, "doc", "") if prim else ""
            fam = getattr(prim, "family", "") if prim else ""
            score = _overlap_score(toks, n or "", doc or "", fam or "")
            if score > 0:
                hits.append({"kind": "primitive",
                             "id": n,
                             "source": "primitive_registry",
                             "summary": f"{n} [{fam}]: {doc[:120]}",
                             "score": round(score, 3)})
        hits.sort(key=lambda h: -h["score"])
        return hits[:limit]

    def _delta_id_of(self, raw: Dict[str, Any]) -> Optional[str]:
        """Extract a delta id from an observation's raw payload, honoring
        the real shapes the loop writes: M1 persists the delta record at
        raw["delta"]["delta_id"]; M2's experience log writes
        raw["delta_id"."""
        if not raw:
            return None
        if raw.get("delta_id"):
            return raw["delta_id"]
        inner = raw.get("delta")
        if isinstance(inner, dict) and inner.get("delta_id"):
            return inner["delta_id"]
        return None

    def _resolve_links(self, ans: UnifiedAnswer) -> List[Dict[str, Any]]:
        """Cross-references from the linkage the loop itself writes.

        delta observation --(raw.delta_id)--> experience records, and
        experience/delta records --(capability_id mention)--> capability
        records. Only structural links; no guessing.
        """
        links = []
        delta_ids = set()
        for h in ans.epistemic_hits:
            rid = self._delta_id_of(self._raw_of(h.get("id")) or {})
            if rid:
                delta_ids.add(rid)
        # experience records -> their delta
        for h in ans.epistemic_hits:
            raw = self._raw_of(h.get("id")) or {}
            did = self._delta_id_of(raw)
            if did and h.get("id") != did:
                links.append({"from": h["id"], "to": did,
                              "via": "delta_id"})
        # capability -> mentioning records
        cap_ids = {h["id"] for h in ans.capability_hits}
        for h in ans.epistemic_hits:
            raw = self._raw_of(h.get("id")) or {}
            blob = _json_norm(raw) + " " + (h.get("summary") or "")
            for cid in cap_ids:
                if cid and cid in blob:
                    links.append({"from": cid, "to": h["id"],
                                  "via": "capability_id_mention"})
        # delta -> synthesized capability (M2 fills C)
        for h in ans.epistemic_hits:
            raw = self._raw_of(h.get("id")) or {}
            inner = raw.get("delta") if isinstance(raw.get("delta"), dict) else {}
            cap = (raw.get("synthesized_capability_id")
                   or raw.get("capability_id")
                   or inner.get("C"))
            if cap and isinstance(cap, str):
                links.append({"from": h["id"], "to": cap,
                              "via": "synthesized_capability"})
        # dedupe
        seen, out = set(), []
        for l in links:
            key = (l["from"], l["to"], l["via"])
            if key not in seen:
                seen.add(key)
                out.append(l)
        return out

    def _raw_of(self, observation_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not observation_id:
            return None
        try:
            for o in self.epistemic.all_observations():
                if o.observation_id == observation_id:
                    return o.raw or {}
        except Exception:
            pass
        return None

    # -- traces -----------------------------------------------------------
    def trace_delta(self, delta_id: str) -> Dict[str, Any]:
        """Everything the loop knows about one delta: the delta record,
        its experience records, and any synthesized capability."""
        out = {"delta_id": delta_id, "delta": None, "experience": [],
               "capability_id": None}
        try:
            observations = self.epistemic.all_observations()
        except Exception:
            observations = []
        for o in observations:
            raw = o.raw or {}
            oid = o.observation_id
            did = self._delta_id_of(raw)
            if oid != delta_id and did != delta_id:
                continue
            is_delta_record = (
                oid == delta_id
                or getattr(o, "source", "") == "technique_delta"
                or isinstance(raw.get("delta"), dict))
            if is_delta_record:
                out["delta"] = {"id": oid, "source": o.source,
                                "summary": (o.content or "")[:200],
                                "raw": raw}
                inner = raw.get("delta") if isinstance(
                    raw.get("delta"), dict) else {}
                cap = (raw.get("synthesized_capability_id")
                       or raw.get("capability_id")
                       or inner.get("C"))
                if cap and isinstance(cap, str):
                    out["capability_id"] = cap
            else:
                out["experience"].append(
                    {"id": oid, "source": o.source,
                     "summary": (o.content or "")[:200]})
        return out

    def trace_capability(self, capability_id: str) -> Dict[str, Any]:
        """Everything the loop knows about one capability: its record,
        mentioning experience records, and history."""
        out = {"capability_id": capability_id, "record": None,
               "experience": [], "history": []}
        try:
            rec = self.capabilities.get(capability_id)
        except Exception:
            rec = None
        if rec is not None:
            out["record"] = {
                "id": getattr(rec, "capability_id", capability_id),
                "name": getattr(rec, "name", "?"),
                "status": getattr(rec, "status", "?"),
                "description": (getattr(rec, "description", "") or "")[:200]}
        try:
            observations = self.epistemic.all_observations()
        except Exception:
            observations = []
        for o in observations:
            blob = _json_norm(o.raw or {}) + " " + (o.content or "")
            if capability_id and capability_id in blob:
                out["experience"].append(
                    {"id": o.observation_id, "source": o.source,
                     "summary": (o.content or "")[:200]})
        try:
            hist = self.capabilities.history(capability_id) or []
        except Exception:
            hist = []
        out["history"] = [
            {"status": getattr(h, "status", "?"), "at": getattr(h, "at", None)}
            for h in hist[:10]]
        return out

    def prior_attempts(self, objective: str) -> List[Dict[str, Any]]:
        """Z-check attempts the loop has made on this objective."""
        out = []
        try:
            observations = self.epistemic.all_observations()
        except Exception:
            return out
        for o in observations:
            if getattr(o, "source", "") != "z-check":
                continue
            if (o.raw or {}).get("objective") == objective:
                out.append({"id": o.observation_id,
                            "classification": (o.raw or {}).get("classification"),
                            "reached": (o.raw or {}).get("reached"),
                            "at": o.at})
        return out

    def similar_experiences(self, objective: str,
                            top_k: int = 3) -> List[Dict[str, Any]]:
        """Prior distillation-loop experiences whose recorded objective
        overlaps this one. This is how a distilled technique influences a
        later decision: the experience is retrieved through the unified
        layer and attached to the Z-check's detail."""
        scored = []
        try:
            observations = self.epistemic.all_observations()
        except Exception:
            return []
        for o in observations:
            if getattr(o, "source", "") != "distillation-loop":
                continue
            score = _overlap_score(_tokens(objective), o.content or "")
            if score > 0:
                scored.append({"id": o.observation_id,
                               "summary": (o.content or "")[:200],
                               "score": round(score, 3),
                               "delta_id": self._delta_id_of(o.raw or {})})
        scored.sort(key=lambda h: -h["score"])
        return scored[:top_k]

    # -- the Z-check through the unified layer -----------------------------
    def z_check(self, objective: str,
              evidence: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None,
              ) -> ZCheckResult:
        """Planner-attempt Z-check with the unified layer's stores.

        Before attempting, consults prior experience through the same
        layer (cross-store retrieval informing the decision); the attempt
        itself is persisted as an epistemic observation.
        """
        res = attempt_z(objective, evidence=evidence, planner=self.planner,
                        registry=self.registry, epistemic=self.epistemic)
        res.detail["prior_experiences"] = self.similar_experiences(objective)
        return res


# ---------------------------------------------------------------------------
# V10-P1 unified memory cutover: the single read/write path for experience
# records, the store adapter catalog, and the census.
#
# Before V10-P1, UnifiedMemory was a query-only layer over the epistemic,
# capability, and planner-registry vocabularies: it never held or wrote
# records, and it was never instantiated in production. Every loop kept its
# own store handles (EpistemicStore here, CapabilityStore there, GapRegistry
# in gaps.db, the oracle trust sidecar, service DBs...), so "unified memory"
# was a name, not a mechanism.
#
# V10-P1 cuts the loops over to this module as the single read/write path
# for experience/memory records:
#
#   record_experience()  -- THE write path. Every experience record written
#       through the acquisition loop goes through here. The write lands in
#       the epistemic store via the FROZEN EpistemicStore API (no second
#       source of truth: the epistemic store stays the physical substrate),
#       stamped with provenance: origin loop, timestamp, causal chain.
#   read_experiences()   -- THE read path. Any loop (dispatch included) reads
#       records back through here, filtered by provenance.
#
# The operational stores themselves are BRIDGED, not migrated: STORE_CATALOG
# registers one adapter entry per persistent store found by the V10-P1
# structural survey (three independent survey passes, 2026-09-27), with the
# authoritative table names verified against a fresh engine boot. The census
# (UnifiedMemory.census) then proves the negative: every table in the engine
# DB must be claimed by exactly one adapter. An unclaimed table is a defect
# -- a private store invisible to the other loops. A table claimed twice is
# a defect -- two sources of truth.
#
# Deliberate exception, documented not hidden: the oracle trust registry
# ({engine_db}.oracle.db) is an INDEPENDENT trust authority. A trust record
# must stay independent of the stores it guards (V9 trust architecture), so
# the facade registers its existence and verifies it is present, but never
# bridges its contents into the unified read/write path. Weakening that
# separation to "unify" it would be a trust defect, not a cutover.
# ---------------------------------------------------------------------------

#: Schema version stamped into every provenance block.
EXPERIENCE_SCHEMA = 1

#: Canonical origin-loop names for provenance.
ORIGIN_ACQUISITION = "acquisition"
ORIGIN_DISPATCH = "dispatch"
ORIGIN_PLANNING = "planning"
ORIGIN_VERIFICATION = "verification"
ORIGIN_TRUST = "trust"
ORIGIN_MEMORY = "memory"
ORIGIN_INTELLECT = "intellect"

#: Raw-JSON key carrying the provenance block. Top-level record fields are
#: never renamed by the cutover; provenance is additive.
PROVENANCE_KEY = "_provenance"


def _canonical_provenance(origin_loop: str,
                          kind: str,
                          write_path: str,
                          causal_chain: Optional[Sequence[str]] = None
                          ) -> Dict[str, Any]:
    """The one provenance block every unified write path stamps.

    Additive and uniform across record types (observations, evidence,
    hypotheses, experiments): schema version, origin loop, record kind,
    causal chain, timestamp, and which facade function performed the write.
    On merge collisions with pre-existing caller provenance, the canonical
    keys win -- the block must stay machine-checkable.
    """
    return {
        "schema": EXPERIENCE_SCHEMA,
        "origin_loop": origin_loop,
        "kind": kind,
        "causal_chain": list(causal_chain or []),
        "recorded_at": time.time(),
        "write_path": write_path,
    }


def _stamp_provenance(existing: Optional[Dict[str, Any]],
                      origin_loop: str,
                      kind: str,
                      write_path: str,
                      causal_chain: Optional[Sequence[str]] = None
                      ) -> Dict[str, Any]:
    """Merge the canonical block over caller-supplied provenance.

    Caller provenance (e.g. a hypothesis's "generated_by" or an experiment's
    arbiter reasoning) is preserved; canonical keys take precedence so the
    block remains machine-checkable. For records whose payload already IS
    a provenance dict (hypotheses, experiments) the block is merged at the
    top level; for generic payloads (observation raw, evidence content)
    callers nest the result under PROVENANCE_KEY ("_provenance").
    """
    merged = dict(existing or {})
    merged.update(_canonical_provenance(origin_loop, kind, write_path,
                                        causal_chain))
    return merged


def record_experience(epistemic: Any,
                      origin_loop: str,
                      kind: str,
                      content: str,
                      raw: Optional[Dict[str, Any]] = None,
                      causal_chain: Optional[Sequence[str]] = None,
                      source: Optional[str] = None,
                      observation_id: Optional[str] = None) -> str:
    """Write one experience record through the unified write path.

    The record lands in the epistemic store via the frozen EpistemicStore
    API (the store stays the single physical substrate -- no duplication),
    with an additive provenance block carrying the origin loop, the write
    timestamp, and the causal chain (ids of the records/events that caused
    this one, e.g. the delta id that produced a distillation experience).

    Returns the observation id. Raises on storage failure: experience
    writes through this path are load-bearing provenance, not advisory
    logging -- callers that must not break their loop keep their own
    try/except (as _persist_attempt does).
    """
    from uuid import uuid4
    obs_id = observation_id or f"exp_{uuid4().hex[:12]}"
    merged = dict(raw or {})
    merged[PROVENANCE_KEY] = _canonical_provenance(
        origin_loop, kind, "unified_memory.record_experience", causal_chain)
    src = source or f"{origin_loop}/{kind}"
    save = getattr(epistemic, "save_observation", None)
    if callable(save):
        from swarm_engine.intellect.epistemic import Observation
        save(Observation(observation_id=obs_id, content=content,
                         source=src, raw=merged))
    else:
        epistemic.record_observation(content=content, source=src, raw=merged)
    return obs_id


def record_evidence(epistemic: Any,
                    origin_loop: str,
                    kind: str,
                    evidence: Any,
                    causal_chain: Optional[Sequence[str]] = None) -> str:
    """Write one evidence record through the unified write path.

    Takes the caller-constructed Evidence object (callers own the
    domain fields: target, supports, content), stamps the canonical
    provenance block additively into its content dict, and persists via
    the frozen EpistemicStore.save_evidence API. Returns the evidence id.
    The arbiter reads only `supports`; the additive block is inert to it.
    """
    evidence.content = dict(evidence.content or {})
    evidence.content[PROVENANCE_KEY] = _stamp_provenance(
        evidence.content.get(PROVENANCE_KEY), origin_loop, kind,
        "unified_memory.record_evidence", causal_chain)
    epistemic.save_evidence(evidence)
    return evidence.evidence_id


def record_hypothesis(epistemic: Any,
                      origin_loop: str,
                      kind: str,
                      hypothesis: Any,
                      causal_chain: Optional[Sequence[str]] = None) -> str:
    """Write one hypothesis record through the unified write path.

    Takes the caller-constructed Hypothesis object (callers own the domain
    fields and may re-save after mutation, e.g. arbiter verdict updates),
    stamps the canonical provenance block into its provenance field
    (caller keys preserved), and persists via the frozen
    EpistemicStore.save_hypothesis API. Returns the hypothesis id.
    """
    hypothesis.provenance = _stamp_provenance(
        hypothesis.provenance, origin_loop, kind,
        "unified_memory.record_hypothesis", causal_chain)
    epistemic.save_hypothesis(hypothesis)
    return hypothesis.hypothesis_id


def record_experiment(epistemic: Any,
                      origin_loop: str,
                      kind: str,
                      experiment: Any,
                      causal_chain: Optional[Sequence[str]] = None) -> str:
    """Write one experiment record through the unified write path.

    Takes the caller-constructed Experiment object, stamps the canonical
    provenance block into its provenance field, and persists via the frozen
    EpistemicStore.save_experiment API. Returns the experiment id.
    """
    experiment.provenance = _stamp_provenance(
        experiment.provenance, origin_loop, kind,
        "unified_memory.record_experiment", causal_chain)
    epistemic.save_experiment(experiment)
    return experiment.experiment_id


def read_experiences(epistemic: Any,
                     origin_loop: Optional[str] = None,
                     kind: Optional[str] = None,
                     limit: int = 100) -> List[Dict[str, Any]]:
    """Read experience records back through the unified read path.

    Filters on the provenance block stamped by record_experience. Records
    written before the cutover (or by loop-owned writers that bypass the
    facade) carry no provenance block: they are still returned -- reachable
    is the point -- but their provenance dict is empty, which is exactly
    how the census distinguishes facade-written records from legacy ones.
    """
    out: List[Dict[str, Any]] = []
    for o in epistemic.all_observations():
        raw = getattr(o, "raw", None) or {}
        prov = raw.get(PROVENANCE_KEY) or {}
        if origin_loop is not None and prov.get("origin_loop") != origin_loop:
            continue
        if kind is not None and prov.get("kind") != kind:
            continue
        out.append({
            "observation_id": getattr(o, "observation_id", ""),
            "content": getattr(o, "content", ""),
            "source": getattr(o, "source", ""),
            "at": getattr(o, "at", None),
            "provenance": dict(prov),
            "raw": raw,
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Store adapter catalog (bridge registry).
#
# One entry per persistent store found by the V10-P1 structural survey.
# "Bridge" means: the store stays exactly where it is, owned by exactly the
# module that owns it today; this catalog makes it REACHABLE through the
# unified layer (adapter metadata + census), with provenance preserved --
# the facade never copies a store's rows into a second table.
#
# db values: "engine" (the shared swarm_engine.db), "gaps" (gaps.db, sibling
# of the engine DB), "acceptance" (acceptance.db), "oracle" (the trust
# sidecar -- independent by design, see the header note), "service"
# (service-private DBs, created at service boot), "project" (per-project
# DBs), "staging" (M3 governed-fetch staging dir -- files, not tables).
# ---------------------------------------------------------------------------

def _adapter(store_id: str, tables: Sequence[str], owner: str, db: str,
             loops: Sequence[str], notes: str = "",
             independent: bool = False) -> Dict[str, Any]:
    return {"store_id": store_id, "tables": tuple(tables), "owner": owner,
            "db": db, "loops": tuple(loops), "notes": notes,
            "independent": independent}


STORE_CATALOG: Tuple[Dict[str, Any], ...] = (
    # -- the shared engine DB ------------------------------------------------
    _adapter("epistemic-store",
             ("observations", "evidence", "hypotheses", "experiments"),
             "runtime/intellect/epistemic.py", "engine",
             ("acquisition", "intellect", "planning", "verification"),
             "Primary experience-record substrate; unified write path lands here."),
    _adapter("capability-store",
             ("plan_capabilities", "capability_goals", "capability_events"),
             "runtime/synthesis/capability_store.py", "engine",
             ("acquisition", "dispatch", "planning", "verification", "trust"),
             "FROZEN interface; read-only from the unified layer."),
    _adapter("knowledge-base",
             ("capabilities", "generation_log"),
             "runtime/memory/knowledge_base.py", "engine",
             ("verification", "synthesis"),
             "Verification outcomes persist in generation_log."),
    _adapter("provenance-store",
             ("provenance", "provenance_events", "capability_prerequisites",
              "acquired_code"),
             "runtime/governance/provenance.py", "engine",
             ("acquisition", "trust", "governance"),
             "acquired_code is governance-owned, shared acquisition/competition."),
    _adapter("lifecycle-store",
             ("lifecycle_log", "lifecycle_state", "lifecycle_succession"),
             "runtime/governance/lifecycle.py", "engine",
             ("acquisition", "verification", "trust"),
             "Quarantine/lifecycle transitions."),
    _adapter("memory-bank",
             ("episodic_memory", "semantic_memory"),
             "runtime/memory/system.py", "engine",
             ("memory", "cognition"),
             "MemorySystem: the engine's episodic/semantic facade."),
    _adapter("failure-memory",
             ("failure_memory",),
             "runtime/memory/failure_memory.py", "engine",
             ("memory", "improvement")),
    _adapter("experience-log",
             ("experience_events",),
             "runtime/intellect/experience.py", "engine",
             ("acquisition", "dispatch"),
             "ExperienceLog event table (owned by the intellect/memory files)."),
    _adapter("acquisition-experience",
             ("acquisition_experience",),
             "runtime/synthesis/acquisition_learning.py", "engine",
             ("acquisition",),
             "Acquisition strategy outcomes (signature/strategy/success/cost)."),
    _adapter("lexicon",
             ("lexical_concepts",),
             "runtime/acquisition/lexical.py", "engine",
             ("acquisition",),
             "Created lazily by the objective bridge."),
    _adapter("semantic-structures",
             ("semantic_structures",),
             "runtime/acquisition/semantic_structure.py", "engine",
             ("acquisition", "cognition"),
             "Created lazily by the objective bridge."),
    _adapter("semantic-capabilities",
             ("semantic_capabilities",),
             "runtime/acquisition/semantic_capability.py", "engine",
             ("acquisition", "cognition", "synthesis")),
    _adapter("external-evidence",
             ("external_evidence",),
             "runtime/acquisition/semantic_capability.py", "engine",
             ("acquisition",)),
    _adapter("semantic-evidence-gaps",
             ("semantic_evidence_gaps",),
             "runtime/acquisition/semantic_evidence_gap.py", "engine",
             ("acquisition", "dispatch")),
    _adapter("integrity-seals",
             ("integrity_seals", "integrity_lineage"),
             "runtime/synthesis/integrity.py", "engine",
             ("dispatch", "trust"),
             "Created lazily by the integrity/quarantine sweep."),
    _adapter("quarantine-sweep",
             ("sweep_runs", "sweep_actions", "quarantine_repair_evidence"),
             "runtime/synthesis/integrity.py", "engine",
             ("trust", "verification"),
             "Created lazily by the quarantine sweep."),
    _adapter("dispatch-audit",
             ("intent_dispatches",),
             "runtime/synthesis/nl_dispatch.py", "engine",
             ("dispatch",),
             "Created lazily on first dispatch."),
    _adapter("composition-abstraction",
             ("composition_observations", "generalized_skeletons"),
             "runtime/synthesis/abstraction.py", "engine",
             ("synthesis", "improvement")),
    _adapter("competition",
             ("competitors", "goal_winners"),
             "runtime/synthesis/competition.py", "engine",
             ("dispatch", "acquisition")),
    _adapter("autonomous-driver",
             ("driver_state",),
             "runtime/capability/autonomous_driver.py", "engine",
             ("capability",),
             "Created lazily by the autonomous driver."),
    _adapter("external-knowledge",
             ("knowledge_requests", "knowledge_answers"),
             "runtime/capability/external_knowledge.py", "engine",
             ("capability", "acquisition"),
             "Created lazily by the external-knowledge queue."),
    _adapter("promoted-primitives",
             ("promoted_primitives",),
             "runtime/capability/primitive_promotion.py", "engine",
             ("planning", "trust")),
    _adapter("representations",
             ("case_memory", "failed_adaptations", "search_bias_op",
              "search_bias_pair", "concepts", "exhausted_searches"),
             "runtime/cognition/representations.py", "engine",
             ("cognition", "planning")),
    _adapter("agenda-intellect",
             ("agenda_questions", "agenda_weights"),
             "runtime/intellect/agenda.py", "engine",
             ("intellect",)),
    _adapter("intellectual-patterns",
             ("intellectual_patterns",),
             "runtime/intellect/patterns.py", "engine",
             ("intellect",)),
    _adapter("longhorizon-core",
             ("checkpoints",),
             "runtime/core/longhorizon.py", "engine",
             ("longhorizon",)),
    _adapter("autonomy-objectives",
             ("objectives",),
             "runtime/core/autonomy.py", "engine",
             ("autonomy",)),
    _adapter("blackboard",
             ("blackboard",),
             "runtime/agents/blackboard.py", "engine",
             ("agents",)),
    _adapter("improvement-agenda",
             ("agenda_events", "agenda_investigations", "agenda_seq",
              "improvement_agenda"),
             "runtime/improvement/agenda.py", "engine",
             ("improvement",)),
    _adapter("improvement-substrate",
             ("improvement_log", "improvement_outcomes", "improvements"),
             "runtime/improvement/substrate.py", "engine",
             ("improvement",)),
    _adapter("goal-language",
             ("abstraction_decisions", "abstraction_events",
              "abstraction_resource_experience", "goal_lang_cycles",
              "goal_lang_seq", "deferred_opportunities",
              "learned_productions"),
             "runtime/improvement/goal_language.py", "engine",
             ("improvement",)),
    _adapter("auto-engineer",
             ("auto_engineer_evidence",),
             "runtime/improvement/auto_engineer.py", "engine",
             ("improvement",)),
    _adapter("experiment-design",
             ("experiment_runs",),
             "runtime/improvement/experiment_design.py", "engine",
             ("improvement", "verification")),
    _adapter("task-synthesis",
             ("synthesis_cycles", "synthesis_seq"),
             "runtime/improvement/task_synthesis.py", "engine",
             ("improvement",)),
    _adapter("curriculum",
             ("curriculum_cycles", "curriculum_seq"),
             "runtime/improvement/curriculum.py", "engine",
             ("improvement",)),
    _adapter("primitive-synthesis",
             ("iterated_primitives",),
             "runtime/cognition/primitive_synthesis.py", "engine",
             ("cognition", "synthesis")),
    _adapter("project",
             ("projects", "project_log", "project_progress", "project_state"),
             "runtime/project/lifecycle.py", "engine",
             ("project",),
             "projects table created by runtime/project/ingestion.py."),
    # -- separate files ------------------------------------------------------
    _adapter("m7-gap-registry",
             ("m7_gap_records", "m7_dependency_inventory", "m7_route_runs"),
             "runtime/acquisition/gaps.py", "gaps",
             ("acquisition", "dispatch"),
             "The only separate-file acquisition store; sibling of engine DB."),
    _adapter("acceptance-overlay",
             ("acceptance_records",),
             "runtime/verification/acceptance.py", "acceptance",
             ("verification",),
             "Separate acceptance.db; no in-tree constructor -- wired by deployment."),
    _adapter("oracle-trust",
             (),
             "runtime/trust/oracle.py", "oracle",
             ("trust",),
             "INDEPENDENT trust authority by design: registered for "
             "existence, never bridged into the unified read/write path.",
             independent=True),
    _adapter("m3-substrate-staging",
             (),
             "runtime/acquisition/substrate.py", "staging",
             ("acquisition",),
             "Governed-fetch staging dir (files, not tables); in-memory "
             "fetch audit is NOT persisted -- documented, not claimed."),
    _adapter("scheduler-db",
             (),
             "runtime/services/scheduler.py", "service",
             ("services",),
             "Service-private DB; created at service boot."),
    _adapter("tasks-db",
             (),
             "runtime/services/tasks.py", "service",
             ("services",),
             "Service-private DB; created at service boot."),
    _adapter("evidence-db",
             (),
             "runtime/services/evidence.py", "service",
             ("services",),
             "Service-private DB; created at service boot."),
    _adapter("artifacts-db",
             (),
             "runtime/services/artifacts.py", "service",
             ("services",),
             "Service-private DB; created at service boot."),
    _adapter("intent-db",
             (),
             "runtime/services/intent_dispatch_api.py", "service",
             ("dispatch",),
             "IntentDispatchService boots a separate full engine-schema DB "
             "(intent.db) under the service base dir. BRIDGED (PLOOP-4): the "
             "census verifies it when a path is supplied -- every table in "
             "intent.db must fall within the engine schema (union of "
             "engine-claimed tables), i.e. a schema copy with no private "
             "tables. Tables are intentionally not enumerated here: the "
             "bridge asserts schema-conformance, not a fixed list."),
    _adapter("project-db",
             (),
             "runtime/project/", "project",
             ("project",),
             "Per-project .remor_project.db + .remor_continuation/*.json."),
)


def store_catalog() -> List[Dict[str, Any]]:
    """Return the registered store adapters (bridge registry)."""
    return [dict(a) for a in STORE_CATALOG]


def _sqlite_tables(db_path: str) -> List[str]:
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "select name from sqlite_master where type='table'").fetchall()
    finally:
        con.close()
    return sorted(r[0] for r in rows
                  if r[0] not in ("sqlite_sequence", "sqlite_stat1"))


class _CensusResult(dict):
    """A census report that is falsy when any defect is found."""

    @property
    def ok(self) -> bool:
        return (not self.get("unclaimed_tables")
                and not self.get("multi_claimed_tables")
                and not self.get("missing_sidecars"))

    def __bool__(self) -> bool:
        return self.ok


def run_census(engine_db_path: str,
               intent_db_path: Optional[str] = None) -> _CensusResult:
    """Enumerate every table in the engine DB and prove each is reachable.

    Every table must be claimed by exactly one STORE_CATALOG adapter with
    db == "engine". Unclaimed tables are private stores invisible to the
    loops (defect); tables claimed by two adapters are two sources of truth
    (defect). Known sidecar files next to the engine DB (gaps.db,
    acceptance.db, the oracle trust DB) are checked for presence and, when
    present, their tables must be claimed by the matching adapter.

    intent_db_path (optional): the IntentDispatchService's intent.db. When
    supplied and present, its tables must all fall within the engine schema
    (the union of engine-claimed tables) -- the bridge proof that it is a
    schema copy with no private tables. A table outside the engine schema
    is a defect (a private store hiding inside the service DB). Absence is
    not a defect: the service boots intent.db lazily at service start.

    This is the adversarial half of the V10-P1 proof: a loop that keeps a
    store outside this catalog fails the census.
    """
    import os
    result: _CensusResult = _CensusResult()
    result["engine_db"] = engine_db_path
    by_table: Dict[str, List[str]] = {}
    for adapter in STORE_CATALOG:
        if adapter["db"] != "engine":
            continue
        for table in adapter["tables"]:
            by_table.setdefault(table, []).append(adapter["store_id"])

    tables = _sqlite_tables(engine_db_path)
    result["tables_found"] = tables
    claimed, unclaimed, multi = [], [], []
    for table in tables:
        owners = by_table.get(table, [])
        if len(owners) == 1:
            claimed.append({"table": table, "store_id": owners[0]})
        elif not owners:
            unclaimed.append(table)
        else:
            multi.append({"table": table, "store_ids": owners})
    result["claimed_tables"] = claimed
    result["unclaimed_tables"] = unclaimed
    result["multi_claimed_tables"] = multi

    # Sidecars: gaps.db and acceptance.db are bridged stores; the oracle DB
    # is the deliberate independent exception (existence only).
    sidecars: Dict[str, Any] = {}
    missing = []
    base = os.path.dirname(os.path.abspath(engine_db_path))
    stem = os.path.splitext(os.path.basename(engine_db_path))[0]
    expectations = {
        "gaps": (os.path.join(base, "gaps.db"),
                 "m7-gap-registry", False),
        "acceptance": (os.path.join(base, "acceptance.db"),
                       "acceptance-overlay", False),
        "oracle": (os.path.join(base, f"{stem}.oracle.db"),
                   "oracle-trust", True),
    }
    for name, (path, store_id, independent) in expectations.items():
        entry: Dict[str, Any] = {"path": path, "store_id": store_id,
                                 "present": os.path.exists(path),
                                 "independent": independent}
        if entry["present"] and not independent:
            adapter = next(a for a in STORE_CATALOG
                           if a["store_id"] == store_id)
            actual = _sqlite_tables(path)
            entry["tables"] = actual
            entry["unclaimed_tables"] = [t for t in actual
                                         if t not in adapter["tables"]]
            if entry["unclaimed_tables"]:
                unclaimed.extend(f"{name}:{t}"
                                 for t in entry["unclaimed_tables"])
        if not entry["present"] and name in ("gaps",):
            # gaps.db is created on first GapRegistry use; its absence on a
            # fresh engine is expected, not a defect.
            entry["note"] = "not yet created (lazy)"
        sidecars[name] = entry
    result["sidecars"] = sidecars
    result["missing_sidecars"] = missing
    result["unclaimed_tables"] = unclaimed

    # intent.db bridge: the IntentDispatchService's separate full
    # engine-schema DB. When a path is supplied and the file exists, every
    # table in it must fall within the engine schema (union of
    # engine-claimed tables) -- proving it is a schema copy, not a private
    # store with extra tables. Absence is expected on a fresh checkout
    # (the service boots it lazily); absence is not a defect.
    engine_schema_tables = set(by_table)
    intent_entry: Dict[str, Any] = {"path": intent_db_path,
                                    "store_id": "intent-db",
                                    "present": bool(intent_db_path)
                                    and os.path.exists(intent_db_path)}
    if intent_entry["present"]:
        actual = _sqlite_tables(intent_db_path)
        intent_entry["tables"] = actual
        outside = [t for t in actual if t not in engine_schema_tables]
        intent_entry["tables_outside_engine_schema"] = outside
        if outside:
            unclaimed.extend(f"intent:{t}" for t in outside)
            result["unclaimed_tables"] = unclaimed
    else:
        intent_entry["note"] = ("not supplied or not yet created "
                                "(service boots it lazily)")
    result["intent_db"] = intent_entry
    return result


# -- UnifiedMemory: the cutover surface -------------------------------------
# (Methods are attached here, after the class definition above, so the
# V10-P1 surface stays in one readable section with the write path and
# the catalog it depends on.)

def _um_record_experience(self: "UnifiedMemory",
                          origin_loop: str,
                          kind: str,
                          content: str,
                          raw: Optional[Dict[str, Any]] = None,
                          causal_chain: Optional[Sequence[str]] = None,
                          source: Optional[str] = None) -> str:
    """Write an experience record through the unified write path."""
    return record_experience(self.epistemic, origin_loop, kind, content,
                             raw=raw, causal_chain=causal_chain,
                             source=source)


def _um_read_experiences(self: "UnifiedMemory",
                         origin_loop: Optional[str] = None,
                         kind: Optional[str] = None,
                         limit: int = 100) -> List[Dict[str, Any]]:
    """Read experience records back through the unified read path."""
    return read_experiences(self.epistemic, origin_loop=origin_loop,
                            kind=kind, limit=limit)


def _um_record_evidence(self: "UnifiedMemory",
                        origin_loop: str,
                        kind: str,
                        evidence: Any,
                        causal_chain: Optional[Sequence[str]] = None) -> str:
    """Write an evidence record through the unified write path."""
    return record_evidence(self.epistemic, origin_loop, kind, evidence,
                           causal_chain=causal_chain)


def _um_record_hypothesis(self: "UnifiedMemory",
                          origin_loop: str,
                          kind: str,
                          hypothesis: Any,
                          causal_chain: Optional[Sequence[str]] = None) -> str:
    """Write a hypothesis record through the unified write path."""
    return record_hypothesis(self.epistemic, origin_loop, kind, hypothesis,
                             causal_chain=causal_chain)


def _um_record_experiment(self: "UnifiedMemory",
                          origin_loop: str,
                          kind: str,
                          experiment: Any,
                          causal_chain: Optional[Sequence[str]] = None) -> str:
    """Write an experiment record through the unified write path."""
    return record_experiment(self.epistemic, origin_loop, kind, experiment,
                             causal_chain=causal_chain)


def _um_census(self: "UnifiedMemory",
               engine_db_path: Optional[str] = None,
               intent_db_path: Optional[str] = None) -> _CensusResult:
    """Run the store census against the engine DB behind this layer.

    Pass intent_db_path (the IntentDispatchService's intent.db) to also
    verify the intent service's engine-schema copy is bridged: every table
    in it must fall within the engine schema, i.e. no private tables.
    """
    path = engine_db_path or getattr(self.epistemic, "db_path", None)
    if not path:
        raise ValueError("census needs an engine DB path: pass "
                         "engine_db_path or bind an epistemic store with a "
                         "db_path")
    return run_census(path, intent_db_path=intent_db_path)


UnifiedMemory.record_experience = _um_record_experience
UnifiedMemory.read_experiences = _um_read_experiences
UnifiedMemory.record_evidence = _um_record_evidence
UnifiedMemory.record_hypothesis = _um_record_hypothesis
UnifiedMemory.record_experiment = _um_record_experiment
UnifiedMemory.census = _um_census
del (_um_record_experience, _um_read_experiences, _um_record_evidence,
     _um_record_hypothesis, _um_record_experiment, _um_census)
