"""Dispatch-path gap wiring (V9-WIRE, 2026-09-27).

The production dispatch failure path registers limitation and
missing-dependency gaps itself, in M7's unified gap registry, with no
operator in the middle. Q12's production limitation recorder is the model:
the system acts on its own observation.

Failure classes (both from REAL dispatch failures, never asserted):
1. Capability limitation -- Q12's recorder (inside NLToolDispatcher's
   failure branches) already quarantines with a structured limitation
   reason. This module reads the LIVE quarantine reason and registers via
   GapRegistry.register_from_diagnosis: real evidence, re-verified live
   against M5's store before the record exists.
2. Missing dependency -- a dispatch whose real failure is a genuine
   ModuleNotFoundError for a genuinely-absent module (verified with
   importlib against the live environment; the module name is never
   trusted from text alone). The module quarantines the capability
   through the governed engine-attributed choke point
   (engine.quarantine_as_engine) with a missing_dependency reason, then
   registers via register_from_diagnosis -- which dual-registers the
   static dependency inventory row (James's rule: never one without the
   other).

Routing is by record shape (the registry's own dispatcher); the wiring
only registers. The returned outcome carries the route the registry
selected, so the loop's turn is observable.

Idempotency: an open gap already covering the same capability+reason is
returned, never duplicated -- repeated failures of the same dispatch do
not spam the registry.

Fail-closed: the wiring NEVER breaks the dispatch. Every internal
exception is caught and reported in the returned outcome dict; the
dispatch result is returned to the caller unchanged.

Q9 LANDED 2026-09-27 ~14:05 EDT: the inline hook is live inside
NLToolDispatcher._wire_failure_gap -- the three _execute_and_record
failure branches and _dispatch_validated's capability_unavailable
refusal. The service layer calls the dispatcher unchanged; this
module's interface is unchanged by where it is called from.
"""

from __future__ import annotations

import importlib.util
import re
from typing import Any, Dict, List, Optional

REGISTERED_BY = "v9-wire:dispatch-failure-path"

# Genuine ModuleNotFoundError text, e.g.
#   "No module named 'piper'"
#   'No module named "PIL"'
#   "failed to load voice 'default': No module named 'piper'"
# The name is a CANDIDATE until importlib confirms absence -- text alone
# never authorizes a quarantine.
_NO_MODULE_NAMED_RE = re.compile(
    r"No module named ['\"]([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)['\"]"
)


def genuinely_missing_module(failure_text: str) -> Optional[str]:
    """Return the top-level module name when the failure text names a
    module that is GENUINELY absent from the live environment.

    Evidence standard, stated exactly:
      1. the caller only invokes this on a REAL dispatch failure
         (ok=False from the real execution path);
      2. the failure text names a module via the No-module-named pattern;
      3. importlib.util.find_spec on the top-level package confirms
         absence -- a name that resolves is never treated as missing,
         no matter what the text claims.
    The attribution ("module X is the missing dependency") is the
    failure text's own claim, verified against the live environment;
    the quarantine reason records it as observed, never asserting more.
    Returns None when no candidate name is found or every candidate
    resolves.
    """
    if not failure_text:
        return None
    for match in _NO_MODULE_NAMED_RE.finditer(failure_text):
        dotted = match.group(1)
        top = dotted.split(".")[0]
        try:
            spec = importlib.util.find_spec(top)
        except Exception:
            # An unimportable-by-construction name (e.g. empty or with
            # pathological characters) is not evidence of a missing
            # dependency -- fail closed.
            continue
        if spec is None:
            return top
    return None


def _failure_texts(result: Any) -> List[str]:
    texts: List[str] = []
    refusal = getattr(result, "refusal", None)
    if refusal:
        texts.append(str(refusal))
    for reason in getattr(result, "reasons", None) or []:
        texts.append(str(reason))
    return texts


def _open_gap_for(registry: Any, capability_id: str,
                  reason: str) -> Optional[Any]:
    """An already-open gap covering this capability+reason, if one exists."""
    try:
        open_gaps = registry.list_gaps(status="open")
    except Exception:
        return None
    for gap in open_gaps or []:
        q = getattr(gap, "quarantine", None)
        if q is None:
            continue
        if getattr(q, "capability_id", None) != capability_id:
            continue
        if (getattr(q, "reason", "") or "") == reason:
            return gap
    return None


def maybe_register_dispatch_gap(engine: Any, result: Any, *,
                                registered_by: str = REGISTERED_BY
                                ) -> Dict[str, Any]:
    """Inspect a failed DispatchResult; register+route a gap when the
    failure is a genuine limitation or missing dependency.

    Returns an outcome dict; never raises. ``registered`` is True only
    when a gap record now covers this failure (newly created or an
    already-open duplicate, flagged ``duplicate_of_open``).
    """
    try:
        return _maybe_register(engine, result,
                               registered_by=registered_by)
    except Exception as exc:  # noqa: BLE001 -- wiring never breaks dispatch
        return {"registered": False,
                "reason": "wiring error: %r" % (exc,)}


def _maybe_register(engine: Any, result: Any,
                    registered_by: str) -> Dict[str, Any]:
    # Local imports: gaps.py is M7's frozen file -- called, never edited.
    from swarm_engine.acquisition.gaps import GapRegistry
    from swarm_engine.synthesis.integrity import (
        get_quarantine_reason, parse_limitation_reason)
    from swarm_engine.acquisition.substrate import parse_dependency_reason

    if getattr(result, "ok", False):
        return {"registered": False,
                "reason": "dispatch ok: nothing to register"}
    capability_id = getattr(result, "capability_id", None)
    if not capability_id:
        return {"registered": False,
                "reason": "no capability_id on the failed dispatch"}

    registry = GapRegistry(engine)

    qr = get_quarantine_reason(capability_id, engine=engine) or {}
    reason = qr.get("reason") or ""

    limitation = parse_limitation_reason(reason) if reason else None
    dependency = parse_dependency_reason(reason) if reason else None

    if limitation is None and dependency is None:
        # No recorded reason names a limitation or a missing dependency.
        # Check the failure itself for a GENUINE missing module -- the
        # dispatch path does not quarantine those (Q12's recorder only
        # handles declared-bound limitations), so the wiring records the
        # system's own observation through the governed quarantine path
        # before registering.
        failure_text = "\n".join(_failure_texts(result))
        missing = genuinely_missing_module(failure_text)
        if missing is None:
            return {"registered": False,
                    "reason": "no limitation or missing-dependency "
                              "evidence: not registered (the registry's "
                              "evidence refusal would bite here)"}
        dep_reason = (
            "missing_dependency: package: %s: dispatch failed and the "
            "failure names module %r as unimportable; importlib confirms "
            "it is absent from the live environment" % (missing, missing))
        try:
            engine.quarantine_as_engine(capability_id, dep_reason)
        except Exception as exc:
            return {"registered": False,
                    "reason": "governed quarantine refused: %r "
                              "(no gap registered without it)" % (exc,)}
        qr = get_quarantine_reason(capability_id, engine=engine) or {}
        reason = qr.get("reason") or ""
        dependency = parse_dependency_reason(reason) if reason else None
        if dependency is None:
            return {"registered": False,
                    "reason": "quarantined but the recorded reason is not "
                              "dependency-shaped: not registered"}

    # Idempotency: an open gap for this capability+reason already covers
    # the failure -- return it, never duplicate.
    existing = _open_gap_for(registry, capability_id, reason)
    if existing is not None:
        return {"registered": True, "duplicate_of_open": True,
                "gap_id": existing.gap_id,
                "route": getattr(existing, "route_name", None),
                "reason": "an open gap already covers this failure"}

    record = registry.register_from_diagnosis(
        capability_id, registered_by=registered_by)
    routed = registry.dispatch(record.gap_id)
    return {"registered": True,
            "gap_id": record.gap_id,
            "route": routed.route_name,
            "outcome": routed.outcome,
            "detail": (routed.detail or "")[:300]}
