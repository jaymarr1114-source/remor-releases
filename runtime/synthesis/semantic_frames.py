"""Semantic intent frames — the replacement for keyword-hint goal parsing.

An IntentFrame is a structured meaning representation derived from the
request text by grammatical analysis, NOT by keyword lookup. The difference
is architectural:

* keyword hints: bag-of-words -> intent (no structure; "number" anywhere
  means numeric output; "create" must appear verbatim in a hint set).
* intent frames: text -> syntactic structure (mood, verb, object NP, PPs,
  quoted spans, arithmetic expressions) -> frame via grammar rules.
  The intent falls out of the STRUCTURE, so paraphrases using different
  words through the same grammatical constructions yield the same frame.

A frame carries:
  intent      — what the user wants (a closed Intent enum)
  entities    — the frame's slots (who/what is involved)
  output_type — what kind of thing the user wants back (a TypeSpec)
  mood        — imperative | interrogative | declarative
  confidence  — 0..1; low confidence + competing parses -> AMBIGUOUS
  alternatives— competing parses, when ambiguity was detected
  trace       — derivation steps, for honest reporting of HOW the frame
                was built (auditable, not a black box)

Fail-closed contract: parse_frame never raises on strange input. It returns
a frame with intent UNKNOWN (no grammatical parse) or AMBIGUOUS (two or
more competing parses within the ambiguity margin). Callers MUST treat
UNKNOWN and AMBIGUOUS as "do not act" — an honest "I don't understand"
beats a misrouted run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from swarm_engine.primitives.core import ANY, BOOL, DICT, LIST, NUM, STR, TypeSpec
from swarm_engine.synthesis import nlu_substrate as _nlu_substrate


class Intent(Enum):
    """Closed set of intents the semantic layer can express.

    Anything outside this set parses as UNKNOWN — the layer does not
    pretend to understand what it cannot represent.
    """
    CREATE_FILE = "create_file"      # make/write a file (code, text, ...)
    CREATE_IMAGE = "create_image"    # generate an image
    CREATE_VIDEO = "create_video"    # generate a video
    CREATE_SONG = "create_song"      # assemble a song
    CREATE_VOICE = "create_voice"    # synthesize speech audio
    ANSWER_META = "answer_meta"      # question about the system's abilities
    ANSWER_FACTUAL = "answer_factual"  # question answerable from inventory
    COMPUTE = "compute"              # arithmetic / deterministic computation
    EXECUTE = "execute"              # do it via the engine's machinery
    ROUTE = "route"                  # route to an existing capability
    AMBIGUOUS = "ambiguous"          # competing parses; do not act
    UNKNOWN = "unknown"              # no parse; do not act


# Entities per intent (all optional; absent = not expressed):
#   CREATE_FILE:  artifact ("file"), language ("python"|...), purpose (str),
#                 filename_hint (str|None)
#   CREATE_IMAGE/VIDEO/SONG/VOICE: prompt (str), the media description
#   ANSWER_META:  topic ("capabilities"|"tasks"|"abilities"|...)
#   ANSWER_FACTUAL: question (str)
#   COMPUTE:      expression (str, normalised arithmetic), description (str)
#   EXECUTE/ROUTE: goal_text (str) — the residual objective for the engine
_AMBIGUITY_MARGIN = 0.15


@dataclass
class IntentFrame:
    raw: str
    intent: Intent
    entities: Dict[str, Any] = field(default_factory=dict)
    output_type: TypeSpec = ANY
    mood: str = "declarative"  # "imperative" | "interrogative" | "declarative"
    confidence: float = 0.0
    alternatives: List["IntentFrame"] = field(default_factory=list)
    trace: List[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """False for UNKNOWN and AMBIGUOUS — callers must not act on these."""
        return self.intent not in (Intent.UNKNOWN, Intent.AMBIGUOUS)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent.value,
            "entities": dict(self.entities),
            "output_type": str(self.output_type),
            "mood": self.mood,
            "confidence": round(self.confidence, 3),
            "actionable": self.actionable,
            "alternatives": [a.as_dict() for a in self.alternatives],
            "trace": list(self.trace),
        }


def parse_frame(raw: str) -> IntentFrame:
    """Parse request text into an IntentFrame via grammatical analysis.

    Implemented in the parser below. Never raises on strange input:
    unparseable text yields Intent.UNKNOWN, competing parses yield
    Intent.AMBIGUOUS. This is the single entry point — the planner's
    parse_goal and the task interface both build on it.
    """
    return _parse(raw)


# ---------------------------------------------------------------------------
# Parser implementation
# ---------------------------------------------------------------------------
#
# The parser is a small deterministic grammar-based semantic parser
# (classical NLP: lexicon with syntactic categories + grammar rules +
# frame construction). It is NOT a keyword->intent table: words map to
# syntactic/semantic categories, and GRAMMAR RULES map structures to
# frames. Held-out paraphrases are the proof: different words through
# the same rules must yield the same frame.
#
# Pipeline:
#   1. tokenise  — words, quoted spans, numbers, operators
#   2. mood      — imperative (verb-first) | interrogative (wh-/aux-first)
#                  | declarative
#   3. lexicon   — verb classes (creation, ...), wh-words, operators
#   4. rules     — clause patterns -> candidate frames with confidences
#   5. frames    — build IntentFrame(s); ambiguity check on top two
# ---------------------------------------------------------------------------


def _parse(raw: str) -> IntentFrame:
    # Implemented by Worker A. Contract:
    # - returns IntentFrame, never raises on str input (empty/whitespace ->
    #   UNKNOWN with confidence 0.0)
    # - non-str input -> UNKNOWN (do not raise; callers pass user text)
    # - competing parses within _AMBIGUITY_MARGIN -> AMBIGUOUS with the
    #   competing frames in .alternatives
    # - every frame carries a non-empty .trace explaining the derivation
    try:
        return _parse_inner(raw)
    except Exception as exc:  # fail closed: never raise on user text
        return IntentFrame(
            raw=raw if isinstance(raw, str) else "",
            intent=Intent.UNKNOWN,
            mood="declarative",
            confidence=0.0,
            trace=["internal parser error (%s); failing closed to UNKNOWN"
                   % type(exc).__name__],
        )


# ---------------------------------------------------------------------------
# 1. Tokenizer
# ---------------------------------------------------------------------------

@dataclass
class _Token:
    kind: str   # WORD | NUMBER | OP | QUOTE | PUNCT
    text: str   # original surface form
    lower: str  # lowercased (words); numbers/ops unchanged


_TOKEN_RE = None


def _token_re():
    global _TOKEN_RE
    if _TOKEN_RE is None:
        import re
        _TOKEN_RE = re.compile(r"""
            (?P<QUOTE>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
          | (?P<NUMBER>\d+(?:\.\d+)?)
          | (?P<LANG>c\+\+|c\#)
          | (?P<WORD>[A-Za-z]+(?:'[a-z]+)?)
          | (?P<OP>[+\-*/\u00d7\u00f7=\^%()])
          | (?P<PUNCT>[.,;:!?])
          | (?P<WS>\s+)
        """, re.VERBOSE)
    return _TOKEN_RE


def _tokenize(raw: str) -> List[_Token]:
    toks: List[_Token] = []
    for m in _token_re().finditer(raw):
        kind = m.lastgroup
        if kind == "WS":
            continue
        text = m.group()
        if kind == "QUOTE":
            # keep the inner text as the surface form; kind stays QUOTE
            inner = text[1:-1]
            toks.append(_Token("QUOTE", inner, inner.lower()))
        elif kind == "WORD":
            low = text.lower()
            if low.endswith("'s"):
                low = low[:-2]  # "what's" -> "what"
            toks.append(_Token("WORD", text, low))
        else:
            toks.append(_Token(kind, text, text.lower()))
    return toks


# ---------------------------------------------------------------------------
# 2. Lexicon — SYNTACTIC categories, never intent keywords.
#    A verb maps to (verb class, default artifact taxonomy); the INTENT
#    comes from grammar rules combining class + structure + NP head.
# ---------------------------------------------------------------------------

# verb -> (verb class, default taxonomy or None)
_VERBS: Dict[str, tuple] = {}
for _w in ("create write make generate build produce construct develop "
           "craft draft prepare code author".split()):
    _VERBS[_w] = ("creation", None)
for _w in "draw paint sketch render illustrate visualize depict".split():
    _VERBS[_w] = ("imagine", "image")
for _w in "film record animate shoot capture".split():
    _VERBS[_w] = ("film", "video")
for _w in "compose sing hum".split():
    _VERBS[_w] = ("music", "song")
for _w in "run execute launch start perform trigger invoke".split():
    _VERBS[_w] = ("dispatch", None)
for _w in "need want wish require".split():
    _VERBS[_w] = ("desire", None)
for _w in "calculate compute evaluate solve".split():
    _VERBS[_w] = ("compute", None)
del _w

_DET = {"a", "an", "the", "this", "that", "these", "those", "my", "your",
        "his", "her", "its", "our", "their", "some", "any", "each", "every"}
_OBJ_PRON = {"me", "us", "you", "him", "her", "them", "it"}
_PREP = {"for", "of", "to", "in", "with", "from", "about", "on", "by",
         "at", "into", "over"}
_WH = {"what", "which", "who", "whom", "whose", "when", "where", "why",
       "how"}
_AUX = {"are", "is", "do", "does", "did", "can", "could", "will", "would",
        "should", "may", "might", "have", "has", "had", "am", "was",
        "were", "shall"}
_ABILITY = {"able", "capable"}

_LANGUAGES = {"python", "javascript", "typescript", "java", "c++", "c#",
              "rust", "go", "golang", "ruby", "php", "swift", "kotlin",
              "html", "css", "sql", "bash", "shell", "r", "matlab",
              "scala", "perl"}

# noun -> artifact taxonomy; taxonomy -> Intent
_NOUN_TAXONOMY: Dict[str, str] = {}
for _w in ("file script program code app application document note text "
           "email report list memo draft article essay story poem letter "
           "snippet function module class").split():
    _NOUN_TAXONOMY[_w] = "file"
for _w in ("image picture photo photograph drawing sketch painting "
           "illustration logo icon diagram portrait artwork").split():
    _NOUN_TAXONOMY[_w] = "image"
for _w in ("video film movie clip animation footage trailer").split():
    _NOUN_TAXONOMY[_w] = "video"
for _w in ("song tune melody track anthem jingle lullaby hymn "
           "ballad").split():
    _NOUN_TAXONOMY[_w] = "song"
for _w in "voice narration voiceover speech audio".split():
    _NOUN_TAXONOMY[_w] = "voice"
del _w

_TAXONOMY_INTENT = {
    "file": Intent.CREATE_FILE,
    "image": Intent.CREATE_IMAGE,
    "video": Intent.CREATE_VIDEO,
    "song": Intent.CREATE_SONG,
    "voice": Intent.CREATE_VOICE,
}

# word operator -> symbol (structural, not intent keywords)
_OP_WORDS = {
    "times": "*", "multiply": "*", "multiplied": "*",
    "plus": "+", "add": "+",
    "minus": "-", "subtract": "-", "less": "-",
    "divided": "/", "over": "/", "divide": "/",
    "mod": "%", "modulo": "%",
}
_OP_SYMS = {"+": "+", "-": "-", "*": "*", "/": "/",
            "\u00d7": "*", "\u00f7": "/", "^": "^", "%": "%"}

# verb stem -> agent noun for purpose nominalisation ("that generates X"
# -> "X generator"). Small and principled: productive -er formation for
# tool-denoting verbs; anything else falls back to a gerund phrase.
_NOMINALIZE = {
    "generate": "generator", "sort": "sorter", "parse": "parser",
    "count": "counter", "create": "creator", "convert": "converter",
    "download": "downloader", "play": "player", "draw": "drawer",
    "write": "writer", "paint": "painter", "sing": "singer",
    "render": "renderer", "search": "searcher", "filter": "filter",
}


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ses") or word.endswith("shes") or \
            word.endswith("ches") or word.endswith("xes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _strip_3sg(verb: str) -> str:
    """'generates' -> 'generate' (3rd-person singular to stem)."""
    return _singular(verb)


def _nominalize(stem: str, obj_words: List[str]) -> str:
    """'generate' + ['random','numbers'] -> 'random number generator'."""
    noun = _NOMINALIZE.get(stem)
    if noun is None:
        noun = stem + "ing"  # honest fallback: gerund phrase
        return " ".join(obj_words + [noun]) if obj_words else noun
    obj = list(obj_words)
    if obj:
        obj[-1] = _singular(obj[-1])
    return " ".join(obj + [noun]) if obj else noun


# ---------------------------------------------------------------------------
# 3. Mood detection (structural: position + category of first tokens)
# ---------------------------------------------------------------------------

def _detect_mood(toks: List[_Token], raw: str) -> tuple:
    trace: List[str] = []
    words = [t for t in toks if t.kind in ("WORD", "LANG")]
    if not words:
        # no words at all: arithmetic like "1-1=?" is declarative-ish;
        # mood is irrelevant there; default declarative.
        return "declarative", ["mood: no word tokens; default declarative"]
    first = words[0].lower
    idx = 0
    if first == "please" and len(words) > 1:
        idx = 1
        first = words[1].lower
        trace.append("mood: leading 'please' skipped (polite imperative)")
    if first in _WH or first in _AUX:
        trace.append("mood: interrogative (%s-first %r)"
                     % ("wh" if first in _WH else "aux", first))
        return "interrogative", trace
    if raw.rstrip().endswith("?") and first not in _VERBS:
        trace.append("mood: interrogative (trailing '?' on non-verb-first)")
        return "interrogative", trace
    if first in _VERBS:
        vclass = _VERBS[first][0]
        trace.append("mood: imperative (verb-first %r, class=%s)"
                     % (first, vclass))
        return "imperative", trace
    trace.append("mood: declarative (first token %r is not wh/aux/verb)"
                 % first)
    return "declarative", trace


# ---------------------------------------------------------------------------
# 4. NP parsing: [det] [modifiers...] head [PP ...] [rel-clause ...]
# ---------------------------------------------------------------------------

@dataclass
class _NP:
    mods: List[str]
    head: Optional[str]
    language: Optional[str]
    rest: List[_Token]  # tokens after the head (PPs, relatives, ...)
    phrase_text: str    # original surface text of the whole NP region


def _parse_np(toks: List[_Token], raw: str) -> _NP:
    i = 0
    n = len(toks)
    # skip leading object pronouns ("write me ...") and determiners
    while i < n and toks[i].kind == "WORD" and \
            (toks[i].lower in _OBJ_PRON or toks[i].lower in _DET):
        i += 1
    mods: List[str] = []
    language: Optional[str] = None
    head: Optional[str] = None
    start = i
    while i < n:
        t = toks[i]
        if t.kind == "QUOTE":
            head = head or "__quoted__"
            i += 1
            break
        if t.kind != "WORD" and t.kind != "LANG":
            break
        w = t.lower
        if w in _PREP or w in _WH or w in _AUX or w in _VERBS or \
                w in ("named", "called"):
            break
        if w in _DET:
            i += 1
            continue
        if w in _LANGUAGES and language is None:
            language = w
            mods.append(w)
            i += 1
            continue
        # first content word that is a taxonomy noun -> head; otherwise
        # accumulate as modifier and keep looking one more step
        if _singular(w) in _NOUN_TAXONOMY:
            head = w
            i += 1
            break
        mods.append(w)
        i += 1
        # if the next token cannot continue an NP, this word was the head
        if i < n:
            nxt = toks[i]
            if nxt.kind != "WORD" and nxt.kind != "LANG" or \
                    nxt.lower in _PREP or nxt.lower in _WH or \
                    nxt.lower in ("named", "called"):
                head = mods.pop()
                break
    rest = toks[i:]
    phrase_text = " ".join(t.text for t in toks[start:i]).strip()
    return _NP(mods=mods, head=head, language=language, rest=rest,
               phrase_text=phrase_text)


def _np_surface(toks: List[_Token]) -> str:
    """Surface text of a token run, dropping determiners."""
    return " ".join(t.text for t in toks
                    if not (t.kind == "WORD" and t.lower in _DET)).strip()


def _extract_purpose(rest: List[_Token]) -> tuple:
    """Purpose from PPs / infinitives / relative clauses after the NP head.

    Returns (purpose or None, trace lines).
    """
    trace: List[str] = []
    purpose: Optional[str] = None
    i = 0
    n = len(rest)
    while i < n:
        t = rest[i]
        if t.kind != "WORD":
            i += 1
            continue
        w = t.lower
        if w == "for" and purpose is None:
            j = i + 1
            seg = []
            while j < n and not (rest[j].kind == "WORD" and
                                 (rest[j].lower in _PREP or
                                  rest[j].lower in _WH or
                                  rest[j].lower in ("named", "called"))):
                seg.append(rest[j])
                j += 1
            purpose = _np_surface(seg)
            trace.append("PP 'for': purpose=%r" % purpose)
            i = j
            continue
        if w == "to" and purpose is None and i + 1 < n and \
                rest[i + 1].kind == "WORD":
            # infinitive "to generate X" -> nominalise
            stem = rest[i + 1].lower
            obj = [x.text for x in rest[i + 2:]
                   if x.kind in ("WORD", "LANG") and
                   x.lower not in _PREP and x.lower not in _WH]
            purpose = _nominalize(stem, obj)
            trace.append("infinitive 'to %s ...': purpose=%r"
                         % (stem, purpose))
            break
        if w in ("that", "which") and purpose is None and i + 1 < n and \
                rest[i + 1].kind == "WORD":
            # relative clause "that generates X" -> nominalise
            stem = _strip_3sg(rest[i + 1].lower)
            obj = [x.text for x in rest[i + 2:]
                   if x.kind in ("WORD", "LANG") and
                   x.lower not in _PREP and x.lower not in _WH]
            purpose = _nominalize(stem, obj)
            trace.append("relative 'that %s ...': purpose=%r"
                         % (rest[i + 1].lower, purpose))
            break
        i += 1
    return purpose, trace


def _extract_filename(toks: List[_Token]) -> tuple:
    for t in toks:
        if t.kind == "QUOTE":
            return t.text, ["quoted span %r -> filename_hint" % t.text]
    for i, t in enumerate(toks):
        if t.kind == "WORD" and t.lower in ("named", "called") and \
                i + 1 < len(toks):
            nxt = toks[i + 1]
            if nxt.kind in ("WORD", "QUOTE"):
                return nxt.text, ["'named/called %s' -> filename_hint"
                                  % nxt.text]
    return None, []


# ---------------------------------------------------------------------------
# 5. Grammar rules: STRUCTURES -> candidate frames.
#    Each rule returns a list of _Candidate (usually 0 or 1).
# ---------------------------------------------------------------------------

@dataclass
class _Candidate:
    intent: Intent
    entities: Dict[str, Any]
    output_type: Any
    mood: str
    confidence: float
    trace: List[str]
    rule: str


def _rule_arithmetic(toks: List[_Token], mood: str) -> List[_Candidate]:
    trace = ["rule R_ARITH: testing arithmetic-expression structure"]
    work = list(toks)
    # strip leading interrogative/compute scaffolding
    words = [t.lower for t in work if t.kind == "WORD"]
    if words[:2] == ["what", "is"] or words[:1] == ["what's"]:
        k = 0
        seen = 0
        while k < len(work) and seen < 2:
            if work[k].kind == "WORD":
                seen += 1
            k += 1
        work = work[k:]
        trace.append("R_ARITH: stripped leading 'what is'")
    elif words[:3] == ["how", "much", "is"]:
        k = 0
        seen = 0
        while k < len(work) and seen < 3:
            if work[k].kind == "WORD":
                seen += 1
            k += 1
        work = work[k:]
        trace.append("R_ARITH: stripped leading 'how much is'")
    # strip trailing '=' and '?'
    while work and ((work[-1].kind == "OP" and work[-1].text == "=") or
                    (work[-1].kind == "PUNCT" and work[-1].text == "?")):
        dropped = work.pop()
        trace.append("R_ARITH: stripped trailing %r" % dropped.text)
    # fold word operators ("divided by" -> "/")
    folded: List[_Token] = []
    i = 0
    while i < len(work):
        t = work[i]
        if t.kind == "WORD" and t.lower in _OP_WORDS:
            sym = _OP_WORDS[t.lower]
            if t.lower in ("divided", "multiplied") and i + 1 < len(work) \
                    and work[i + 1].kind == "WORD" \
                    and work[i + 1].lower == "by":
                i += 1  # consume "by"
            folded.append(_Token("OP", sym, sym))
            trace.append("R_ARITH: word operator %r -> %r" % (t.text, sym))
        else:
            folded.append(t)
        i += 1
    # merge leading unary minus
    if len(folded) >= 2 and folded[0].kind == "OP" \
            and folded[0].text == "-" and folded[1].kind == "NUMBER":
        folded[1] = _Token("NUMBER", "-" + folded[1].text,
                           "-" + folded[1].lower)
        folded.pop(0)
        trace.append("R_ARITH: folded leading unary minus")
    # structural check: NUMBER (OP NUMBER)+ with binary ops only
    if len(folded) < 3:
        trace.append("R_ARITH: no fire (fewer than 3 tokens)")
        return []
    ok = folded[0].kind == "NUMBER"
    j = 1
    while ok and j < len(folded):
        ok = (folded[j].kind == "OP" and folded[j].text in "+-*/%^" and
              j + 1 < len(folded) and folded[j + 1].kind == "NUMBER")
        j += 2
    if not ok:
        trace.append("R_ARITH: no fire (not NUMBER (OP NUMBER)+)")
        return []
    expr = "".join(t.text for t in folded)
    trace.append("R_ARITH: FIRED arithmetic structure -> %r" % expr)
    return [_Candidate(
        intent=Intent.COMPUTE,
        entities={"expression": expr,
                  "description": "arithmetic expression"},
        output_type=NUM,
        mood=mood,
        confidence=0.95,
        trace=trace + ["frame COMPUTE conf=0.95 (output_type=num)"],
        rule="R_ARITH",
    )]


def _substrate_np(obj_toks: List[_Token], raw: str, verb: str,
                ) -> Optional[Dict[str, Any]]:
    """NP analysis via the ACQUIRED NLU substrate (never hand-rolled).

    Returns the substrate's frame-ready NP parts, or None when the
    substrate is absent, errors, or finds no taxonomy-mapped noun chunk.
    All linguistic decisions come from the acquired model; this is pure
    plumbing. Never raises.
    """
    try:
        # The substrate needs the ORIGINAL surface (the grammar's tokens
        # already split "256x256" into pieces): slice the raw text after
        # the imperative verb.
        obj_text = ""
        at = raw.lower().find(verb.lower())
        if at >= 0:
            obj_text = raw[at + len(verb):].strip()
        if not obj_text:
            obj_text = " ".join(t.text for t in obj_toks)
        return _nlu_substrate.analyze_np(obj_text, _NOUN_TAXONOMY, _singular)
    except Exception:
        return None


def _rule_imperative_creation(toks: List[_Token], verb: str, vclass: str,
                              vtax: Optional[str], mood: str,
                              raw: str) -> List[_Candidate]:
    trace = ["rule R_IMP_CREATION: [imperative + %s-verb %r + NP]"
             % (vclass, verb)]
    # object region = tokens after the verb (skip polite 'please')
    start = 0
    for k, t in enumerate(toks):
        if t.kind == "WORD" and t.lower == verb:
            start = k + 1
            break
    obj_toks = toks[start:]
    np = _parse_np(obj_toks, raw)
    trace.append("NP: mods=%s head=%r language=%r phrase=%r"
                 % (np.mods, np.head, np.language, np.phrase_text))
    head_tax = _NOUN_TAXONOMY.get(_singular(np.head.lower())) \
        if np.head and np.head != "__quoted__" else None
    if head_tax is None and np.head not in (None, "__quoted__"):
        # taxonomy may sit on a modifier ("voice message", "song request")
        for m in np.mods:
            if _singular(m.lower()) in _NOUN_TAXONOMY:
                head_tax = _NOUN_TAXONOMY[_singular(m.lower())]
                trace.append("taxonomy from modifier %r -> %s"
                             % (m, head_tax))
                break
    if head_tax is None:
        # The hand-rolled NP scan found no taxonomy noun -- e.g. dimension
        # tokens like "256x256" abort its scan before the head. Ask the
        # ACQUIRED NLU substrate (never a hand-rolled repair): None when the
        # substrate is absent or unconfident, and the grammar path below
        # then runs exactly as before.
        sub = _substrate_np(obj_toks, raw, verb)
        if sub is not None:
            trace.append(
                "NP via acquired NLU substrate: chunk=%r head=%r taxonomy=%r"
                % (sub["phrase_text"], sub["head"], sub["taxonomy"]))
            language = next((m for m in sub["mods"] if m in _LANGUAGES), None)
            np = _NP(mods=sub["mods"], head=sub["head"], language=language,
                     rest=_tokenize(sub["rest_text"]),
                     phrase_text=sub["phrase_text"])
            head_tax = sub["taxonomy"]
    trace.append("head taxonomy=%r verb default=%r" % (head_tax, vtax))

    filename, fn_trace = _extract_filename(obj_toks)
    trace.extend(fn_trace)
    purpose, pur_trace = _extract_purpose(np.rest)
    trace.extend(pur_trace)

    cands: List[_Candidate] = []

    def _file_entities() -> Dict[str, Any]:
        ent: Dict[str, Any] = {"artifact": "file"}
        if np.language:
            ent["language"] = np.language
        if purpose:
            ent["purpose"] = purpose
        if filename:
            ent["filename_hint"] = filename
        return ent

    def _media_entities() -> Dict[str, Any]:
        ent: Dict[str, Any] = {}
        prompt_bits = [np.phrase_text] + [_np_surface(np.rest)]
        prompt = " ".join(b for b in prompt_bits if b).strip()
        if prompt:
            ent["prompt"] = prompt
        if filename:
            ent["filename_hint"] = filename
        return ent

    if vtax is not None and head_tax is not None and vtax != head_tax:
        # verb class and NP head disagree -> genuine structural ambiguity
        trace.append("verb/NP taxonomy conflict: emitting both parses")
        cands.append(_Candidate(
            intent=_TAXONOMY_INTENT[vtax],
            entities=_media_entities(),
            output_type=STR, mood=mood, confidence=0.70,
            trace=trace + ["frame %s conf=0.70 (verb-class taxonomy wins)"
                           % _TAXONOMY_INTENT[vtax].name],
            rule="R_IMP_CREATION/verb"))
        cands.append(_Candidate(
            intent=_TAXONOMY_INTENT[head_tax],
            entities=_media_entities() if head_tax != "file"
            else _file_entities(),
            output_type=STR, mood=mood, confidence=0.65,
            trace=trace + ["frame %s conf=0.65 (NP-head taxonomy wins)"
                           % _TAXONOMY_INTENT[head_tax].name],
            rule="R_IMP_CREATION/noun"))
        return cands

    tax = head_tax or vtax
    if tax is None:
        trace.append("no artifact noun and no verb default: "
                     "defaulting to file (underspecified)")
        ent = _file_entities()
        ent["underspecified"] = True
        cands.append(_Candidate(
            intent=Intent.CREATE_FILE, entities=ent, output_type=STR,
            mood=mood, confidence=0.60,
            trace=trace + ["frame CREATE_FILE conf=0.60 (underspecified)"],
            rule="R_IMP_CREATION"))
        return cands

    intent = _TAXONOMY_INTENT[tax]
    ent = _file_entities() if tax == "file" else _media_entities()
    conf = 0.90 if head_tax == tax else 0.75
    trace.append("frame %s conf=%.2f (taxonomy=%s)"
                 % (intent.name, conf, tax))
    cands.append(_Candidate(intent=intent, entities=ent, output_type=STR,
                            mood=mood, confidence=conf,
                            trace=trace, rule="R_IMP_CREATION"))
    return cands


def _rule_imperative_dispatch(toks: List[_Token], verb: str,
                              mood: str) -> List[_Candidate]:
    trace = ["rule R_IMP_DISPATCH: [imperative + dispatch-verb %r]" % verb]
    start = 0
    for k, t in enumerate(toks):
        if t.kind == "WORD" and t.lower == verb:
            start = k + 1
            break
    goal = " ".join(t.text for t in toks[start:]
                    if not (t.kind == "PUNCT")).strip()
    trace.append("frame EXECUTE conf=0.80 goal_text=%r" % goal)
    return [_Candidate(intent=Intent.EXECUTE,
                       entities={"goal_text": goal},
                       output_type=ANY, mood=mood, confidence=0.80,
                       trace=trace, rule="R_IMP_DISPATCH")]


def _rule_imperative_compute(toks: List[_Token], verb: str,
                             mood: str) -> List[_Candidate]:
    trace = ["rule R_IMP_COMPUTE: [imperative + compute-verb %r + expr]"
             % verb]
    start = 0
    for k, t in enumerate(toks):
        if t.kind == "WORD" and t.lower == verb:
            start = k + 1
            break
    sub = _rule_arithmetic(toks[start:], mood)
    if not sub:
        trace.append("R_IMP_COMPUTE: no fire (no arithmetic after verb)")
        return []
    cand = sub[0]
    cand.trace = trace + cand.trace
    cand.confidence = 0.93
    cand.rule = "R_IMP_COMPUTE"
    return [cand]


def _rule_meta(toks: List[_Token], mood: str,
               raw: str) -> List[_Candidate]:
    trace = ["rule R_META: [interrogative + 2nd-person + ability-predicate]"]
    words = [t.lower for t in toks if t.kind == "WORD"]
    has_you = "you" in words
    do_forms = {"do", "does", "doing", "done"}
    # ability predicate: able/capable + to/of + verb complement, e.g.
    # "able to do", "able to help", "capable of doing"
    has_ability = False
    ability_form = ""
    for i, w in enumerate(words):
        if w not in _ABILITY or i + 2 >= len(words):
            continue
        prep, comp = words[i + 1], words[i + 2]
        if prep == "to" and (comp in do_forms or
                             comp not in _DET | _PREP | _WH | _AUX):
            has_ability, ability_form = True, "able to " + comp
            break
        if prep == "of" and comp.endswith("ing"):
            has_ability, ability_form = True, "capable of " + comp
            break
    has_can_do = ("can" in words or "could" in words) and \
        any(w in do_forms for w in words)
    if not (has_you and (has_ability or has_can_do)):
        trace.append("R_META: no fire (no 2nd-person ability structure)")
        return []
    trace.append("R_META: FIRED ability-question structure "
                 "(you + %s)" % (ability_form if has_ability
                                 else "can/could do"))
    topic = "capabilities"
    # "in terms of X" -> topic X
    for i, w in enumerate(words):
        if w == "terms" and i >= 1 and words[i - 1] == "in" \
                and i + 2 < len(words) and words[i + 1] == "of":
            topic = words[i + 2]
            trace.append("R_META: topic from 'in terms of' -> %r" % topic)
            break
        if w in ("of", "for") and i > 0 and words[i - 1] == "capable" \
                and i + 1 < len(words) and not words[i + 1].endswith("ing"):
            # "capable of X" where X is a gerund ("doing") is the verb
            # complement, not a topic -- leave the default
            topic = words[i + 1]
            trace.append("R_META: topic from 'capable of/for' -> %r"
                         % topic)
            break
    trace.append("frame ANSWER_META conf=0.85 topic=%r" % topic)
    return [_Candidate(intent=Intent.ANSWER_META,
                       entities={"topic": topic},
                       output_type=STR, mood=mood, confidence=0.85,
                       trace=trace, rule="R_META")]


def _rule_factual(mood: str, raw: str) -> List[_Candidate]:
    trace = ["rule R_FACT: interrogative fallback -> ANSWER_FACTUAL"]
    question = raw.strip()
    trace.append("frame ANSWER_FACTUAL conf=0.60")
    return [_Candidate(intent=Intent.ANSWER_FACTUAL,
                       entities={"question": question},
                       output_type=STR, mood=mood, confidence=0.60,
                       trace=trace, rule="R_FACT")]


def _rule_desire(toks: List[_Token], mood: str) -> List[_Candidate]:
    trace = ["rule R_DESIRE: [declarative + 1st-person + desire-verb]"]
    words = [t for t in toks if t.kind == "WORD"]
    if len(words) < 2 or words[0].lower not in ("i", "we"):
        trace.append("R_DESIRE: no fire (not 1st-person first)")
        return []
    verb = words[1].lower
    if verb not in _VERBS or _VERBS[verb][0] != "desire":
        trace.append("R_DESIRE: no fire (%r not a desire verb)" % verb)
        return []
    goal = " ".join(t.text for t in toks
                    if not (t.kind == "PUNCT")).strip()
    # drop the "I need" scaffolding from the goal text
    goal_words = goal.split()
    goal = " ".join(goal_words[2:]) if len(goal_words) > 2 else goal
    trace.append("frame ROUTE conf=0.70 goal_text=%r" % goal)
    return [_Candidate(intent=Intent.ROUTE,
                       entities={"goal_text": goal},
                       output_type=ANY, mood=mood, confidence=0.70,
                       trace=trace, rule="R_DESIRE")]


# ---------------------------------------------------------------------------
# 6. Driver: tokenise -> mood -> rules -> candidates -> frame / ambiguity
# ---------------------------------------------------------------------------

def _candidate_frame(raw: str, cand: _Candidate) -> IntentFrame:
    return IntentFrame(
        raw=raw,
        intent=cand.intent,
        entities=dict(cand.entities),
        output_type=cand.output_type,
        mood=cand.mood,
        confidence=cand.confidence,
        trace=list(cand.trace),
    )


def _parse_inner(raw: str) -> IntentFrame:
    if not isinstance(raw, str):
        return IntentFrame(
            raw="", intent=Intent.UNKNOWN, mood="declarative",
            confidence=0.0,
            trace=["non-str input (%s); failing closed to UNKNOWN"
                   % type(raw).__name__])
    if not raw.strip():
        return IntentFrame(
            raw=raw, intent=Intent.UNKNOWN, mood="declarative",
            confidence=0.0,
            trace=["empty/whitespace input; no grammatical parse"])

    toks = _tokenize(raw)
    trace_head = ["tokenise: %d tokens (%s)"
                  % (len(toks), ", ".join(
                      "%s=%d" % (k, sum(1 for t in toks if t.kind == k))
                      for k in ("WORD", "NUMBER", "OP", "QUOTE", "PUNCT")
                      if any(t.kind == k for t in toks)))]

    mood, mood_trace = _detect_mood(toks, raw)
    candidates: List[_Candidate] = []

    # arithmetic is mood-independent: try it first
    candidates.extend(_rule_arithmetic(toks, mood))

    words = [t for t in toks if t.kind == "WORD"]
    first_verb = None
    first_vclass = None
    first_vtax = None
    widx = 0
    if words and words[0].lower == "please" and len(words) > 1:
        widx = 1
    if len(words) > widx and words[widx].lower in _VERBS:
        first_verb = words[widx].lower
        first_vclass, first_vtax = _VERBS[first_verb]

    if mood == "imperative" and first_verb:
        if first_vclass in ("creation", "imagine", "film", "music"):
            candidates.extend(_rule_imperative_creation(
                toks, first_verb, first_vclass, first_vtax, mood, raw))
        elif first_vclass == "dispatch":
            candidates.extend(_rule_imperative_dispatch(toks, first_verb,
                                                        mood))
        elif first_vclass == "compute":
            candidates.extend(_rule_imperative_compute(toks, first_verb,
                                                       mood))
    elif mood == "interrogative":
        candidates.extend(_rule_meta(toks, mood, raw))
        if not any(c.rule == "R_META" for c in candidates):
            candidates.extend(_rule_factual(mood, raw))
    elif mood == "declarative":
        candidates.extend(_rule_desire(toks, mood))

    if not candidates:
        return IntentFrame(
            raw=raw, intent=Intent.UNKNOWN, mood=mood, confidence=0.0,
            trace=trace_head + mood_trace +
            ["no grammar rule fired; failing closed to UNKNOWN"])

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    top = candidates[0]
    if len(candidates) >= 2 and \
            top.confidence - candidates[1].confidence <= _AMBIGUITY_MARGIN:
        alts = [_candidate_frame(raw, c) for c in candidates
                if top.confidence - c.confidence <= _AMBIGUITY_MARGIN]
        amb_trace = (trace_head + mood_trace + top.trace +
                     ["AMBIGUOUS: %d parses within %.2f "
                      "(%s)" % (len(alts), _AMBIGUITY_MARGIN,
                                ", ".join("%s=%.2f" % (a.intent.name,
                                                       a.confidence)
                                          for a in alts))])
        return IntentFrame(
            raw=raw, intent=Intent.AMBIGUOUS, mood=mood,
            confidence=top.confidence,
            alternatives=alts,
            trace=amb_trace)
    frame = _candidate_frame(raw, top)
    frame.trace = trace_head + mood_trace + frame.trace
    return frame


# ---------------------------------------------------------------------------
# Representational boundaries (documented, not faked)
# ---------------------------------------------------------------------------
#
# The grammar deliberately does not cover:
# * negation / modality ("don't create a file", "could you maybe ...") —
#   parses as the positive form; negation scope is out of scope.
# * coordination ("create a file and draw a picture") — only the first
#   clause's structure drives the frame.
# * anaphora / ellipsis ("make it bigger", "and one for sorting") — no
#   discourse state, so these go UNKNOWN or underspecified.
# * non-English input — the lexicon is English-only; other languages
#   yield UNKNOWN (fail closed, honest).
# * genuinely novel verb senses ("screenshot this", "grep the logs") —
#   unknown verbs fail closed to UNKNOWN rather than guessing.
# * multi-step plans ("first X then Y") — one clause, one frame.
# * purpose PPs other than for/to/that ("a file with random numbers")
#   are not mined for purpose; the frame still fires without purpose.
# * leading unary minus inside longer expressions ("5 * -3") — only a
#   leading unary minus is folded; interior ones fail the NUMBER/OP
#   alternation and the rule does not fire.
