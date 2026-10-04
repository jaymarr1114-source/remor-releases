"""First-pass triage rules (charter C-3.3/C-3.4).

Every rule is a pure, mechanical function of the finding's content:
(terminal_state, provenance completeness, payload). The same finding
content ALWAYS yields the same triage -- a triage assignment that can't
be reproduced from the finding's content is a judgment wearing a
mechanism's clothes, and this package refuses to be one.

Rule priority (first match wins); each assignment cites its rule id:

  R-BOUNDARY     terminal_state == "BOUNDARY_ESTABLISHED"
                 -> "boundary"
                 (the finding maps a limit/obstruction -- boundary
                 information, never a capability proposal)
  R-INVESTIGATE  terminal_state in ("QUESTION_RESOLVED",
                 "INSUFFICIENT_EVIDENCE", "INCONCLUSIVE", "BLOCKED")
                 -> "propose_investigation"
                 (a resolved question is the seed of further inquiry --
                 theory pipeline Questioning -> Scientific Inquiry; an
                 unconverged inquiry is open work, not buried evidence)
  R-CAPABILITY   payload carries a well-formed capability_proposal block
                 AND terminal_state is capability-eligible
                 -> "propose_capability"
                 (the block must be explicit and well-formed; no vibes)
  R-RETAIN       everything else with a known terminal state
                 -> "retain"
                 (C-3.4: converged evidence with no capability proposal
                 is kept with its terminal state -- true-but-irrelevant
                 is retained, neither discarded nor promoted)

DELTA vs the as-built run-controller score rule (documented, not
papered over): for questioning terminals the controller applies
"propose_investigation iff score >= 3 else retain" (a Phase-2 legacy
heuristic). These rules AGREE on QUESTION_RESOLVED (both propose
investigation) and DIFFER on INSUFFICIENT_EVIDENCE (legacy: retain;
here: propose_investigation -- open work is not buried) and
BOUNDARY_ESTABLISHED (legacy: retain; here: boundary -- the flag
exists precisely for mapped limits). When the controller is
authorized to call this pass, the score rule is superseded; until
then this package stands alone and the controller is untouched.

Triage is advisory, never admission (C-3.3). This module assigns flags;
it cannot admit, register, or promote anything -- there is deliberately
no admit/promote/register API anywhere in this package.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Vocabulary (frozen shapes, mirrored -- never redefined here)
# ---------------------------------------------------------------------------

TRIAGE_RETAIN = "retain"
TRIAGE_PROPOSE_CAPABILITY = "propose_capability"
TRIAGE_PROPOSE_INVESTIGATION = "propose_investigation"
TRIAGE_BOUNDARY = "boundary"

TRIAGE_FLAGS: Tuple[str, ...] = (
    TRIAGE_RETAIN,
    TRIAGE_PROPOSE_CAPABILITY,
    TRIAGE_PROPOSE_INVESTIGATION,
    TRIAGE_BOUNDARY,
)

TERMINAL_BOUNDARY = "BOUNDARY_ESTABLISHED"

# Terminal states whose honest first-pass advisory is further
# investigation: a resolved question seeds the next inquiry; an
# unconverged inquiry is open work.
INVESTIGATE_TERMINALS = frozenset({
    "QUESTION_RESOLVED",
    "INSUFFICIENT_EVIDENCE",
    "INCONCLUSIVE",
    "BLOCKED",
})

# Terminal states from which a capability may be PROPOSED (never
# admitted) -- converged-positive work only. HYPOTHESIS_REFUTED is
# deliberately excluded: a refuted hypothesis is evidence (retained),
# never a capability candidate, however well-formed the block.
CAPABILITY_ELIGIBLE = frozenset({
    "HYPOTHESIS_SUPPORTED",
    "NOVELTY_CLASSIFIED",
    "CANDIDATE_GENERATED",
    "DISCOVERY_VERIFIED",
    "MODEL_REVISED",
})

# Backwards-compat alias (the set of converged-positive states).
CONVERGED_POSITIVE = CAPABILITY_ELIGIBLE


class TriageRefused(Exception):
    """The triage pass refuses the content: unknown terminal state, a
    finding that already carries a triage, or a re-triage attempted
    without new evidence. Named, never silent."""


# ---------------------------------------------------------------------------
# Capability-proposal block (mechanical schema)
# ---------------------------------------------------------------------------

def _well_formed_capability_proposal(payload: Optional[Dict[str, Any]]) -> bool:
    """A payload proposes a capability iff it carries a well-formed
    capability_proposal block: a non-empty name, a non-empty description,
    and a non-empty list of evidence refs. Every field is checked
    mechanically -- no semantic judgment, no vibes."""
    if not isinstance(payload, dict):
        return False
    block = payload.get("capability_proposal")
    if not isinstance(block, dict):
        return False
    name = block.get("name")
    description = block.get("description")
    evidence_refs = block.get("evidence_refs")
    return (
        isinstance(name, str) and bool(name.strip())
        and isinstance(description, str) and bool(description.strip())
        and isinstance(evidence_refs, list) and len(evidence_refs) > 0
        and all(isinstance(r, str) and r for r in evidence_refs)
    )


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

def assign_triage(*, terminal_state: str,
                  payload: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Assign a first-pass triage flag by the mechanical rules.

    Returns (triage_flag, rule_id). Raises TriageRefused for content this
    pass cannot triage (unknown terminal state -- fail-closed, never a
    default guess).
    """
    if terminal_state == TERMINAL_BOUNDARY:
        return TRIAGE_BOUNDARY, "R-BOUNDARY"
    if terminal_state in INVESTIGATE_TERMINALS:
        return TRIAGE_PROPOSE_INVESTIGATION, "R-INVESTIGATE"
    if (terminal_state in CAPABILITY_ELIGIBLE
            and _well_formed_capability_proposal(payload)):
        return TRIAGE_PROPOSE_CAPABILITY, "R-CAPABILITY"
    # R-RETAIN is the default, but only for known terminal states --
    # an unknown terminal state is a content error, not a retain.
    from swarm_engine.curiosity.evidence.records import TERMINAL_STATES
    if terminal_state not in TERMINAL_STATES:
        raise TriageRefused(
            f"cannot triage unknown terminal_state {terminal_state!r}: "
            f"fail-closed, never a default guess")
    return TRIAGE_RETAIN, "R-RETAIN"
