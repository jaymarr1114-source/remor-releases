"""External technique ingestion — phase zero of the technique-distillation loop.

An external demonstration (a subagent trace, a chat turn that actually
demonstrated a procedure, or a plugin-bot action sequence) is the Y in the
delta schema: what the external agent did. Ingestion persists the Y record,
forms the Y-Z delta against REMOR's current capability/primitive inventory
(Z), and persists the delta — but ONLY where the causal-discipline rule is
met:

    A conversation yields a delta record ONLY where there is an actual
    observable capability gap and sufficient evidence of what was done.
    Instructions ("you should learn to sort") are not demonstrations; a
    chat turn with no procedure steps is insufficient evidence and is
    refused, never NLP-guessed into a record.

Plugin-bot actions ride this exact same path: the plugin adapter accepts the
same structured record shape as the subagent-trace adapter, differing only in
source. (No live bot source exists yet — bots are in the product vision — so
the plugin path is proven with synthetic-but-structured records, labeled as
such.)

Experience candidates ride this path too (Q5): an accepted L1 organizational
experience candidate (submit_candidate) is an external agent's demonstrated
technique with an io_contract carrying observed inputs/outputs — the exact
Y material the causal-discipline gate requires.

Evidence-kind decision (Q5, taken not deferred): technique deltas are
FIRST-CLASS in the epistemic store — EpistemicStore observations carry a
free-text `source` field, and deltas persist with source="technique_delta"
with no whitelist to fight. The EvidenceStore mirror (services/evidence.py)
exists solely for the GUI Evidence view, whose three-kind taxonomy
(hypothesis/observation/inference) is a shared contract with the GUI layer:
its `by_kind` counts and "No observations recorded yet" states branch on
kind. Adding a fourth kind would edit a file outside acquisition ownership,
ripple into GUI filtering and contract tests, and buy nothing epistemically.
A technique delta IS an observation in the epistemic sense — a recorded
observation of demonstrated technique — so the kind="observation" mirror
with the explicit "record_kind": "technique_delta" marker inside the JSON
is the honest encoding, not a workaround. list_technique_deltas() below
makes the mirror a proper first-class retrieval path. If the GUI track ever
wants a fourth kind, that is a coordinated interface change through Felix —
flagged, not taken unilaterally here.

Downstream phases (M2+) fill DeltaRecord.V and DeltaRecord.C; ingestion
leaves them at their honest initial values.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional
from uuid import uuid4

if TYPE_CHECKING:  # pragma: no cover
    from swarm_engine.intellect.epistemic import EpistemicStore


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class ExternalAction:
    """One observed action of an external demonstration."""
    kind: str  # "tool_call" | "capability_call" | "procedure_step" | "reasoning_step"
    name: str
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)  # observed outcome proof


@dataclass
class ExternalDemonstration:
    """A structured external demonstration: the raw Y material."""
    source: str  # "subagent_trace" | "chat_turn" | "plugin_bot" | "experience_candidate"
    objective: str
    actions: List[ExternalAction] = field(default_factory=list)
    outcome: Dict[str, Any] = field(default_factory=dict)
    required_tools: List[str] = field(default_factory=list)
    at: float = field(default_factory=time.time)
    # Provenance rides through to the persisted Y record's raw payload so a
    # consumer can tell how the demonstration was produced. In particular,
    # simulated sources MUST label themselves here
    # ({"simulated": True, ...}); the label is preserved end to end and the
    # mechanism is what is proven, never the simulated source.
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass
class YRecord:
    """The persisted Y: what the external agent did, as data."""
    y_id: str
    source: str
    objective: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    outcome: Dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DeltaRecord:
    """The persisted Y-Z delta. Frozen schema: V and C are filled by M2."""
    delta_id: str
    X: str  # objective
    Y: Dict[str, Any]  # {y_id, action_count, source}
    Z: Dict[str, Any]  # inventory summary
    gap: str  # the Y-Z description
    T: Dict[str, Any]  # {technique_name, required_tools}
    E: List[str]  # evidence ids
    D: List[str]  # dependencies (may be empty)
    V: Dict[str, Any]  # {"status": "unverified", "method": None} — M2 fills
    C: Optional[Any]  # None — M2 fills

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class InventorySnapshot:
    """Z: REMOR's own inventory at ingestion time."""
    capabilities: List[str] = field(default_factory=list)  # ACTIVE capability names/ids
    primitives: List[str] = field(default_factory=list)  # primitive names
    at: float = field(default_factory=time.time)

    def vocabulary(self) -> set:
        return set(self.capabilities) | set(self.primitives)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class IngestResult:
    recorded: bool
    reason: str
    y_id: Optional[str]
    delta_id: Optional[str]
    detail: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def _coerce_records(payload: Any) -> List[Any]:
    """Extract the record list from a capability-store listing payload.

    Accepts a dict wrapping a list under a known key, or a bare list.
    """
    if isinstance(payload, dict):
        for key in ("capabilities", "items", "records"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []
    if isinstance(payload, list):
        return payload
    return []


def _record_field(rec: Any, key: str) -> Any:
    if isinstance(rec, dict):
        return rec.get(key)
    return getattr(rec, key, None)


def build_inventory(capability_store: Any = None,
                    primitive_registry: Any = None) -> InventorySnapshot:
    """Snapshot the current capability/primitive inventory (Z).

    capability_store is anything with list_capabilities(status_filter="all")
    returning a dict with a list of records carrying name/capability_id and
    status (dicts or attribute-bearing objects; only ACTIVE included).
    primitive_registry is anything with a names() method or an __iter__ of
    names. With both None, returns an empty snapshot — documented meaning:
    an empty inventory means every required tool is a gap.
    """
    capabilities: List[str] = []
    if capability_store is not None:
        try:
            payload = capability_store.list_capabilities(status_filter="all")
        except TypeError:
            payload = capability_store.list_capabilities()
        for rec in _coerce_records(payload):
            if _record_field(rec, "status") != "ACTIVE":
                continue
            name = _record_field(rec, "name") or _record_field(rec, "capability_id")
            if name:
                capabilities.append(str(name))
    primitives: List[str] = []
    if primitive_registry is not None:
        names = primitive_registry.names() if hasattr(primitive_registry, "names") \
            else primitive_registry
        primitives = [str(n) for n in names if n]
    return InventorySnapshot(capabilities=capabilities, primitives=primitives,
                             at=time.time())


# ---------------------------------------------------------------------------
# Ingestion core
# ---------------------------------------------------------------------------

def _has_sufficient_evidence(demo: ExternalDemonstration) -> bool:
    """At least one action must have BOTH non-empty inputs AND non-empty
    outputs — an observed technique, not an instruction."""
    return any(bool(a.inputs) and bool(a.outputs) for a in demo.actions)


def _objective_overlap(objective: str, capabilities: List[str]) -> bool:
    """Case-insensitive overlap between the objective and ACTIVE capability
    names: either a capability name is a substring of the objective, or any
    objective word of >=4 chars is a substring of a capability name."""
    obj = objective.lower()
    words = [w for w in obj.split() if len(w) >= 4]
    for cap in capabilities:
        cap_l = cap.lower()
        if cap_l and cap_l in obj:
            return True
        if any(w in cap_l for w in words):
            return True
    return False


def _objective_covered(demo: ExternalDemonstration,
                       inventory: InventorySnapshot,
                       z_check: Any = None) -> tuple:
    """Decide whether the demo's objective is already covered.

    When a `z_check` callable is supplied -- (objective, evidence) ->
    result with a `.reached` attribute, e.g. unified_memory.attempt_z --
    the demonstrated behavior DEFINES Z: a bounded PURE-sandboxed planner
    attempt is executed against the demo's observed I/O, and the objective
    counts as covered only if the planner reproduces that behavior.
    Without a z_check, falls back to the lexical _objective_overlap
    heuristic (Q5 behavior, preserved for backward compatibility).
    Returns (covered: bool, detail: dict).
    """
    if z_check is None:
        return _objective_overlap(demo.objective, inventory.capabilities), {}
    # Behavioral Z: evidence comes from the demo's own observed actions,
    # using the same single-key-unwrap convention as the Z-check layer.
    from swarm_engine.intellect.unified_memory import evidence_from_demo_actions
    pairs = evidence_from_demo_actions(demo.actions)
    try:
        res = z_check(demo.objective, pairs)
    except Exception as exc:  # fail closed: an undecidable Z is a gap
        return False, {"z_error": repr(exc)}
    detail = {"reached": bool(getattr(res, "reached", False)),
              "classification": getattr(res, "classification", "?"),
              "strategy": getattr(res, "strategy", "?"),
              "ops_used": list(getattr(res, "ops_used", []) or [])}
    return bool(getattr(res, "reached", False)), detail


def ingest_external_demonstration(demo: ExternalDemonstration,
                                  inventory: InventorySnapshot,
                                  epistemic: "EpistemicStore",
                                  evidence_store: Any = None,
                                  z_check: Any = None) -> IngestResult:
    """Ingest an external demonstration through the causal-discipline gate.

    Gate order: (1) sufficient evidence; (2) observable gap. On a gap, the Y
    record and the delta are persisted via the frozen EpistemicStore API,
    plus a demonstration Evidence entry whose id populates delta.E. If an
    evidence_store is provided (anything with add_entry(kind, text, source)),
    the delta summary is mirrored there so the Evidence view surfaces it.

    `z_check`, when supplied, replaces the lexical _objective_overlap
    decision at gate 2 with a bounded PURE-sandboxed planner attempt whose
    demonstrated behavior defines Z (see _objective_covered). A behavioral
    gap -- the planner cannot reproduce the demo's observed I/O -- is an
    observable gap and records a delta.
    """
    # Gate 1: sufficient evidence.
    if not _has_sufficient_evidence(demo):
        return IngestResult(recorded=False, reason="insufficient_evidence",
                            y_id=None, delta_id=None, detail={})

    # Gate 2: observable gap.
    vocab = inventory.vocabulary()
    missing = sorted(set(demo.required_tools) - vocab)
    detail: Dict[str, Any] = {}
    if missing:
        gap = f"technique requires {missing}, absent from inventory"
    else:
        covered, z_detail = _objective_covered(demo, inventory, z_check)
        if z_detail:
            detail["z_check"] = z_detail
        if not covered:
            gap = ("novel composition: all tools known but no ACTIVE "
                   "capability covers objective")
        else:
            return IngestResult(recorded=False, reason="no_observable_gap",
                                y_id=None, delta_id=None, detail=detail)

    # Persist via the unified-memory facade (lazy import keeps this module
    # light). The y record, the delta record, and the demonstration
    # evidence all go through the facade's canonical write path: the
    # original demonstration provenance is preserved inside the raw
    # payload, and the facade adds its canonical _provenance block.
    from swarm_engine.intellect.epistemic import Evidence
    from swarm_engine.intellect.unified_memory import (
        record_experience, record_evidence)

    y = YRecord(
        y_id=f"y_{uuid4().hex[:12]}",
        source=demo.source,
        objective=demo.objective,
        actions=[asdict(a) for a in demo.actions],
        outcome=demo.outcome,
        at=demo.at,
    )
    delta_id = f"delta_{uuid4().hex[:12]}"
    evidence_id = f"ev_{uuid4().hex[:12]}"
    delta = DeltaRecord(
        delta_id=delta_id,
        X=demo.objective,
        Y={"y_id": y.y_id, "action_count": len(demo.actions),
           "source": demo.source},
        Z={"capabilities": list(inventory.capabilities),
           "primitives": list(inventory.primitives),
           "vocabulary_size": len(vocab), "at": inventory.at},
        gap=gap,
        T={"technique_name": demo.objective,
           "required_tools": sorted(demo.required_tools)},
        E=[evidence_id],
        D=[],
        V={"status": "unverified", "method": None},
        C=None,
    )
    record_experience(
        epistemic,
        origin_loop="acquisition",
        kind="technique_y",
        content=f"external technique demonstration: {demo.objective}",
        raw={"y_record": y.as_dict(), "provenance": dict(demo.provenance)},
        source="technique_y",
    )
    record_experience(
        epistemic,
        origin_loop="acquisition",
        kind="technique_delta",
        content=f"technique delta (Y-Z): {delta_id} for {demo.objective}",
        raw={"delta": delta.as_dict()},
        causal_chain=[y.y_id],
        source="technique_delta",
    )
    record_evidence(
        epistemic,
        origin_loop="acquisition",
        kind="demonstration_evidence",
        evidence=Evidence(
            evidence_id=evidence_id,
            target_id=delta_id,
            supports=True,
            content={"kind": "demonstration_evidence",
                     "actions": y.actions,
                     "outcome": demo.outcome},
            source=demo.source,
        ),
        causal_chain=[delta_id],
    )

    if evidence_store is not None:
        # EvidenceStore.add_entry kind is restricted to a whitelist that does
        # not include "technique_delta", so mirror under the legal
        # "observation" kind with the delta kind carried inside the text.
        text = json.dumps({
            "record_kind": "technique_delta",
            "delta_id": delta_id,
            "y_id": y.y_id,
            "objective": demo.objective,
            "gap": gap,
            "source": demo.source,
        })
        evidence_store.add_entry(kind="observation", text=text,
                                 source=demo.source)

    return IngestResult(recorded=True, reason="gap_recorded", y_id=y.y_id,
                        delta_id=delta_id,
                        detail={**detail, "gap": gap})


# ---------------------------------------------------------------------------
# Source adapters
# ---------------------------------------------------------------------------

def _from_structured_trace(record: Dict[str, Any], source: str) -> ExternalDemonstration:
    """Shared shape: steps of {tool|name, inputs, outputs, ok}, an objective,
    and an outcome. A top-level "provenance" dict rides through untouched so
    simulated sources keep their explicit label end to end."""
    trace_id = record.get("trace_id")
    actions: List[ExternalAction] = []
    for step in record.get("steps", []) or []:
        name = step.get("tool") or step.get("name") or ""
        actions.append(ExternalAction(
            kind="tool_call",
            name=str(name),
            inputs=step.get("inputs") or {},
            outputs=step.get("outputs") or {},
            evidence={"ok": step.get("ok"), "trace_id": trace_id},
        ))
    required_tools = sorted({a.name for a in actions if a.name})
    prov = record.get("provenance")
    return ExternalDemonstration(
        source=source,
        objective=record.get("objective", ""),
        actions=actions,
        outcome=record.get("outcome") or {},
        required_tools=required_tools,
        provenance=dict(prov) if isinstance(prov, dict) else {},
    )


def ingest_subagent_trace(trace: Dict[str, Any], inventory: InventorySnapshot,
                           epistemic: "EpistemicStore",
                           evidence_store: Any = None) -> IngestResult:
    """Ingest a subagent execution trace.

    trace: {trace_id, objective, steps: [{tool|name, inputs, outputs, ok}],
    outcome}.
    """
    demo = _from_structured_trace(trace, "subagent_trace")
    return ingest_external_demonstration(demo, inventory, epistemic,
                                         evidence_store)


def ingest_chat_turn(turn: Dict[str, Any], inventory: InventorySnapshot,
                     epistemic: "EpistemicStore",
                     evidence_store: Any = None) -> IngestResult:
    """Ingest a chat turn — but ONLY when it carries demonstrated procedure
    steps. A plain turn (text only, or empty procedure_steps) has no observed
    actions and is refused as insufficient_evidence; the text is deliberately
    NOT NLP-mined for technique claims, per the causal-discipline rule.

    turn: {text, classification (optional), procedure_steps (optional):
    [{step, inputs, outputs}], outcome (optional)}.
    """
    actions: List[ExternalAction] = []
    for s in turn.get("procedure_steps") or []:
        actions.append(ExternalAction(
            kind="procedure_step",
            name=str(s.get("step") or ""),
            inputs=s.get("inputs") or {},
            outputs=s.get("outputs") or {},
            evidence={},
        ))
    required_tools = sorted({a.name for a in actions if a.name})
    demo = ExternalDemonstration(
        source="chat_turn",
        objective=turn.get("text", ""),
        actions=actions,
        outcome=turn.get("outcome") or {},
        required_tools=required_tools,
    )
    return ingest_external_demonstration(demo, inventory, epistemic,
                                         evidence_store)


def ingest_plugin_action(record: Dict[str, Any], inventory: InventorySnapshot,
                         epistemic: "EpistemicStore",
                         evidence_store: Any = None) -> IngestResult:
    """Ingest a plugin-bot action record. This path is live and accepts the
    same structured record shape as the subagent-trace adapter; only the
    source differs ("plugin_bot").

    Provenance note: no live bot source exists yet — plugin bots are in the
    product vision — so this adapter is proven with synthetic-but-structured
    records, labeled as such. When real bots exist they emit this shape and
    this adapter needs no change.
    """
    demo = _from_structured_trace(record, "plugin_bot")
    return ingest_external_demonstration(demo, inventory, epistemic,
                                         evidence_store)


# ---------------------------------------------------------------------------
# Evidence-kind retrieval (Q5)
# ---------------------------------------------------------------------------

def list_technique_deltas(evidence_store: Any) -> List[Dict[str, Any]]:
    """First-class retrieval of technique deltas from the Evidence view path.

    The EvidenceStore mirror encodes deltas as kind="observation" entries
    whose JSON text carries "record_kind": "technique_delta" (see the
    evidence-kind decision in this module's docstring). This helper returns
    the decoded delta payloads, newest first. Entries that fail to parse as
    JSON are skipped, never fatal — the mirror is an adapter, and a corrupt
    entry must not break the retrieval path.
    """
    result = evidence_store.list_entries(kind="observation", limit=1000)
    if not result.get("ok"):
        return []
    deltas: List[Dict[str, Any]] = []
    for entry in result.get("entries", []) or []:
        try:
            payload = json.loads(entry.get("text") or "")
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(payload, dict) and \
                payload.get("record_kind") == "technique_delta":
            deltas.append(payload)
    return deltas


# ---------------------------------------------------------------------------
# Experience-candidate source (Q5)
# ---------------------------------------------------------------------------

def _candidate_field(cand: Any, key: str, default: Any = None) -> Any:
    if isinstance(cand, dict):
        return cand.get(key, default)
    return getattr(cand, key, default)


def ingest_experience_candidate(candidate: Any, inventory: InventorySnapshot,
                                epistemic: "EpistemicStore",
                                evidence_store: Any = None) -> IngestResult:
    """Ingest an L1 organizational experience candidate as an external
    demonstration.

    An accepted experience candidate (OrganizationalExperience.submit_candidate
    in runtime/agent_org/experience.py) is what an external agent — an
    engine-issued agent identity, not REMOR's native planner — demonstrated:
    technique_name, description, and an io_contract carrying observed inputs
    and outputs. That is exactly the Y material the causal-discipline gate
    requires, produced by real organizational machinery, not a fixture.

    candidate: the ExperienceCandidate dataclass or a dict with
    {candidate_id, agent_id, technique_name, description, problem_class,
    io_contract {inputs, outputs}, evidence_refs, tags}.

    The technique_name is the demonstrated capability: if the inventory
    already covers it (ACTIVE capability or primitive of that name), there
    is no observable gap and nothing is recorded. A candidate whose
    io_contract lacks observed inputs AND outputs is refused as
    insufficient_evidence — a technique claim without demonstrated I/O is
    not a demonstration.
    """
    technique = str(_candidate_field(candidate, "technique_name") or "")
    io_contract = _candidate_field(candidate, "io_contract") or {}
    if not isinstance(io_contract, dict):
        io_contract = {}
    inputs = io_contract.get("inputs") or {}
    outputs = io_contract.get("outputs") or {}
    # Fall back to worked examples when the contract carries none directly.
    if (not inputs or not outputs) and isinstance(
            io_contract.get("examples"), list) and io_contract["examples"]:
        first = io_contract["examples"][0] or {}
        inputs = inputs or first.get("inputs") or first.get("args") or {}
        outputs = outputs or first.get("outputs") or first.get("expected") or {}

    problem_class = str(_candidate_field(candidate, "problem_class") or "")
    objective = (f"{problem_class}: {technique}".strip(": ")
                 or str(_candidate_field(candidate, "description") or "")
                 or technique)
    action = ExternalAction(
        kind="procedure_step",
        name=technique,
        inputs=inputs if isinstance(inputs, dict) else {"value": inputs},
        outputs=outputs if isinstance(outputs, dict) else {"value": outputs},
        evidence={
            "candidate_id": _candidate_field(candidate, "candidate_id"),
            "agent_id": _candidate_field(candidate, "agent_id"),
            "evidence_refs": _candidate_field(candidate, "evidence_refs") or {},
            "code_digest_hint": bool(_candidate_field(candidate, "code")),
        },
    )
    prov = _candidate_field(candidate, "provenance")
    demo = ExternalDemonstration(
        source="experience_candidate",
        objective=objective,
        actions=[action],
        outcome={"description": _candidate_field(candidate, "description") or "",
                 "evidence_refs": _candidate_field(candidate, "evidence_refs") or {}},
        required_tools=[technique] if technique else [],
        provenance=dict(prov) if isinstance(prov, dict) else {},
    )
    return ingest_external_demonstration(demo, inventory, epistemic,
                                         evidence_store)
