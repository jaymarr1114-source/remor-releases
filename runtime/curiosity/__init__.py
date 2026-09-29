"""Curiosity domain package (runtime/curiosity/).

Phase 1 of the Curiosity Executive plan (James, 2026-09-29). Everything under
this package is the *curiosity domain* side of the charter boundary:

  * writers for the fenced Curiosity Evidence Store (C-7) live here
    (runtime/curiosity/evidence/),
  * the GAM roll-call channel (schemas, scheduler, classifier,
    append-only attestation ledger, pull-only exposure, Phase-1
    test-double responders) lives here (runtime/curiosity/rollcall/),
  * Primary-side code NEVER writes to curiosity stores; Primary Acceptance
    inspects them read-only (Phase 4+),
  * enforcement machinery (Phase 1, mission CUR-P1B) lives OUTSIDE this
    package and offers no mutation API importable from here — by
    construction.

The domain fence is enforced at runtime by the writer layer
(runtime/curiosity/evidence/writer.py), not just by documentation.
"""
from __future__ import annotations

#: The import-path root of the curiosity domain. The domain fence in
#: writer.py admits callers only from modules under this root (or from the
#: writer/stub modules themselves); anything else is refused loudly.
CURIOSITY_PACKAGE_ROOT = __name__

__all__ = ["CURIOSITY_PACKAGE_ROOT"]
