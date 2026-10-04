"""First-pass triage (charter C-3.3/C-3.4).

Advisory only: assigns RETAIN / PROPOSE_CAPABILITY / PROPOSE_INVESTIGATION
/ BOUNDARY to terminal findings by mechanical, reproducible rules. Triage
is not admission -- this package cannot admit, register, or promote.
"""

from .ledger import TriageEvent, TriageLedger
from .rules import (
    CAPABILITY_ELIGIBLE,
    CONVERGED_POSITIVE,
    INVESTIGATE_TERMINALS,
    TRIAGE_BOUNDARY,
    TRIAGE_FLAGS,
    TRIAGE_PROPOSE_CAPABILITY,
    TRIAGE_PROPOSE_INVESTIGATION,
    TRIAGE_RETAIN,
    TriageRefused,
    assign_triage,
)
from .triage import persist_triaged, retriage, triage_finding

__all__ = [
    "TRIAGE_FLAGS",
    "TRIAGE_RETAIN",
    "TRIAGE_PROPOSE_CAPABILITY",
    "TRIAGE_PROPOSE_INVESTIGATION",
    "TRIAGE_BOUNDARY",
    "CONVERGED_POSITIVE",
    "CAPABILITY_ELIGIBLE",
    "INVESTIGATE_TERMINALS",
    "TriageEvent",
    "TriageLedger",
    "TriageRefused",
    "assign_triage",
    "triage_finding",
    "retriage",
    "persist_triaged",
]
