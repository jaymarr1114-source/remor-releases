"""Experience miner: seed acquisition gaps from observed deltas (ACQ-MINE-1).

V10-GAP-GENESIS named the residual honestly: experience mining was ABSENT.
Gaps seeded only from failures (dispatch failures, quarantine, limitation
diagnoses), never from observed deltas. This module is the miner.

It reads validated charter-9 delta records from the unified-memory substrate
(kind="technique_delta"), identifies deltas evidencing an open capability gap,
and emits well-formed GapRecords through GapRegistry.register -- the real
inlet, never a side channel.

Mining rule (mechanical, documented -- no judgment calls):
  1. Read observations with kind="technique_delta" via read_experiences.
  2. Extract raw["delta"]; re-validate with validate_delta. Invalid -> skip
     honestly (never invent, never repair).
  3. Skip if already mined: a gap with registered_by="experience-miner" whose
     evidence cites this observation ID already exists in the registry.
  4. Best-effort ownership: skip if resulting_capability_c exactly names a
     capability present in AcquiredCodeStore. (Best-effort: the store's names
     are admission-time generated; the already-mined check is the binding
     dedup. Documented, not oversold.)
  5. Extract I/O pairs from evidence_e run artifacts carrying io_pairs.
     Need >= MIN_EVIDENCE_EXAMPLES, else the delta is not acquirable through
     the technique route -> skip honestly, do not invent pairs.
  6. Emit GapRecord with TechniqueBlock(objective, delta, note) + observation
     evidence citing the delta record IDs; registered_by="experience-miner".

What this is NOT:
  - It does not discover techniques. runtime/intellect/patterns.py's
    PatternMiner discovers statistical patterns in event metrics; it has no
    connection to the gap registry and a different shape. Inventoried,
    not duplicated, not forked.
  - It does not re-verify the delta's gap claim. The charter-9 validation
    (validate_delta) already enforced the causal discipline at write time;
    the miner re-validates the schema mechanically.
  - It does not decide admission. The acquire path (technique route ->
    DistillationLoop) decides; the miner only surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

REGISTERED_BY = "experience-miner"


@dataclass
class MinedGap:
    """One mineable delta: the source records plus the translated payload."""
    observation_id: str
    delta: Dict[str, Any]              # charter-9 delta dict (validated)
    io_pairs: List[Dict[str, Any]]     # [{input: {...}, output: ...}]
    technique_name: str = ""
    resulting_capability: str = ""


def _extract_io_pairs(delta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Mechanically extract input/output pairs from evidence_e.

    Documented rule: run artifacts may carry an `io_pairs` list of
    {input: {...}, output: ...}. Pairs with non-dict input or missing
    output are dropped, never repaired. Anything else in evidence_e is
    ignored (not an error -- the delta may evidence its technique
    another way, but then it is not mineable by this rule).
    """
    pairs: List[Dict[str, Any]] = []
    for art in delta.get("evidence_e", []) or []:
        if not isinstance(art, dict):
            continue
        for p in art.get("io_pairs", []) or []:
            if not isinstance(p, dict):
                continue
            inp = p.get("input")
            if not isinstance(inp, dict) or "output" not in p:
                continue
            pairs.append({"input": dict(inp), "output": p["output"]})
    return pairs


def _to_m2_delta(delta: Dict[str, Any],
                 io_pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Translate charter-9 delta vocabulary to M1/M2 DeltaRecord vocabulary.

    The technique route (_acquire_technique) builds a DeltaRecord from these
    fields; the translation is field-for-field, no invention:
      objective_x        -> objective
      external_demo_y    -> external_actions
      native_inventory_z -> prior_capability
      capability_gap     -> capability_gap
      technique_t.name   -> technique (+ probe check text)
      io_pairs           -> evidence [{input, output}]
      dependencies_d     -> dependencies
      verification_v     -> verification (namespaced under charter9)
    """
    t = delta.get("technique_t") or {}
    probe = t.get("probe") or {}
    return {
        "objective": delta.get("objective_x", ""),
        "external_actions": delta.get("external_demo_y", ""),
        "prior_capability": delta.get("native_inventory_z", ""),
        "capability_gap": delta.get("capability_gap", ""),
        "technique": "%s: %s" % (t.get("name", ""),
                                 probe.get("check", "")),
        "evidence": [{"input": p["input"], "output": p["output"]}
                     for p in io_pairs],
        "dependencies": list(delta.get("dependencies_d", []) or []),
        "verification": {"charter9": True,
                         "verification_v": delta.get("verification_v", "")},
        "source": "experience-mining",
    }


class ExperienceMiner:
    """Scans delta records; emits technique gaps for the mineable ones."""

    def __init__(self, epistemic: Any, acquired_code: Any,
                 min_pairs: Optional[int] = None):
        self.epistemic = epistemic
        self.acquired_code = acquired_code
        if min_pairs is None:
            from swarm_engine.acquisition.delta import MIN_EVIDENCE_EXAMPLES
            min_pairs = MIN_EVIDENCE_EXAMPLES
        self.min_pairs = min_pairs

    # -- mining ---------------------------------------------------------

    def scan(self) -> List[MinedGap]:
        """Return the currently mineable deltas. Pure read + rule application;
        writes nothing."""
        from runtime.intellect.unified_memory import read_experiences
        from runtime.intellect.delta_capture import validate_delta
        out: List[MinedGap] = []
        for obs in read_experiences(self.epistemic, kind="technique_delta",
                                    limit=10000):
            obs_id = obs.get("observation_id", "")
            delta = (obs.get("raw") or {}).get("delta")
            if not isinstance(delta, dict):
                continue
            if validate_delta(delta):
                continue  # invalid -> skip honestly
            t = delta.get("technique_t") or {}
            tname = t.get("name", "")
            cap = delta.get("resulting_capability_c", "") or ""
            if cap and self._is_admitted(cap):
                continue  # already owned -> no gap
            pairs = _extract_io_pairs(delta)
            if len(pairs) < self.min_pairs:
                continue  # not acquirable via technique route -> skip
            out.append(MinedGap(observation_id=obs_id, delta=delta,
                                io_pairs=pairs, technique_name=tname,
                                resulting_capability=cap))
        return out

    def _is_admitted(self, capability_name: str) -> bool:
        """Best-effort ownership check: exact name match in AcquiredCodeStore."""
        try:
            get = getattr(self.acquired_code, "get", None)
            if not callable(get):
                return False
            return get(capability_name) is not None
        except Exception:
            return False  # store unreadable -> do not claim ownership

    # -- emission -------------------------------------------------------

    def already_mined(self, registry: Any, observation_id: str) -> bool:
        """True if the registry holds an experience-miner gap citing this
        observation ID."""
        for rec in registry.list_gaps():
            if getattr(rec, "registered_by", "") != REGISTERED_BY:
                continue
            for ev in rec.evidence or []:
                if not isinstance(ev, dict):
                    continue
                detail = ev.get("detail") or {}
                if detail.get("delta_observation_id") == observation_id:
                    return True
        return False

    def emit_gap(self, registry: Any, mined: MinedGap):
        """Build the TechniqueBlock gap record and register it through the
        registry's real inlet. Raises GapRefused if the record is invalid
        (fail-closed, never a side channel)."""
        from swarm_engine.acquisition.gaps import GapRecord, TechniqueBlock
        if self.already_mined(registry, mined.observation_id):
            raise ValueError(
                "delta %s already mined: refusing duplicate gap"
                % mined.observation_id)
        m2 = _to_m2_delta(mined.delta, mined.io_pairs)
        objective = ("Mined technique gap: %s (from observed delta %s)"
                     % (mined.technique_name, mined.observation_id))
        record = GapRecord(
            registered_by=REGISTERED_BY,
            summary=objective,
            technique=TechniqueBlock(
                objective=objective,
                delta=m2,
                note=("Mined from observed delta %s by %s; technique '%s'; "
                      "resulting capability '%s' not in admitted inventory; "
                      "%d I/O pairs extracted from evidence_e.")
                     % (mined.observation_id, REGISTERED_BY,
                        mined.technique_name, mined.resulting_capability,
                        len(mined.io_pairs))),
            evidence=[{
                "kind": "observation",
                "observed": (
                    "technique delta %s records capability '%s' absent "
                    "from the admitted capability inventory"
                    % (mined.observation_id, mined.resulting_capability)),
                "detail": {
                    "delta_observation_id": mined.observation_id,
                    "technique": mined.technique_name,
                    "capability_gap": (mined.delta.get("capability_gap")
                                       or "")[:500],
                    "resulting_capability": mined.resulting_capability,
                    "n_io_pairs": len(mined.io_pairs),
                    "mined_by": REGISTERED_BY,
                },
            }],
        )
        return registry.register(record)

    def mine_and_emit(self, registry: Any) -> List[Any]:
        """Scan and emit one gap per mineable, not-already-mined delta.
        Returns the registered GapRecords."""
        out = []
        for mined in self.scan():
            if self.already_mined(registry, mined.observation_id):
                continue
            out.append(self.emit_gap(registry, mined))
        return out
