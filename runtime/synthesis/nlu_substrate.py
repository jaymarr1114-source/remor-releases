"""swarm_engine/synthesis/nlu_substrate.py

Dispatch wiring built ON TOP of the acquired NLU substrate.

This module contains ZERO linguistic rules of its own: no regex grammars,
no token patterns, no hand-rolled NP logic. Every linguistic decision --
tokenization, part-of-speech, noun chunks, dependency heads -- comes from
the externally acquired model (spaCy + en_core_web_sm, acquired through the
governed substrate path: real artifact, hash-verified, sandbox-scoped;
see proofs/v9dispatch_acquire_2026-09-28.py). This module is pure plumbing:

    acquired model -> noun chunks -> taxonomy lookup -> frame-ready NP parts

Honest degradation: if the substrate is absent (never acquired on this
machine) or errors, every entry point returns None and callers fall back
to the existing grammar path unchanged. Nothing is fabricated, nothing is
guessed, and a missing substrate never changes existing behavior.

The domain taxonomy itself (image/video/song/voice/file word lists) is NOT
linguistic understanding -- it is the product's own artifact vocabulary,
owned by semantic_frames and injected here as a parameter so this module
never imports (or duplicates) it.
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Callable, Dict, List, Mapping, Optional

# The acquired model artifact. Overridable for tests; the default is the
# artifact the governed acquisition actually installed.
_MODEL_NAME = os.environ.get("NLU_SPACY_MODEL", "en_core_web_sm")

_nlp: Any = None
_load_attempted = False
last_error: str = ""


def _load() -> Any:
    """Load (once) the acquired model. None when absent or broken."""
    global _nlp, _load_attempted, last_error
    if _load_attempted:
        return _nlp
    _load_attempted = True
    try:
        spacy = importlib.import_module("spacy")
    except Exception as exc:
        last_error = f"spacy not importable: {type(exc).__name__}: {exc}"
        return None
    try:
        _nlp = spacy.load(_MODEL_NAME)
    except Exception as exc:
        last_error = (f"model {_MODEL_NAME!r} not loadable: "
                      f"{type(exc).__name__}: {exc}")
        _nlp = None
    return _nlp


def substrate_available() -> bool:
    """True when the acquired substrate loads. Never raises."""
    return _load() is not None


def analyze_np(obj_text: str,
               taxonomy: Mapping[str, str],
               singular: Callable[[str], str],
               ) -> Optional[Dict[str, Any]]:
    """Noun-phrase analysis of an object region, via the acquired substrate.

    Finds the first noun chunk whose dependency head singularizes into the
    injected taxonomy. Returns None when the substrate is unavailable, when
    parsing fails, or when no chunk maps to the taxonomy -- in all of which
    the caller keeps its existing grammar path.

    The returned dict is frame-ready NP parts (no token objects, so this
    module never touches the frame layer's types):
      head:        chunk root, lowercased (the taxonomy noun)
      mods:        other content words of the chunk, lowercased, in order
                   (determiners and punctuation dropped, like the grammar)
      phrase_text: chunk surface minus determiners
      rest_text:   object-region text after the chunk
      taxonomy:    the taxonomy value the head mapped to
    """
    nlp = _load()
    if nlp is None or not obj_text or not obj_text.strip():
        return None
    try:
        doc = nlp(obj_text)
    except Exception as exc:
        global last_error
        last_error = f"substrate parse failed: {type(exc).__name__}: {exc}"
        return None
    for chunk in doc.noun_chunks:
        root = chunk.root
        head = (root.text or "").lower()
        if not head:
            continue
        try:
            tax = taxonomy.get(singular(head))
        except Exception:
            continue
        if tax is None:
            continue
        mods: List[str] = []
        phrase_bits: List[str] = []
        first_content = None
        for tok in chunk:
            if tok.pos_ == "DET" or tok.is_punct or tok.is_space:
                continue
            if first_content is None:
                first_content = tok
            phrase_bits.append(tok.text)
            if tok.i == root.i:
                continue
            mods.append((tok.text or "").lower())
        # Preserve the user's original surface (e.g. "256x256", not the
        # tokenizer's "256 x 256"): slice the object-region text by the
        # chunk's character span, minus any leading determiner.
        if first_content is not None:
            phrase_text = obj_text[first_content.idx:chunk.end_char].strip()
        else:
            phrase_text = " ".join(phrase_bits).strip()
        rest_text = obj_text[chunk.end_char:].strip()
        return {"head": head, "mods": mods, "phrase_text": phrase_text,
                "rest_text": rest_text, "taxonomy": tax}
    return None


def reset_for_tests() -> None:
    """Drop the cached model (tests only)."""
    global _nlp, _load_attempted, last_error
    _nlp = None
    _load_attempted = False
    last_error = ""
