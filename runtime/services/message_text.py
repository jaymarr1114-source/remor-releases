"""Message-text normalization (conversational-minimum mission, Worker 1).

Dictation artifacts: a dictated message can arrive with stray leading
or trailing punctuation (", make a picture of a dog with brown fur and
white spots,"). Left verbatim, the junk becomes the run's goal text and
gets echoed back in refusal/failure cards ("I couldn't complete ,
make a picture..."). This module strips those artifacts BEFORE the
text becomes a goal.
"""
from __future__ import annotations

import string


def normalize_message_text(text: object) -> object:
    """Strip dictation artifacts from the EDGES of a message.

    Strips whitespace, then leading/trailing ASCII punctuation
    (``string.punctuation``), iteratively, so ", hello, " -> "hello".
    Stops before the string would go empty: a pure-punctuation message
    ("...") is left intact for downstream to handle honestly, and a
    whitespace-only message becomes "" so the caller's "text is
    required" path rejects it.

    Deliberately does NOT touch:
      * inner text -- byte-preserved (only the edges are stripped);
      * Unicode punctuation ("«hello»", "hello…") -- ASCII-only by
        design, since dictation artifacts in this pipeline are ASCII.
    Non-string input is returned unchanged (the caller still rejects
    it via its own type check). Pure function: no state, no I/O.
    """
    if not isinstance(text, str):
        return text
    s = text.strip()
    while len(s) > 1:
        new = s.strip(string.punctuation).strip()
        if not new or new == s:
            break
        s = new
    return s
