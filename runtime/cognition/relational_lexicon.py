"""
swarm_engine/cognition/relational_lexicon.py

RelationalLexicon: learned pattern -> (predicate, role_map) associations for
binary semantic relations.

Where SemanticLexicon learns which *phrases* co-occur with which primitive
ops (unary, e.g. "falls below" -> less_than), this module learns *who does
what to whom*: for sentences expressing a binary relation between two
entities, it learns a pattern with entity slots and an explicit mapping
from surface positions to canonical argument roles.

Example (learned, never told):
  ("blue is supported by red", RelFact("supports", "red", "blue"))
  -> pattern ("_E1_","is","supported","by","_E2_")
     predicate "supports", role_map {0:1, 1:0}
  i.e. the first surface entity is the PATIENT (arg1), the second is the
  AGENT (arg0). The role map is discovered by ALIGNMENT: the surface
  entities are located inside the fact's argument list, so the mapping
  from surface order to argument order is learned from the verified pair,
  not hand-authored.

What this is NOT:
  - Not a parser: no POS tags, no dependency arcs, no grammar rules.
    Only tokenization plus entity-slot abstraction.
  - Not keyword routing: "red supports blue" and "blue supports red" share
    the exact word multiset but produce DIFFERENT RelFacts, because the
    decision comes from the learned role map applied to surface ORDER.
  - Not training leakage: training pairs come from `discover`, whose
    verdict is driven by independent behavioral probes authored from the
    world; the sentence only constrains the entity set. Interpretations
    are verified against fresh probes before acceptance.

Fail-closed (mirrors semantic_lexicon.py):
  - Unknown pattern -> None (never guess).
  - Ambiguous pattern (one pattern ever mapped to two different
    (predicate, role_map) pairs) -> None.
  - Fewer or more than 2 distinct known entities in the sentence -> None.
  - Any probe fails -> None.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from itertools import permutations
from typing import Dict, List, Optional, Sequence, Set, Tuple

_WORD_RE = re.compile(r"[a-z0-9']+")

MIN_SUPPORT = 2  # a pattern must be seen with the same mapping >= this often


def tokenize(sentence: str) -> List[str]:
    """Lowercase word tokens. Trivially general (no vocabulary, no grammar)."""
    return _WORD_RE.findall(sentence.lower())


# ---------------------------------------------------------------------------
# RelFact: immutable binary relation in canonical role order.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RelFact:
    """A binary relational fact. arg0 is the AGENT (canonical role 0),
    arg1 is the PATIENT (canonical role 1)."""
    predicate: str
    arg0: str
    arg1: str

    def args(self) -> Tuple[str, str]:
        return (self.arg0, self.arg1)


def agent_of(fact: RelFact) -> str:
    """Behavioral interface: the agent of the relation is arg0."""
    return fact.arg0


def patient_of(fact: RelFact) -> str:
    """Behavioral interface: the patient of the relation is arg1."""
    return fact.arg1


# ---------------------------------------------------------------------------
# RelationalLexicon
# ---------------------------------------------------------------------------

class RelationalLexicon:
    """Learns pattern -> (predicate, role_map) from verified
    (sentence, RelFact) pairs. Persisted in sqlite."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        # entity names known from training facts' args (lowercased)
        self._entities: Set[str] = set()
        # pattern -> {(pred, role_map_key): count}
        self._obs: Dict[Tuple[str, ...],
                        Dict[Tuple[str, Tuple[Tuple[int, int], ...]], int]] = {}
        # committed: pattern -> (predicate, role_map dict)
        self._committed: Dict[Tuple[str, ...],
                              Tuple[str, Dict[int, int]]] = {}
        # ambiguous patterns: fail closed at interpret time
        self._ambiguous: Set[Tuple[str, ...]] = set()
        self._init_db()
        self._load()

    # -- persistence ----------------------------------------------------
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS relational_pattern_obs (
                pattern TEXT, pred TEXT, role_map TEXT, cnt INTEGER,
                PRIMARY KEY (pattern, pred, role_map))""")
            c.execute("""CREATE TABLE IF NOT EXISTS relational_patterns (
                pattern TEXT PRIMARY KEY, pred TEXT, role_map TEXT,
                support INTEGER, confidence REAL, ambiguous INTEGER,
                learned_at REAL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS relational_entities (
                name TEXT PRIMARY KEY)""")
            c.execute("""CREATE TABLE IF NOT EXISTS relational_training_pairs (
                pair_id INTEGER PRIMARY KEY AUTOINCREMENT,
                sentence TEXT, fact_canonical TEXT, learned_at REAL)""")

    def _load(self) -> None:
        with self._conn() as c:
            for r in c.execute(
                    "SELECT pattern, pred, role_map, cnt "
                    "FROM relational_pattern_obs").fetchall():
                pat = tuple(json.loads(r["pattern"]))
                key = (r["pred"], tuple(
                    tuple(p) for p in json.loads(r["role_map"])))
                self._obs.setdefault(pat, {})[key] = r["cnt"]
            for r in c.execute("SELECT name FROM relational_entities").fetchall():
                self._entities.add(r["name"])
        self._recompute()

    def _recompute(self) -> None:
        """Derive committed/ambiguous status from observations."""
        self._committed = {}
        self._ambiguous = set()
        for pat, counts in self._obs.items():
            if len(counts) > 1:
                # One pattern ever mapped to two different
                # (predicate, role_map) pairs -> ambiguous, fail closed.
                self._ambiguous.add(pat)
                continue
            (pred, rmk), cnt = next(iter(counts.items()))
            if cnt >= MIN_SUPPORT:
                self._committed[pat] = (pred, dict(rmk))

    def _persist_pattern(self, pat: Tuple[str, ...]) -> None:
        counts = self._obs.get(pat, {})
        total = sum(counts.values())
        ambiguous = 1 if pat in self._ambiguous else 0
        with self._conn() as c:
            for (pred, rmk), cnt in counts.items():
                c.execute(
                    "INSERT OR REPLACE INTO relational_pattern_obs "
                    "(pattern, pred, role_map, cnt) VALUES (?,?,?,?)",
                    (json.dumps(list(pat)), pred,
                     json.dumps([list(p) for p in rmk]), cnt))
            if pat in self._committed:
                pred, role_map = self._committed[pat]
                rmk = tuple(sorted(role_map.items()))
                conf = counts[(pred, rmk)] / total if total else 0.0
                c.execute(
                    "INSERT OR REPLACE INTO relational_patterns "
                    "(pattern, pred, role_map, support, confidence, "
                    " ambiguous, learned_at) VALUES (?,?,?,?,?,?,?)",
                    (json.dumps(list(pat)), pred,
                     json.dumps([list(p) for p in rmk]),
                     counts[(pred, rmk)], conf, ambiguous, time.time()))
            else:
                c.execute(
                    "INSERT OR REPLACE INTO relational_patterns "
                    "(pattern, pred, role_map, support, confidence, "
                    " ambiguous, learned_at) VALUES (?,?,?,?,?,?,?)",
                    (json.dumps(list(pat)), None, None, total, 0.0,
                     ambiguous, time.time()))

    def _persist_entities(self) -> None:
        with self._conn() as c:
            for name in self._entities:
                c.execute(
                    "INSERT OR IGNORE INTO relational_entities (name) "
                    "VALUES (?)", (name,))

    # -- entity / pattern machinery -------------------------------------
    def _known_names(self,
                     extra: Optional[Sequence[str]] = None) -> Set[str]:
        names = set(self._entities)
        for e in extra or ():
            names.add(e.lower())
        return names

    def _surface_entities(self, sentence: str,
                          names: Set[str]) -> List[str]:
        """Known entity names occurring in the sentence, in SURFACE ORDER
        (first appearance), deduplicated."""
        seen: List[str] = []
        for tok in tokenize(sentence):
            if tok in names and tok not in seen:
                seen.append(tok)
        return seen

    def _pattern(self, sentence: str, names: Set[str]
                 ) -> Tuple[Tuple[str, ...], List[str]]:
        """Token sequence with entity mentions replaced by _E1_, _E2_, ...
        in order of first appearance. Returns (pattern, surface_entities)."""
        surface = self._surface_entities(sentence, names)
        slot = {name: f"_E{i + 1}_" for i, name in enumerate(surface)}
        pat = tuple(slot.get(tok, tok) for tok in tokenize(sentence))
        return pat, surface

    def _align(self, surface: List[str], fact: RelFact
               ) -> Optional[Dict[int, int]]:
        """Learn role_map by ALIGNMENT: locate each surface entity inside
        the fact's argument list. Returns {surface_pos: arg_pos} or None
        if alignment is impossible/ambiguous."""
        fargs = [fact.arg0.lower(), fact.arg1.lower()]
        if fargs[0] == fargs[1]:
            return None
        role_map: Dict[int, int] = {}
        for i, s in enumerate(surface):
            hits = [j for j, f in enumerate(fargs) if f == s.lower()]
            if len(hits) != 1:
                return None
            role_map[i] = hits[0]
        return role_map

    # -- learning -------------------------------------------------------
    def train(self, pairs: Sequence[Tuple[str, RelFact]],
              candidate_entities: Optional[Sequence[str]] = None
              ) -> Dict[str, object]:
        """Batch-learn from verified (sentence, RelFact) pairs.

        For each pair: learn entity names from the fact's args, build the
        entity-slot pattern, align surface entities to fact args to get
        the role_map, and aggregate. Returns learning stats."""
        stats = {"usable": 0, "skipped": 0, "committed": 0, "ambiguous": 0}
        touched: Set[Tuple[str, ...]] = set()
        for sent, fact in pairs:
            if not sent or not sent.strip() or fact is None:
                stats["skipped"] += 1
                continue
            # Learn entity names from the fact's own args (lowercased).
            self._entities.add(fact.arg0.lower())
            self._entities.add(fact.arg1.lower())
            names = self._known_names(candidate_entities)
            pat, surface = self._pattern(sent, names)
            if len(surface) != 2:
                stats["skipped"] += 1
                continue
            role_map = self._align(surface, fact)
            if role_map is None:
                stats["skipped"] += 1
                continue
            rmk = tuple(sorted(role_map.items()))
            key = (fact.predicate, rmk)
            d = self._obs.setdefault(pat, {})
            d[key] = d.get(key, 0) + 1
            touched.add(pat)
            stats["usable"] += 1
        self._recompute()
        for pat in touched:
            self._persist_pattern(pat)
        self._persist_entities()
        with self._conn() as c:
            for sent, fact in pairs:
                if fact is None:
                    continue
                c.execute(
                    "INSERT INTO relational_training_pairs "
                    "(sentence, fact_canonical, learned_at) VALUES (?,?,?)",
                    (sent, repr(fact), time.time()))
        stats["committed"] = len(self._committed)
        stats["ambiguous"] = len(self._ambiguous)
        return stats

    # -- honest discovery -----------------------------------------------
    def discover(self, sentence: str,
                 candidate_entities: Sequence[str],
                 candidate_predicates: Sequence[str],
                 probes: Sequence[Tuple[str, str]]
                 ) -> Optional[RelFact]:
        """Honest source of verified training pairs.

        Searches all (pred, a0, a1) with a0,a1 drawn from the entities
        mentioned in the sentence (ordered permutations) and pred in
        candidate_predicates. Keeps facts answering EVERY probe
        (probes are independent behavioral evidence authored from the
        world; the sentence only constrains the entity set). Returns the
        fact iff EXACTLY ONE candidate survives, else None."""
        names = self._known_names(candidate_entities)
        mentioned = self._surface_entities(sentence, names)
        if len(mentioned) < 2:
            return None
        survivors: List[RelFact] = []
        for pred in candidate_predicates:
            for a0, a1 in permutations(mentioned, 2):
                fact = RelFact(pred, a0, a1)
                if all(self._probe_ok(fact, kind, expected)
                       for kind, expected in probes):
                    survivors.append(fact)
        if len(survivors) != 1:
            return None
        return survivors[0]

    @staticmethod
    def _probe_ok(fact: RelFact, kind: str, expected: str) -> bool:
        expected = expected.lower()
        if kind == "agent":
            return agent_of(fact).lower() == expected
        if kind == "patient":
            return patient_of(fact).lower() == expected
        return False

    # -- interpretation -------------------------------------------------
    def interpret(self, sentence: str,
                  candidate_entities: Sequence[str],
                  probes: Optional[Sequence[Tuple[str, str]]] = None
                  ) -> Optional[RelFact]:
        """Interpret a sentence via the learned lexicon. Fail closed:
        unknown pattern -> None; ambiguous pattern -> None; anything but
        exactly 2 distinct known entities -> None; any probe failure ->
        None."""
        if not sentence or not sentence.strip():
            return None
        names = self._known_names(candidate_entities)
        pat, surface = self._pattern(sentence, names)
        if len(surface) != 2:
            return None
        if pat in self._ambiguous:
            return None
        entry = self._committed.get(pat)
        if entry is None:
            return None
        predicate, role_map = entry
        try:
            args = {role_map[i]: surface[i] for i in (0, 1)}
            fact = RelFact(predicate, args[0], args[1])
        except KeyError:
            return None
        for kind, expected in probes or ():
            if not self._probe_ok(fact, kind, expected):
                return None
        return fact

    # -- audit surface --------------------------------------------------
    def slot_pattern(self, sentence: str,
                     candidate_entities: Optional[Sequence[str]] = None
                     ) -> Tuple[Tuple[str, ...], List[str]]:
        """Public, additive, read-only: entity-slot pattern plus surface
        entities (in surface order) for a sentence. Shares the exact
        machinery of learn/interpret/discover but commits nothing and
        alters no semantics; exists so the evidence seeker can represent
        a failed interpretation as a first-class gap."""
        names = self._known_names(candidate_entities)
        return self._pattern(sentence, names)

    def entries(self) -> List[Dict[str, object]]:
        """Committed pattern entries (for audit / tests)."""
        out = []
        for pat, (pred, role_map) in sorted(self._committed.items()):
            counts = self._obs[pat]
            rmk = tuple(sorted(role_map.items()))
            support = counts[(pred, rmk)]
            total = sum(counts.values())
            out.append({
                "pattern": pat,
                "predicate": pred,
                "role_map": dict(role_map),
                "support": support,
                "confidence": support / total if total else 0.0,
            })
        return out

    def ambiguous_patterns(self) -> List[Tuple[str, ...]]:
        return sorted(self._ambiguous)

    def stats(self) -> Dict[str, int]:
        return {"committed": len(self._committed),
                "ambiguous": len(self._ambiguous),
                "entities": len(self._entities),
                "observed_patterns": len(self._obs)}
