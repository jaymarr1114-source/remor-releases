"""Dispatch learning service (Phase 4, items 2-3).

Product-native integration of dispatch -> organizational learning. The
trust core is unchanged: evidence lives in the chained, externally
anchored ``ao_dispatch_evidence`` table, review re-derives through
``ReviewBoard.verify_dispatch_evidence``, and admission goes through
``ExperienceStore.admit_dispatch_knowledge``. This service is the
product-level choke point that replaces driver-orchestrated calls:

* the NL dispatcher calls ``capture_evidence`` natively on success
  (item 2) -- attribution is fail-closed (bound agent_id, never the
  free-form producer string);
* ``complete_dispatch_knowledge`` is the normal dispatch-completion
  policy (item 3): idempotent capture -> independent review ->
  L2 admission with held-out generality cases, in one product call;
* ``get_evidence`` / ``list_evidence`` are the governed read contract
  (item 2): authenticated callers only; reads return the evidence
  document plus chain-audit standing and never create trust.

Caller authorization (documented choice): every method authenticates the
caller first (AuthorizationError when the credential is absent, wrong, or
belongs to a destroyed identity). ``capture_evidence`` and
``complete_dispatch_knowledge`` then require the authenticated identity
to be EITHER the attributed ``agent_id`` itself OR the engine
(``remor:engine``, which holds every decision class as root authority --
this is the native product path: the dispatcher never holds agent
credentials) OR a holder of the reserved ``agent:dispatch_capture``
grant. The grant clause is evaluated live against the registry, but the
closed grantable set in
``swarm_engine.governance.oracle_binding`` cannot mint that class today,
so the only live callers are the attributed agent and the engine; the
check is in place so a future delegation path flows through without a
code change. Reads require authentication only -- no decision class --
because they create nothing.

Wiring (documented): the coordinator constructs one service per
(org, engine) pair as
``DispatchLearningService(org, AgentDirectory(org.oregistry), engine)``.
The constructor ASSIGNS ``org.review.dispatch_engine = engine`` -- the
review board needs the live engine to re-derive dispatches, and whoever
constructed the service last wins, so the coordinator must not bind two
engines to one review board. When ``engine`` is omitted, the already
bound ``org.review.dispatch_engine`` is used; when neither exists the
constructor refuses outright (review cannot re-derive without an
engine -- fail closed, never an unactionable service).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from swarm_engine.agent_org.dispatch_learning import (
    capture_dispatch_evidence,
    get_evidence as _get_evidence,
    list_evidence as _list_evidence,
)
from swarm_engine.agent_org.store import digest
from swarm_engine.governance.caller_authorization import (
    AgentDirectory,
    AuthorizationError,
    CallerContext,
)
from swarm_engine.governance.oracle_binding import ENGINE_PRODUCER_ID

# Reserved decision class for delegated dispatch-capture authority.
# Evaluated live by _authorize_capture; see the module docstring for why
# no live grant path mints it today.
DISPATCH_CAPTURE_GRANT = "agent:dispatch_capture"


class DispatchLearningService:
    """Product choke point for dispatch -> organizational learning."""

    def __init__(self, org: Any, agents: AgentDirectory,
                 engine: Any = None):
        if not isinstance(agents, AgentDirectory):
            raise TypeError(
                "DispatchLearningService requires an AgentDirectory for "
                f"caller auth, got {type(agents).__name__}")
        self.org = org
        self.agents = agents
        self.store = org.store
        if engine is None:
            engine = getattr(org.review, "dispatch_engine", None)
        if engine is None:
            raise RuntimeError(
                "DispatchLearningService refused: no live engine -- pass "
                "engine= or bind org.review.dispatch_engine first; the "
                "review board cannot re-derive dispatches without one")
        # Documented wiring: the service owns the review board's engine
        # binding from construction on.
        org.review.dispatch_engine = engine
        self.engine = engine
        # The native product caller: the ORG's engine handle (its token
        # authenticates against the org's registry, which is what
        # `agents` is bound to). The dispatcher uses this for native
        # capture; attribution still comes from the bound agent_id.
        self._engine_caller = CallerContext.from_pair(org.engine)

    # -- caller identity ------------------------------------------------
    @property
    def engine_caller(self) -> CallerContext:
        """The product-native caller identity for native capture."""
        return self._engine_caller

    def _authenticate_caller(self, caller: Any) -> str:
        """Authenticate the caller; return the identity. Raises
        AuthorizationError when the credential is absent, malformed,
        wrong, or belongs to a destroyed identity (401-style: a dead
        credential fails authentication, it is not "authenticated but
        unpermitted")."""
        ctx = CallerContext.from_pair(caller)
        aid = ctx.agent_id
        if not aid:
            raise AuthorizationError(
                "unauthenticated: empty caller identity: "
                "dispatch-learning operation refused")
        if aid == ENGINE_PRODUCER_ID:
            if self.agents.authenticate(aid, ctx.token):
                return aid
            raise AuthorizationError(
                "unauthenticated: engine credential failed: "
                "dispatch-learning operation refused")
        # 1) directory-registered identity (AgentDirectory.register_agent).
        if self.agents.authenticate(aid, ctx.token):
            return aid
        # 2) org factory-agent identity: agent_id -> producer credential
        #    bound at factory registration; destroyed agents fail here.
        row = self.store.latest("ao_agents", "agent_id", aid)
        if row is not None and row.get("state") != "DESTROYED":
            producer_id = row.get("producer_id")
            if producer_id and self.agents.reg.authenticate(
                    producer_id, ctx.token):
                return aid
        raise AuthorizationError(
            f"unauthenticated: {aid!r}: credential failed: "
            "dispatch-learning operation refused")

    def _producer_for(self, identity: str) -> str:
        """Resolve an authenticated identity to its producer id for grant
        checks (factory agents authenticate as agent_id but hold grants
        as their bound producer)."""
        if identity == ENGINE_PRODUCER_ID:
            return identity
        row = self.store.latest("ao_agents", "agent_id", identity)
        if row is not None and row.get("producer_id"):
            return row["producer_id"]
        return identity

    def _authorize_capture(self, identity: str, agent_id: str) -> None:
        """Enforce the capture authorization rule (documented choice):
        the authenticated caller must be the attributed agent itself, or
        the engine (root authority -- the native product path), or hold
        the reserved dispatch-capture grant."""
        if identity == agent_id:
            return
        if identity == ENGINE_PRODUCER_ID:
            return
        try:
            if self.agents.reg.producer_authorized(
                    self._producer_for(identity), DISPATCH_CAPTURE_GRANT):
                return
        except Exception:
            pass
        raise AuthorizationError(
            f"capture refused: {identity!r} is neither the attributed "
            f"agent {agent_id!r} nor the engine and holds no "
            f"{DISPATCH_CAPTURE_GRANT!r} grant (default-deny)")

    # -- item 2: native capture ------------------------------------------
    def capture_evidence(self, dispatch_id: str, agent_id: str,
                         assignment_id: str, args: Dict[str, Any],
                         result_value: Any, caller: Any) -> str:
        """Capture attributable dispatch evidence for one real dispatch.

        The caller must authenticate AND be the attributed agent_id, the
        engine, or a dispatch-capture grant holder (see module docstring).
        Attribution is re-verified inside capture_dispatch_evidence
        (agent exists and is live; assignment belongs to the agent) --
        the caller identity never supplies attribution. Returns the
        evidence_id; raises on any inconsistency (fail closed).
        """
        identity = self._authenticate_caller(caller)
        self._authorize_capture(identity, agent_id)
        return capture_dispatch_evidence(
            self.engine, self.store, dispatch_id, agent_id, assignment_id,
            args, result_value)

    # -- item 2: governed reads -------------------------------------------
    def _chain_standing(self) -> Dict[str, Any]:
        """Chain-audit + external-anchor standing of the evidence table.
        Informational only: reads never create trust and never refuse on
        standing (a broken chain is reported, not hidden)."""
        try:
            audit_ok, audit_msg = self.store.audit("ao_dispatch_evidence")
        except Exception as exc:
            audit_ok, audit_msg = False, f"{type(exc).__name__}: {exc}"
        try:
            from swarm_engine.governance.anchor import collect_anchor_heads
            anchor_ok, anchor_msg = self.org.anchor.verify(
                collect_anchor_heads(self.store, self.org.oregistry))
        except Exception as exc:
            anchor_ok, anchor_msg = False, f"{type(exc).__name__}: {exc}"
        return {
            "audit_ok": bool(audit_ok),
            "audit_msg": str(audit_msg)[:300],
            "anchor_ok": bool(anchor_ok),
            "anchor_msg": str(anchor_msg)[:300],
        }

    def get_evidence(self, evidence_id: str, caller: Any) -> Dict[str, Any]:
        """Governed read: the caller must be an authenticated live agent
        (the engine qualifies as the operator identity). Returns the
        evidence document plus chain-audit standing. Raises
        AuthorizationError when unauthenticated, KeyError when unknown.
        Reads never create trust."""
        self._authenticate_caller(caller)
        ev = _get_evidence(self.store, evidence_id)
        return {"evidence": ev.as_dict(), "chain": self._chain_standing()}

    def list_evidence(self, caller: Any) -> List[Dict[str, Any]]:
        """Governed list: same authentication contract as get_evidence.
        Chain standing is computed once for the whole call."""
        self._authenticate_caller(caller)
        standing = self._chain_standing()
        return [{"evidence": ev.as_dict(), "chain": dict(standing)}
                for ev in _list_evidence(self.store)]

    # -- item 3: the dispatch-completion policy -----------------------------
    def complete_dispatch_knowledge(
            self, *, dispatch_id: str, agent_id: str, assignment_id: str,
            args: Dict[str, Any], result_value: Any, technique_name: str,
            code: str, entrypoint: str, problem_class: str,
            tags: List[str], io_contract: Dict[str, Any],
            params: Dict[str, Any], generality_cases: List[Any],
            caller: Any):
        """One product-level call for normal dispatch completion:

        1. capture (idempotent -- an existing evidence row for this
           dispatch+agent is reused, never duplicated);
        2. independent review (``verify_dispatch_evidence`` re-derives
           the dispatch in a subprocess; raises ``VerificationFailed``
           on any refusal -- fail closed);
        3. L2 admission (``admit_dispatch_knowledge`` with the supplied
           held-out generality cases; origin ``dispatch_discovery``).

        Returns ``(True, exp_id)`` or ``(False, reasons)``; raises on
        authentication/authorization failure, on review refusal, and on
        replayed admission (the admission layer's duplicate guard).
        """
        identity = self._authenticate_caller(caller)
        self._authorize_capture(identity, agent_id)
        # Idempotent capture: the evidence id is deterministic in
        # (dispatch_id, agent_id); reuse the existing row when present.
        evidence_id = "dsp_ev_" + digest(dispatch_id + agent_id)[:16]
        if self.store.latest(
                "ao_dispatch_evidence", "evidence_id",
                evidence_id) is None:
            captured = capture_dispatch_evidence(
                self.engine, self.store, dispatch_id, agent_id,
                assignment_id, args, result_value)
            if captured != evidence_id:  # paranoia: ids must agree
                raise RuntimeError(
                    "completion policy refused: capture returned "
                    f"{captured!r}, expected {evidence_id!r}")
        verdict = self.org.review.verify_dispatch_evidence(evidence_id)
        if not verdict.admitted:
            return False, list(verdict.reasons)
        return self.org.experience.admit_dispatch_knowledge(
            evidence_id=evidence_id, technique_name=technique_name,
            code=code, entrypoint=entrypoint or "selftest",
            problem_class=problem_class, tags=list(tags),
            io_contract=dict(io_contract), params=dict(params or {}),
            generality_cases=list(generality_cases), agent_id=agent_id)
