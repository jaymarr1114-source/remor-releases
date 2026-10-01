"""swarm_engine/services/availability.py

GET /api/availability/coming-soon -- the data-driven inventory behind the
GUI's Coming Soon page (design batch: app/static/coming_soon.js).

Every entry names a genuinely-unavailable capability in the shared typed-
unavailability shape from contract_types.py:

    {"ok": False, "unavailable": {"code", "reason", "gui": "coming_soon",
                                  "missing_substrate"}}

Because the listing itself is an available read, each entry carries the
`unavailable` fields nested under "unavailable", plus:

  kind        -- the frontend coming-soon kind this entry feeds. Covers
                 the 5 design-batch registry kinds (remote-dispatch,
                 evidence-docs, external-bots, song-synthesis,
                 project-routing); the remaining entries use consistently
                 namespaced new kinds the GUI can adopt.
  feature     -- human title for the Coming Soon page.
  route       -- {"method", "path"} of the REAL endpoint that returns the
                 typed 501 payload for this capability, or null when the
                 capability is genuinely ABSENT (no route exists at all).
  probe       -- the request body that drives the route to its typed
                 payload (path variables are merged by dispatch); used by
                 tests, documented here so the listing stays checkable.
  http_status -- the HTTP status the real endpoint returns (501), or null
                 for absent capabilities.

Nothing here simulates. Every route-backed entry is verified by
tests/test_coming_soon.py, which drives the real route and asserts the
entry's code/reason/missing_substrate EXACTLY match the live handler
output -- the test fails on drift, so the listing cannot go stale
silently. Absent entries are verified by route-table scans plus the
corresponding honest-absence evidence (404s on plausible paths; fresh
stores returning honest emptiness).

EXTENSION HOOK (sibling task: NL intent-dispatch): if POST
/api/intent/dispatch lands HONESTLY-UNAVAILABLE outcomes, append their
entries to EXTRA_ENTRIES below (same shape) before finalizing; the
endpoint merges them into the listing and test_coming_soon.py verifies
them the same way. Do NOT invent entries here without a real backing
route or a verified genuine absence.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# ----------------------------------------------------------------------
# Extension hook for the NL intent-dispatch sibling task. Entries appended
# here (same shape as ENTRIES) are merged into the listing and contract-
# tested identically. Empty until the sibling's HONESTLY-UNAVAILABLE
# NL outcomes land.
# ----------------------------------------------------------------------
EXTRA_ENTRIES: List[Dict[str, Any]] = []


def register_extension_entries(
        entries: List[Dict[str, Any]]) -> None:
    """Append sibling-task entries to the coming-soon listing.

    Each entry must use the same shape as ENTRIES (kind, feature, route,
    probe, http_status, unavailable{...}). Entries are verified by
    tests/test_coming_soon.py -- a code with no real backing fails the
    suite, so only genuinely-unavailable capabilities belong here.
    """
    for entry in entries:
        _validate_entry(entry)
    EXTRA_ENTRIES.extend(entries)


def _validate_entry(entry: Dict[str, Any]) -> None:
    required = {"kind", "feature", "route", "probe", "http_status",
                "unavailable"}
    missing = required - set(entry)
    if missing:
        raise ValueError(f"coming-soon entry missing fields {missing}")
    un = entry["unavailable"]
    for field in ("code", "reason", "missing_substrate"):
        if not un.get(field):
            raise ValueError(
                f"coming-soon entry {entry.get('kind')!r}: "
                f"unavailable.{field} must be non-empty")
    if un.get("gui") != "coming_soon":
        raise ValueError(
            f"coming-soon entry {entry.get('kind')!r}: "
            "unavailable.gui must be 'coming_soon'")
    if entry["route"] is None and entry["http_status"] is not None:
        raise ValueError(
            f"coming-soon entry {entry.get('kind')!r}: absent capability "
            "(route null) must have http_status null")
    if entry["route"] is not None and entry["http_status"] != 501:
        raise ValueError(
            f"coming-soon entry {entry.get('kind')!r}: route-backed "
            "unavailability must map to HTTP 501")


def _entry(kind: str, feature: str, code: str, reason: str,
           missing_substrate: str,
           route: Optional[Dict[str, str]],
           probe: Optional[Dict[str, Any]],
           http_status: Optional[int]) -> Dict[str, Any]:
    entry = {
        "kind": kind,
        "feature": feature,
        "route": route,
        "probe": probe,
        "http_status": http_status,
        "unavailable": {
            "code": code,
            "reason": reason,
            "gui": "coming_soon",
            "missing_substrate": missing_substrate,
        },
    }
    _validate_entry(entry)
    return entry


# ----------------------------------------------------------------------
# The inventory. code/reason/missing_substrate are copied from the real
# handler output (see the source pointers); tests/test_coming_soon.py
# asserts exact equality against the live routes, so any drift in the
# handlers fails the suite until the entry is updated to match.
# ----------------------------------------------------------------------
ENTRIES: List[Dict[str, Any]] = [
    _entry(
        kind="remote-dispatch",
        feature="Remote Dispatch",
        code="remote_dispatch",
        reason=("the backend remote-dispatch route is implemented and "
                "bench-verified for X11 targets (device pairing, explicit "
                "user consent, scoped cursor control with kill switch), but "
                "the GUI Dispatch tab is not bound to it and no general "
                "device substrate exists: there is no user-facing remote "
                "cursor control yet."),
        missing_substrate="GUI Dispatch-tab binding / non-X11 target substrate",
        route=None, probe=None, http_status=None,
    ),
    _entry(
        kind="evidence-docs",
        feature="Evidence documents",
        code="evidence_doc_generation",
        reason=("the backend does not produce AI-authored evidence documents "
                "(soul.md, theory.md, hypothetical inferences.md): no such "
                "files exist anywhere in the runtime and no API emits their "
                "contents. The evidence store only hosts user/agent-authored "
                "documents; the engine never writes them on its own behalf."),
        missing_substrate="engine-authored evidence-document producer",
        route=None, probe=None, http_status=None,
    ),
    _entry(
        kind="project-routing",
        feature="Automatic project routing",
        code="auto_route_conversation",
        reason=("Routing is explicit-operator-only: the runtime has no "
                "topic or intent classifier substrate that could assign a "
                "conversation to a project chat automatically, so any "
                "'auto' answer would be a guess."),
        missing_substrate="intent/topic classifier over conversation content",
        route={"method": "POST", "path": "/api/routing/auto"},
        probe={"conversation_id": "conv-probe"},
        http_status=501,
    ),
    _entry(
        kind="purchase-permanent-agents",
        feature="Purchase permanent agents",
        code="purchase_permanent_agents",
        reason=("no payment or subscription-billing substrate exists in the "
                "runtime: there is no way to charge, no purchase ledger, and "
                "no entitlement store to grant permanent agents against"),
        missing_substrate="payment_provider + purchase_ledger + "
                          "entitlement_store",
        route={"method": "POST", "path": "/api/agents/purchase-permanent"},
        probe={"template_id": "tpl_llm"},
        http_status=501,
    ),
    _entry(
        kind="template-token-balances",
        feature="Per-template token balances",
        code="template_token_balance",
        reason=("no token-measurement substrate exists in the runtime: "
                "tokens are never counted for any template, so no "
                "per-template balance can be reported or enforced"),
        missing_substrate="token_meter",
        route={"method": "GET",
               "path": "/api/agent-templates/tpl_llm_coder_v1/token-balance"},
        probe={},
        http_status=501,
    ),
    _entry(
        kind="llm-template-instantiation",
        feature="LLM-template agent instantiation",
        code="agent_instantiation",
        reason=("template tpl_llm_coder_v1: substrate_kind 'llm' is not "
                "wired in this service (no provider / grant source). "
                "Honest ABSENT: refusing rather than simulating."),
        missing_substrate="instantiable substrate for substrate_kind 'llm'",
        route={"method": "POST", "path": "/api/agents"},
        probe={"template_id": "tpl_llm_coder_v1"},
        http_status=501,
    ),
    _entry(
        kind="non-python-execute",
        feature="Non-Python code execution",
        code="EXECUTE_LANGUAGE_UNSUPPORTED",
        reason=("execute_code was asked for language 'javascript'; only "
                "python executes in the artifact sandbox"),
        missing_substrate=("language runtime registry "
                           "(only the python interpreter exists)"),
        route={"method": "POST", "path": "/api/execute"},
        probe={"code": "console.log(1)", "language": "javascript"},
        http_status=501,
    ),
    _entry(
        kind="token-metering",
        feature="Token-based metering",
        code="metering_token_balance",
        reason=("no token-measurement substrate exists in the runtime: "
                "tokens are never counted for any run or template, so no "
                "token balance can be reported, metered, or enforced"),
        missing_substrate="token_meter",
        route={"method": "GET", "path": "/api/metering/token-balances"},
        probe={},
        http_status=501,
    ),
    _entry(
        kind="chat-turn-metering",
        feature="Chat-turn metering",
        code="chat_turn_metering",
        reason=("no chat substrate exists in these backend services: the "
                "GUI chat is frontend-local, so backend metering cannot "
                "count chat turns. The 50/day plan figure is a draft "
                "constant with no enforcement point"),
        missing_substrate="chat_service",
        route={"method": "GET", "path": "/api/metering/chat-turns"},
        probe={},
        http_status=501,
    ),
]


def build_coming_soon() -> Dict[str, Any]:
    """The GET /api/availability/coming-soon payload.

    The listing itself is available (ok: True); every entry inside is a
    genuinely-unavailable capability in the shared typed shape.
    """
    entries = [dict(e) for e in ENTRIES] + [dict(e) for e in EXTRA_ENTRIES]
    return {"ok": True, "coming_soon": entries, "count": len(entries)}


def routes_for_availability() -> Dict[Any, Any]:
    """{(method, path): handler(body_dict)} for the availability contract."""

    def _get_coming_soon(body: Dict[str, Any]) -> Dict[str, Any]:
        return build_coming_soon()

    return {
        ("GET", "/api/availability/coming-soon"): _get_coming_soon,
    }
