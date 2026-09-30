"""The intent register with intent-state ownership (D-4, DECIDED 2026-09-29).

Paper: architecture/exec-creativity-controller_2026-09-29.md.

D-4 DECIDED (paper §9 T1): the creativity executive's selection
grammar begins with intent-state ownership — "What is wanted?
(intent)" → "What creative process state is admissible?" →
"Enter/continue that state." The three-question shape is preserved
from the Primary's grammar; the substance differs by proven need
(§1's category-error argument).

Paper §2: "Intent: the user's commission, stated as an outcome (the
James↔Felix mirror, 2026-09-27/28). The controller registers *what is
wanted*, not how to make it. Intent is the only input the user owes."
Paper §7 flow step 1: "REMOR registers the intent — outcome stated,
method owned by the machine (the James↔Felix mirror)."

Scope fence (the packet): this module is the intent register and the
D-4 selection grammar ONLY. NOT built here — the ledger (§3
DEFERRED), novelty check (§4), critique machinery (§6), composition
search, peer arbitration (§5), the executive itself, the run
controller, budgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .stages import (
    CreativeStage,
    STAGE_ORDER,
    StageRefused,
    admissible_next,
    refinement_loop_admissible,
)


class IntentRefused(Exception):
    """Raised when an intent cannot be registered. The message is
    always the exact reason."""


INTENT_OUTCOME_OWED_REASON = (
    "intent refused: no outcome stated — 'Intent is the only input the "
    "user owes' (creativity paper §2), and it is owed."
)


@dataclass(frozen=True)
class CreativeIntent:
    """The commissioned outcome (paper §2, §7 step 1).

    principal: the commissioning principal (the user who stated it).
    outcome: the outcome text, as stated — the ONLY input the user owes.
    method_owned_by_machine: the James↔Felix mirror — outcome stated,
        method owned by the machine (§7).
    created_at: ISO-8601 UTC timestamp of registration.
    """

    principal: str
    outcome: str
    method_owned_by_machine: bool = True
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def register_intent(principal: str, outcome: str | None) -> CreativeIntent:
    """Register a commissioned outcome.

    An intent with no stated outcome is refused with the exact reason:
    the paper says "Intent is the only input the user owes" — and it
    IS owed (§2). Empty and whitespace-only outcomes count as unstated.
    """
    if outcome is None or not str(outcome).strip():
        raise IntentRefused(INTENT_OUTCOME_OWED_REASON)
    return CreativeIntent(principal=principal, outcome=str(outcome))


def _validated(intent: CreativeIntent) -> CreativeIntent:
    if not isinstance(intent, CreativeIntent):
        raise IntentRefused(
            "intent refused: %r is not a registered CreativeIntent (D-4, paper §9 T1)"
            % (intent,)
        )
    if not intent.outcome or not intent.outcome.strip():
        raise IntentRefused(INTENT_OUTCOME_OWED_REASON)
    return intent


def admissible_stages(intent: CreativeIntent) -> tuple[CreativeStage, ...]:
    """D-4's second question: "What creative process state is
    admissible?" (paper §9 T1).

    Intent is the only admissible FIRST state; the rest follow the
    six-stage order (D-5, §2). Returns the full ordered six for a
    valid intent.
    """
    _validated(intent)
    return STAGE_ORDER


def select_state(
    intent: CreativeIntent,
    current_stage: CreativeStage | None = None,
    *,
    refinement_iterations_used: int | None = None,
    refinement_bound: int | None = None,
) -> CreativeStage:
    """D-4's full selection grammar (§9 T1, DECIDED 2026-09-29):

        "What is wanted? (intent)"
        → "What creative process state is admissible?"
        → "Enter/continue that state."

    q1+q2 are answered by admissible_stages(); q3 resolves to a stage:
    - no current stage → enter INTENT (the only admissible first state);
    - current stage mid-process → enter the ordered forward next stage;
    - current stage REFINEMENT → the bound decides: another loop-back
      iteration is admissible only while refinement_loop_admissible()
      holds; on exhaustion the state to enter is RELEASE (§11 Q6:
      release with state/evidence or stop — never loop forever).
      REFINEMENT therefore REQUIRES refinement_iterations_used and
      refinement_bound — the bound is an explicit parameter, never a
      chosen constant;
    - current stage RELEASE → refused: release is terminal (D-5, §2).

    The refinement loop-back re-enters at VARIATION (the head of the
    "iterate on the strongest candidates" loop, §2): new candidates
    along the explicit dimensions, then critique again.
    """
    admissible_stages(intent)  # q1 + q2; raises the exact refusal
    if current_stage is None:
        return CreativeStage.INTENT
    if not isinstance(current_stage, CreativeStage):
        raise StageRefused(
            "stage refused: %r is not one of the six ordered creative "
            "stages (D-5, paper §2)" % (current_stage,)
        )
    if current_stage is CreativeStage.RELEASE:
        raise StageRefused(
            "stage refused: RELEASE is terminal — there is no state to "
            "enter after release (D-5, paper §2)"
        )
    if current_stage is CreativeStage.REFINEMENT:
        if refinement_iterations_used is None or refinement_bound is None:
            raise StageRefused(
                "stage refused: REFINEMENT requires an explicit iteration "
                "bound — §11 Q6 is decided (bounded turns, never infinite) "
                "but the mission does not pick James's numbers; pass "
                "refinement_iterations_used and refinement_bound"
            )
        if refinement_loop_admissible(refinement_iterations_used, refinement_bound):
            return CreativeStage.VARIATION
        return CreativeStage.RELEASE
    return admissible_next(current_stage)[0]
