"""CUR-HARDEN-1 drill helpers (curiosity-domain side).

This module lives under runtime/curiosity/hardening/, so calls it makes
into CuriosityWriter.submit() pass the writer's domain fence (the fence
requires a curiosity-domain caller) — the battery drives the REAL write
path through here. Conversely, quarantine_* calls made from here must be
REFUSED by the stores' quarantine fence (quarantine is a governance-plane
action; curiosity cannot hide its own evidence): the battery asserts the
refusal through these helpers.
"""

from __future__ import annotations

from swarm_engine.curiosity.evidence.records import (
    CuriosityFinding,
    EvidenceProvenance,
)
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.evidence.writer import CuriosityWriter
from swarm_engine.curiosity.frm.ledger import EpochLedger


def submit_finding(store: CuriosityEvidenceStore, seed: str,
                   evidence_id: str) -> str:
    """Submit one finding through the REAL writer (fenced) write path."""
    finding = CuriosityFinding.new(
        loop="questioning",
        bounded_objective="harden1-probe-%s" % seed,
        origin="CURIOUSITY_INITIATED",
        terminal_state="QUESTION_RESOLVED",
        provenance=EvidenceProvenance(
            loop="questioning",
            bounded_objective="harden1-probe-%s" % seed,
            model="harden1-probe"),
        payload_ref="payload/harden1/%s" % seed)
    finding.evidence_id = evidence_id
    CuriosityWriter(store).submit(finding)
    return evidence_id


def try_quarantine_evidence(store: CuriosityEvidenceStore, evidence_id: str,
                            reason: str):
    """Attempt quarantine from a curiosity-domain frame: must be refused."""
    return store.quarantine_corrupt(evidence_id, reason)


def try_quarantine_frm(ledger: EpochLedger, seq: int, reason: str):
    """Attempt FRM quarantine from a curiosity-domain frame: must refuse."""
    return ledger.quarantine_seq(seq, reason)
