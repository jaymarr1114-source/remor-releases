"""Pull-only reads over enforcement state. Unguarded by design: exposure is
read-only, so it is safe (and required) to be reachable from anywhere,
including the curiosity domain."""

from __future__ import annotations

from typing import List, Optional, Tuple

from ._persistence import KillLedger, StateStore
from .states import DOMAIN, EnforcementRecord, RollbackDirective


def read_state(
    state_dir: str, domain: str = DOMAIN
) -> Optional[EnforcementRecord]:
    return StateStore(state_dir).read_record(domain)


def read_rollback_status(
    state_dir: str, domain: str = DOMAIN
) -> List[RollbackDirective]:
    return StateStore(state_dir).read_directives(domain)


def read_kill_ledger(state_dir: str, domain: str = DOMAIN) -> List[dict]:
    return KillLedger(state_dir).records(domain)


def verify_kill_ledger(state_dir: str) -> Tuple[bool, str]:
    return KillLedger(state_dir).verify_chain()
