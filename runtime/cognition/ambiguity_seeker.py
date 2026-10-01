"""
swarm_engine/cognition/ambiguity_seeker.py

Ambiguity-gap data structures (DEAD-CODE-1, 2026-10-01).

The AmbiguitySeeker agency loop (REPRESENT → GENERATE → SELECT → EXECUTE →
LEARN) was removed: it had no callers and the loop never ran in production.
What remains are the two dataclasses it would have operated on —
AmbiguityGap (an AMBIGUOUS predicate's rival programs as data) and
AmbiguityQuery (a discriminating query with per-candidate predictions).

NOTE: AmbiguityGap and AmbiguityQuery currently have no consumers either;
they are retained as data-shape documentation only. A follow-up may remove
them or wire the agency loop they were designed for.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass
class AmbiguityGap:
    """A predicate whose evidence admits rival programs."""
    predicate: str
    evidence: List[Dict[str, Any]]          # retained evidence dicts
    candidates: List[Any]                   # competing Expr programs
    candidate_names: List[str]               # canonical forms
    schema: Tuple[str, ...]                 # input schema
    n_evidence: int = 0


@dataclass
class AmbiguityQuery:
    """A discriminating query: entity pair + predicted outputs."""
    entity_pair: Tuple[str, str]
    inputs: Dict[str, Any]                  # role-keyed inputs
    predictions: List[Any]                  # per-candidate predicted output
    information_gain: float = 0.0


