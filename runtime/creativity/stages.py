"""The six ordered creative stages (D-5, DECIDED 2026-09-29).

Paper: architecture/exec-creativity-controller_2026-09-29.md
(Strategy/spec paper EXEC-CREATIVITY-1, 2026-09-29).

D-5 DECIDED (paper §9 T2): the six elements are ORDERED STAGES of one
creative process — Intent → Generation → Variation → Critique →
Refinement → Release — rather than six independent boundary classes.
Selection is preserved via "which stage owns the current state."

Every descriptor below is drawn EXACTLY from paper §2 (each stage's
role as specified there) and carries the verbatim §2 quote it is
drawn from, so the citation is machine-checkable. No invented
responsibilities.

The refinement iteration bound is an EXPLICIT PARAMETER with no
default invented: paper §8 Q6 was NEEDS-JAMES-DECISION at dispatch
and is now DECIDED in paper §11 Q6 (James, 2026-09-30) as "bounded
turns with an adaptive stop condition inside the bound ... never
loops forever" — but no number was set. The mission does not pick
James's numbers.

Scope fence (the packet): NOT built here — the ledger mechanism
(§3 DEFERRED), the novelty check (§4 proposed), the critique
machinery (§6 DEFERRED implementation), the composition search,
peer arbitration (§5), the executive itself, the run controller,
budgets, and anything depending on the decided §11 governance
questions. The dual-executive build hold remains in force.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class CreativeStage(IntEnum):
    """The six ordered creative stages (D-5). IntEnum so the paper's
    order — intent → generation → variation → critique → refinement
    → release (§2) — is the enum order."""

    INTENT = 1
    GENERATION = 2
    VARIATION = 3
    CRITIQUE = 4
    REFINEMENT = 5
    RELEASE = 6


class StageRefused(Exception):
    """Raised when a stage transition is not admissible. The message
    is always the exact reason."""


@dataclass(frozen=True)
class StageDescriptor:
    """Per-stage descriptor: decision class, inputs, outputs,
    termination condition (paper §9 T2), each drawn from paper §2.

    paper_section names the paper section(s) the descriptor is drawn
    from; paper_quote is the VERBATIM §2 sentence it is drawn from,
    so the citation can be checked against the paper text.
    """

    stage: CreativeStage
    decision_class: str
    inputs: str
    outputs: str
    termination_condition: str
    paper_section: str
    paper_quote: str


_STAGE_DESCRIPTORS: dict[CreativeStage, StageDescriptor] = {
    CreativeStage.INTENT: StageDescriptor(
        stage=CreativeStage.INTENT,
        decision_class="commission registration — what is wanted",
        inputs="the user's commission, stated as an outcome",
        outputs="registered intent — what is wanted, not how to make it",
        termination_condition="the commission is registered as a stated outcome",
        paper_section="§2 (D-5); §9 T2",
        paper_quote=(
            "The controller registers *what is wanted*, not how to make it. "
            "Intent is the only input the user owes."
        ),
    ),
    CreativeStage.GENERATION: StageDescriptor(
        stage=CreativeStage.GENERATION,
        decision_class="composition of verified primitives",
        inputs="verified primitives (the verified side of the ledger, §3)",
        outputs="candidate artifacts",
        termination_condition="candidate artifacts composed from verified primitives",
        paper_section="§2 (D-5); §3 verified-factual substrate rule",
        paper_quote="compose verified primitives into candidate artifacts",
    ),
    CreativeStage.VARIATION: StageDescriptor(
        stage=CreativeStage.VARIATION,
        decision_class="search along explicit dimensions",
        inputs=(
            "candidate artifacts; explicit dimensions "
            "(style, structure, medium, constraint-satisfaction)"
        ),
        outputs="candidates varied along the explicit dimensions",
        termination_condition="the explicit dimensions have been searched",
        paper_section="§2 (D-5)",
        paper_quote=(
            "produce candidates along explicit dimensions "
            "(style, structure, medium, constraint-satisfaction) — "
            "variation is searched, not hoped for."
        ),
    ),
    CreativeStage.CRITIQUE: StageDescriptor(
        stage=CreativeStage.CRITIQUE,
        decision_class="internal evaluation",
        inputs="candidate artifacts",
        outputs="evaluation against novelty, value, and honesty",
        termination_condition="every candidate evaluated against novelty, value, and honesty",
        paper_section="§2 (D-5); §6 (DEFERRED implementation — descriptor only)",
        paper_quote=(
            "internal evaluation against novelty, value, and honesty "
            "(see §6). Critique is a mechanism, not a mood."
        ),
    ),
    CreativeStage.REFINEMENT: StageDescriptor(
        stage=CreativeStage.REFINEMENT,
        decision_class="iteration on the strongest candidates",
        inputs="the strongest candidates (from critique)",
        outputs="refined candidates",
        termination_condition=(
            "the iteration bound is reached — on exhaustion, release with "
            "state/evidence or stop; never loop forever (§11 Q6, DECIDED "
            "2026-09-30). The bound is an explicit parameter, never a "
            "chosen constant."
        ),
        paper_section="§2 (D-5); §11 Q6 (decided 2026-09-30)",
        paper_quote="iterate on the strongest candidates; the loop is bounded",
    ),
    CreativeStage.RELEASE: StageDescriptor(
        stage=CreativeStage.RELEASE,
        decision_class="publication",
        inputs="the finished artifact with full provenance",
        outputs="the Artifact Lab gallery entry",
        termination_condition="terminal — the artifact is published to the gallery",
        paper_section="§2 (D-5); §7 (REFERENCE flow)",
        paper_quote="publish to the Artifact Lab gallery with full provenance",
    ),
}

# The ordered forward process (§2): intent → generation → variation →
# critique → refinement → release.
STAGE_ORDER: tuple[CreativeStage, ...] = tuple(CreativeStage)


def describe(stage: CreativeStage) -> StageDescriptor:
    """Return the §2-drawn descriptor for a stage."""
    if not isinstance(stage, CreativeStage):
        raise StageRefused(
            "stage refused: %r is not one of the six ordered creative "
            "stages (D-5, paper §2)" % (stage,)
        )
    return _STAGE_DESCRIPTORS[stage]


def is_terminal(stage: CreativeStage) -> bool:
    """Release is terminal (D-5): nothing is admissible after it."""
    describe(stage)  # validates membership, keeps the refusal exact
    return stage is CreativeStage.RELEASE


def admissible_next(stage: CreativeStage) -> tuple[CreativeStage, ...]:
    """The ordered forward transition (§2, D-5).

    Refinement may additionally return to Variation/Critique — the
    paper's "iterate on the strongest candidates" (§2). The loop-back
    is structural here; whether another loop-back iteration is
    admissible is gated by refinement_loop_admissible(), whose
    iteration bound is an explicit required parameter (§11 Q6).

    Release is terminal: no next stage is admissible.
    """
    describe(stage)  # validates membership, keeps the refusal exact
    return {
        CreativeStage.INTENT: (CreativeStage.GENERATION,),
        CreativeStage.GENERATION: (CreativeStage.VARIATION,),
        CreativeStage.VARIATION: (CreativeStage.CRITIQUE,),
        CreativeStage.CRITIQUE: (CreativeStage.REFINEMENT,),
        CreativeStage.REFINEMENT: (
            CreativeStage.VARIATION,
            CreativeStage.CRITIQUE,
            CreativeStage.RELEASE,
        ),
        CreativeStage.RELEASE: (),
    }[stage]


def refinement_loop_admissible(iterations_used: int, bound: int) -> bool:
    """Whether one more refinement loop-back iteration is admissible.

    iterations_used: how many critique→refinement turns have run.
    bound: the iteration bound — an EXPLICIT REQUIRED PARAMETER with
    no default invented. §8 Q6 was NEEDS-JAMES-DECISION at dispatch
    and is now DECIDED in §11 Q6 (2026-09-30) as bounded turns with
    an adaptive stop inside the bound, never infinite — but no number
    was set, so the mission does not pick one. Omitting the bound
    raises TypeError: the parameter is required.
    """
    if not isinstance(iterations_used, int) or not isinstance(bound, int):
        raise StageRefused(
            "stage refused: refinement iterations_used and bound must both "
            "be integers (got %r, %r)" % (iterations_used, bound)
        )
    if iterations_used < 0 or bound < 0:
        raise StageRefused(
            "stage refused: refinement iterations_used and bound must be "
            "non-negative (got %d, %d)" % (iterations_used, bound)
        )
    return iterations_used < bound
