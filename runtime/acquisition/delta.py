"""
swarm_engine/acquisition/delta.py

M2 (technique distillation): the delta record.

The frozen delta schema (campaign file, James 2026-09-27 — the useful
experience record):
  X  objective             — what the external agent was trying to do
  Y  external_actions      — what the external agent actually did (observable)
  Z  prior_capability      — what REMOR could already do at the time
  Y-Z capability_gap      — the demonstrated capability gap
  T  technique             — the technique/procedure demonstrated
  E  evidence              — evidence of what was done (behavioral examples)
  D  dependencies          — dependencies of the technique
  V  verification          — how the demonstration was verified
  C  synthesized_capability— the resulting synthesized capability (filled in
                             by the distillation loop, None before)

Causal discipline (enforced in code, not a comment): a record is VALID only
where there is an actual observable capability gap with sufficient evidence
of what was done. DeltaRecord.validate() raises DeltaValidationError
otherwise — the transcript is not a magical training set, and not every
instruction represents a reproducible capability.

This module is M2-owned. M1's ingest.py constructs these records from the
frozen schema; distill.py consumes them. The field names below ARE the
frozen interface.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


class DeltaValidationError(ValueError):
    """Raised when a delta record fails the causal-discipline check."""


# Minimum behavioral examples for a record to be distillable. Fewer than
# this and there is nothing to split into build vs held-out evidence.
MIN_EVIDENCE_EXAMPLES = 4
MIN_HELDOUT_EXAMPLES = 2


def _delta_id(objective: str, technique: str, at: float) -> str:
    blob = f"{objective}\x00{technique}\x00{at:.3f}"
    return "dlt_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class DeltaRecord:
    """One useful experience record: the Y-Z delta with its evidence."""

    objective: str                                  # X
    external_actions: str                           # Y
    prior_capability: str                           # Z
    capability_gap: str                             # Y-Z
    technique: str                                  # T
    evidence: List[Dict[str, Any]]                   # E: [{input:{...}, output:...}]
    dependencies: List[str] = field(default_factory=list)   # D
    verification: Dict[str, Any] = field(default_factory=dict)  # V
    synthesized_capability: Optional[Dict[str, Any]] = None     # C
    delta_id: str = ""
    source: str = "external-ingestion"
    at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.delta_id:
            self.delta_id = _delta_id(self.objective, self.technique,
                                      self.at)

    # ------------------------------------------------------------------
    # Causal discipline
    # ------------------------------------------------------------------
    def validate(self) -> "DeltaRecord":
        """Enforce the causal discipline. Raises DeltaValidationError."""
        if not (self.objective or "").strip():
            raise DeltaValidationError(
                "delta has no objective (X): nothing was attempted")
        if not (self.external_actions or "").strip():
            raise DeltaValidationError(
                "delta has no observable external actions (Y): "
                "an instruction without demonstrated technique is not recorded")
        if not (self.technique or "").strip():
            raise DeltaValidationError(
                "delta names no technique (T): without a demonstrated "
                "procedure there is nothing to distill")
        if not (self.capability_gap or "").strip():
            raise DeltaValidationError(
                "delta states no capability gap (Y-Z): a record exists only "
                "where the external agent demonstrably did something REMOR "
                "could not")
        ev = self.evidence or []
        if len(ev) < MIN_EVIDENCE_EXAMPLES:
            raise DeltaValidationError(
                f"delta has {len(ev)} evidence examples, need at least "
                f"{MIN_EVIDENCE_EXAMPLES}: insufficient evidence of what "
                "was done")
        for i, ex in enumerate(ev):
            if not isinstance(ex, dict) or "input" not in ex or "output" not in ex:
                raise DeltaValidationError(
                    f"evidence[{i}] is not a concrete input/output example: "
                    "evidence must show the technique actually working")
            if not isinstance(ex["input"], dict):
                raise DeltaValidationError(
                    f"evidence[{i}].input must be a dict of named arguments")
        return self

    # ------------------------------------------------------------------
    # Evidence handling
    # ------------------------------------------------------------------
    def evidence_examples(self) -> List[Tuple[Dict[str, Any], Any]]:
        """Normalize E into [(input_dict, expected_output), ...]."""
        return [(dict(ex["input"]), ex["output"]) for ex in (self.evidence or [])]

    def split_evidence(self) -> Tuple[List[Tuple[Dict[str, Any], Any]],
                                      List[Tuple[Dict[str, Any], Any]]]:
        """Split E into (build, held-out).

        The held-out examples are NEVER used to build the technique — they
        are the causal check that the distilled capability generalizes
        rather than memorizing. Raises if there are not enough examples
        to split honestly.
        """
        examples = self.evidence_examples()
        if len(examples) < MIN_EVIDENCE_EXAMPLES:
            raise DeltaValidationError(
                f"cannot split {len(examples)} examples into build/held-out")
        n_held = max(MIN_HELDOUT_EXAMPLES, len(examples) // 3)
        return examples[:-n_held], examples[-n_held:]

    # ------------------------------------------------------------------
    # Persistence shape (the frozen interface)
    # ------------------------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Keep the frozen field vocabulary explicit.
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeltaRecord":
        """Build from the frozen schema shape (what M1's ingest writes)."""
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def mark_synthesized(self, synthesized: Dict[str, Any]) -> "DeltaRecord":
        """Fill in C after a successful distillation. Returns self."""
        self.synthesized_capability = dict(synthesized)
        return self
