"""CUR-P6D curiosity-frame probe.

This module's ``__name__`` is ``swarm_engine.curiosity.hardening.
p6d_curiosity_probe`` -- a genuine curiosity-domain frame. It deliberately
imports NO enforcement mutation module (the import-time guard would refuse);
instead the proof driver (``__main__``, governance plane) hands it a
constructed engine and it calls mutation from a curiosity frame. The
call-time guard in the persistence layer must fire.

Two probes:
  attempt_reenable_from_curiosity_frame -- engine.re_enable with the
      AUTHORIZED issuer string ("james") for a WARNING_1 -> RUNNING
      transition. The issuer table would allow it; the caller frame must
      not. Expected: DomainSeparationError, state unchanged.
  attempt_ack_from_curiosity_frame -- engine.acknowledge_rollback, which
      calls ensure_external_caller() first thing. Expected:
      DomainSeparationError.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def attempt_reenable_from_curiosity_frame(
    engine: Any,
    domain: str,
    issuer: str,
    reason_refs: Optional[Dict[str, Any]] = None,
) -> Any:
    """Call the real re-enable path from a curiosity-domain frame."""
    return engine.re_enable(domain, issuer, reason_refs or {})


def attempt_ack_from_curiosity_frame(
    engine: Any, domain: str, directive_id: str, ack_ref: str
) -> Any:
    """Call the real rollback-ack write path from a curiosity-domain frame."""
    return engine.acknowledge_rollback(domain, directive_id, ack_ref)
