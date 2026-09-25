"""
swarm_engine/cognition/evidence_seeker.py

Phase 14: autonomous evidence acquisition -- the first genuine agency step.

The agency gap: until now, when RelationalLexicon.interpret() returned None
for an unknown pattern, the system just failed closed. It could not REPRESENT
the gap, CHOOSE an evidence-acquisition action, EXECUTE it, and LEARN.

This module closes that loop in the smallest genuine instance:

  1. REPRESENT: a failed interpretation becomes an EvidenceGap dataclass --
     the gap is now data (sentence, entity-slot pattern, surface entities,
     candidate predicate), not just a None.
  2. CHOOSE: the seeker's fixed MINIMAL-EVIDENCE POLICY -- for a binary
     pattern, knowing the AGENT determines the patient by elimination, so
     exactly ONE oracle question (ask_role(pred, "agent", entities))
     fully determines the role mapping. The test audits this choice:
     act vs stay closed, and which single question to ask.
  3. EXECUTE: run the query against the WorldOracle. The learner NEVER sees
     the oracle's ground-truth facts directly -- only answers to its own
     queries. Every query is logged (count + args) for the autonomy audit.
  4. LEARN: build probes from the answer, run the standard discover
     protocol (behavioral-verification gauntlet: exactly one surviving fact),
     and train once. Oracle answers None -> gap stays OPEN; discovery
     fails -> gap stays OPEN. No hallucination, no invented pairs.

What this is NOT (deliberately out of scope):
  - No multi-step plans, no information-gain computation over candidate
    queries: the single-question policy is FIXED, not computed.
  - No ambiguity-driven seeking: the seeker only fires on unknown patterns.
  - No language-generation: the question is a structured oracle call.
  The natural next boundary is ambiguity/conflict-driven seeking plus
  query selection by expected information gain.

Fail-closed invariants (same discipline as the lexicon):
  - Sentences the lexicon already handles: no oracle contact at all.
  - A pattern already acquired in this run: no re-query.
  - Oracle returns None: gap recorded OPEN, nothing trained.
  - Discovery fails: gap recorded OPEN, nothing trained.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from swarm_engine.cognition.relational_lexicon import (
    RelFact, RelationalLexicon,
)

_SLOT_RE = re.compile(r"^_E\d+_$")


# ---------------------------------------------------------------------------
# WorldOracle: the environment. Holds ground-truth RelFacts. The ONLY
# interface the learner may use is ask_role; the facts are never visible.
# ---------------------------------------------------------------------------

class WorldOracle:
    """Environment holding ground-truth RelFacts (test-authored, like a
    blocks world). The learner NEVER sees the facts directly -- only the
    answers to its own ask_role queries. Every query is logged for the
    autonomy audit."""

    def __init__(self, facts: Sequence[RelFact]):
        self._facts: List[RelFact] = list(facts)
        self._query_log: List[Tuple[str, str, Tuple[str, ...]]] = []

    def ask_role(self, predicate: str, role: str,
                 entities: Tuple[str, ...]) -> Optional[str]:
        """Return the agent/patient entity from the oracle fact matching
        (predicate, set(entities)), or None if it has no such fact."""
        ents = tuple(e.lower() for e in entities)
        self._query_log.append((predicate.lower(), role, ents))
        want = set(ents)
        for f in self._facts:
            if (f.predicate.lower() == predicate.lower()
                    and {f.arg0.lower(), f.arg1.lower()} == want):
                if role == "agent":
                    return f.arg0
                if role == "patient":
                    return f.arg1
                return None
        return None

    @property
    def query_count(self) -> int:
        """Number of ask_role calls issued so far."""
        return len(self._query_log)

    def queries(self) -> List[Tuple[str, str, Tuple[str, ...]]]:
        """The full logged action history: (predicate, role, entities)."""
        return list(self._query_log)


# ---------------------------------------------------------------------------
# EvidenceGap: first-class represented gap.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceGap:
    """A represented interpretation gap: the lexicon failed closed on
    `sentence`, and the seeker turned that failure into data."""
    sentence: str
    slot_pattern: Tuple[str, ...]   # entity-slot pattern, e.g. ("_E1_","upholds","_E2_")
    entities: Tuple[str, ...]       # surface entities in surface order
    candidate_predicate: str        # non-entity tokens of the pattern joined


# ---------------------------------------------------------------------------
# SeekerResult: the full audit trail of one seek() run.
# ---------------------------------------------------------------------------

@dataclass
class SeekerResult:
    gaps: List[EvidenceGap] = field(default_factory=list)
    open_gaps: List[EvidenceGap] = field(default_factory=list)
    queries_made: int = 0                     # exactly one per resolved query
    action_log: List[Dict[str, object]] = field(default_factory=list)
    pairs_acquired: List[Tuple[str, RelFact]] = field(default_factory=list)
    patterns_committed: List[Dict[str, object]] = field(default_factory=list)
    train_stats: Dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# EvidenceSeeker
# ---------------------------------------------------------------------------

class EvidenceSeeker:
    """Autonomous evidence acquisition for unknown binary relational
    patterns.

    Fixed minimal-evidence policy (documented, not computed): for a binary
    pattern, asking which surface entity is the AGENT determines the full
    role mapping -- the patient is the other entity by elimination. So
    exactly one ask_role("agent") query per unknown binary pattern. No
    multi-query probing, no information-gain search; the agency audited
    here is act-vs-stay-closed plus which single question to ask.
    """

    @staticmethod
    def _candidate_predicate(slot_pattern: Tuple[str, ...]) -> str:
        """The pattern's non-entity tokens joined -- derived generally,
        never hardcoded. e.g. ("_E1_","upholds","_E2_") -> "upholds"."""
        return " ".join(t for t in slot_pattern if not _SLOT_RE.match(t))

    def seek(self, lexicon: RelationalLexicon, oracle: WorldOracle,
             sentences: Sequence[str],
             candidate_entities: Sequence[str]) -> SeekerResult:
        result = SeekerResult()
        # Exact (slot_pattern, entities) shapes already queried this run.
        # NOTE: the no-requery rule is scoped to the exact sentence shape,
        # not the bare pattern: MIN_SUPPORT=2 means a second sentence with
        # the same pattern but DIFFERENT entities is independent evidence
        # the lexicon needs (one query per sentence max), and the seeker's
        # train is deferred to the end of the run, so interpret() cannot
        # yet handle it. A blanket per-pattern skip would contradict the
        # minimality audit (one query per sentence).
        queried_keys: set = set()
        acquired: List[Tuple[str, RelFact]] = []

        committed_before = self._committed_snapshot(lexicon)

        for sent in sentences:
            # 1. Pure lexicon attempt (no probes). Handles it -> no gap,
            #    and the oracle is never consulted (no wasted queries).
            if lexicon.interpret(sent, candidate_entities) is not None:
                result.action_log.append({
                    "stage": "known_no_query",
                    "sentence": sent,
                })
                continue

            # 2. REPRESENT the gap as data.
            slot_pat, surface = lexicon.slot_pattern(sent, candidate_entities)
            gap = EvidenceGap(
                sentence=sent,
                slot_pattern=slot_pat,
                entities=tuple(surface),
                candidate_predicate=self._candidate_predicate(slot_pat),
            )
            result.gaps.append(gap)

            if len(surface) != 2 or not gap.candidate_predicate:
                # Not a binary relational pattern -- cannot determine a
                # role mapping from one question; stay open, no query.
                result.open_gaps.append(gap)
                result.action_log.append({
                    "stage": "gap_open",
                    "sentence": sent,
                    "slot_pattern": slot_pat,
                    "entities": tuple(surface),
                    "reason": "non_binary_or_no_predicate",
                })
                continue

            key = (slot_pat, tuple(surface))
            if key in queried_keys:
                # Same sentence shape already queried this run -- the
                # answer and pair are already in hand; re-querying the
                # oracle would add zero information.
                result.action_log.append({
                    "stage": "already_queried_no_requery",
                    "sentence": sent,
                    "slot_pattern": slot_pat,
                    "entities": tuple(surface),
                })
                continue
            queried_keys.add(key)

            # 3. CHOOSE the evidence action: one ask_role("agent") query.
            #    The agent determines the patient by elimination, so this
            #    single question is the minimal sufficient evidence.
            answer = oracle.ask_role(
                gap.candidate_predicate, "agent", gap.entities)
            result.queries_made += 1
            entry: Dict[str, object] = {
                "stage": "gap_represented",
                "sentence": sent,
                "slot_pattern": slot_pat,
                "entities": tuple(surface),
                "query": ("ask_role", gap.candidate_predicate,
                          "agent", tuple(surface)),
                "answer": answer,
            }
            result.action_log.append(entry)

            # 4. EXECUTE result: no answer -> gap stays OPEN, no training.
            other = [e for e in surface if e.lower() != (answer or "").lower()]
            if answer is None or not other:
                result.open_gaps.append(gap)
                entry["stage"] = "gap_open"
                entry["reason"] = "oracle_no_fact"
                continue

            # 5. LEARN: build probes from the answer, run the standard
            #    discover verification gauntlet. Failure -> gap stays OPEN.
            probes = [("agent", answer), ("patient", other[0])]
            fact = lexicon.discover(
                sent, candidate_entities, [gap.candidate_predicate], probes)
            if fact is None:
                result.open_gaps.append(gap)
                entry["stage"] = "gap_open"
                entry["reason"] = "discovery_failed"
                entry["probes"] = probes
                continue

            entry["stage"] = "pair_verified"
            entry["probes"] = probes
            entry["fact"] = fact
            acquired.append((sent, fact))
            result.pairs_acquired.append((sent, fact))

        # 6. Train ONCE on all verified pairs.
        result.train_stats = lexicon.train(acquired, candidate_entities)
        result.patterns_committed = self._newly_committed(
            lexicon, committed_before)

        # Final audit stages: mark which gaps got committed.
        committed_preds = {e["predicate"] for e in result.patterns_committed}
        for entry in result.action_log:
            if entry.get("stage") == "pair_verified":
                fact = entry["fact"]
                entry["stage"] = ("pattern_committed"
                                  if fact.predicate in committed_preds
                                  else "pair_not_committed")

        return result

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _committed_snapshot(lexicon: RelationalLexicon
                            ) -> Dict[Tuple[str, ...], Tuple[str, Dict[int, int]]]:
        return {tuple(e["pattern"]): (e["predicate"], dict(e["role_map"]))
                for e in lexicon.entries()}

    @classmethod
    def _newly_committed(cls, lexicon: RelationalLexicon,
                         before: Dict[Tuple[str, ...],
                                       Tuple[str, Dict[int, int]]]
                         ) -> List[Dict[str, object]]:
        after = cls._committed_snapshot(lexicon)
        out = []
        for pat, (pred, role_map) in sorted(after.items()):
            if before.get(pat) != (pred, role_map):
                out.append({"pattern": pat, "predicate": pred,
                            "role_map": role_map})
        return out
