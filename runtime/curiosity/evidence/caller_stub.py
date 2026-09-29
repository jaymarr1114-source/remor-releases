"""Loop-side caller stub (Phase 1).

The Curiosity Executive, Run Controller, and loops are NOT built in Phase
1 — but the writer's caller side is stubbed with a test double that REALLY
submits through the actual write path (CuriosityWriter.submit), so the
store, writer, fence, and validation are exercised end-to-end through the
genuine call path, not by poking the store's internals.

The stub lives INSIDE the curiosity domain (module under runtime.curiosity)
so it passes the writer's domain fence the way a real Phase-2 loop caller
will.
"""
from __future__ import annotations

from typing import Optional

from .records import CuriosityFinding, EvidenceProvenance
from .writer import CuriosityWriter


class LoopCallerStub:
    """A stand-in for a curiosity loop's write side: builds a finding with
    the loop's identity and submits it through the real writer."""

    def __init__(self, writer: CuriosityWriter, loop: str,
                 model: str = "stub-loop/phase1"):
        self._writer = writer
        self._loop = loop
        self._model = model

    def submit_finding(self, bounded_objective: str, origin: str,
                       terminal_state: str, payload_ref: str,
                       triage: Optional[str] = None) -> CuriosityFinding:
        provenance = EvidenceProvenance(
            loop=self._loop, bounded_objective=bounded_objective,
            model=self._model, triage=triage)
        finding = CuriosityFinding.new(
            loop=self._loop, bounded_objective=bounded_objective,
            origin=origin, terminal_state=terminal_state,
            provenance=provenance, payload_ref=payload_ref)
        return self._writer.submit(finding)

    def submit_raw(self, finding: CuriosityFinding) -> CuriosityFinding:
        """Submit an already-built finding through the real writer path.

        Exists so adversarial tests can drive MALFORMED records through the
        genuine in-domain call path (proving EvidenceRefused fires inside
        the domain fence, not merely at it). Production loops use
        submit_finding; this is the test seam.
        """
        return self._writer.submit(finding)
