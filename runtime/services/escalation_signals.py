"""Escalation signals interface — FROZEN CONTRACT (LEARNED-ESCALATION-1).

Producer: grower track (distill loop). Consumer: production serving (router).
Frozen 2026-10-02 by Felix to unblock the production-serving track.

The distill loop learns when escalation helps (from observed outcomes).
Until the grower track implements the producer, get_escalation_signal
returns None and consumers use the heuristic fallback.

Contract:
  * get_escalation_signal(prompt) -> Optional[EscalationSignal]
  * Signal carries: should_escalate, confidence [0,1], reason, source.
  * Consumer MUST fall back to heuristic when signal is None.
  * Consumer MUST record when the learned signal disagrees with the
    heuristic outcome (misroute detection).
  * Heuristic is the fail-closed fallback, never removed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EscalationSignal:
    """A learned escalation decision for a prompt."""
    should_escalate: bool
    confidence: float  # 0.0 to 1.0
    reason: str
    source: str = "learned"  # "learned" or "heuristic"


# Producer hook: the grower track sets this when the distill loop
# has learned signals. None means "no learned signals available."
_producer: Optional[callable] = None


def set_signal_producer(fn) -> None:
    """Grower track calls this to install the learned-signal producer."""
    global _producer
    _producer = fn


def get_escalation_signal(prompt: str) -> Optional[EscalationSignal]:
    """Return the learned signal for prompt, or None if unavailable.

    Production serving calls this; falls back to heuristic on None.
    """
    if _producer is None:
        return None
    try:
        return _producer(prompt)
    except Exception:
        return None


def heuristic_signal(prompt: str) -> EscalationSignal:
    """The fail-closed heuristic fallback. Never removed.

    Simple rules: long prompts, question words, and explicit
    "think hard" markers suggest escalation.
    """
    lowered = prompt.lower()
    score = 0
    reasons = []
    if len(prompt) > 200:
        score += 1
        reasons.append("long prompt")
    if any(w in lowered for w in ("why", "how", "explain", "analyze")):
        score += 1
        reasons.append("question word")
    if "think hard" in lowered or "think_hard" in lowered:
        score += 2
        reasons.append("explicit think-hard")
    return EscalationSignal(
        should_escalate=score >= 1,
        confidence=min(0.9, 0.3 + 0.2 * score),
        reason="; ".join(reasons) or "no escalation triggers",
        source="heuristic",
    )


def decide_escalation(prompt: str) -> tuple:
    """Consumer entry point: (should_escalate, signal, used_learned).

    Tries the learned signal; falls back to heuristic. Returns whether
    the learned signal was used, so misroutes can be recorded.
    """
    learned = get_escalation_signal(prompt)
    if learned is not None and learned.confidence >= 0.5:
        return learned.should_escalate, learned, True
    h = heuristic_signal(prompt)
    return h.should_escalate, h, False
