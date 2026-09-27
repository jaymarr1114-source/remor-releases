"""Shared typed-unavailability shape for backend-contract workers.

Every contract that cannot be served names the missing substrate honestly
instead of simulating it. This shape is identical to the one specified in the
mission packet so all workers' responses are uniform.
"""
from __future__ import annotations

from typing import Any, Dict


def contract_unavailable(code: str, reason: str,
                         missing_substrate: str) -> Dict[str, Any]:
    """Return the shared unavailable-contract payload.

    `code` is a short machine-readable contract code (e.g.
    "capabilities.developer_auth"), `reason` is a human explanation of why
    this cannot be served, and `missing_substrate` names the runtime piece
    that does not exist.
    """
    return {
        "ok": False,
        "unavailable": {
            "code": code,
            "reason": reason,
            "gui": "coming_soon",
            "missing_substrate": missing_substrate,
        },
    }


def contract_limit(code: str, limit: int, current: int,
                   reset: str) -> Dict[str, Any]:
    """Return the shared quota-refusal payload.

    A real mechanism enforced `code`: `limit` is the cap, `current` is the
    measured value that hit it, `reset` describes how the quota frees up.
    Always causal -- the code named here is enforced by reading real state
    (registry rows, sqlite counts), never by a test-time stub.
    """
    return {
        "ok": False,
        "limit": {
            "code": code,
            "limit": limit,
            "current": current,
            "reset": reset,
        },
    }


# ---------------------------------------------------------------------
# route-table dispatch
# ---------------------------------------------------------------------
# Each contract module exposes routes_*() returning
# {("METHOD", "/path/with/{var}"): handler}. dispatch() matches a request
# against the table, merges {var} path params into the body dict, and calls
# the handler(body_dict) -> JSON-serializable dict, so the GUI server can
# adopt handlers verbatim.

from typing import Callable, Optional, Tuple  # noqa: E402

RouteKey = Tuple[str, str]
Handler = Callable[[Dict[str, Any]], Dict[str, Any]]


def _match(pattern: str, path: str) -> Optional[Dict[str, str]]:
    """Match '/api/agents/{agent_id}' against a real path.

    Returns the {var} bindings, or None if the pattern does not match.
    """
    p_segs = [s for s in pattern.split("/") if s]
    r_segs = [s for s in path.split("/") if s]
    if len(p_segs) != len(r_segs):
        return None
    binds: Dict[str, str] = {}
    for p, r in zip(p_segs, r_segs):
        if p.startswith("{") and p.endswith("}"):
            binds[p[1:-1]] = r
        elif p != r:
            return None
    return binds


def dispatch(routes: Dict[RouteKey, Handler], method: str, path: str,
             body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Route one request through a contract's route table.

    Path variables are merged into a copy of `body` before the handler
    runs, so handlers take exactly one argument. Unknown routes get an
    honest 404-shaped payload.
    """
    method = method.upper()
    for (m, pattern), handler in routes.items():
        if m != method:
            continue
        binds = _match(pattern, path)
        if binds is not None:
            merged = dict(body or {})
            merged.update(binds)
            return handler(merged)
    return {
        "ok": False,
        "error": f"no route for {method} {path}",
        "http_status": 404,
    }
