"""Minting curiosity-initiated triggers (C-1.3).

mint_initiated_trigger() is the ONLY honest way to create a trigger with
origin CURIOUSITY_INITIATED: it validates the presentation fail-closed,
mints the ledger record, and returns the trigger. The LivePath
(runtime.core) is never imported here — structurally, no Primary
request can be involved in the minting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from swarm_engine.curiosity.executive.boundary import new_trigger
from swarm_engine.curiosity.initiated.ledger import (
    ORIGIN_INITIATED,
    InitiationLedger,
    InitiationRecord,
    OriginForged,
)

if TYPE_CHECKING:  # pragma: no cover
    from swarm_engine.curiosity.executive.boundary import CuriosityTrigger


def mint_initiated_trigger(*, boundary_class: str, question_text: str,
                           bounded_objective: str,
                           ledger: InitiationLedger):
    """Mint a curiosity-initiated trigger with its causal record.

    Fail-closed: empty presentation fields are refused before anything
    is minted. The returned trigger's origin is CURIOUSITY_INITIATED
    (frozen spelling) and its trigger_id has exactly one ledger record
    with primary_request_id=None and livepath_involvement=False.
    """
    if not (boundary_class or "").strip():
        raise OriginForged("initiated trigger requires a boundary_class")
    if not (bounded_objective or "").strip():
        raise OriginForged("initiated trigger requires a bounded_objective")
    trigger = new_trigger(
        boundary_class=boundary_class,
        question_text=question_text,
        bounded_objective=bounded_objective,
        origin=ORIGIN_INITIATED,
    )
    # Validate now, not later: a malformed trigger must never reach the
    # executive, and must never leave a ledger orphan behind it.
    trigger.validate()
    ledger.mint(
        trigger_id=trigger.trigger_id,
        boundary_class=boundary_class,
        bounded_objective=bounded_objective,
    )
    return trigger
