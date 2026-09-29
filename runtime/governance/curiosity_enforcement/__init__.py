"""Pull-only read surface for the curiosity enforcement mechanism.

This is the ONLY surface importable from the curiosity domain: it exposes
state reads, the rollback-directive status, and kill-ledger reads. There is no
mutation name here -- no clear, reset, override, transition, or re-enable is
reachable through this package namespace. Mutation lives in the private,
domain-guarded modules (``_engine``, ``l3_combiner``, ``_persistence``
writers) and refuses curiosity-domain callers at import time and at call time.
"""

from __future__ import annotations

from typing import List, Optional

from .read_api import (
    read_kill_ledger,
    read_rollback_status,
    read_state,
    verify_kill_ledger,
)
from .states import (
    DOMAIN,
    ISSUER_ENFORCEMENT,
    ISSUER_FRM,
    ISSUER_JAMES,
    ISSUER_PRIMARY,
    ISSUER_SAFETY_AUTHORITY,
    KILL_LEDGER_RECORD_TYPE,
    EnforcementRecord,
    EnforcementState,
    RollbackDirective,
)

__all__ = [
    "DOMAIN",
    "ISSUER_ENFORCEMENT",
    "ISSUER_FRM",
    "ISSUER_JAMES",
    "ISSUER_PRIMARY",
    "ISSUER_SAFETY_AUTHORITY",
    "KILL_LEDGER_RECORD_TYPE",
    "EnforcementRecord",
    "EnforcementState",
    "RollbackDirective",
    "read_state",
    "read_rollback_status",
    "read_kill_ledger",
    "verify_kill_ledger",
]
