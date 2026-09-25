"""
swarm_engine/cognition/engine.py

CognitiveEngine.propose() — wires E, G, H together.

The load-bearing property of this module: it produces Hypothesis objects and
NOTHING ELSE. A hypothesis is a plan dict — the same shape every other plan
in the engine already is. This module never registers a capability, never
writes to the primitive registry, and never marks anything trusted. Every
successful hypothesis is handed to the caller's existing
Composer.analyze / AdmissionController.admit path unchanged; H only fires
after the caller reports back what admission decided.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from swarm_engine.cognition.learning import Learner
from swarm_engine.cognition.reasoning import ReasoningEngine
from swarm_engine.cognition.representations import (
    CaseMemory, ConceptGraph, ExhaustedSearchMemory, SearchBias,
)


@dataclass
class SolveResult:
    solved: bool
    plan: Optional[Dict[str, Any]] = None
    origin: Optional[str] = None
    derivation: str = ""
    stages_tried: List[str] = field(default_factory=list)
    candidates_tried: int = 0
    elapsed_ms: float = 0.0
    expressiveness: Optional[Dict[str, Any]] = None
    ambiguous: List[Dict[str, Any]] = field(default_factory=list)
    contradiction: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"solved": self.solved, "plan": self.plan, "origin": self.origin,
                "derivation": self.derivation, "stages_tried": self.stages_tried,
                "candidates_tried": self.candidates_tried,
                "elapsed_ms": round(self.elapsed_ms, 2),
                "expressiveness": self.expressiveness,
                "ambiguous": list(self.ambiguous),
                "contradiction": self.contradiction}


class CognitiveEngine:
    def __init__(self, registry, db_path: str = "swarm_engine.db",
                 max_size: int = 3, max_candidates: int = 60000):
        # max_candidates default kept in sync with GeneralSynthesizer's own
        # default. Found directly: this constructor had its own separate
        # hardcoded 20000, left over from before pool_per_type/
        # max_arg_combinations were raised — every CognitiveEngine caller
        # was silently using the stale, now-too-small budget regardless of
        # what GeneralSynthesizer's own default said, and a previously
        # passing case (add(abs(a), b)) started failing as a result.
        self.reg = registry
        self.bias = SearchBias(db_path=db_path)
        self.concepts = ConceptGraph(db_path=db_path)
        self.cases = CaseMemory(db_path=db_path)
        self.exhausted = ExhaustedSearchMemory(db_path=db_path)
        self.reasoning = ReasoningEngine(registry, self.bias, self.concepts, self.cases,
                                         max_size=max_size, max_candidates=max_candidates)
        self.learner = Learner(self.bias, self.concepts, self.cases)
        self._pending: Dict[str, Any] = {}

    def propose(self, goal: str, examples: Sequence[Tuple[Dict[str, Any], Any]],
               param_name: str) -> SolveResult:
        return self.propose_multi(goal, examples, (param_name,))

    def propose_multi(self, goal: str, examples: Sequence[Tuple[Dict[str, Any], Any]],
                      param_names: Sequence[str],
                      oracle=None) -> SolveResult:
        started = time.time()
        # Optional oracle for ambiguity resolution during synthesis
        if oracle is not None:
            self.reasoning._synthesis_oracle = oracle
        elif hasattr(self, "_synthesis_oracle"):
            self.reasoning._synthesis_oracle = self._synthesis_oracle

        # Failure-guided search: a goal whose exact search shape already
        # exhausted the candidate budget without success is not retried
        # identically.
        signature = self._exhaustion_signature(param_names, examples)
        if self.exhausted.was_exhausted(signature):
            elapsed = (time.time() - started) * 1000
            return SolveResult(solved=False, stages_tried=["exhausted-skip"],
                              candidates_tried=0, elapsed_ms=elapsed)

        result = self.reasoning.solve_multi(goal, examples, param_names)
        self._pending[goal] = (result.hypothesis, examples, param_names)

        elapsed = (time.time() - started) * 1000
        if result.hypothesis is None:
            expressiveness = None
            if "synthesis" in result.stages_tried:
                expressiveness = self.reasoning.expressiveness.diagnose(
                    examples, param_names,
                    unsatisfiable_args=getattr(result, "unsatisfiable_args", None))
                self.exhausted.record(
                    signature, result.candidates_tried,
                    param_names=param_names, examples=examples,
                    expressible_by_type=(expressiveness or {}).get("expressible_by_type"),
                    goal=goal)
            return SolveResult(solved=False, stages_tried=result.stages_tried,
                              candidates_tried=result.candidates_tried,
                              elapsed_ms=elapsed, expressiveness=expressiveness,
                              ambiguous=result.ambiguous,
                              contradiction=result.contradiction)
        return SolveResult(solved=True, plan=result.hypothesis.plan,
                           origin=result.hypothesis.origin,
                           derivation=result.hypothesis.derivation,
                           stages_tried=result.stages_tried,
                           candidates_tried=result.candidates_tried,
                           elapsed_ms=elapsed,
                           ambiguous=result.ambiguous,
                           contradiction=result.contradiction)

    def _exhaustion_signature(self, param_names: Sequence[str],
                              examples: Sequence[Tuple[Dict[str, Any], Any]]) -> str:
        """A signature identifying "this exact search", not "this goal" —
        keyed on the structural shape (param count, value types, and the
        literal example values) rather than the goal's wording, so a
        differently-worded goal over the SAME examples is still recognized
        as the same exhausted search, and the same wording over DIFFERENT
        examples is correctly treated as a new attempt.

        2026-09-14: the signature ALSO incorporates (a) the search-policy
        version and (b) the registry's operator-vocabulary fingerprint.
        An exhaustion record is evidence about the POLICY and VOCABULARY
        that were active when the search failed. The growth loop changes
        both routinely -- a repaired search policy, or a newly admitted
        capability, makes a previously-exhausted search genuinely
        different, and a stale record must not block the retry (observed
        directly: after the admission-pollution deferral was generalized,
        the plink goal that motivated the repair still failed, because the
        pre-repair exhaustion record fired the dedup skip before the new
        policy ever ran). Old records are naturally orphaned: they were
        hashed without these components and can never match a new
        signature. No migration needed.
        """
        import hashlib
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION
        try:
            opset = sorted(self.reg.names())
        except Exception:
            opset = []
        opset_fp = hashlib.sha256(repr(opset).encode()).hexdigest()[:16]
        payload = repr((SEARCH_POLICY_VERSION, opset_fp, tuple(param_names),
                        tuple((tuple(sorted(a.items())), b) for a, b in examples)))
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def record_admission_outcome(self, goal: str, succeeded: bool) -> Dict[str, Any]:
        pending = self._pending.pop(goal, None)
        if pending is None:
            return self.learner.record_outcome(goal, None, succeeded)
        hypothesis, examples, param_names = pending
        return self.learner.record_outcome(goal, hypothesis, succeeded,
                                           examples=examples, param_names=param_names)
