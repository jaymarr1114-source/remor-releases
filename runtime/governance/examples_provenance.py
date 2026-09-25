"""
swarm_engine/governance/examples_provenance.py

O19 -- driver-supplied worked examples: supply-chain provenance.

Worked examples are the root of the trust chain (the task definition
itself), and every downstream verdict -- smoke expectations (O2), objective
verification, postconditions, requirement "value oracles", repair synthesis
-- rests on them. Where examples surface as engine-evaluated expectations
the evaluation is already bound; this module closes the remaining gap: the
chain from the entry point (driver identity, examples digest, timestamp,
all chained) through each consumer, so every downstream verdict cites the
exact example batch it was judged against.

What this proves: complete provenance -- which driver supplied which
examples, and which downstream verdicts rested on them; a tamper-evident
example set (the batch is content-addressed and its bytes are pinned in the
oracle definition).

What this does NOT prove: that the examples are CORRECT. The driver authors
the task; no second independent task author exists. Binding example-truth
would require one. This is unbindable by construction, not by omission.

Mechanism: each distinct example set is registered as a data oracle named
``examples_batch:<batch_id>`` where ``batch_id = "exb_" + sha256(canonical
examples)[:20]``. Registration is idempotent (re-supplying the same examples
returns the existing version without a new row). The oracle row carries the
driver identity in its ``source`` field, the examples digest in
``definition_digest`` (plus the full example bytes in ``definition_text``
for replay), and the timestamp in ``created_at`` -- all hash-chained.
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from swarm_engine.governance.binding_helpers import canonical_digest


def examples_batch_id(examples: Sequence[Any]) -> str:
    """Content-addressed batch identity for a driver example set."""
    return "exb_" + canonical_digest(list(examples))[:20]


def record_examples_batch(registry, engine_handle, goal: str,
                          examples: Sequence[Any], entry: str,
                          driver_id: Optional[str] = None) -> Optional[str]:
    """Record a driver example set at a task entry point.

    Returns the batch id, or None when there are no examples. Registers the
    batch as a data oracle (idempotent); the chained oracle row is the
    (driver identity, examples digest, timestamp) record. No registry ->
    no record (caller must guard); this function itself requires both.
    """
    if not examples:
        return None
    examples = list(examples)
    batch_id = examples_batch_id(examples)
    name = "examples_batch:" + batch_id
    source = "driver:%s via %s" % (driver_id or "unattributed", entry)
    engine_handle.register_oracle(
        name,
        {"kind": "examples_batch", "goal": goal,
         "examples_digest": canonical_digest(examples),
         "count": len(examples), "examples": examples},
        input_contract="driver-supplied worked examples",
        output_contract="content-addressed batch identity",
        source=source)
    return batch_id


def cite_batch(batch_id: Optional[str]) -> str:
    """Verdict-string citation fragment for an example batch."""
    return f" [examples_batch={batch_id}]" if batch_id else ""


def batch_oracle_id(engine_producer_id: str, batch_id: str) -> str:
    """The registry oracle id for a batch citation.

    Deterministic: ``"orc_" + sha256(engine_producer_id + name)[:20]``,
    mirroring ``OracleRegistry.register_oracle``. Lets an auditor resolve
    a ``[examples_batch=exb_…]`` citation to its exact chained row.
    """
    from swarm_engine.governance.oracle_binding import _digest
    name = "examples_batch:" + batch_id
    return "orc_" + _digest(engine_producer_id + "\x00" + name)[:20]


def batch_oracle_row(registry, engine_producer_id: str,
                     batch_id: str) -> Optional[dict]:
    """Fetch the stored head row for a batch citation (None if absent)."""
    return registry.oracle_head(batch_oracle_id(engine_producer_id,
                                                batch_id))
