"""M+29.31 -- Generic objective-to-behavioral-obligation grounding.

Extracts a bounded, structured obligation representation from a clause
of natural-language text, using ONLY general English modal-verb syntax
(must/should/can/cannot/etc.) and positional heuristics -- never any
domain vocabulary, project name, or expected-output literal. This is a
genuine, if narrow, syntactic signal: it distinguishes clauses that
impose an obligation ("the character must be able to jump") from
clauses that merely mention domain words without imposing one (a
section heading like "2. Element System"), which the existing purely
lexical-density confidence signal (content_signal in objective_bridge.py)
cannot do -- a word-dense heading scores as "confidently discovered"
there, while this module correctly reports no obligation found.

Explicitly bounded and honest about it: this is NOT semantic role
labeling, NOT dependency parsing, and does not resolve what the
extracted "action"/"subject" tokens actually mean -- it identifies
modal-marked obligation structure only, and returns None (not a guess)
when no modal marker is present, or an explicit AMBIGUOUS
classification when multiple conflicting markers are found in one
clause.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Generic English modal/obligation vocabulary. Every entry here is a
# common-language grammatical marker, never a domain- or
# project-specific word. Multi-word markers are checked before their
# single-word substrings (e.g. "must not" before "must") so negation is
# never lost.
_NEGATIVE_MODALS: List[str] = [
    "must not", "should not", "may not", "cannot", "can not", "never",
]
_REQUIRED_MODALS: List[str] = [
    "must", "shall", "is required to", "are required to", "has to",
    "have to", "needs to", "need to",
]
_RECOMMENDED_MODALS: List[str] = ["should"]
_PERMITTED_MODALS: List[str] = [
    "can", "may", "could", "is able to", "are able to",
]

_ALL_MODAL_GROUPS = [
    ("PROHIBITED", _NEGATIVE_MODALS),
    ("REQUIRED", _REQUIRED_MODALS),
    ("RECOMMENDED", _RECOMMENDED_MODALS),
    ("PERMITTED", _PERMITTED_MODALS),
]

_STOP = {
    "a", "an", "the", "of", "to", "for", "and", "or", "in", "on", "with",
    "from", "into", "by", "as", "is", "are", "be", "being", "been", "that",
    "this", "it", "its", "at", "which", "who", "whom", "whose", "not",
}


@dataclass
class BehavioralObligation:
    strength: str            # REQUIRED / RECOMMENDED / PERMITTED / PROHIBITED
    marker: str               # the actual matched modal phrase
    subject: str               # best-effort token(s) preceding the modal
    action: str                 # best-effort token immediately following the modal
    raw_text: str
    ambiguous: bool = False
    ambiguity_reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "strength": self.strength, "marker": self.marker,
            "subject": self.subject, "action": self.action,
            "raw_text": self.raw_text, "ambiguous": self.ambiguous,
            "ambiguity_reason": self.ambiguity_reason,
        }


def _find_all_markers(lowered_text: str) -> List[Any]:
    """Return every (start, end, strength, marker) match, with any
    shorter marker whose span is fully contained inside a longer
    marker's span excluded (e.g. "should" must not also register
    separately inside "should not" -- that is one marker, not two
    conflicting ones)."""
    found = []
    for strength, markers in _ALL_MODAL_GROUPS:
        for marker in sorted(markers, key=len, reverse=True):
            pattern = r"\b" + re.escape(marker) + r"\b"
            for m in re.finditer(pattern, lowered_text):
                found.append((m.start(), m.end(), strength, marker))
    found.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    kept: List[Any] = []
    for start, end, strength, marker in found:
        if any(k[0] <= start and end <= k[1] for k in kept):
            continue  # contained within an already-kept, longer match
        kept.append((start, end, strength, marker))
    kept.sort(key=lambda t: t[0])
    return kept


def extract_behavioral_obligation(clause_text: str) -> Optional[BehavioralObligation]:
    """Extract a single obligation from one clause of text. Returns
    None (not a guess) when no modal marker is present -- this is the
    honest negative-control behavior: prose that mentions domain
    vocabulary without a genuine obligation marker produces nothing.
    """
    lowered = clause_text.lower()
    matches = _find_all_markers(lowered)
    if not matches:
        return None

    # Ambiguity: two DIFFERENT-strength markers present in one clause
    # (e.g. "can... but must...") -- genuinely conflicting obligation
    # strength within a single clause is reported as ambiguous rather
    # than silently picking one.
    strengths_present = {m[2] for m in matches}
    ambiguous = len(strengths_present) > 1
    ambiguity_reason = (
        f"multiple conflicting modal strengths in one clause: {sorted(strengths_present)}"
        if ambiguous else ""
    )

    start, end, strength, marker = matches[0]
    words = re.findall(r"[A-Za-z0-9']+", clause_text)
    lower_words = [w.lower() for w in words]

    # Locate the marker's own word span within the tokenized clause by
    # re-tokenizing the marker itself, so multi-word markers align.
    marker_words = marker.split()
    action, subject = "", ""
    for i in range(len(lower_words) - len(marker_words) + 1):
        if lower_words[i:i + len(marker_words)] == marker_words:
            after = [w for w in lower_words[i + len(marker_words):] if w not in _STOP]
            action = after[0] if after else ""
            before = [w for w in lower_words[:i] if w not in _STOP]
            subject = before[-1] if before else ""
            break

    if not action:
        ambiguous = True
        ambiguity_reason = (ambiguity_reason + "; " if ambiguity_reason else "") + \
            "no content word found after the modal marker"

    return BehavioralObligation(
        strength=strength, marker=marker, subject=subject, action=action,
        raw_text=clause_text.strip(), ambiguous=ambiguous,
        ambiguity_reason=ambiguity_reason,
    )
