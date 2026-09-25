"""
swarm_engine/cognition/grounding_lexicon.py

Learned semantic -> executable grounding.

The precise missing capability this closes: a mechanism that autonomously
derives executable behavioral evidence (worked input -> output examples, and
the executable program discovered from them) for a VERIFIED semantic
sub-task (a RelFact: predicate + arg0/arg1 role-fillers), with NO
developer-authored semantic -> executable mapping.

How it works (all predicate handling is token-opaque):
  1. learn(pairs): group GroundingPairs by fact.predicate -- the predicate
     string is never inspected, branched on, or looked up; it is only a
     grouping key. Per group, run the EXISTING GeneralSynthesizer over the
     worked (role-keyed input dict, world-observed output) examples to
     discover an executable program. Role-keyed inputs (a0_mass, a1_charge,
     ...) mean the program is learned over ROLES, never entity names.
  2. ground(fact, world): for a known predicate, observe both role-fillers
     through the world, bind observations to the learned input schema via
     the generic a0_/a1_ role convention, and run the program. Unknown
     predicate, unlearned predicate, conflicted predicate, corrupt entry,
     or missing observation -> None (fail closed).
  3. make_objective(): derive an acquisition objective -- a
     CapabilityRequirement with the clause text as description, the learned
     schema as param_names, and the worked examples -- consumable by
     CapabilitySpec.from_requirement (the orchestrator's own consumption
     point). Full acquisition integration is the next worker's job; this
     module stops at producing the objective.
  4. Conflict: new evidence for a learned predicate re-runs synthesis on
     the UNION of old + new evidence. If no single program fits, the entry
     is marked CONFLICTED (both evidence sets retained, old program kept),
     the governed capability transitions to CONTRADICTED, and ground()
     fails closed. Never silently chooses.
  5. Governance/persistence: learned groundings go through the existing
     governed path -- SemanticAdmissionController.submit_hypothesis ->
     validate (behavioral evidence) -> admit -- backed by
     SemanticCapabilityStore. Entries persist in sqlite; fresh-process
     rehydration re-validates the program against retained evidence AND
     the governed capability's admitted state; any mismatch -> CORRUPT,
     fail closed.

Anti-simulation: this module never names a predicate, verb, entity, or
law. The predicate token is opaque throughout; the only domain knowledge
declared is GROUND_ARITH_OPS -- the generic pure-numeric vocabulary the
synthesizer may compose (the declared language of this world's physics,
analogous to the synthesizer's existing pure-op restriction, not
task-specific knowledge). There is no RelFact -> primitive table, no
predicate-name dispatch, no fixture-name dispatch.

The world the evidence comes from lives in the harness
(work/grounding_world.py); this module never imports it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from swarm_engine.acquisition.pipeline import (
    CapabilityRequirement as AcquisitionRequirement,
)
from swarm_engine.acquisition.semantic_capability import (
    SemanticAdmissionController,
    SemanticCapability,
    SemanticCapabilityStore,
    SemanticEvidence,
    SemanticState,
)
from swarm_engine.cognition.relational_lexicon import RelFact
from swarm_engine.cognition.representations import (
    Expr,
    SearchBias,
    evaluate_expr,
)
from swarm_engine.cognition.semantic_interpreter import FilteredRegistryView
from swarm_engine.cognition.synthesis import GeneralSynthesizer
from swarm_engine.primitives import build_registry

# The declared language of the world's physics: generic pure numeric
# primitives the synthesizer may compose into grounding programs. This is
# domain vocabulary (numbers in -> number out), never predicate knowledge.
GROUND_ARITH_OPS = frozenset({
    "add", "subtract", "multiply", "divide",
    "negate", "abs", "max", "min",
})

STATUS_LEARNED = "learned"
STATUS_CONFLICTED = "conflicted"
STATUS_UNLEARNED = "unlearned"
STATUS_CORRUPT = "corrupt"
STATUS_AMBIGUOUS = "ambiguous"
_KNOWN_STATUSES = {STATUS_LEARNED, STATUS_CONFLICTED, STATUS_UNLEARNED,
                   STATUS_CORRUPT, STATUS_AMBIGUOUS}

_ROLE_KEY = re.compile(r"^(a[01])_(.+)$")


@dataclass
class GroundingPair:
    """One piece of training evidence: a VERIFIED RelFact (produced by the
    relational lexicon, never hand-constructed), the role-keyed input dict
    built from world observations, and the world-observed outcome."""
    fact: RelFact
    inputs: Dict[str, Any]
    output: Any
    clause_text: str = ""


@dataclass
class GroundingEntry:
    """A learned grounding for one predicate token."""
    predicate: str
    expr: Optional[Expr]
    input_schema: Tuple[str, ...]
    support: int
    status: str
    semantic_id: Optional[str] = None
    # retained evidence: {"inputs","output","clause_text","consistent"}
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def usable(self) -> bool:
        return (self.status == STATUS_LEARNED and self.expr is not None
                and self.semantic_id is not None)


@dataclass
class GroundResult:
    """Outcome of grounding one verified RelFact against the world."""
    predicate: str
    predicted: Any
    inputs: Dict[str, Any]
    input_schema: Tuple[str, ...]
    semantic_id: str


class GroundingLexicon:
    """Learns predicate -> executable program mappings from evidence.

    Predicate tokens are opaque grouping keys throughout. The module never
    imports the world; ground() receives the world as an argument (the
    sensor interface), exactly as production use would supply it.
    """

    def __init__(self, db_path: str, capability_db_path: str,
                 max_size: int = 3,
                 search_timeout_s: float = 60.0) -> None:
        self.db_path = db_path
        self.capability_db_path = capability_db_path
        self.max_size = max_size
        self.search_timeout_s = search_timeout_s
        base = build_registry()
        self._registry = base
        self._view = FilteredRegistryView(base, GROUND_ARITH_OPS)
        self._cap_store = SemanticCapabilityStore(capability_db_path)
        self._governor = SemanticAdmissionController(self._cap_store)
        self._entries: Dict[str, GroundingEntry] = {}
        self._init_db()
        self._load_entries()

    # -- persistence -------------------------------------------------
    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS grounding_entries (
                predicate TEXT PRIMARY KEY,
                expr_json TEXT, input_schema_json TEXT NOT NULL,
                support INTEGER NOT NULL, status TEXT NOT NULL,
                semantic_id TEXT, evidence_json TEXT NOT NULL,
                updated_at REAL NOT NULL)""")

    def _persist_entry(self, entry: GroundingEntry) -> None:
        entry.updated_at = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO grounding_entries
                   (predicate, expr_json, input_schema_json, support, status,
                    semantic_id, evidence_json, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (entry.predicate,
                 json.dumps(entry.expr.as_dict()) if entry.expr else None,
                 json.dumps(list(entry.input_schema)),
                 entry.support, entry.status, entry.semantic_id,
                 json.dumps(entry.evidence), entry.updated_at))

    def _load_entries(self) -> None:
        """Rehydrate from sqlite. Fail closed: any entry that does not
        re-validate (unparseable program, schema mismatch, program no
        longer reproduces retained evidence, governed capability missing /
        not admitted / program mismatch) becomes CORRUPT and unusable."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT predicate, expr_json, input_schema_json, support,"
                " status, semantic_id, evidence_json FROM grounding_entries"
            ).fetchall()
        for predicate, expr_json, schema_json, support, status, sid, ev_json in rows:
            entry = self._revalidate_row(
                predicate, expr_json, schema_json, support, status, sid,
                ev_json)
            self._entries[predicate] = entry

    def _revalidate_row(self, predicate, expr_json, schema_json, support,
                        status, sid, ev_json) -> GroundingEntry:
        def corrupt(reason: str) -> GroundingEntry:
            return GroundingEntry(
                predicate=predicate, expr=None, input_schema=(),
                support=int(support or 0), status=STATUS_CORRUPT,
                semantic_id=sid, evidence=[])

        try:
            schema = tuple(json.loads(schema_json))
            evidence = json.loads(ev_json)
        except (json.JSONDecodeError, TypeError):
            return corrupt("unparseable row")
        if status not in _KNOWN_STATUSES:
            return corrupt("unknown status")
        if not all(isinstance(k, str) and _ROLE_KEY.match(k) for k in schema):
            return corrupt("bad schema")
        if not isinstance(evidence, list):
            return corrupt("bad evidence")

        expr: Optional[Expr] = None
        if expr_json:
            try:
                expr = Expr.from_dict(json.loads(expr_json))
            except (json.JSONDecodeError, KeyError, TypeError,
                    AttributeError):
                return corrupt("unparseable program")
            if not isinstance(expr, Expr):
                return corrupt("program not an Expr")

        if status == STATUS_UNLEARNED:
            return GroundingEntry(predicate, None, schema, int(support or 0),
                                  status, None, evidence)

        if expr is None:
            return corrupt("missing program")

        # The retained evidence that was consistent must still reproduce.
        try:
            for ev in evidence:
                if not ev.get("consistent", True):
                    continue
                if evaluate_expr(expr, ev["inputs"], self._view) != ev["output"]:
                    return corrupt("program/evidence mismatch")
        except Exception:
            return corrupt("program evaluation failed")

        # The governed capability must still admit exactly this program.
        cap = self._cap_store.get(sid) if sid else None
        if cap is None:
            return corrupt("governed capability missing")
        if status == STATUS_LEARNED:
            if cap.state != SemanticState.ADMITTED:
                return corrupt("capability not admitted")
        elif status == STATUS_CONFLICTED:
            if cap.state != SemanticState.CONTRADICTED:
                return corrupt("capability not contradicted")
        prog = (cap.interpretation or {}).get("program")
        if not prog or Expr.from_dict(prog).canonical() != expr.canonical():
            return corrupt("capability program mismatch")

        return GroundingEntry(predicate, expr, schema, int(support or 0),
                              status, sid, evidence)

    # -- learning ------------------------------------------------------
    def learn(self, pairs: Sequence[GroundingPair]) -> Dict[str, Dict[str, Any]]:
        """Learn grounding programs from verified evidence pairs.

        Groups by fact.predicate (opaque token). Per predicate, unions the
        new pairs with retained evidence and re-runs synthesis on the
        union:
          - program found and verifies on the union -> learned (new) or
            refined (known): governed, persisted.
          - no single program fits the union -> CONFLICTED: old program +
            both evidence sets retained, governed capability contradicted,
            ground() fails closed. Never silently chooses.
        Returns a per-predicate report.
        """
        grouped: Dict[str, List[GroundingPair]] = {}
        for p in pairs:
            if p.fact is None or not p.fact.predicate:
                continue
            grouped.setdefault(p.fact.predicate, []).append(p)
        report: Dict[str, Dict[str, Any]] = {}
        for predicate, new_pairs in grouped.items():
            report[predicate] = self._learn_predicate(predicate, new_pairs)
        return report

    def _learn_predicate(self, predicate: str,
                         new_pairs: List[GroundingPair]) -> Dict[str, Any]:
        old = self._entries.get(predicate)
        # Superseded evidence is audit history, not training data: a
        # conflict that was resolved by a newer evidence regime must not
        # resurrect itself inside a later union.
        retained: List[Dict[str, Any]] = (
            [ev for ev in old.evidence if not ev.get("superseded")]
            if old else [])
        # Evidence attribution: a pair whose own fact names a different
        # predicate is not evidence about THIS predicate (mislabeled
        # input, wrong sensor stream). Learning it here would let one
        # predicate's observations contradict -- and invalidate -- an
        # unrelated predicate's governed capability. Drop misattributed
        # pairs; if none remain, the entry is untouched.
        attributed = [p for p in new_pairs
                      if p.fact is not None and p.fact.predicate == predicate]
        if new_pairs and not attributed:
            return {"status": old.status if old else STATUS_UNLEARNED,
                    "reason": "no evidence attributed to predicate; "
                              "misattributed pairs ignored"}
        fresh = [{"inputs": dict(p.inputs), "output": p.output,
                  "clause_text": p.clause_text, "consistent": True}
                 for p in attributed]
        union = retained + fresh
        if not union:
            return {"status": STATUS_UNLEARNED, "reason": "no evidence"}

        schema = tuple(sorted(union[0]["inputs"].keys()))
        if any(tuple(sorted(ev["inputs"].keys())) != schema for ev in union):
            # Schema drift: the evidence no longer describes one measurable
            # setup; cannot be one program over one schema.
            return self._mark_conflict(
                predicate, old, union, schema,
                reason="input schema mismatch across evidence")

        # Honesty threshold: with a single example, no program can
        # be confidently identified (any structure can fit). Run
        # synthesis to obtain candidate programs for the ambiguity
        # seeker, but mark AMBIGUOUS (not LEARNED) -- the seeker will
        # gather disambiguating evidence.
        _sparse = len(union) < 2

        examples = [(ev["inputs"], ev["output"]) for ev in union]
        expr, ambiguous = self._synthesize(examples, schema)
        if _sparse:
            # Sparse evidence: never admit, even if synthesis found a
            # program. Construct ambiguity records from the found
            # program (as a candidate) so the seeker has something to
            # distinguish. If no program was found, use empty records
            # (seeker will fail closed).
            sparse_ambiguous = list(ambiguous) if ambiguous else []
            if expr is not None and not sparse_ambiguous:
                # No rivals found, but evidence is too sparse for
                # confidence. Create a synthetic record with the found
                # program as a candidate; the seeker will query to
                # validate it.
                sparse_ambiguous = [{
                    "kind": "sparse_evidence",
                    "candidate": expr.canonical(),
                    "candidate_expr": expr,
                    "reason": "single example: program not "
                              "identified by evidence",
                }]
            return self._mark_ambiguous(
                predicate, old, union, schema, sparse_ambiguous, examples)
        if expr is None:
            # Distinguish genuine ambiguity (rival programs fit but
            # disagree) from mere search failure: ambiguity triggers
            # autonomous evidence seeking; true failure marks conflict.
            if ambiguous:
                return self._mark_ambiguous(predicate, old, union, schema,
                                            ambiguous, examples)
            return self._mark_conflict(
                predicate, old, union, schema,
                reason="no single program fits the union of evidence")

        if not self._verifies(expr, examples):
            return self._mark_conflict(
                predicate, old, union, schema,
                reason="synthesized program failed re-verification")

        # Underdetermination check: a program with at least as many
        # numeric literals ("magic numbers") as training examples can
        # memorize rather than generalize. Such a program is not
        # identified by the evidence, even if no explicit rival was
        # found -- mark AMBIGUOUS and seek disambiguating evidence
        # rather than admitting a likely-spurious fit.
        n_literals = self._count_literals(expr)
        if n_literals >= len(examples) and n_literals > 0:
            # Synthesize ambiguity records from literal perturbation:
            # the "rivals" are the program with perturbed literals.
            # For now, mark ambiguous with a synthetic record.
            synth_ambiguous = [{
                "kind": "underdetermined",
                "candidate": expr.canonical(),
                "candidate_expr": expr,
                "reason": "program has %d literals for %d examples; "
                          "underdetermined" % (n_literals, len(examples)),
                "rival": None,
                "rival_expr": None,
                "distinguishing_probe": None,
                "candidate_output": None,
                "rival_output": None,
            }]
            return self._mark_ambiguous(predicate, old, union, schema,
                                        synth_ambiguous, examples)

        for ev in union:
            ev["consistent"] = True
        if old is not None and old.status == STATUS_LEARNED and old.expr is not None:
            if old.expr.canonical() == expr.canonical():
                old.support = len(union)
                old.evidence = union
                self._persist_entry(old)
                return {"status": STATUS_LEARNED, "support": old.support,
                        "semantic_id": old.semantic_id, "refined": False,
                        "program": expr.canonical()}
            return self._govern_refinement(predicate, old, expr, schema, union)
        return self._govern_new(predicate, expr, schema, union)

    def _synthesize(self, examples: Sequence[Tuple[Dict[str, Any], Any]],
                    schema: Sequence[str]
                    ) -> Tuple[Optional[Expr], List[Dict[str, Any]]]:
        """Discover an executable program from worked examples via the
        existing GeneralSynthesizer. Fresh SearchBias per call so language
        or cross-predicate evidence never leaks into the search.

        Returns (expr, ambiguous): the synthesized program (or None) and
        the list of ambiguity records from the search trace -- candidates
        that fit the evidence but were suppressed because rival
        explanations disagree on discriminating probes. A non-empty
        ambiguous list with expr=None means the evidence is genuinely
        ambiguous (not merely hard); the caller should seek
        disambiguating evidence rather than fail closed silently.
        """
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            bias = SearchBias(db_path=tmp.name)
            # pool_per_type / max_arg_combinations / max_candidates are
            # COMPLETENESS parameters, raised above the class defaults
            # because the defaults structurally exclude needed bank entries
            # at level 2 (a needed mul() sat beyond the default pool cut on
            # a cold search -- found directly, not tuned to any predicate).
            # Raising them makes the search more complete, never more
            # task-specific: no predicate, entity, or law knowledge enters.
            synth = GeneralSynthesizer(
                self._view, bias, max_size=self.max_size,
                pool_per_type=400, max_arg_combinations=12000,
                max_candidates=200000,
                wall_clock_limit_s=self.search_timeout_s)
            hyp, trace = synth.search(list(examples), list(schema))
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        ambiguous = list(getattr(trace, "ambiguous", []) or [])
        if hyp is None or hyp.expr is None:
            return None, ambiguous
        return hyp.expr, ambiguous

    def _verifies(self, expr: Expr,
                  examples: Sequence[Tuple[Dict[str, Any], Any]]) -> bool:
        """Independent re-verification: the program must reproduce every
        retained worked example exactly (defense in depth; search already
        guarantees this, but admission must not trust it blindly)."""
        try:
            return all(evaluate_expr(expr, inputs, self._view) == out
                       for inputs, out in examples)
        except Exception:
            return False

    def _count_literals(self, expr: Expr) -> int:
        """Count numeric literal occurrences in a program."""
        count = 0
        def walk(e: Expr) -> None:
            nonlocal count
            if e.is_leaf():
                if e.is_literal and isinstance(e.literal, (int, float)) \
                        and not isinstance(e.literal, bool):
                    count += 1
                return
            for _, child in e.children:
                walk(child)
        walk(expr)
        return count

    # -- governance ------------------------------------------------------
    def _description(self, predicate: str) -> str:
        return f"executable grounding of relational predicate '{predicate}'"

    def _interpretation(self, predicate: str, expr: Expr,
                        schema: Sequence[str], support: int) -> Dict[str, Any]:
        return {"kind": "predicate_grounding",
                "predicate": predicate,
                "program": expr.as_dict(),
                "program_canonical": expr.canonical(),
                "input_schema": list(schema),
                "support": support}

    def _evidence(self, union: List[Dict[str, Any]]) -> List[SemanticEvidence]:
        return [SemanticEvidence(
            kind="behavioral",
            source="grounding_lexicon.learn",
            payload={"n_evidence_pairs": len(union),
                     "train_reproduction": "all_match",
                     "clause_texts": [ev.get("clause_text", "")
                                      for ev in union]},
            supports=True,
            strength=0.8)]

    def _govern_new(self, predicate: str, expr: Expr,
                    schema: Tuple[str, ...],
                    union: List[Dict[str, Any]]) -> Dict[str, Any]:
        cap = self._governor.submit_hypothesis(
            self._description(predicate),
            self._interpretation(predicate, expr, schema, len(union)),
            evidence=self._evidence(union),
            provenance={"module": "grounding_lexicon.v1",
                        "predicate": predicate})
        examples = [(ev["inputs"], ev["output"]) for ev in union]
        vrep = self._governor.validate(cap.semantic_id,
                                       behavioral_cases=examples)
        if not vrep.get("ok"):
            self._persist_unlearned(predicate, schema, union)
            return {"status": STATUS_UNLEARNED,
                    "reason": f"governance validation failed: {vrep.get('reasons')}"}
        arep = self._governor.admit(cap.semantic_id)
        if not arep.get("ok"):
            self._persist_unlearned(predicate, schema, union)
            return {"status": STATUS_UNLEARNED,
                    "reason": f"governance admission failed: {arep.get('reasons')}"}
        entry = GroundingEntry(predicate, expr, schema, len(union),
                               STATUS_LEARNED, cap.semantic_id, union)
        self._entries[predicate] = entry
        self._persist_entry(entry)
        return {"status": STATUS_LEARNED, "support": len(union),
                "semantic_id": cap.semantic_id, "refined": False,
                "program": expr.canonical()}

    def _govern_refinement(self, predicate: str, old: GroundingEntry,
                           expr: Expr, schema: Tuple[str, ...],
                           union: List[Dict[str, Any]]) -> Dict[str, Any]:
        """New evidence is consistent with a single program but it differs
        from the previously learned one: supersede the old governed
        capability (version chain, not silent replacement)."""
        prev = self._cap_store.get(old.semantic_id) if old.semantic_id else None
        if prev is not None:
            prev.state = SemanticState.SUPERSEDED
            self._cap_store.store(prev)
            parent_id, version = prev.semantic_id, prev.version + 1
        else:
            parent_id, version = old.semantic_id, 1
        cap = self._governor.submit_hypothesis(
            self._description(predicate),
            self._interpretation(predicate, expr, schema, len(union)),
            evidence=self._evidence(union),
            provenance={"module": "grounding_lexicon.v1",
                        "predicate": predicate, "refinement": True})
        cap.parent_id = parent_id
        cap.version = version
        self._cap_store.store(cap)
        examples = [(ev["inputs"], ev["output"]) for ev in union]
        vrep = self._governor.validate(cap.semantic_id,
                                       behavioral_cases=examples)
        arep = self._governor.admit(cap.semantic_id) if vrep.get("ok") else {"ok": False}
        if not (vrep.get("ok") and arep.get("ok")):
            return self._mark_conflict(
                predicate, old, union, schema,
                reason="refinement failed governance")
        entry = GroundingEntry(predicate, expr, schema, len(union),
                               STATUS_LEARNED, cap.semantic_id, union)
        self._entries[predicate] = entry
        self._persist_entry(entry)
        return {"status": STATUS_LEARNED, "support": len(union),
                "semantic_id": cap.semantic_id, "refined": True,
                "program": expr.canonical()}

    def _mark_conflict(self, predicate: str, old: Optional[GroundingEntry],
                       union: List[Dict[str, Any]], schema: Tuple[str, ...],
                       reason: str) -> Dict[str, Any]:
        """No single program fits the union of evidence: mark CONFLICTED,
        retain everything, contradict the governed capability, fail closed
        on use. Never silently chooses between the conflicting claims."""
        for ev in union:
            ev.setdefault("consistent", True)
        expr = old.expr if old is not None else None
        if old is not None and old.expr is not None:
            # Flag which retained evidence the old program still explains.
            for ev in union:
                try:
                    ev["consistent"] = (
                        evaluate_expr(old.expr, ev["inputs"], self._view)
                        == ev["output"])
                except Exception:
                    ev["consistent"] = False
        sid = old.semantic_id if old is not None else None
        if sid:
            self._governor.add_evidence(
                sid,
                SemanticEvidence(
                    kind="behavioral",
                    source="grounding_lexicon.conflict",
                    payload={"reason": reason,
                             "n_evidence_pairs": len(union)},
                    supports=False,
                    strength=0.9))
            # add_evidence() transitions ADMITTED -> CONTRADICTED by design.
        entry = GroundingEntry(predicate, expr, schema, len(union),
                               STATUS_CONFLICTED, sid, union)
        self._entries[predicate] = entry
        self._persist_entry(entry)
        return {"status": STATUS_CONFLICTED, "support": len(union),
                "semantic_id": sid, "reason": reason}

    def _mark_ambiguous(self, predicate: str, old: Optional[GroundingEntry],
                        union: List[Dict[str, Any]], schema: Tuple[str, ...],
                        ambiguous: List[Dict[str, Any]],
                        examples: Sequence[Tuple[Dict[str, Any], Any]]
                        ) -> Dict[str, Any]:
        """Rival programs fit the evidence but disagree on discriminating
        probes: mark AMBIGUOUS (not CONFLICTED -- the evidence is
        consistent, the interpretation is not). Retains the evidence,
        the candidate programs, and their distinguishing probes so an
        ambiguity seeker can generate information-gain queries. Never
        admits a program while ambiguity remains."""
        for ev in union:
            ev.setdefault("consistent", True)
        # Extract distinct candidate programs from ambiguity records.
        # Store the Expr objects (not just canonical strings) so the
        # seeker can evaluate them on novel inputs.
        candidates = []
        seen = set()
        for amb in ambiguous:
            for key, expr_key in (("candidate", "candidate_expr"),
                                  ("rival", "rival_expr")):
                prog = amb.get(key)
                expr_obj = amb.get(expr_key)
                if prog and prog not in seen:
                    seen.add(prog)
                    candidates.append({
                        "program": prog,
                        "expr": expr_obj,
                        "distinguishing_probe": amb.get("distinguishing_probe"),
                        "candidate_output": amb.get("candidate_output"),
                        "rival_output": amb.get("rival_output"),
                    })
        sid = old.semantic_id if old is not None else None
        entry = GroundingEntry(predicate, None, schema, len(union),
                               STATUS_AMBIGUOUS, sid, union)
        # Stash candidates on the entry for the seeker (not persisted;
        # re-derived on demand from the retained evidence).
        entry.ambiguity_candidates = candidates  # type: ignore[attr-defined]
        self._entries[predicate] = entry
        self._persist_entry(entry)
        return {"status": STATUS_AMBIGUOUS, "support": len(union),
                "semantic_id": sid,
                "n_candidates": len(candidates),
                "candidates": candidates,
                "reason": "rival programs fit evidence but disagree; "
                          "seeking disambiguating evidence"}

    def _persist_unlearned(self, predicate: str, schema: Tuple[str, ...],
                           union: List[Dict[str, Any]]) -> None:
        entry = GroundingEntry(predicate, None, schema, len(union),
                               STATUS_UNLEARNED, None, union)
        self._entries[predicate] = entry
        self._persist_entry(entry)

    # -- conflict resolution ------------------------------------------------
    def resolve_conflict(self, predicate: str,
                         new_pairs: Sequence[GroundingPair]
                         ) -> Dict[str, Any]:
        """Evidence-driven resolution of a CONFLICTED entry.

        Learns ONLY from `new_pairs` (the new evidence regime) -- the old
        regime's pairs are not re-admitted into the union, so a genuine
        law change does not immediately re-conflict with itself. The new
        pairs must identify a single program under the same honesty gates
        as ordinary learning (synthesis, re-verification, the
        underdetermination gate). Any failure leaves the entry CONFLICTED
        and touches nothing.

        On success the old program's governed capability moves
        CONTRADICTED -> SUPERSEDED (version chain, not silent replacement),
        the new program is governed and admitted as the next version, and
        the retired old-regime pairs are kept on the new entry flagged
        `superseded` (audit history; excluded from future training unions
        and from revalidation replay).
        """
        old = self._entries.get(predicate)
        if old is None:
            return {"ok": False, "status": STATUS_UNLEARNED,
                    "reason": "no lexicon entry for predicate"}
        if old.status != STATUS_CONFLICTED:
            return {"ok": False, "status": old.status,
                    "reason": ("entry is %s, not conflicted; resolution only "
                               "applies to conflicted entries") % old.status}
        fresh = [{"inputs": dict(p.inputs), "output": p.output,
                  "clause_text": p.clause_text, "consistent": True}
                 for p in new_pairs
                 if p.fact is not None and p.fact.predicate == predicate]
        if not fresh:
            return {"ok": False, "status": STATUS_CONFLICTED,
                    "reason": "no new evidence for predicate; conflict stands"}
        schema = tuple(sorted(fresh[0]["inputs"].keys()))
        if any(tuple(sorted(ev["inputs"].keys())) != schema for ev in fresh):
            return {"ok": False, "status": STATUS_CONFLICTED,
                    "reason": "input schema mismatch across new evidence; "
                              "conflict stands"}
        examples = [(ev["inputs"], ev["output"]) for ev in fresh]
        expr, ambiguous = self._synthesize(examples, schema)
        if expr is None or ambiguous:
            return {"ok": False, "status": STATUS_CONFLICTED,
                    "reason": ("new evidence does not identify a program; "
                               "conflict stands")}
        if not self._verifies(expr, examples):
            return {"ok": False, "status": STATUS_CONFLICTED,
                    "reason": ("synthesized program failed re-verification; "
                               "conflict stands")}
        n_literals = self._count_literals(expr)
        if n_literals >= len(examples) and n_literals > 0:
            return {"ok": False, "status": STATUS_CONFLICTED,
                    "reason": ("replacement program underdetermined "
                               "(memorization, not identification); "
                               "conflict stands")}
        result = self._govern_refinement(predicate, old, expr, schema, fresh)
        if result.get("status") != STATUS_LEARNED:
            return dict(result, ok=False)
        # Retire the old regime's raw pairs onto the new entry as flagged
        # history (support stays the count of backing new-regime pairs).
        retired = [dict(ev, superseded=True, consistent=False)
                   for ev in (old.evidence or [])]
        new_entry = self._entries[predicate]
        new_entry.evidence = list(new_entry.evidence) + retired
        self._persist_entry(new_entry)
        result["ok"] = True
        result["n_retired"] = len(retired)
        result["superseded_semantic_id"] = old.semantic_id
        return result

    # -- use ---------------------------------------------------------------
    def get(self, predicate: str) -> Optional[GroundingEntry]:
        return self._entries.get(predicate)

    def entries(self) -> List[GroundingEntry]:
        return [self._entries[k] for k in sorted(self._entries)]

    def known_predicates(self) -> List[str]:
        return sorted(self._entries)

    def ground(self, fact: RelFact, world: Any) -> Optional[GroundResult]:
        """Ground a verified RelFact: observe both role-fillers through the
        world, bind to the learned schema via the generic a0_/a1_ role
        convention, run the program. Fail closed on: unknown predicate,
        non-learned / conflicted / corrupt entry, unknown entity, missing
        property, evaluation error."""
        if fact is None or not fact.predicate:
            return None
        entry = self._entries.get(fact.predicate)
        if entry is None or not entry.usable():
            return None
        try:
            obs0 = world.observe(fact.arg0)
            obs1 = world.observe(fact.arg1)
        except Exception:
            return None
        if not obs0 or not obs1:
            return None
        inputs: Dict[str, Any] = {}
        for key in entry.input_schema:
            m = _ROLE_KEY.match(key)
            if not m:
                return None  # schema outside the role convention
            obs = obs0 if m.group(1) == "a0" else obs1
            prop = m.group(2)
            if prop not in obs:
                return None
            inputs[key] = obs[prop]
        try:
            predicted = evaluate_expr(entry.expr, inputs, self._view)
        except Exception:
            return None
        return GroundResult(predicate=fact.predicate, predicted=predicted,
                            inputs=inputs, input_schema=entry.input_schema,
                            semantic_id=entry.semantic_id)

    # -- acquisition objective ----------------------------------------------
    def make_objective(self, predicate: str,
                       clause_text: str = "") -> Optional[AcquisitionRequirement]:
        """Derive the acquisition objective for a learned predicate: a
        CapabilityRequirement whose description is the verified clause text,
        whose param_names come from the learned input schema, and whose
        worked examples are the retained world-observed evidence.

        This is the bridge the boundary characterization identified as
        missing: verified semantic sub-task -> executable behavioral
        evidence the orchestrator can consume via
        CapabilitySpec.from_requirement. Full acquisition integration
        (calling orchestrator.resolve) is the next worker's job.
        """
        entry = self._entries.get(predicate)
        if entry is None or not entry.usable():
            return None
        examples = [(dict(ev["inputs"]), ev["output"]) for ev in entry.evidence
                    if ev.get("consistent", True)]
        if not examples:
            return None
        return AcquisitionRequirement(
            name=f"ground:{predicate}",
            description=clause_text or self._description(predicate),
            examples=examples,
            param_names=tuple(entry.input_schema),
            origin={"grounding_predicate": predicate,
                    "semantic_id": entry.semantic_id,
                    "program": entry.expr.canonical(),
                    "support": entry.support,
                    "source": "grounding_lexicon.make_objective"})
