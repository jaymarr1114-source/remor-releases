"""The verified-substrate ledger (creativity paper §3 mechanism).

SPECIFIED principle (paper §3, standing law since 2026-09-29): the creativity
controller may compose ONLY from the verified side — never the excess
information that is unverified. This module is the mechanism the paper
DEFERRED: a read-side ledger view over the existing verification records.

The ledger holds NO verification authority. It reads existing records, it
never admits, verifies, attests, or overrides. Any attempt to use it as a
verification authority is refused with an exact reason.

Verification sources (fresh structural re-map 2026-10-01, canonical @44e19e4):
- EvidenceStore (runtime/services/evidence.py, EVIDENCE-WIRE-1): verify_entry
  is the attested act (named verifier, timestamp); get_substrate_entry is the
  ONLY factual-substrate read path (unverified -> QUARANTINED_SUBSTRATE_REFUSED
  dict, never a silent skip); list_substrate is the verified-only view.
- ProvenanceStore (runtime/governance/provenance.py): ProvenanceRecord per
  capability_id with TrustLevel (QUARANTINED=0 < UNKNOWN=1 < SCANNED=2 <
  SANDBOXED=3 < TESTED=4 < TRUSTED=5, ordinal comparisons); set_trust is the
  trust-change path (tamper-evident when oracle-bound, legacy direct-write
  otherwise — documented as unbound).
- Gate crossings are recorded in gate_queue.md (human-readable); the
  machine-readable proxy the ledger consumes is a verified evidence entry
  citing the gate verdict. There is no single machine-readable gate-crossing
  registry in the current tree — recorded as an ambiguity in the mission
  report, not papered over.

Citation discipline: a verification event must NAME the primitive it verifies
(the primitive_id appears in the evidence entry's text or source). An entry
that does not name what it verifies cannot verify it — citing an unrelated
verified entry as proof for a primitive is refused.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from runtime.services.evidence import (
    EvidenceStore,
    QuarantinedSubstrateRefused,
)
from runtime.governance.provenance import (
    ProvenanceStore,
    TrustLevel,
)
from runtime.acquisition.gaps import (
    GapRecord,
    GapRegistry,
    TechniqueBlock,
)


class LedgerRefused(Exception):
    """The ledger refused an operation: exact reason carried, never silent."""


# Verification sources the ledger will consult. Anything else is refused at
# entry construction — the ledger does not invent sources of truth.
SOURCE_EVIDENCE = "evidence"
SOURCE_PROVENANCE = "provenance"
VERIFICATION_SOURCES = (SOURCE_EVIDENCE, SOURCE_PROVENANCE)

# A provenance-sourced primitive counts as verified only at TRUSTED or above.
# (TrustLevel comparisons are ordinal; QUARANTINED=0 .. TRUSTED=5.)
PROVENANCE_MIN_TRUST = TrustLevel.TRUSTED

# Device-proof status vocabulary. Carried on the entry for the RELEASE phase's
# admission criteria; it does NOT decide legality here — legality is decided
# by the verification event alone.
DEVICE_UNCONFIRMED = "unconfirmed"
DEVICE_CONFIRMED = "confirmed"
DEVICE_NOT_APPLICABLE = "not_applicable"
DEVICE_PROOF_VALUES = (
    DEVICE_UNCONFIRMED,
    DEVICE_CONFIRMED,
    DEVICE_NOT_APPLICABLE,
)

# Attribute names that would make the ledger a verification authority. Any
# access attempt raises LedgerRefused via __getattr__ — the ledger admits
# nothing, verifies nothing, overrides nothing, attests nothing.
_AUTHORITY_DENYLIST = frozenset({
    "verify", "verify_entry", "admit", "admit_capability", "attest",
    "attestation", "override", "unverify", "mark_verified",
})


@dataclass(frozen=True)
class LedgerEntry:
    """One primitive's claim to a place on the verified side.

    primitive_id: the capability/technique identifier.
    verification_source: "evidence" | "provenance" (nothing else).
    verification_ref: evidence entry id (int) | provenance capability_id (str).
    gate_reference: the gate verdict/commit that admitted it (e.g.
        "gate:CREATIVITY-SLICE-1@s<sha> 61/61" or a gate_queue.md record).
    device_proof: "unconfirmed" | "confirmed" | "not_applicable".
    """
    primitive_id: str
    verification_source: str
    verification_ref: Any
    gate_reference: str
    device_proof: str = DEVICE_UNCONFIRMED
    registered_at: float = field(default_factory=time.time)


@dataclass
class CompositionVerdict:
    """The outcome of Ledger.check_composition.

    legal: True only if EVERY primitive resolved to the verified side.
    checked: primitive ids that resolved verified.
    illegal: [{primitive_id, reason}] — each named, none dropped silently.
    gaps: gap_ids registered for the unverified demand (deduplicated).
    absent: primitive ids excluded from the composition — the visible record
        of what was left out and why (never hidden).
    """
    legal: bool
    checked: List[str] = field(default_factory=list)
    illegal: List[Dict[str, str]] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    absent: List[str] = field(default_factory=list)


class Ledger:
    """Read-side view over the tree's verification records.

    The ledger indexes LedgerEntries (primitive -> its verification event)
    and answers check_composition. It re-resolves every entry against the
    LIVE stores at check time — verification status is never cached between
    add_entry and check_composition. No hidden caching of trust.
    """

    def __init__(self,
                 evidence_store: EvidenceStore,
                 provenance_store: ProvenanceStore,
                 gap_registry: Optional[GapRegistry] = None,
                 gap_db_path: Optional[str] = None,
                 engine: Any = None) -> None:
        if evidence_store is None or provenance_store is None:
            raise LedgerRefused(
                "ledger needs live evidence and provenance stores; got "
                f"evidence_store={evidence_store!r} "
                f"provenance_store={provenance_store!r}")
        self._evidence = evidence_store
        self._provenance = provenance_store
        if gap_registry is not None:
            self._gaps = gap_registry
        else:
            if not gap_db_path:
                raise LedgerRefused(
                    "ledger needs a gap registry or a gap_db_path; the "
                    "named-gap path must go through the real gap machinery")
            # engine may be None: on the observation-evidence path used for
            # named gaps, GapRegistry.register() never dereferences it
            # (verified by reading runtime/acquisition/gaps.py register()).
            self._gaps = GapRegistry(engine, db_path=gap_db_path)
        self._entries: Dict[str, LedgerEntry] = {}

    def __getattr__(self, name: str) -> Any:
        # Fires only for missing attributes — i.e. exactly the case of
        # someone reaching for verification authority the ledger lacks.
        if name in _AUTHORITY_DENYLIST:
            raise LedgerRefused(
                f"the ledger is not a verification authority: {name!r} "
                "refused — the ledger reads existing verification records; "
                "it admits nothing, verifies nothing, overrides nothing, "
                "attests nothing")
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}")

    # -- entry indexing -------------------------------------------------
    def add_entry(self, entry: LedgerEntry) -> LedgerEntry:
        """Index a primitive's verification event. The event is validated
        LIVE at add time; a second live resolution happens at every check.
        Refused with the exact reason when the event is not verifiable now.
        """
        if not isinstance(entry, LedgerEntry):
            raise LedgerRefused(
                f"add_entry needs a LedgerEntry, got {type(entry).__name__}")
        if not entry.primitive_id or not entry.primitive_id.strip():
            raise LedgerRefused("ledger entry needs a non-empty primitive_id")
        if entry.verification_source not in VERIFICATION_SOURCES:
            raise LedgerRefused(
                f"unknown verification source {entry.verification_source!r}; "
                f"must be one of {list(VERIFICATION_SOURCES)}")
        if not entry.gate_reference or not entry.gate_reference.strip():
            raise LedgerRefused(
                f"primitive {entry.primitive_id!r}: a ledger entry needs a "
                "gate reference — the gate verdict/commit that admitted it")
        if entry.device_proof not in DEVICE_PROOF_VALUES:
            raise LedgerRefused(
                f"primitive {entry.primitive_id!r}: device_proof "
                f"{entry.device_proof!r} not in {list(DEVICE_PROOF_VALUES)}")
        ok, reason = self._resolve(entry)  # raises on quarantined substrate
        if not ok:
            raise LedgerRefused(
                f"cannot index primitive {entry.primitive_id!r}: {reason}")
        self._entries[entry.primitive_id] = entry
        return entry

    def indexed(self) -> List[str]:
        """Primitive ids currently indexed (index state, not trust)."""
        return sorted(self._entries.keys())

    # -- the legality predicate ------------------------------------------
    def check_composition(self, primitive_ids: List[str]) -> CompositionVerdict:
        """LEGAL iff EVERY primitive resolves to the verified side right now.

        One unverified primitive fails the whole composition — no partial
        credit, no silent dropping. Unverified demand registers a named gap
        through the real gap machinery and is recorded in `absent`.
        Consulting a quarantined entry raises QuarantinedSubstrateRefused —
        never a verdict.
        """
        verdict = CompositionVerdict(legal=True)
        for pid in primitive_ids:
            entry = self._entries.get(pid)
            if entry is None:
                reason = (f"no ledger entry for primitive {pid!r}: "
                          "unverified demand")
                verdict.illegal.append(
                    {"primitive_id": pid, "reason": reason})
                verdict.gaps.append(self._ensure_gap(pid, reason))
                verdict.absent.append(pid)
                continue
            ok, reason = self._resolve(entry)  # raises on quarantine
            if ok:
                verdict.checked.append(pid)
            else:
                verdict.illegal.append(
                    {"primitive_id": pid, "reason": reason})
                verdict.gaps.append(self._ensure_gap(pid, reason))
                verdict.absent.append(pid)
        # Deduplicate gap ids (same primitive demanded twice, or a gap that
        # already existed open in the registry).
        seen = set()
        verdict.gaps = [g for g in verdict.gaps
                        if not (g in seen or seen.add(g))]
        verdict.legal = not verdict.illegal
        return verdict

    # -- live resolution (no cached trust) --------------------------------
    def _resolve(self, entry: LedgerEntry) -> "tuple[bool, str]":
        """Re-resolve the entry's verification event against the live store.

        Returns (True, "") or (False, reason). Raises
        QuarantinedSubstrateRefused when the resolution would consult
        quarantined substrate.
        """
        if entry.verification_source == SOURCE_EVIDENCE:
            return self._resolve_evidence(entry)
        if entry.verification_source == SOURCE_PROVENANCE:
            return self._resolve_provenance(entry)
        # Unreachable: add_entry rejects other sources. Kept as a
        # fail-closed guard rather than trusting the constructor path.
        raise LedgerRefused(
            f"unknown verification source {entry.verification_source!r}")

    def _resolve_evidence(self, entry: LedgerEntry) -> "tuple[bool, str]":
        res = self._evidence.get_substrate_entry(entry.verification_ref)
        if not res.get("ok"):
            err = str(res.get("error", ""))
            if "QUARANTINED_SUBSTRATE_REFUSED" in err:
                raise QuarantinedSubstrateRefused(
                    f"ledger refusing to consult quarantined substrate for "
                    f"primitive {entry.primitive_id!r}: {err}")
            return (False, f"verification event {entry.verification_ref!r} "
                           f"no longer resolvable: {err}")
        ev = res["entry"]
        text = str(ev.get("text") or "")
        source = str(ev.get("source") or "")
        # Citation discipline: the event must name the primitive it verifies.
        if entry.primitive_id not in text and entry.primitive_id not in source:
            return (False,
                    f"evidence entry {entry.verification_ref!r} is verified "
                    f"but does not cite primitive {entry.primitive_id!r} "
                    "(citation discipline: the event must name what it "
                    "verifies)")
        return (True, "")

    def _resolve_provenance(self, entry: LedgerEntry) -> "tuple[bool, str]":
        rec = self._provenance.get(entry.verification_ref)
        if rec is None:
            return (False,
                    f"provenance record {entry.verification_ref!r} no longer "
                    "resolvable")
        if rec.trust >= PROVENANCE_MIN_TRUST:
            return (True, "")
        return (False,
                f"provenance trust for {entry.verification_ref!r} is "
                f"{rec.trust.name}, below {PROVENANCE_MIN_TRUST.name}")

    # -- named gaps --------------------------------------------------------
    def _ensure_gap(self, primitive_id: str, reason: str) -> str:
        """Register the unverified demand as a named gap through the real
        gap machinery. An already-open gap for the same primitive is reused
        — repeated checks do not spam the registry.
        """
        marker = f"[creativity-ledger] unverified primitive {primitive_id!r}"
        for existing in self._gaps.list_gaps():
            if (marker in (existing.summary or "")
                    and existing.status in ("open", "routed", "acquiring")):
                return existing.gap_id
        record = GapRecord(
            summary=(f"{marker}: demanded by a creative composition with no "
                     "live verification event"),
            registered_by="creativity-ledger",
            technique=TechniqueBlock(
                objective=(f"establish a verification event for primitive "
                           f"{primitive_id!r} (admit it through the gate) "
                           "so it may join the verified substrate"),
                note=(f"Obstruction: {reason}. The current mechanism cannot "
                      "cross it because the ledger is read-side only — it "
                      "cannot manufacture verification. Acquisition and the "
                      "verification gate must produce the event; the creative "
                      "task proceeds visibly without this element.")),
            evidence=[{
                "kind": "observation",
                "observed": (f"composition legality check refused primitive "
                             f"{primitive_id!r}"),
                "detail": (f"{reason}. Checked at {time.time():.0f} against "
                           "the live evidence and provenance stores; no "
                           "verification event resolvable."),
            }],
        )
        registered = self._gaps.register(record)
        return registered.gap_id
