"""Deterministic intent router (§3).

Routes a natural-language request to exactly one persisted, admitted,
currently-active capability -- or refuses. This is NOT general language
understanding and NOT an LLM: it is a bounded, deterministic matcher over
the capability store, built on two real substrates:

  * acquisition.intent.infer_required_effects / infer_postconditions --
    the deterministic cue -> effect lexicon;
  * synthesis.capability_match.rank_compatible -- structural scoring of
    active capabilities against a goal (behavioral when examples exist,
    structural otherwise).

Routing policy (fail closed):
  1. Empty/oversized/non-text input -> refuse.
  2. Exact goal binding (the store's own goal -> capability map): if the
     normalized request text exactly matches a bound goal AND that
     capability is effectively active (tri-system), route via "exact_goal".
  3. Otherwise structural ranking over effectively-active candidates:
     - a top candidate at/above min_score wins only if unambiguous
       (sole candidate, or lead over runner-up >= ambiguity_margin);
       otherwise refuse "ambiguous_intent".
  4. Effect fallback: if effects were inferred from the request text but
     no candidate reached min_score, select the effectively-active
     candidate whose plan-declared effects (the acquisition.intent
     lexicon namespace, e.g. "media_image") best match the inferred
     effects, instead of refusing "unknown_intent". Deterministic
     tie-break, recorded in the reasons: largest declared∩inferred
     overlap, then fewest declared effects beyond the inferred set
     (specificity), then lexicographically smallest capability_id.
     Routes via "effect_fallback". No inferred effects -> still
     "unknown_intent".
  5. A routed capability is identified by validated capability_id only.
     The router never resolves caller-supplied callables, names, or paths.

"Effectively active" means integrity.effective_status(...) == "active"
across store, provenance-trust, and lifecycle -- store status alone is
not enough (a deliberately-quarantined row resurrected by raw status
mutation must not become routable).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.acquisition.intent import infer_required_effects
from swarm_engine.synthesis import capability_match as _cm
from swarm_engine.synthesis.integrity import effective_status

MAX_TEXT_LEN = 2000
DEFAULT_MIN_SCORE = 0.55
DEFAULT_AMBIGUITY_MARGIN = 0.15


@dataclass
class RouteResult:
    ok: bool
    capability_id: Optional[str] = None
    capability_name: Optional[str] = None
    via: Optional[str] = None          # "exact_goal" | "structural" | "effect_fallback"
    score: float = 0.0
    effects: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    refusal: Optional[str] = None      # machine-readable refusal code

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "capability_id": self.capability_id,
            "capability_name": self.capability_name,
            "via": self.via,
            "score": self.score,
            "effects": self.effects,
            "reasons": self.reasons,
            "refusal": self.refusal,
        }


class IntentRouter:
    """Deterministic NL -> capability router. Bound to one engine."""

    def __init__(self, engine,
                 min_score: float = DEFAULT_MIN_SCORE,
                 ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
                 max_text_len: int = MAX_TEXT_LEN):
        self.engine = engine
        self.min_score = min_score
        self.ambiguity_margin = ambiguity_margin
        self.max_text_len = max_text_len

    # -- candidate set ----------------------------------------------------
    def _active_candidates(self) -> List[Any]:
        """Capabilities whose EFFECTIVE status is active (tri-system)."""
        out = []
        for rec in self.engine.capabilities.list(status="active", limit=1000):
            if getattr(rec, "status", None) != "active":
                continue
            try:
                eff = effective_status(self.engine, rec.capability_id)
            except Exception:
                continue
            if eff.get("effective") == "active" and eff.get("consistent"):
                out.append(rec)
        return out

    # -- effect fallback --------------------------------------------------
    @staticmethod
    def _declared_plan_effects(rec) -> set:
        """Lexicon-namespace effects a capability's admitted plan declares.

        Reads the plan's own "effects" key (e.g. ["media_image"] as
        declared by media_plan()). Records whose plans predate the
        declaration declare nothing and can never match here.
        """
        try:
            plan = rec.plan or {}
        except Exception:
            return set()
        eff = plan.get("effects") or []
        return {str(e) for e in eff if isinstance(e, str)}

    def _effect_fallback(self, text: str, effects: List[str],
                         candidates: List[Any],
                         reasons: List[str]) -> Optional[RouteResult]:
        """Route by declared plan effects when structural scoring fails.

        Returns a RouteResult, or None when no effectively-active
        candidate declares any of the inferred effects (caller then
        refuses unknown_intent as before).
        """
        inferred = set(effects)
        scored = []
        for rec in candidates:
            declared = self._declared_plan_effects(rec)
            overlap = declared & inferred
            if not overlap:
                continue
            scored.append((rec, declared, overlap))
        if not scored:
            return None
        # Deterministic, recorded tie-break: largest overlap, then most
        # specific (fewest declared effects beyond the inferred set),
        # then smallest capability_id (total order -- exactly one winner).
        scored.sort(key=lambda t: (-len(t[2]), len(t[1] - inferred),
                                   t[0].capability_id))
        rec, declared, overlap = scored[0]
        cap_id = rec.capability_id
        full = self.engine.capabilities.get(cap_id)
        r2 = list(reasons) + [
            f"effect fallback: inferred_effects={sorted(inferred)}; "
            f"{cap_id[:12]}... declares {sorted(declared)} "
            f"(overlap {sorted(overlap)}); tie-break: overlap desc, "
            f"specificity asc, capability_id asc; "
            f"{len(scored)} effect-matching candidate(s) considered",
        ]
        return RouteResult(
            ok=True, capability_id=cap_id,
            capability_name=getattr(full, "name", None),
            via="effect_fallback", score=0.0, effects=effects,
            reasons=r2)
    # -- routing ----------------------------------------------------------
    def route(self, text: str) -> RouteResult:
        if not isinstance(text, str):
            return RouteResult(ok=False, refusal="invalid_input",
                               reasons=["request text must be a string"])
        stripped = text.strip()
        if not stripped:
            return RouteResult(ok=False, refusal="empty_intent",
                               reasons=["empty request text"])
        if len(text) > self.max_text_len:
            return RouteResult(ok=False, refusal="oversized_input",
                               reasons=[f"request text exceeds "
                                        f"{self.max_text_len} chars"])

        effects = infer_required_effects(stripped)
        reasons = [f"inferred_effects={effects}"]

        # 1. exact goal binding
        bindings = self.engine.capabilities.goal_bindings()
        hit = bindings.get(stripped.lower())
        if hit:
            try:
                eff = effective_status(self.engine, hit)
            except Exception as exc:
                eff = {"effective": "unknown", "consistent": False}
            if eff.get("effective") == "active" and eff.get("consistent"):
                rec = self.engine.capabilities.get(hit)
                reasons.append(f"exact goal binding -> {hit[:12]}...")
                return RouteResult(
                    ok=True, capability_id=hit,
                    capability_name=getattr(rec, "name", None),
                    via="exact_goal", score=1.0, effects=effects,
                    reasons=reasons)
            reasons.append(f"exact goal binding {hit[:12]}... is not "
                           f"effectively active ({eff.get('effective')}); "
                           "not routable")
            return RouteResult(ok=False, refusal="capability_unavailable",
                               effects=effects, reasons=reasons)

        # 2. structural ranking over effectively-active candidates
        candidates = self._active_candidates()
        if not candidates:
            return RouteResult(ok=False, refusal="unknown_intent",
                               effects=effects,
                               reasons=reasons + ["no effectively-active "
                                                  "capabilities to route to"])
        scored = _cm.rank_compatible(
            candidates, stripped,
            composer=self.engine.composer,
            registry=self.engine.primitives,
            min_score=self.min_score)
        if not scored:
            if effects:
                fb = self._effect_fallback(stripped, effects, candidates,
                                           reasons)
                if fb is not None:
                    return fb
                reasons = reasons + ["effect fallback: no effectively-active "
                                     "capability declares any of the "
                                     "inferred effects"]
            return RouteResult(ok=False, refusal="unknown_intent",
                               effects=effects,
                               reasons=reasons + [
                                   f"no candidate reached min_score "
                                   f"{self.min_score}"])
        top = scored[0]
        if len(scored) > 1:
            gap = top.score - scored[1].score
            if gap < self.ambiguity_margin:
                return RouteResult(
                    ok=False, refusal="ambiguous_intent", effects=effects,
                    reasons=reasons + [
                        f"ambiguous: top {top.capability_id[:12]}... "
                        f"(score {top.score:.3f}) leads runner-up "
                        f"{scored[1].capability_id[:12]}... "
                        f"(score {scored[1].score:.3f}) by {gap:.3f} < "
                        f"margin {self.ambiguity_margin}"])
        rec = self.engine.capabilities.get(top.capability_id)
        reasons.append(f"structural match {top.capability_id[:12]}... "
                       f"score={top.score:.3f}")
        reasons.extend(top.reasons or [])
        return RouteResult(
            ok=True, capability_id=top.capability_id,
            capability_name=getattr(rec, "name", None),
            via="structural", score=top.score, effects=effects,
            reasons=reasons)
