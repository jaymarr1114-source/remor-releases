"""Shared enforcement core (STUDENT-ENFORCE-1).

One grant-check location for both the inlet path (ChatService) and the
student serving path (GovernedStudent/router). Previously each had its
own enforcement-state reading and RUNNING check — now unified here.

The enforcement state comes from the authority's store when configured,
or RUNNING (the authority's bootstrap semantic) when not.
"""
from __future__ import annotations

from typing import Callable, Optional

RUNNING = "RUNNING"


def read_enforcement_state(state_dir: Optional[str] = None) -> str:
    """Read the real enforcement state.

    From the store's record when a state dir is configured; RUNNING when
    the authority has no record (its own bootstrap semantic) or when no
    dir is configured. Never raises — returns RUNNING on any read failure
    (fail-open on the read, fail-closed on the check).
    """
    if not state_dir:
        return RUNNING
    try:
        from swarm_engine.governance.curiosity_enforcement.read_api import (
            read_state)
    except Exception:
        return RUNNING
    try:
        rec = read_state(state_dir)
    except Exception:
        return RUNNING
    if rec is None:
        return RUNNING
    state = rec.state.value if hasattr(rec.state, "value") else rec.state
    return str(state)


def make_enforcement_reader(
        state_dir: Optional[str] = None,
        override: Optional[Callable[[], str]] = None,
) -> Callable[[], str]:
    """Build the enforcement-state callable both paths use.

    Override (for tests/operators without a store) takes precedence;
    otherwise reads from the store via read_enforcement_state.
    """
    if override is not None:
        return override
    return lambda: read_enforcement_state(state_dir)


def is_running(state: str) -> bool:
    """True iff the enforcement state permits work."""
    return state == RUNNING


def check_running(state: str) -> Optional[str]:
    """Return None if RUNNING, else the refusal reason.

    Both paths fail closed with the real reason when not RUNNING.
    """
    if is_running(state):
        return None
    return (
        f"enforcement state is {state!r} (was {RUNNING} at check); "
        f"work refused, zero allocation while enforcement is not {RUNNING}"
    )
