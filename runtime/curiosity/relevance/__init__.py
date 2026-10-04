"""Curiosity side's relevance input (CUR-P5C).

Presents curiosity findings to Primary Acceptance's relevance question
honestly: content verbatim, provenance verbatim (triage advisory only),
no score fabrication -- the gate judges, the curiosity side presents.

Charter C-3.2: Primary Acceptance is the final relevance authority.
Relevance is the first question acceptance asks; the acceptance bar is
the second. This package is the curiosity side's half of that fence:
the honest input. It never judges, never scores, never admits.
"""

from .present import (
    RelevanceInput,
    build_gate_finding,
    present_for_acceptance,
)

__all__ = [
    "RelevanceInput",
    "build_gate_finding",
    "present_for_acceptance",
]
