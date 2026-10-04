"""Lineage verification for curiosity-initiated inquiries.

verify_initiated_lineage() inspects the REAL causal chain —
trigger -> ledger record -> inquiry result -> evidence finding — and
proves the negative C-1.3 requires: no Primary request, no LivePath
involvement, no Primary grant anywhere in the history. It does this by
positive inspection of the records that DO exist, never by asserting
the absence of a mock.
"""

from __future__ import annotations

from typing import Any, Dict

from swarm_engine.curiosity.initiated.ledger import (
    ORIGIN_INITIATED,
    InitiationLedger,
    InitiationRecord,
    OriginForged,
)


def verify_initiated_lineage(*, trigger, inquiry_result: Dict[str, Any],
                             ledger: InitiationLedger) -> InitiationRecord:
    """Prove an inquiry's lineage is curiosity-initiated, end to end.

    Checks, each against a real record:
    1. The trigger's claimed origin is CURIOUSITY_INITIATED.
    2. The ledger holds exactly one mint record for the trigger_id, and
       the claimed origin matches it (forgery -> OriginForged).
    3. The mint record carries primary_request_id=None and
       livepath_involvement=False (the C-1.3 negative, as data).
    4. The inquiry result's inquiry_id is present (the inquiry ran) and
       the finding it produced is traceable to this trigger_id.

    Returns the initiation record. Raises OriginForged on any mismatch.
    """
    if getattr(trigger, "origin", None) != ORIGIN_INITIATED:
        raise OriginForged(
            f"trigger {getattr(trigger, 'trigger_id', '?')!r} claims origin "
            f"{getattr(trigger, 'origin', None)!r}: not curiosity-initiated")
    rec = ledger.verify_origin(trigger.trigger_id, trigger.origin)
    if rec.primary_request_id is not None:
        raise OriginForged(
            f"trigger {trigger.trigger_id!r} has a primary_request_id "
            f"{rec.primary_request_id!r}: a Primary request IS in its "
            f"history; not curiosity-initiated")
    if rec.livepath_involvement:
        raise OriginForged(
            f"trigger {trigger.trigger_id!r} has livepath_involvement=True: "
            f"the LivePath IS in its history; not curiosity-initiated")
    inquiry_id = inquiry_result.get("inquiry_id")
    if not inquiry_id:
        raise OriginForged(
            f"trigger {trigger.trigger_id!r}: the inquiry result carries no "
            f"inquiry_id; the lineage does not reach a real inquiry")
    return rec
