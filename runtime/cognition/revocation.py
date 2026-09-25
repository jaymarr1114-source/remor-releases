"""Evidence-driven capability revocation.

The missing causal link the revocation mission exists to build: when the
grounding lexicon's evidence adjudication contradicts a learned law, the
executable capabilities admitted *from* that law must lose their
epistemic standing too -- not by a test calling a revoke() method, but
because the persisted epistemic state says so.

Lifecycle:
  1. export_grounding_capability() admits a plan capability and records
     PROVENANCE: (capability_id -> predicate, semantic_id) -- which
     governed semantic capability this executable was admitted from.
  2. Later, learn() with genuinely contradictory evidence moves the
     lexicon entry to CONFLICTED (the existing evidence-caused semantic
     transition; see GroundingLexicon._mark_conflict).
  3. sync_epistemic_revocation(engine, lexicon) derives, from the
     persisted lexicon state crossed with the provenance table, which
     admitted executables are no longer epistemically justified, moves
     them to quarantined (so normal reuse machinery -- resolve_goal,
     find_compatible, boot rehydration -- refuses them), unregisters
     their `acquired.<id>` primitives, and propagates the invalidation
     through the REAL dependency graph: plan operations of the form
     `acquired.<capability_id>` (transitive closure, fixpoint).

Nothing here selects targets: the sync takes no capability ids, no
predicates, no reasons. Every invalidation is derived from persisted
epistemic state. Integrity failure (a corrupted executable: plan
fingerprint mismatch, missing primitives) and epistemic invalidation
(an intact executable whose justification was contradicted) remain
separate mechanisms with separate event records -- they share the
quarantined terminal status because both mean "do not use", but the
capability event log distinguishes the cause.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Set

from swarm_engine.cognition.grounding_lexicon import (
    STATUS_CONFLICTED,
    STATUS_CORRUPT,
    STATUS_LEARNED,
)

# Statuses whose governing entry can no longer justify an admitted
# executable. AMBIGUOUS is deliberately absent: ambiguity means the old
# program still fits all evidence (the interpretation is uncertain, not
# contradicted), so revoking it would be premature.
_INVALID_ENTRY_STATUSES = {STATUS_CONFLICTED, STATUS_CORRUPT}

_PROVENANCE_DDL = """
CREATE TABLE IF NOT EXISTS grounding_asset_provenance (
  capability_id TEXT PRIMARY KEY,
  predicate     TEXT NOT NULL,
  semantic_id   TEXT,
  lexicon_db    TEXT,
  created_at    REAL NOT NULL)
"""


def record_grounding_provenance(store: Any, capability_id: str,
                                predicate: str,
                                semantic_id: Optional[str],
                                lexicon_db: Optional[str] = None) -> None:
    """Persist which governed semantic capability an admitted executable
    was admitted from. Called once, at export/admission time, by
    grounding_assets.export_grounding_capability()."""
    with store._conn() as c:
        c.execute(_PROVENANCE_DDL)
        c.execute(
            "INSERT OR REPLACE INTO grounding_asset_provenance "
            "(capability_id, predicate, semantic_id, lexicon_db, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (capability_id, predicate, semantic_id, lexicon_db, time.time()))


def read_grounding_provenance(store: Any) -> List[Dict[str, Any]]:
    """All provenance rows (empty when the table was never created)."""
    with store._conn() as c:
        c.execute(_PROVENANCE_DDL)
        rows = c.execute(
            "SELECT capability_id, predicate, semantic_id, lexicon_db,"
            " created_at FROM grounding_asset_provenance").fetchall()
    keys = ("capability_id", "predicate", "semantic_id", "lexicon_db",
            "created_at")
    return [dict(zip(keys, r)) for r in rows]


def _assess_entry(entry: Any, semantic_id: Optional[str]) -> Optional[str]:
    """Why a law-derived executable lost its epistemic standing, or None
    if it stands.

    The single rule for every law-derived artifact (admitted capabilities
    and promoted primitives alike): it is valid iff the lexicon still
    carries a LEARNED entry for its predicate governed by the SAME
    semantic capability id it was derived from. A conflicted/corrupt
    entry, a missing entry, or a semantic-id mismatch (the entry was
    refined/superseded past this artifact's program) invalidates it.
    UNLEARNED/AMBIGUOUS are not contradictions: the old program still
    fits all evidence, so the artifact stands.
    """
    if entry is None:
        return "governing entry absent"
    if entry.status in _INVALID_ENTRY_STATUSES:
        return f"governing entry {entry.status}"
    if entry.status != STATUS_LEARNED:
        return None
    if entry.semantic_id != semantic_id:
        return (f"governing capability superseded "
                f"(derived from {semantic_id}, "
                f"entry now {entry.semantic_id})")
    return None


def plan_acquired_refs(plan: Any, registry: Any = None) -> Set[str]:
    """Capability ids this plan depends on, from its persisted structure.

    A composed plan calls child capabilities through operations named
    `acquired.<capability_id>`; this walks the whole plan (steps,
    branches, nested lambdas) and collects the referenced ids. Pure
    function of the stored plan -- the existing dependency graph, not a
    second one.

    2026-09-19: when a registry is supplied, bare op names that resolve
    to tagged acquired primitives (e.g. acquired_code-path capabilities
    registered under their node name, tagged in the registry's
    `_acquired_capability_ids` index) are resolved to their backing
    capability ids too. Without a registry the historical
    `acquired.`-prefix behavior is preserved.
    """
    refs: Set[str] = set()
    tag_index = (getattr(registry, "_acquired_capability_ids", None)
                 if registry is not None else None)
    if not isinstance(tag_index, dict):
        tag_index = None

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            op = node.get("op")
            if isinstance(op, str):
                if op.startswith("acquired."):
                    refs.add(op[len("acquired."):])
                elif tag_index is not None and op in tag_index:
                    refs.add(tag_index[op])
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v)

    _walk(plan or {})
    return refs


def _quarantine(store: Any, registry: Any, cap_id: str, reason: str,
                detail: Dict[str, Any]) -> bool:
    """Move one record to quarantined and unregister its primitive.

    Returns True when a transition happened (record was active).

    2026-09-19 (R11): unregisters EVERY primitive name bound to this
    capability id, not just `acquired.<id>`. The orchestrator registers a
    node-name alias (e.g. `vub_the_value`) for run-acquired nodes, tracked
    in the registry's `_acquired_capability_ids` tag index; leaving the
    alias registered meant a quarantined dependent stayed resolvable and
    executable in-process (only a fresh process, which skips quarantined
    rows at boot, saw it as invalid).
    """
    rec = store.get(cap_id)
    if rec is None or rec.status != "active":
        return False
    payload = dict(detail)
    payload["reason"] = reason
    payload["at"] = time.time()
    store.set_status(cap_id, "quarantined")
    # The event name IS the mechanism: epistemic_revocation (contradicted
    # evidence) vs dependency_revocation (invalidated through a revoked
    # dependency) vs corruption_detected (integrity, logged by admission).
    store.log(cap_id, reason, json.dumps(payload))
    tag_index = getattr(registry, "_acquired_capability_ids", None)
    names = [f"acquired.{cap_id}"]
    if isinstance(tag_index, dict):
        names.extend(k for k, v in tag_index.items()
                     if v == cap_id and k not in names)
    for prim_name in names:
        try:
            registry.unregister(prim_name)
        except Exception:
            pass
        if isinstance(tag_index, dict):
            tag_index.pop(prim_name, None)
    return True


def plan_op_names(plan: Any) -> Set[str]:
    """Every op name string appearing anywhere in a plan's structure.

    Used to catch plans that call a revoked promoted primitive by its
    bare registered name (promoted primitives are invoked as plain ops,
    not through the `acquired.` prefix).
    """
    names: Set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            op = node.get("op")
            if isinstance(op, str):
                names.add(op)
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v)

    _walk(plan or {})
    return names


def _quarantine_promoted(promoter: Any, registry: Any, record: Any,
                         reasons: List[Optional[str]]) -> bool:
    """Move one promoted primitive to quarantined and unregister it.

    A law-derived promotion loses standing iff EVERY law link is invalid
    by the same rule as admitted capabilities (no remaining valid
    justification). Law-agnostic promotions (no links) never reach here.
    Returns True when a transition happened.
    """
    if record.status != "active":
        return False
    detail = {"reason": "epistemic_revocation",
              "law_links": list(record.law_links),
              "link_reasons": list(reasons),
              "at": time.time()}
    try:
        registry.unregister(record.name)
    except Exception:
        pass
    record.status = "quarantined"
    record.quarantine_detail = detail
    promoter.store.save(record)
    return True


def propagate_plan_quarantine(store: Any, registry: Any,
                               seed_invalid_ids: Set[str],
                               cause: str = "epistemic",
                               invalid_op_names: Set[str] = frozenset()
                               ) -> Dict[str, Any]:
    """Fixpoint: quarantine every ACTIVE plan whose persisted plan calls an
    invalidated capability, and unregister its primitive so execution
    fails fast instead of running a broken composition.

    Dependencies come from the persisted plan graph
    (`plan_acquired_refs`, which resolves both `acquired.<id>` ops and
    tagged bare acquired names when given the registry) plus
    `invalid_op_names` (bare op names such as revoked promoted
    primitives, matched via `plan_op_names`). No ids, names, predicates,
    or reasons are selected here -- the seed set is the only input.

    cause="epistemic" logs `dependency_revocation` (the epistemic guard
    treats these as withdrawn justification: identical re-admission is
    refused without vindication). cause="deliberate" logs
    `dependency_quarantined` (derived state: eligible for automatic
    recovery once the dependencies are present again, and NOT treated
    as an epistemic withdrawal).
    """
    event = ("dependency_revocation" if cause == "epistemic"
             else "dependency_quarantined")
    invalid: Set[str] = set(seed_invalid_ids)
    propagated: List[str] = []
    frontier: Set[str] = set(seed_invalid_ids)
    while frontier:
        next_frontier: Set[str] = set()
        for rec in store.list(status="active", limit=100000):
            if rec.capability_id in invalid:
                continue
            try:
                deps = plan_acquired_refs(rec.plan, registry)
                ops = plan_op_names(rec.plan)
            except Exception:
                continue
            hit = deps & invalid
            name_hit = ops & set(invalid_op_names)
            if not hit and not name_hit:
                continue
            detail = {"depends_on_invalid": sorted(hit),
                      "depends_on_invalid_ops": sorted(name_hit),
                      "all_acquired_refs": sorted(deps),
                      "cause": cause}
            if _quarantine(store, registry, rec.capability_id,
                           event, detail):
                propagated.append(rec.capability_id)
                next_frontier.add(rec.capability_id)
        invalid |= next_frontier
        frontier = next_frontier
    return {"propagated": propagated, "invalid_ids": sorted(invalid)}


def sync_epistemic_revocation(engine: Any, lexicon: Any) -> Dict[str, Any]:
    """Derive and apply epistemic invalidations from persisted state.

    For every grounding-asset provenance row, the admitted executable is
    epistemically valid iff the lexicon still carries a LEARNED entry for
    its predicate governed by the SAME semantic capability id the asset
    was admitted from. A conflicted/corrupt entry, a missing entry, or a
    semantic-id mismatch (the entry was refined/superseded past this
    asset's program) invalidates the asset. Invalid assets are
    quarantined and their primitives unregistered.

    Promoted primitives are assessed by the same rule through their
    law links: a law-derived promotion stands iff AT LEAST ONE linked
    law is still valid; when every link is invalid it is quarantined
    and unregistered. Promotions with no law links (law-agnostic,
    e.g. from cognition syntheses) are never touched here.

    Then the invalidation propagates through the persisted
    plan-dependency graph to a fixpoint: plans calling invalidated
    capabilities (`acquired.<id>` ops) or revoked promoted primitives
    (bare op names) are themselves quarantined.

    Takes no targets. Idempotent: already-non-active records are
    reported, not re-logged.
    """
    store = engine.capabilities
    registry = engine.primitives
    report: Dict[str, Any] = {
        "revoked": [], "propagated": [], "already_inactive": [],
        "record_absent": [], "invalid_entries": 0,
        "revoked_promoted": [],
    }
    invalid_ids: Set[str] = set()

    for prov in read_grounding_provenance(store):
        cap_id = prov["capability_id"]
        predicate = prov["predicate"]
        entry = lexicon.get(predicate)
        invalid_reason = _assess_entry(entry, prov["semantic_id"])
        if invalid_reason is None:
            # No contradiction for this asset (including UNLEARNED /
            # AMBIGUOUS entries): leave standing.
            continue
        report["invalid_entries"] += 1
        rec = store.get(cap_id)
        if rec is None:
            report["record_absent"].append(cap_id)
            continue
        if rec.status != "active":
            report["already_inactive"].append(cap_id)
            continue
        detail = {"predicate": predicate,
                  "asset_semantic_id": prov["semantic_id"],
                  "entry_semantic_id": (entry.semantic_id
                                        if entry is not None else None),
                  "entry_status": (entry.status
                                   if entry is not None else None)}
        if _quarantine(store, registry, cap_id, "epistemic_revocation",
                       detail):
            report["revoked"].append(cap_id)
            invalid_ids.add(cap_id)

    # Promoted-primitive phase: close the side channel through which a
    # contradicted law's executable could survive as a promoted
    # primitive. Derived from persisted law links x lexicon state --
    # no names, no predicates selected.
    revoked_promoted: Set[str] = set()
    promoter = getattr(engine, "primitive_promoter", None)
    pstore = getattr(promoter, "store", None) if promoter is not None else None
    if pstore is not None:
        try:
            records = pstore.all()
        except Exception:
            records = []
        for prec in records:
            if getattr(prec, "status", "active") != "active":
                continue
            links = getattr(prec, "law_links", None) or []
            if not links:
                continue  # not law-derived: out of scope for this sync
            reasons = [_assess_entry(lexicon.get(l.get("predicate")),
                                     l.get("semantic_id")) for l in links]
            if not all(reasons):
                continue  # at least one live justification remains
            if _quarantine_promoted(promoter, registry, prec, reasons):
                report["revoked_promoted"].append(prec.name)
                revoked_promoted.add(prec.name)

    # Dependency propagation: any ACTIVE plan that calls an invalidated
    # capability or a revoked promoted primitive is itself no longer
    # executable-as-justified. Fixpoint over the persisted plan graph
    # (no second dependency system).
    prop = propagate_plan_quarantine(
        store, registry, set(invalid_ids), cause="epistemic",
        invalid_op_names=set(revoked_promoted))
    report["propagated"] = prop["propagated"]

    report["unrelated_active"] = len(store.list(status="active",
                                                limit=100000))
    return report
