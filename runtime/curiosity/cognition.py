"""Curiosity cognition: the reasoning inlet for curiosity microcontrollers.

D-8 (decided): curiosity uses the same cognition/brain as the Primary --
no separate minds. Reasoning models are cognition utilities for the
microcontrollers: a microcontroller that requires reasoning calls
MicrocontrollerSubstrate.cognize(), and the substrate routes the request
to its registered CognitionProvider. This module provides the curiosity
substrate's registered provider.

PrecisionCognitionProvider is a DETERMINISTIC mechanical reasoning
provider: it performs the question-precision operations (parse the
imprecise question -> extract the unknown -> compose the precise
question -> score it on the checkable checklist) through the same
mechanical functions, exposed behind the CognitionProvider protocol so
the loop's reasoning genuinely travels the substrate's cognition inlet
(active microcontroller -> substrate.cognize -> provider) rather than
calling the machinery directly. No external model is consulted; the
provider never hard-codes answers -- it processes arbitrary question
text. If a future reasoning model is registered via
set_cognition_provider, the loop's cognize calls route to it unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.core.microcontroller.substrate import (
    CognitionProvider, CognitionResult)

# ---------------------------------------------------------------------------
# text machinery: the genuine (mechanical) precision mechanism
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset(
    "the a an of to in for on and or is are was were be been being it its "
    "this that these those as at by with from into about than then so such "
    "no not but what which how why when where who whom whose whether does "
    "do did will would should can could has have had there their them they "
    "we you your our us i me my he she his her him if than".split())

_INTERROGATIVE_RE = re.compile(
    r"\b(what|which|how|why|whether|when|where|who|whom|whose)\b", re.I)
_YESNO_RE = re.compile(
    r"^\s*(is|are|can|could|does|do|did|will|would|should|has|have)\b", re.I)

_OBSERVABLE_MARKERS = (
    "observ", "measur", "evidence", "data", "test", "experiment",
    "example", "instance", "demonstrat", "show", "detect", "record")

_SCOPE_PREP_RE = re.compile(
    r"\b(in|for|under|within|across|during|between|through)\s+"
    r"((?:the|a|an)\s+)?([a-z][a-z\-]*(?:\s+[a-z][a-z\-]*){0,2})", re.I)


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-zA-Z][a-zA-Z\-]*", text.lower())


def _content_terms(text: str) -> List[str]:
    seen: List[str] = []
    for tok in _tokenize(text):
        if len(tok) > 3 and tok not in _STOPWORDS and tok not in seen:
            seen.append(tok)
    return seen


def extract_unknown(question_text: str) -> Tuple[str, List[str]]:
    """Pull the unknown out of arbitrary question text: the interrogative
    complement (what follows what/which/how/...), minus articles. Returns
    (unknown_phrase, content_terms). Never empty: falls back to the
    question's content terms when no interrogative structure is found."""
    text = (question_text or "").strip()
    terms = _content_terms(text)
    m = _INTERROGATIVE_RE.search(text)
    if m:
        rest = text[m.end():]
        toks = _tokenize(rest)
        toks = [t for t in toks
                if t not in ("the", "a", "an") and len(t) > 1]
        # Drop a leading auxiliary ("what is the mechanism" -> mechanism).
        if toks and toks[0] in ("is", "are", "was", "were", "does", "do",
                                "did", "will", "would", "should", "can",
                                "could", "has", "have"):
            toks = toks[1:]
        phrase = " ".join(toks[:4]).strip()
        if phrase:
            return phrase, terms
    m2 = _YESNO_RE.search(text)
    if m2:
        rest = text[m2.end():]
        toks = [t for t in _tokenize(rest)
                if t not in ("the", "a", "an", "there")]
        phrase = " ".join(toks[:4]).strip()
        if phrase:
            return phrase, terms
    # Fallback: the unknown is the question's own content.
    return " ".join(terms[:4]) or text[:60], terms


def extract_scope(question_text: str) -> List[str]:
    """Prepositional scope hints: 'in X', 'for Y', 'under Z' spans."""
    scopes: List[str] = []
    for m in _SCOPE_PREP_RE.finditer(question_text or ""):
        span = f"{m.group(1)} {m.group(3)}".lower().strip()
        if span not in scopes:
            scopes.append(span)
    return scopes[:3]


def mentions_observable(question_text: str) -> bool:
    lowered = (question_text or "").lower()
    return any(mk in lowered for mk in _OBSERVABLE_MARKERS)


def compose_precise_question(unknown: str, observable: Optional[str],
                             scopes: List[str]) -> str:
    """Build the candidate precise question from the slots. With a found
    observable the question names the evidence path and a falsifier
    (score 3 shape); without one it asks what would count as evidence
    (score 2 shape) -- the score then measures the real gap."""
    scope_str = f" {scopes[0]}" if scopes else ""
    if observable:
        short = observable.strip()
        if len(short) > 140:
            short = short[:140].rstrip() + "..."
        return (
            f"What observable evidence would resolve '{unknown}'{scope_str}, "
            f"given that {short} -- and what observation would falsify "
            f"the current best account of '{unknown}'?")
    return (f"What would count as observable evidence for "
            f"'{unknown}'{scope_str}?")


def precision_score(precise_question: str, unknown: str,
                    observable: Optional[str]) -> Tuple[int, List[str]]:
    """The checkable precision checklist (0-3). Each check is a string
    property of the composed question -- inspectable, not asserted."""
    q = (precise_question or "").lower()
    checks: List[str] = []
    c1 = bool(unknown) and unknown.lower() in q
    checks.append(f"names the unknown ({unknown!r}): {c1}")
    c2 = ("observable evidence" in q
          or (bool(observable) and observable[:40].lower() in q))
    checks.append(f"names an observable evidence path: {c2}")
    c3 = "falsif" in q
    checks.append(f"names a falsifier/decision criterion: {c3}")
    return int(c1) + int(c2) + int(c3), checks


# ---------------------------------------------------------------------------
# the provider: mechanical reasoning behind the CognitionProvider protocol
# ---------------------------------------------------------------------------

#: Operations the questioning loop requests through the cognition inlet.
OP_EXTRACT = "extract_unknown"
OP_COMPOSE = "compose_precise"
OP_SCORE = "score_precision"


class PrecisionCognitionProvider:
    """Deterministic mechanical reasoning for curiosity microcontrollers.

    Implements the substrate's CognitionProvider protocol: the loop calls
    substrate.cognize(mc_id, prompt, context) with context["operation"]
    naming one of the OP_* operations, and the provider runs the genuine
    mechanical precision operation over the supplied inputs. Results are
    returned as JSON text in a successful CognitionResult; malformed
    requests fail closed (ok=False), never hallucinated.
    """

    def request_cognition(self, *, mc_id: str, prompt: str,
                          context: Dict[str, Any]) -> CognitionResult:
        op = (context or {}).get("operation")
        try:
            if op == OP_EXTRACT:
                question_text = context.get("question_text", "")
                unknown, terms = extract_unknown(question_text)
                return CognitionResult(ok=True, text=json.dumps({
                    "unknown": unknown,
                    "terms": terms,
                    "observable_mentioned":
                        mentions_observable(question_text),
                }))
            if op == OP_COMPOSE:
                precise = compose_precise_question(
                    context.get("unknown", ""),
                    context.get("observable"),
                    list(context.get("scopes", [])))
                return CognitionResult(ok=True, text=json.dumps({
                    "precise_question": precise,
                }))
            if op == OP_SCORE:
                score, checks = precision_score(
                    context.get("precise_question", ""),
                    context.get("unknown", ""),
                    context.get("observable"))
                return CognitionResult(ok=True, text=json.dumps({
                    "score": score,
                    "checks": checks,
                }))
        except Exception as exc:  # fail-closed: never a partial result
            return CognitionResult(
                ok=False,
                error=f"cognition_failed: {type(exc).__name__}: {exc}")
        return CognitionResult(
            ok=False,
            error=f"unknown_cognition_operation: {op!r} "
                  f"(mc {mc_id}; prompt {prompt[:60]!r})")


# Re-export the protocol names for curiosity-side importers.
__all__ = [
    "PrecisionCognitionProvider",
    "CognitionProvider", "CognitionResult",
    "OP_EXTRACT", "OP_COMPOSE", "OP_SCORE",
    "extract_unknown", "extract_scope", "mentions_observable",
    "compose_precise_question", "precision_score",
]
