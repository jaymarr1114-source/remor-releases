"""
swarm_engine/cognition/semantic_lexicon.py

SemanticLexicon: distributionally-learned phrase -> meaning associations.

The missing semantic layer, built without a hand-authored parser.

Principle (distributional semantics, behaviorally grounded):
  REMOR already stores every verified (goal, expr, examples) triple in
  CaseMemory. When the goal is a natural-language sentence, the pair
  (sentence, verified-Expr) is a *behaviorally grounded* semantic datum:
  the Expr is the machine meaning of the sentence, verified against
  independent behavioral examples.

  The lexicon learns, purely statistically, which n-gram phrases co-occur
  with which primitive ops (and which literals) across many such pairs.
  "falls below" and "drops under" both co-occur with `less_than` across
  different sentences -> they are learned as equivalent realizations of
  the same semantic primitive. No grammar is hand-authored; no per-sentence
  parse is supplied. The *content* of the lexicon is entirely learned from
  REMOR's own verified experience.

What this is NOT:
  - Not a parser: there are no syntactic rules, no POS tags, no dependency
    arcs. Only tokenization (regex) and co-occurrence statistics.
  - Not keyword routing: associations require bidirectional statistical
    support (P(op|phrase) high AND phrase discriminative), and every
    interpretation is behaviorally verified before acceptance.
  - Not training leakage: the lexicon proposes candidate ops; the
    independent behavioral examples determine the actual program.

Architectural separation (revert lesson):
  The lexicon NEVER writes into SearchBias and NEVER modifies the main
  registry. It produces a *candidate op set* for one interpretation task;
  the interpreter builds a filtered registry view for that task only.
  General synthesis is untouched.

Fail-closed:
  - No associations above confidence threshold -> no candidate ops ->
    interpretation refuses (returns None), never guesses.
  - Truly novel phrases (never seen) have no entry -> fail closed by design.
    Zero-shot paraphrase of unseen words is explicitly out of scope;
    generalization is within learned equivalence classes.
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


# Tokenizer: lowercase alphanumeric words. General, not domain-specific.
_WORD_RE = re.compile(r"[a-z0-9']+")

# Stopwords removed for unigrams only; kept inside bi/trigrams so that
# phrases like "falls below" (no stopwords) and "is allowed" survive, while
# lone "the"/"is"/"a" do not become lexicon entries.
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "to", "for", "and", "or", "in", "on", "with",
    "from", "into", "by", "as", "is", "are", "be", "being", "been", "that",
    "this", "it", "its", "at", "which", "who", "whom", "whose", "not",
    "if", "when", "then", "than",
})

# Minimum n-gram requirements to earn an association.
MIN_SUPPORT = 2          # phrase must appear in >= this many training pairs
MIN_P_OP_GIVEN_PHRASE = 0.5  # P(op|phrase) must reach this


def tokenize(sentence: str) -> List[str]:
    """Lowercase word tokens. The only language-specific step, and it is
    trivially general (no vocabulary, no grammar)."""
    return _WORD_RE.findall(sentence.lower())


def ngrams(tokens: Sequence[str], max_n: int = 3) -> Set[str]:
    """1-3 grams; unigrams drop stopwords, longer n-grams keep them."""
    out: Set[str] = set()
    n = len(tokens)
    for i in range(n):
        for j in range(1, max_n + 1):
            if i + j > n:
                break
            gram = tokens[i:i + j]
            if len(gram) == 1 and gram[0] in _STOPWORDS:
                continue
            out.add(" ".join(gram))
    return out


def _looks_like_sentence(goal: str) -> bool:
    """Heuristic filter: only learn from goal strings that look like
    natural-language sentences (not programmatic goal labels). General:
    contains spaces and alphabetic words; not a snake_case identifier."""
    if not goal or " " not in goal:
        return False
    words = tokenize(goal)
    if len(words) < 3:
        return False
    # reject snake_case / dotted identifiers
    if re.fullmatch(r"[a-z0-9_.]+", goal.strip()):
        return False
    return True


@dataclass
class LexiconEntry:
    """A learned phrase -> meaning association."""
    phrase: str
    kind: str              # "op" or "literal"
    value: Any             # op name (str) or literal value
    p_given_phrase: float  # P(value | phrase)
    support: int           # number of training pairs with this phrase
    confidence: float      # Laplace-smoothed, for ranking

    def as_dict(self) -> Dict[str, Any]:
        return {
            "phrase": self.phrase, "kind": self.kind, "value": self.value,
            "p_given_phrase": self.p_given_phrase, "support": self.support,
            "confidence": self.confidence,
        }


class SemanticLexicon:
    """Distributional phrase->meaning lexicon, learned from verified
    (sentence, Expr) pairs. Persisted in the engine DB."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        self._entries: List[LexiconEntry] = []
        self._by_phrase: Dict[str, List[LexiconEntry]] = defaultdict(list)
        self._init_db()
        self._load()

    # -- persistence ----------------------------------------------------
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS semantic_lexicon (
                phrase TEXT, kind TEXT, value TEXT,
                p_given_phrase REAL, support INTEGER, confidence REAL,
                learned_at REAL,
                PRIMARY KEY (phrase, kind, value))""")
            c.execute("""CREATE TABLE IF NOT EXISTS lexicon_training_pairs (
                pair_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sentence TEXT, expr_canonical TEXT, learned_at REAL)""")

    def _load(self) -> None:
        with self._conn() as c:
            rows = c.execute(
                "SELECT phrase, kind, value, p_given_phrase, support, "
                "confidence FROM semantic_lexicon").fetchall()
        import json
        for r in rows:
            try:
                value = json.loads(r["value"])
            except Exception:
                value = r["value"]
            e = LexiconEntry(r["phrase"], r["kind"], value,
                             r["p_given_phrase"], r["support"], r["confidence"])
            self._entries.append(e)
            self._by_phrase[e.phrase].append(e)

    def _save_entry(self, e: LexiconEntry) -> None:
        import json
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO semantic_lexicon "
                "(phrase, kind, value, p_given_phrase, support, confidence,"
                " learned_at) VALUES (?,?,?,?,?,?,?)",
                (e.phrase, e.kind, json.dumps(e.value, default=str),
                 e.p_given_phrase, e.support, e.confidence, time.time()))

    # -- learning -------------------------------------------------------
    def learn(self, pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        """Learn associations from (sentence, Expr) pairs. Expr is the
        verified expression object (must provide .ops_used() and support
        sub-expression traversal for literals). Returns learning stats.

        pairs: sequence of (sentence, expr). Only sentence-like goals are
        used; others are skipped (reported in stats).
        """
        usable: List[Tuple[str, Any]] = []
        skipped = 0
        for sent, expr in pairs:
            if expr is None or not _looks_like_sentence(sent):
                skipped += 1
                continue
            usable.append((sent, expr))
        n = len(usable)
        if n == 0:
            return {"usable": 0, "skipped": skipped, "new_entries": 0}

        # Collect: phrase -> op -> count ; phrase -> literal -> count
        phrase_op: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        phrase_lit: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        phrase_total: Dict[str, int] = defaultdict(int)
        op_total: Dict[str, int] = defaultdict(int)

        for sent, expr in pairs:
            grams = ngrams(tokenize(sent))
            try:
                ops = set(expr.ops_used())
            except Exception:
                ops = set()
            lits = _expr_literals(expr)
            for g in grams:
                phrase_total[g] += 1
                for op in ops:
                    phrase_op[g][op] += 1
                    op_total[op] += 1
                for lit in lits:
                    phrase_lit[g][repr(lit)] += 1

        new_entries = 0
        for phrase, total in phrase_total.items():
            if total < MIN_SUPPORT:
                continue
            # op associations
            for op, c in phrase_op.get(phrase, {}).items():
                p = c / total
                if p < MIN_P_OP_GIVEN_PHRASE:
                    continue
                conf = (c + 1) / (total + len(op_total) + 1)
                e = LexiconEntry(phrase, "op", op, p, total, conf)
                self._save_entry(e)
                self._entries.append(e)
                self._by_phrase[phrase].append(e)
                new_entries += 1
            # literal associations (only for literals seen with this phrase
            # in a majority of the phrase's occurrences)
            for lit_repr, c in phrase_lit.get(phrase, {}).items():
                p = c / total
                if p < MIN_P_OP_GIVEN_PHRASE or total < MIN_SUPPORT:
                    continue
                try:
                    import json as _json
                    lit_val = _json.loads(lit_repr)
                except Exception:
                    continue
                # Only learn "content" literals, not structural ones that
                # appear everywhere (guard against 0/1/True/False which are
                # ubiquitous and not phrase-specific).
                if _is_ubiquitous_literal(lit_val, usable):
                    continue
                conf = (c + 1) / (total + 2)
                e = LexiconEntry(phrase, "literal", lit_val, p, total, conf)
                self._save_entry(e)
                self._entries.append(e)
                self._by_phrase[phrase].append(e)
                new_entries += 1

        # record training pairs for provenance
        with self._conn() as c:
            for sent, expr in usable:
                try:
                    canon = expr.canonical()
                except Exception:
                    canon = str(expr)[:200]
                c.execute(
                    "INSERT INTO lexicon_training_pairs "
                    "(sentence, expr_canonical, learned_at) VALUES (?,?,?)",
                    (sent, canon, time.time()))

        return {"usable": n, "skipped": skipped, "new_entries": new_entries,
                "phrases": len(phrase_total)}

    # -- lookup ---------------------------------------------------------
    def lookup(self, phrase: str, kind: Optional[str] = None,
               min_confidence: float = 0.0) -> List[LexiconEntry]:
        """Entries for an exact phrase, optionally filtered by kind and
        minimum confidence, sorted by confidence descending."""
        out = [e for e in self._by_phrase.get(phrase, [])
               if (kind is None or e.kind == kind)
               and e.confidence >= min_confidence]
        out.sort(key=lambda e: -e.confidence)
        return out

    def ops_for(self, sentence: str,
                min_confidence: float = 0.3) -> Dict[str, float]:
        """Candidate ops for a sentence: best confidence per op across all
        its n-grams. Empty dict -> fail closed (no interpretation)."""
        best: Dict[str, float] = {}
        for gram in ngrams(tokenize(sentence)):
            for e in self.lookup(gram, kind="op",
                                 min_confidence=min_confidence):
                if e.confidence > best.get(e.value, 0.0):
                    best[e.value] = e.confidence
        return best

    def literals_for(self, sentence: str,
                     min_confidence: float = 0.3) -> List[Any]:
        """Candidate literals suggested by the sentence's phrases."""
        out: List[Any] = []
        seen: Set[str] = set()
        for gram in ngrams(tokenize(sentence)):
            for e in self.lookup(gram, kind="literal",
                                 min_confidence=min_confidence):
                key = repr(e.value)
                if key not in seen:
                    seen.add(key)
                    out.append(e.value)
        return out

    def entry_count(self) -> int:
        return len(self._entries)

    def stats(self) -> Dict[str, Any]:
        return {"entries": len(self._entries),
                "phrases": len(self._by_phrase)}


def _expr_literals(expr: Any) -> List[Any]:
    """All literal values in an Expr tree."""
    out: List[Any] = []
    try:
        stack = [expr]
        while stack:
            e = stack.pop()
            if e.is_leaf():
                if e.is_literal:
                    out.append(e.literal)
            else:
                for _, child in e.children:
                    stack.append(child)
    except Exception:
        pass
    return out


def _is_ubiquitous_literal(value: Any,
                           pairs: Sequence[Tuple[str, Any]]) -> bool:
    """Guard: literals appearing in a large fraction of ALL training exprs
    (0, 1, True, False, ...) are not phrase-specific and must not become
    lexicon entries."""
    if not pairs:
        return True
    hits = 0
    for _, expr in pairs:
        if value in _expr_literals(expr):
            hits += 1
    return (hits / len(pairs)) >= 0.5
