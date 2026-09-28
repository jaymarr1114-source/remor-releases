"""swarm_engine/synthesis/nlu_substrate.py

Dispatch wiring built ON TOP of the acquired NLU substrate.

Substrate adapters (each behind the same interface below):
  - spacy: noun chunks + dependency heads from the acquired spaCy model
    (spaCy + en_core_web_sm, governed acquisition; see
    proofs/v9dispatch_acquire_2026-09-28.py). Bench substrate.
  - nltk: tokenization + part-of-speech from the acquired NLTK models
    (TreebankWordTokenizer, averaged-perceptron POS tagger -- trained
    artifacts, acquired through the governed substrate path; see
    proofs/v9nluarm_acquire_2026-09-28.py), plus minimal mechanical
    NP-span assembly over those POS tags. Phone-shippable substrate
    (pure-python NLTK wheel; the one compiled hard dep, regex, is served
    as Android wheels by Chaquopy's own repo -- no custom native build,
    no Android-blocked artifacts).

This module contains no regex grammars, no token patterns, and no
hand-rolled NP parser of its own. Every linguistic classification --
token boundaries, part-of-speech -- comes from an externally acquired
trained model. The NLTK adapter additionally groups POS-tagged tokens
into noun-headed spans; that grouping is mechanical adapter glue over
the acquired tags (stated plainly: it is the documented cost of the
portable substrate), not a grammar and not a second parser. The spaCy
adapter is unchanged.

Honest degradation: if no substrate is available (never acquired on
this machine) or errors, every entry point returns None and callers
fall back to the existing grammar path unchanged. Nothing is
fabricated, nothing is guessed, and a missing substrate never changes
existing behavior.

The domain taxonomy itself (image/video/song/voice/file word lists) is
NOT linguistic understanding -- it is the product's own artifact
vocabulary, owned by semantic_frames and injected here as a parameter
so this module never imports (or duplicates) it.

Substrate selection: spaCy first (bench behavior identical to
V9-DISPATCH), then NLTK. NLU_SUBSTRATE=spacy|nltk forces one (tests);
anything else (or unset) means auto. NLU_NO_VENDOR=1 disables the
vendored fallback (honest-absence testing only).
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# The acquired spaCy model artifact. Overridable for tests; the default is
# the artifact the governed acquisition actually installed.
_MODEL_NAME = os.environ.get("NLU_SPACY_MODEL", "en_core_web_sm")

# Where the phone-shippable NLTK substrate lives when vendored into the
# runtime tree (runtime/vendor/nlu_nltk/): the pure-python package plus
# its trained-model data. NLU_NLTK_DATA overrides the data dir (proofs).
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "vendor", "nlu_nltk")

_substrate: Optional[Tuple[str, Any]] = None
_load_attempted = False
last_error: str = ""


def _wanted() -> str:
    return os.environ.get("NLU_SUBSTRATE", "auto").strip().lower()


def _load_spacy() -> Any:
    """The bench substrate. None when absent or broken."""
    try:
        spacy = importlib.import_module("spacy")
    except Exception as exc:
        return None, f"spacy not importable: {type(exc).__name__}: {exc}"
    try:
        return spacy.load(_MODEL_NAME), ""
    except Exception as exc:
        return None, (f"model {_MODEL_NAME!r} not loadable: "
                      f"{type(exc).__name__}: {exc}")


def _nltk_data_dir() -> Optional[str]:
    """Locate the acquired NLTK trained-model data. None when absent."""
    env = os.environ.get("NLU_NLTK_DATA", "").strip()
    if env and os.path.isdir(env):
        return env
    vendored = os.path.join(_VENDOR_DIR, "nltk_data")
    if os.path.isdir(vendored):
        return vendored
    return None


def _load_nltk() -> Any:
    """The phone-shippable substrate. None when absent or broken.

    Package: pure-python NLTK wheel (governed acquisition). Data: the
    acquired trained model (averaged-perceptron tagger weights).
    Tokenization is the TreebankWordTokenizer (needs no data tables).
    Never downloads: missing data is honest absence.
    Returns (namespace, error).
    """
    try:
        nltk = importlib.import_module("nltk")
    except Exception:
        # NLU_NO_VENDOR=1: honest-absence testing; the vendored fallback
        # stays off so "substrate absent" is genuinely observable.
        if os.environ.get("NLU_NO_VENDOR", "").strip().lower() not in (
                "", "0", "false", "no"):
            return None, "nltk not importable (vendored discovery disabled)"
        vendor_pkg = _VENDOR_DIR
        if os.path.isdir(os.path.join(vendor_pkg, "nltk")):
            # Append (not prepend): the first import attempt already
            # searched the whole path, so this only adds the fallback.
            if vendor_pkg not in sys.path:
                sys.path.append(vendor_pkg)
            try:
                nltk = importlib.import_module("nltk")
            except Exception as exc:
                return None, (f"vendored nltk not importable: "
                              f"{type(exc).__name__}: {exc}")
        else:
            return None, "nltk not importable and no vendored copy"
    data_dir = _nltk_data_dir()
    if data_dir is not None and data_dir not in nltk.data.path:
        nltk.data.path.insert(0, data_dir)
    try:
        from nltk.tokenize import TreebankWordTokenizer
        from nltk.tag import pos_tag
        tokenizer = TreebankWordTokenizer()
        # Probe: forces the trained models to load. LookupError here
        # means the data is absent -> honest degradation, no download.
        toks = tokenizer.tokenize("probe words")
        pos_tag(toks)
    except Exception as exc:
        return None, (f"nltk substrate not usable: {type(exc).__name__}: "
                      f"{exc}")
    return {"tokenizer": tokenizer, "pos_tag": pos_tag}, ""


def _load() -> Optional[Tuple[str, Any]]:
    """Load (once) the best available substrate. None when absent."""
    global _substrate, _load_attempted, last_error
    if _load_attempted:
        return _substrate
    _load_attempted = True
    want = _wanted()
    errors = []
    if want in ("auto", "spacy"):
        nlp, err = _load_spacy()
        if nlp is not None:
            _substrate = ("spacy", nlp)
            return _substrate
        errors.append(err)
    if want in ("auto", "nltk"):
        mods, err = _load_nltk()
        if mods is not None:
            _substrate = ("nltk", mods)
            return _substrate
        errors.append(err)
    last_error = "; ".join(e for e in errors if e)
    _substrate = None
    return None


def substrate_available() -> bool:
    """True when an acquired substrate loads. Never raises."""
    return _load() is not None


def substrate_kind() -> Optional[str]:
    """'spacy', 'nltk', or None. Introspection for proofs. Never raises."""
    loaded = _load()
    return loaded[0] if loaded else None


# Tags the NLTK adapter treats as noun-phrase material. The linguistic
# classification (which token gets which tag) is the acquired perceptron
# tagger's; this set only bounds the mechanical span scan.
_NP_TAGS = frozenset([
    "DT", "PRP$",
    "JJ", "JJR", "JJS",
    "NN", "NNS", "NNP", "NNPS",
    "CD",
])


def _analyze_nltk(obj_text: str,
                  taxonomy: Mapping[str, str],
                  singular: Callable[[str], str],
                  mods: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Noun-phrase analysis via the acquired NLTK models.

    Mechanical scan over acquired POS tags: maximal spans of NP-material
    tags, head = final noun of the span, first span whose head
    singularizes into the injected taxonomy wins. Returns the same
    frame-ready dict as the spaCy adapter, or None when the substrate
    errors or no span maps to the taxonomy.
    """
    tokenizer = mods["tokenizer"]
    pos_tag = mods["pos_tag"]
    try:
        spans = list(tokenizer.span_tokenize(obj_text))
    except Exception:
        return None
    if not spans:
        return None
    toks = [obj_text[a:b] for a, b in spans]
    try:
        tagged = pos_tag(toks)
    except Exception:
        return None
    tags = [tg for _, tg in tagged]
    n = len(tags)
    i = 0
    while i < n:
        if tags[i] in _NP_TAGS:
            j = i
            while j < n and tags[j] in _NP_TAGS:
                j += 1
            noun_ix = [k for k in range(i, j) if tags[k].startswith("NN")]
            if noun_ix:
                head_tok = toks[noun_ix[-1]]
                head = head_tok.lower()
                try:
                    tax = taxonomy.get(singular(head))
                except Exception:
                    tax = None
                if tax is not None:
                    cmods = [toks[k].lower() for k in range(i, j)
                             if k != noun_ix[-1]
                             and (tags[k].startswith("JJ")
                                  or tags[k].startswith("NN")
                                  or tags[k] == "CD")]
                    k0 = i
                    while k0 < j and tags[k0] in ("DT", "PRP$"):
                        k0 += 1
                    # Preserve the user's original surface: slice the
                    # object-region text by token character spans, minus
                    # any leading determiner.
                    phrase_text = obj_text[spans[k0][0]:spans[j - 1][1]].strip()
                    rest_text = obj_text[spans[j - 1][1]:].strip()
                    return {"head": head, "mods": cmods,
                            "phrase_text": phrase_text,
                            "rest_text": rest_text, "taxonomy": tax}
            i = j
        else:
            i += 1
    return None


def _analyze_spacy(obj_text: str,
                   taxonomy: Mapping[str, str],
                   singular: Callable[[str], str],
                   nlp: Any) -> Optional[Dict[str, Any]]:
    """Noun-phrase analysis via the acquired spaCy model. Unchanged."""
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


def analyze_np(obj_text: str,
               taxonomy: Mapping[str, str],
               singular: Callable[[str], str],
               ) -> Optional[Dict[str, Any]]:
    """Noun-phrase analysis of an object region, via the acquired substrate.

    Finds the first noun-headed span whose head singularizes into the
    injected taxonomy. Returns None when no substrate is available, when
    parsing fails, or when no span maps to the taxonomy -- in all of
    which the caller keeps its existing grammar path.

    The returned dict is frame-ready NP parts (no token objects, so this
    module never touches the frame layer's types):
      head:        the taxonomy noun, lowercased
      mods:        other content words of the span, lowercased, in order
                   (determiners and punctuation dropped, like the grammar)
      phrase_text: span surface minus determiners
      rest_text:   object-region text after the span
      taxonomy:    the taxonomy value the head mapped to
    """
    loaded = _load()
    if loaded is None or not obj_text or not obj_text.strip():
        return None
    kind, impl = loaded
    if kind == "nltk":
        return _analyze_nltk(obj_text, taxonomy, singular, impl)
    return _analyze_spacy(obj_text, taxonomy, singular, impl)


def reset_for_tests() -> None:
    """Drop the cached substrate (tests only)."""
    global _substrate, _load_attempted, last_error
    _substrate = None
    _load_attempted = False
    last_error = ""
