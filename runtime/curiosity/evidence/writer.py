"""The curiosity-side writer: the SOLE write path into the fenced
Curiosity Evidence Store (C-7).

Domain fence (real mechanism, executed on every write):
  * the store module (store.py) offers NO write API — writes arrive here,
  * `CuriosityWriter.submit()` inspects the call stack: the first caller
    frame outside the evidence package must belong to a module under the
    curiosity domain root (`runtime.curiosity.*`); any other caller gets
    `DomainFenceError` and NOTHING is written,
  * the record is then validated (EvidenceRefused on malformed) and only
    then persisted.

By-construction complements (proved in the battery):
  * the writer class is defined and exported ONLY inside the curiosity
    package — no Primary module imports it (a repo-wide import scan in the
    proof asserts this),
  * a Primary-side module attempting to write (a real module outside
    runtime.curiosity) is refused at runtime with DomainFenceError.

Honest limits (cf. runtime/governance/caller_authorization.py):
  * in-process callers share memory with the engine; this layer does not
    defend against a caller reading another caller's memory or against a
    caller deliberately forging its own frame metadata — it defends the
    real case: Primary-side machinery has no legitimate write API and any
    accidental or interface-level write attempt fails loudly.
"""
from __future__ import annotations

import inspect
from typing import Optional

from . import store as _store_module  # noqa: F401  (frame-skip identity)
from .records import CuriosityFinding, DomainFenceError
from .store import CuriosityEvidenceStore
from .. import CURIOSITY_PACKAGE_ROOT

# Frames belonging to these modules are the write path itself, never the
# caller whose domain is being checked.
_WRITE_PATH_MODULES = frozenset({
    __name__,                       # writer.py
    _store_module.__name__,         # store.py
})


def _caller_domain() -> str:
    """The module name of the first caller outside the evidence write path."""
    for frame_info in inspect.stack():
        mod = inspect.getmodule(frame_info.frame)
        name = mod.__name__ if mod is not None else "<unknown>"
        if name not in _WRITE_PATH_MODULES:
            return name
    return "<unknown>"


def require_curiosity_caller() -> str:
    """Fail-closed domain check: the caller must be inside the curiosity
    domain. Returns the caller module name on success."""
    caller = _caller_domain()
    root = CURIOSITY_PACKAGE_ROOT
    if caller != root and not caller.startswith(root + "."):
        raise DomainFenceError(
            f"primary-side write refused: caller {caller!r} is outside the "
            f"curiosity domain ({root}.*); curiosity evidence writers are "
            "curiosity-side only (charter C-7)")
    return caller


class CuriosityWriter:
    """The sole write path into the fenced Curiosity Evidence Store.

    Instantiate ONCE per store and hand to the curiosity-side caller
    (loop stubs in Phase 1; the Curiosity Run Controller in Phase 2).
    There is deliberately no way to bypass the fence: `submit` IS the
    write path, and the fence runs inside it.
    """

    def __init__(self, store: CuriosityEvidenceStore,
                 epistemic: Optional[object] = None):
        self._store = store
        # UNIFIED-MEMORY-1: optional epistemic store for unified visibility.
        # When provided, every fenced submit also flows a visibility record
        # through the unified memory path (record_experience). The fenced
        # store remains the physical substrate and the security boundary;
        # the unified path is the cross-loop visibility facade (gaps.py
        # pattern). The visibility record carries metadata (evidence_id,
        # loop, timestamp), not the finding's full content — the fenced
        # store holds the content; Primary inspects it read-only via the
        # governed P5C path.
        self._epistemic = epistemic

    def submit(self, finding: CuriosityFinding) -> CuriosityFinding:
        """Validate, domain-check, then persist. Raises DomainFenceError
        for out-of-domain callers, EvidenceRefused for malformed records,
        ValueError for duplicate evidence_id. Never partially writes."""
        require_curiosity_caller()
        finding.validate()
        result = self._store._insert(finding)
        # UNIFIED-MEMORY-1: cross-loop visibility through the unified path.
        if self._epistemic is not None:
            try:
                from swarm_engine.intellect.unified_memory import (
                    record_experience)
                record_experience(
                    self._epistemic,
                    origin_loop="curiosity",
                    kind="evidence",
                    content=(f"curiosity finding submitted: "
                             f"{finding.evidence_id}"),
                    raw={"evidence_id": finding.evidence_id,
                         "fenced_store": "curiosity_evidence"},
                    source="curiosity/evidence")
            except Exception:
                pass  # Visibility is advisory; fenced write already done.
        return result
