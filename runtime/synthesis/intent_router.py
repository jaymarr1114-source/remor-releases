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
     - no candidate at/above min_score -> refuse "unknown_intent";
     - top candidate wins only if unambiguous: either it is the sole
       candidate or its lead over the runner-up is >= ambiguity_margin;
       otherwise refuse "ambiguous_intent".
  4. A routed capability is identified by validated capability_id only.
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
    via: Optional[str] = None          # "exact_goal" | "structural"
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
