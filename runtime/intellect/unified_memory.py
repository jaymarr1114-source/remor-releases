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

    Returns the observation id. The record carries the full delta schema
    so later unified queries can retrieve it and trace it back to the
    delta and the synthesized capability.
    """
    from uuid import uuid4
    from swarm_engine.intellect.epistemic import Observation
    raw = {"objective": objective, "delta_id": delta_id,
           "Y": Y, "Z": Z, "T": T, "E": E, "D": D, "V": V,
           "synthesized_capability_id": C}
    obs_id = f"exp_{uuid4().hex[:12]}"
    epistemic.save_observation(Observation(
        observation_id=obs_id,
        content=f"distillation experience: {objective} (technique: {T})",
        source="distillation-loop",
        raw=raw))
    return obs_id


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
        epistemic.record_observation(
            content=(f"z-check on {result.objective!r}: "
                     f"{result.classification} "
                     f"(strategy={result.strategy or 'none'}, "
                     f"ops={result.ops_used}, reached={result.reached})"),
            source="z-check",
            raw={"type": "z_check_attempt", "objective": result.objective,
                 **result.as_dict()})
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
