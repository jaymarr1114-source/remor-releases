"""Contract 2 — Agents: GUI-facing API over the real AgentRegistry.

Everything here executes the real agent-org machinery on real sqlite
stores under a caller-supplied base_dir (scratch in tests, the app's data
dir in production). Nothing is stubbed:

* list_agents / get_agent read the real AgentRegistry rows.
* list_templates reads the real TemplateRegistry (seeded blueprints).
* register_agent goes through the real AgentFactory -> AgentRegistry.register,
  with the free-tier guard (max 3 live agents) enforced in the mechanism:
  a lock serializes check-and-register, so concurrent double-submits cannot
  slip a 4th agent past the count.
* destroy_agent goes through the real AgentRegistry.destroy.

Threading model: the agent-org substrate opens its sqlite connections
thread-affine (default check_same_thread=True) and the substrate files are
not ours to modify, so ALL store-touching calls are marshaled onto one
dedicated DB thread (_SameThreadExecutor). The service-level RLock still
serializes logical operations (check-and-register is atomic across caller
threads); the executor only guarantees sqlite calls happen on a single
thread. The double-submit race test exercises 10 genuinely concurrent
callers against this arrangement.

Honest absences (classified, not simulated):
* "Purchase Permanent Agents": no payment/billing substrate exists anywhere
  in the runtime -> HONESTLY-UNAVAILABLE with code + reason.
* Per-template token balances: no token-measurement substrate exists ->
  HONESTLY-UNAVAILABLE. The template's contracts dict is reported verbatim
  (including resource_requirements where genuinely present); numbers are
  never invented.
* The llm template is a blueprint only: AgentFactory._default_substrate
  raises SubstrateUnavailable for substrate_kind "llm", and register_agent
  surfaces that as an unavailable payload rather than faking an instance.
"""
from __future__ import annotations

import os
import queue
import threading
from typing import Any, Callable, Dict, List, Optional, TypeVar

from swarm_engine.agent_org.exceptions import LifecycleError
from swarm_engine.agent_org.factory import AgentFactory
from swarm_engine.agent_org.identity import AgentRecord
from swarm_engine.agent_org.registry import AgentRegistry, TERMINAL
from swarm_engine.agent_org.store import OrgStore
from swarm_engine.agent_org.substrates import SubstrateUnavailable
from swarm_engine.agent_org.templates import (
    AgentTemplate,
    TemplateRegistry,
    seed_templates,
)
from swarm_engine.governance.oracle_binding import OracleRegistry
from swarm_engine.services.contract_types import (
    contract_limit,
    contract_unavailable,
    dispatch,
)

# Free-tier plan constants. These are plan-driven caps enforced here; the
# "purchase more" path that would raise them does not exist (see
# purchase_permanent_agent).
MAX_AGENTS_FREE = 3

AGENT_CAP_CODE = "free_tier_agent_cap"
PURCHASE_CODE = "purchase_permanent_agents"
INSTANTIATION_CODE = "agent_instantiation"
TOKEN_BALANCE_CODE = "template_token_balance"

T = TypeVar("T")


class _SameThreadExecutor:
    """Run callables on one dedicated thread, synchronously.

    Needed because the agent-org substrate's sqlite connections are
    thread-affine. Every store-touching call from any caller thread is
    funneled here; exceptions propagate to the caller. The thread never
    acquires the service lock, so lock -> executor nesting cannot deadlock.
    """

    def __init__(self) -> None:
        self._q: "queue.Queue" = queue.Queue()
        self._t = threading.Thread(target=self._loop, daemon=True,
                                   name="remor-agent-db")
        self._t.start()

    def _loop(self) -> None:
        while True:
            fn, ev, box = self._q.get()
            try:
                box.append(("ok", fn()))
            except BaseException as ex:  # noqa: BLE001 - must propagate
                box.append(("err", ex))
            ev.set()

    def run(self, fn: Callable[[], T]) -> T:
        ev = threading.Event()
        box: List[Any] = []
        self._q.put((fn, ev, box))
        ev.wait()
        status, payload = box[0]
        if status == "err":
            raise payload
        return payload


def _record_to_dict(rec: AgentRecord) -> Dict[str, Any]:
    return {
        "agent_id": rec.agent_id,
        "template_id": rec.template_id,
        "template_version": rec.template_version,
        "template_pin": rec.template_pin,
        "substrate_kind": rec.substrate_kind,
        "state": rec.state,
        "producer_id": rec.producer_id,
        "created_at": rec.created_at,
    }


def _template_to_dict(tpl: AgentTemplate, instantiable: bool) -> Dict[str, Any]:
    return {
        "template_id": tpl.template_id,
        "family": tpl.family,
        "version": tpl.version,
        "substrate_kind": tpl.substrate_kind,
        # substrate_kind is the blueprint's declared kind, not a model
        # identity: these templates are agent blueprints, not LLM models.
        "instantiable": instantiable,
        "contracts": dict(tpl.contracts),
    }


class AgentService:
    """GUI-facing agents contract over real agent-org machinery."""

    def __init__(self, base_dir: str,
                 max_agents: int = MAX_AGENTS_FREE) -> None:
        self.base_dir = base_dir
        self.max_agents = max_agents
        os.makedirs(base_dir, exist_ok=True)
        # Serializes logical operations (check-and-register atomicity)
        # across caller threads.
        self._lock = threading.RLock()
        # All sqlite-touching work runs on this one thread.
        self._db = _SameThreadExecutor()
        self._db.run(self._build)

    # -- construction (runs on the DB thread) ---------------------------
    def _build(self) -> None:
        store = OrgStore(os.path.join(self.base_dir, "agent_org.db"))
        oregistry = OracleRegistry(os.path.join(self.base_dir, "oracle.db"))
        engine = oregistry.engine_handle()
        self.templates = TemplateRegistry(store, engine)
        seed_templates(self.templates)  # idempotent
        self.registry = AgentRegistry(
            store, oregistry, engine, os.path.join(self.base_dir, "agents"))
        self.factory = AgentFactory(self.registry, self.templates)

    def _dbcall(self, fn: Callable[[], T]) -> T:
        """Run fn on the DB thread. Call with self._lock held."""
        return self._db.run(fn)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def list_agents(self, state: Optional[str] = None) -> Dict[str, Any]:
        """All agents from the real registry, optionally filtered by state."""
        with self._lock:
            recs = self._dbcall(lambda: self.registry.list(state=state))
            return {
                "ok": True,
                "agents": [_record_to_dict(r) for r in recs],
                "count": len(recs),
                "free_tier_max": self.max_agents,
            }

    def get_agent(self, agent_id: str) -> Dict[str, Any]:
        with self._lock:
            try:
                rec = self._dbcall(lambda: self.registry.get(agent_id))
            except KeyError:
                return {"ok": False,
                        "error": f"unknown agent {agent_id!r}"}
            return {"ok": True, "agent": _record_to_dict(rec)}

    def active_agent_count(self) -> int:
        """Agents occupying a free-tier slot (non-terminal states)."""
        with self._lock:
            recs = self._dbcall(lambda: self.registry.list())
            return sum(1 for r in recs if r.state not in TERMINAL)

    def list_templates(self) -> Dict[str, Any]:
        """Blueprints genuinely registered. substrate_kind is reported
        honestly: these are agent blueprints, not LLM model identities.
        `instantiable` is False for llm-kind templates because no
        instantiable LLM substrate exists in this build (the factory raises
        SubstrateUnavailable rather than simulating one)."""
        with self._lock:
            tpls = self._dbcall(lambda: self.templates.list())
            fams = sorted({t.family for t in tpls})
            kinds = sorted({t.substrate_kind for t in tpls})
            return {
                "ok": True,
                "templates": [
                    _template_to_dict(t, t.substrate_kind != "llm")
                    for t in tpls
                ],
                "families": fams,          # the types that genuinely exist
                "substrate_kinds": kinds,
                "note": ("Templates are agent blueprints, not model "
                         "identities. 'Limited to select few types' = the "
                         "families listed here; nothing else exists."),
            }

    # ------------------------------------------------------------------
    # free-tier guard (the causal mechanism point)
    # ------------------------------------------------------------------
    def _check_free_tier_limit(self) -> Optional[Dict[str, Any]]:
        """Return a limit payload if the cap is reached, else None.

        Called with self._lock held, immediately before the real
        registry.register, so count-and-create is atomic against
        double-submit races. Tests revert this method (monkeypatch to
        return None) to prove the guard -- not the registry -- is what
        refuses the 4th agent.
        """
        n = sum(1 for r in self._dbcall(lambda: self.registry.list())
                if r.state not in TERMINAL)
        if n >= self.max_agents:
            return contract_limit(
                AGENT_CAP_CODE, self.max_agents, n,
                reset="destroy an agent to free a slot")
        return None

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def register_agent(self, template_id: Any,
                       version: Optional[str] = None,
                       actor: str = "remor:gui") -> Dict[str, Any]:
        """Register one agent from a real template, enforcing the cap."""
        if not isinstance(template_id, str) or not template_id.strip():
            return {"ok": False,
                    "error": f"invalid template_id {template_id!r}: "
                             "must be a non-empty string"}
        with self._lock:
            try:
                self._dbcall(lambda: self.templates.get(template_id, version))
            except KeyError:
                return {"ok": False,
                        "error": f"unknown template {template_id!r}"}
            limit = self._check_free_tier_limit()
            if limit is not None:
                return limit
            try:
                rec = self._dbcall(
                    lambda: self.factory.create(template_id, version))
            except SubstrateUnavailable as ex:
                # Real mechanism: the blueprint exists but no substrate can
                # instantiate it. Refuse honestly; do not simulate.
                return contract_unavailable(
                    INSTANTIATION_CODE, str(ex),
                    missing_substrate="instantiable substrate for "
                                      "substrate_kind 'llm'")
            return {"ok": True, "agent": _record_to_dict(rec)}

    def destroy_agent(self, agent_id: str, actor: str = "remor:gui",
                      reason: str = "") -> Dict[str, Any]:
        with self._lock:
            try:
                rec = self._dbcall(
                    lambda: self.registry.destroy(
                        agent_id, actor=actor,
                        reason=reason or "destroyed via GUI"))
            except KeyError:
                return {"ok": False,
                        "error": f"unknown agent {agent_id!r}"}
            except LifecycleError as ex:
                return {"ok": False, "error": str(ex)}
            return {"ok": True, "agent": _record_to_dict(rec)}

    # ------------------------------------------------------------------
    # honest absences
    # ------------------------------------------------------------------
    def purchase_permanent_agent(self, template_id: Any = None) -> Dict[str, Any]:
        """'Purchase Permanent Agents' has no payment substrate anywhere in
        the runtime (coordinator-verified: zero hits for billing/payment).
        There is nothing to charge, nothing to persist a purchase against,
        and no entitlement ledger to read back -- so this is
        HONESTLY-UNAVAILABLE, not a stubbed checkout."""
        return contract_unavailable(
            PURCHASE_CODE,
            "no payment or subscription-billing substrate exists in the "
            "runtime: there is no way to charge, no purchase ledger, and no "
            "entitlement store to grant permanent agents against",
            missing_substrate="payment_provider + purchase_ledger + "
                              "entitlement_store")

    def template_token_balance(self, template_id: Any = None) -> Dict[str, Any]:
        """Per-template token allowances do not exist: there is no token
        measurement anywhere in the runtime (zero hits). The GUI annotation
        ('each type of agent uses its own token allowance') describes a
        product concept with no measuring substrate behind it. Report the
        template's genuine resource_requirements contract instead of
        inventing numbers."""
        return contract_unavailable(
            TOKEN_BALANCE_CODE,
            "no token-measurement substrate exists in the runtime: tokens are "
            "never counted for any template, so no per-template balance can "
            "be reported or enforced",
            missing_substrate="token_meter")


# ---------------------------------------------------------------------
# construction + route table
# ---------------------------------------------------------------------

def build_agent_service(base_dir: str,
                        max_agents: int = MAX_AGENTS_FREE) -> AgentService:
    return AgentService(base_dir, max_agents=max_agents)


def routes_for_agents(service: AgentService) -> Dict[Any, Any]:
    """{(method, path): handler(body_dict)} for the GUI to adopt.

    Paths support {agent_id} variables (merged into body by dispatch).
    """
    def _list_agents(body: Dict[str, Any]) -> Dict[str, Any]:
        state = body.get("state")
        if state is not None and not isinstance(state, str):
            return {"ok": False, "error": "state must be a string"}
        return service.list_agents(state=state)

    def _get_agent(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.get_agent(body.get("agent_id", ""))

    def _register(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.register_agent(
            body.get("template_id"),
            version=body.get("version"),
            actor=body.get("actor", "remor:gui"))

    def _destroy(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.destroy_agent(
            body.get("agent_id", ""),
            actor=body.get("actor", "remor:gui"),
            reason=body.get("reason", ""))

    def _templates(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.list_templates()

    def _purchase(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.purchase_permanent_agent(body.get("template_id"))

    def _token_balance(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.template_token_balance(body.get("template_id"))

    return {
        ("GET", "/api/agents"): _list_agents,
        ("GET", "/api/agents/{agent_id}"): _get_agent,
        ("POST", "/api/agents"): _register,
        ("POST", "/api/agents/{agent_id}/destroy"): _destroy,
        ("GET", "/api/agent-templates"): _templates,
        ("POST", "/api/agents/purchase-permanent"): _purchase,
        ("GET", "/api/agent-templates/{template_id}/token-balance"): _token_balance,
    }


def dispatch_agents(service: AgentService, method: str, path: str,
                    body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return dispatch(routes_for_agents(service), method, path, body)
