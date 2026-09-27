"""Caller authorization: persistent agent identities + privileged-op enforcement.

This is the general caller-authorization layer the oracle-binding mission
deliberately did not build (it bound oracle *producers* only). It answers:

  "who is calling, are they who they claim, and are they allowed to do
   this specific thing" -- for every privileged operation in REMOR.

Identities
----------
An agent identity is a row in ``ob_producers`` (producer_type='agent')
holding ONLY a sha256(token) -- the token itself is shown once at
registration and never persisted -- plus an immutable registration row in
``ob_agents`` and chained lifecycle events in ``ob_agent_events``.

Authentication is ``hmac.compare_digest(sha256(presented), stored_hash)``
AND status=='active' AND a matching chained registration attestation AND
no 'destroyed' lifecycle event. Destroying an agent flips the status,
appends a chained 'destroyed' event, revokes every live decision-class
authorization it held, and revokes every grant it issued. A destroyed
credential stays dead across fresh processes: the event is chained rows,
not memory.

Authorization
-------------
Decision classes (``agent:*``) are granted per agent via
``ob_producer_authorizations`` and revoked via chained
``ob_producer_authz_revocations`` rows. Default-deny: anything not
explicitly granted is refused. The engine producer (``remor:engine``)
holds every class as a root authorization from bootstrap.

Enforcement
-----------
Every privileged operation takes a ``caller`` (CallerContext, an
(agent_id, token) pair, or an EngineOracleHandle) and calls
``require_authorized(registry, caller, DECISION_*, op_name)`` FIRST.
Refusals raise AuthorizationError with an explicit reason -- never a
silent downgrade.

Honest limits (see also the mission threat model):
  * A bearer token identifies its holder: anyone holding an agent's token
    IS that agent to this layer (bearer theft is out of scope; protect the
    token like a password).
  * In-process callers share memory with the engine; this layer does not
    defend against a caller reading another caller's memory.
  * Tamper-evidence (chained rows) detects naive row mutation, not a full
    database rewrite with recomputed chains (external anchoring is the
    next boundary, per the trust-anchor mission).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.governance.oracle_binding import (
    AGENT_DECISION_CLASSES,
    DECISION_GRANT,
    ENGINE_PRODUCER_ID,
    OracleBindingError,
    OracleRegistry,
)


class AuthorizationError(Exception):
    """A privileged operation refused for caller reasons. The message is
    the explicit reason; it is never swallowed or downgraded."""


# HTTP permission spectrum (Phase 3 operator auth). James's decided
# direction: token-based caller identities mapped to HTTP identities, with
# the permission class chosen by the user at grant time:
#   allow-once            -- one authorized mutating call, then consumed;
#   allow-for-project     -- mutating calls scoped to one project_id only
#                            (cross-project or non-project use refused);
#   effectively-unlimited -- every mutating route (the operator class).
# Implemented as grants in the AgentDirectory grant model (chained rows in
# ob_http_permissions -- issue/consume/revoke events, same append-only
# discipline as the decision-class and effect-grant models), not as a
# parallel bypass.
HTTP_SCOPE_ONCE = "allow-once"
HTTP_SCOPE_PROJECT = "allow-for-project"
HTTP_SCOPE_UNLIMITED = "effectively-unlimited"
HTTP_PERMISSION_SCOPES = (
    HTTP_SCOPE_ONCE, HTTP_SCOPE_PROJECT, HTTP_SCOPE_UNLIMITED)
# Preference order when several live permissions cover one call: the most
# reusable wins, so a single-use grant is never burned while a reusable
# one covers the same call. Deterministic.
_HTTP_SCOPE_PREFERENCE = (
    HTTP_SCOPE_UNLIMITED, HTTP_SCOPE_PROJECT, HTTP_SCOPE_ONCE)


@dataclass
class CallerContext:
    """Who is calling a privileged operation. The token is the credential;
    the agent_id is the claimed identity it must match."""
    agent_id: str
    token: str

    @classmethod
    def from_pair(cls, pair: Any) -> "CallerContext":
        if isinstance(pair, CallerContext):
            return pair
        if isinstance(pair, (tuple, list)) and len(pair) == 2:
            return cls(agent_id=str(pair[0]), token=str(pair[1]))
        # EngineOracleHandle duck-type: producer_id + memory-only _token.
        pid = getattr(pair, "producer_id", None)
        tok = getattr(pair, "_token", None)
        if pid and tok:
            return cls(agent_id=str(pid), token=str(tok))
        raise AuthorizationError(
            "unauthenticated: caller must be a CallerContext, an "
            "(agent_id, token) pair, or an engine oracle handle; got "
            f"{type(pair).__name__}: privileged operation refused")

    @classmethod
    def from_engine_handle(cls, handle: Any) -> "CallerContext":
        return cls.from_pair(handle)


@dataclass
class AgentCredential:
    """(agent_id, token) pair. The token is shown ONCE at registration."""
    agent_id: str
    token: str

    def context(self) -> CallerContext:
        return CallerContext(agent_id=self.agent_id, token=self.token)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AgentDirectory:
    """Lifecycle + authorization for persistent agent identities.

    All state lives in the OracleRegistry's sqlite DB (chained tables), so
    identities, grants, and revocations survive a fresh process: a new
    OracleRegistry on the same db_path rehydrates everything, and a
    destroyed credential stays dead because the 'destroyed' event is a
    chained row, not memory.
    """

    def __init__(self, registry: OracleRegistry):
        self.reg = registry

    # -- registration -------------------------------------------------
    def register_agent(self, caller: Any, *,
                       template_id: str = "", template_version: str = "",
                       substrate_id: str = "", substrate_kind: str = "",
                       source: str = "",
                       decision_classes: Tuple[str, ...] = (),
                       agent_id: str = ""
                       ) -> AgentCredential:
        """Create a persistent agent identity. The caller must hold
        DECISION_GRANT (identity issuance is part of grant issuance --
        minting identities that can hold grants is privileged).

        Returns the credential; the token is shown ONCE and never
        persisted. Optionally grants decision classes (each recorded as a
        chained authorization row attributed to the registering caller).
        agent_id, when given, pins the identity id (must be unused --
        lets a harness bind a caller identity to an existing agent id);
        otherwise a fresh id is minted from the token."""
        registrar = self.check(caller, DECISION_GRANT,
                               op_name="register_agent")
        registrar_ctx = CallerContext.from_pair(caller)
        token = secrets.token_hex(32)
        if agent_id:
            if not re.fullmatch(r"[A-Za-z0-9_:.-]{1,128}", agent_id):
                raise AuthorizationError(
                    f"register_agent: invalid agent_id {agent_id!r}")
            dup = self.reg._conn.execute(
                "SELECT 1 FROM ob_producers WHERE producer_id=?",
                (agent_id,)).fetchone()
            if dup:
                raise AuthorizationError(
                    f"register_agent: agent_id {agent_id!r} already exists")
        else:
            agent_id = f"agent_{_digest(token)[:16]}"
        cur = self.reg._conn.cursor()
        cur.execute(
            "INSERT INTO ob_producers (producer_id, producer_type, source, "
            "created_at, token_hash, status) VALUES (?, 'agent', ?, ?, ?, "
            "'active')",
            (agent_id, source, _now(), _digest(token)))
        self.reg._insert_chained("ob_producer_attestations", {
            "attest_id": f"reg_{_digest(token)[:16]}",
            "producer_id": agent_id,
            "token_hash": _digest(token),
            "attested_at": _now()})
        self.reg._insert_chained("ob_agents", {
            "agent_id": agent_id, "producer_id": agent_id,
            "template_id": template_id, "template_version": template_version,
            "substrate_id": substrate_id, "substrate_kind": substrate_kind,
            "source": source, "registered_by": registrar,
            "created_at": _now()})
        self.reg._insert_chained("ob_agent_events", {
            "event_id": f"agev_{_digest(token)[:16]}",
            "agent_id": agent_id, "event": "registered",
            "actor": registrar, "reason": source[:500], "at": _now()})
        self.reg._conn.commit()
        for cls in decision_classes:
            self.reg.authorize_producer(agent_id, cls, registrar,
                                        registrar_ctx.token)
        return AgentCredential(agent_id, token)

    def agent_grants(self, agent_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """Enumerate everything attributed to an agent: live decision-class
        authorizations, issued effect grants (live), and lifecycle events.
        This is the enumeration James's remote-revoke rides on."""
        cur = self.reg._conn.cursor()
        cur.execute("SELECT decision_class, auth_id, granted_by, granted_at "
                    "FROM ob_producer_authorizations WHERE producer_id=?",
                    (agent_id,))
        authz = [dict(r) for r in cur.fetchall()
                 if self.reg.producer_authorized(
                     agent_id, r["decision_class"])]
        cur.execute("SELECT grant_id, effect, pattern, scope, granted_at "
                    "FROM ob_grants WHERE granted_by=? ORDER BY seq ASC",
                    (agent_id,))
        heads: Dict[str, Dict[str, Any]] = {}
        for r in cur.fetchall():
            heads[r["grant_id"]] = dict(r)
        cur.execute("SELECT grant_id, revoked FROM ob_grants ORDER BY seq ASC")
        live: Dict[str, bool] = {}
        for r in cur.fetchall():
            live[r["grant_id"]] = not r["revoked"]
        grants = [g for gid, g in heads.items() if live.get(gid)]
        cur.execute("SELECT event, actor, reason, at FROM ob_agent_events "
                    "WHERE agent_id=? ORDER BY seq ASC", (agent_id,))
        events = [dict(r) for r in cur.fetchall()]
        return {"authorizations": authz, "issued_grants": grants,
                "lifecycle": events,
                "http_permissions": self.live_http_permissions(agent_id)}

    # -- destruction / revocation ---------------------------------------
    def destroy_agent(self, agent_id: str, caller: Any,
                      reason: str = "") -> Dict[str, Any]:
        """Destroy an agent's identity: the credential dies immediately and
        stays dead across fresh processes. The destroyer must hold
        DECISION_GRANT. Every live decision-class authorization the agent
        held is revoked (chained event) and every effect grant it issued is
        revoked. Returns the enumeration of what was revoked.

        This is the mechanism James's remote-revoke decision rides on."""
        destroyer = self.check(caller, DECISION_GRANT,
                               op_name="destroy_agent", target=agent_id)
        if agent_id == ENGINE_PRODUCER_ID:
            raise AuthorizationError(
                "the engine producer cannot be destroyed")
        before = self.agent_grants(agent_id)
        if not before["lifecycle"]:
            raise AuthorizationError(
                f"unknown agent {agent_id!r}: destroy refused")
        if self._destroyed(agent_id):
            raise AuthorizationError(
                f"agent {agent_id!r} is already destroyed")
        cur = self.reg._conn.cursor()
        # Status flip (fast path) + chained destroy event (tamper-evident;
        # authenticate() refuses on the event even if the status row is
        # tampered back to 'active').
        cur.execute("UPDATE ob_producers SET status='destroyed' "
                    "WHERE producer_id=?", (agent_id,))
        self.reg._insert_chained("ob_agent_events", {
            "event_id": f"agev_destroy_{_digest(agent_id + str(time.time_ns()))[:16]}",
            "agent_id": agent_id, "event": "destroyed",
            "actor": destroyer, "reason": reason[:500], "at": _now()})
        self.reg._conn.commit()
        destroyer_ctx = CallerContext.from_pair(caller)
        revoked_authz = []
        for a in before["authorizations"]:
            self.reg.revoke_producer_authorization(
                agent_id, a["decision_class"], destroyer,
                destroyer_ctx.token,
                reason=f"agent destroyed: {reason}"[:500])
            revoked_authz.append(a["decision_class"])
        revoked_grants = []
        for g in before["issued_grants"]:
            self.reg.revoke_grant(g["grant_id"], destroyer,
                                  destroyer_ctx.token)
            revoked_grants.append(g["grant_id"])
        # The destroyed agent's live HTTP permissions die with it (chained
        # revoke events, same as decision-class authorizations). Its
        # credential is already dead via the destroyed lifecycle event, so
        # this is belt-and-braces hygiene for the enumeration surface.
        revoked_http = []
        for p in self.live_http_permissions(agent_id):
            self._append_http_event(p, destroyer, consumed=0, revoked=1)
            revoked_http.append(p["grant_id"])
        # The destroyed agent's credential is dead: authenticate() refuses
        # on the chained destroy event even with the right token (verified
        # causally by the adversarial battery with the real credential).
        return {"agent_id": agent_id, "destroyed_by": destroyer,
                "revoked_authorizations": revoked_authz,
                "revoked_grants": revoked_grants,
                "revoked_http_permissions": revoked_http}

    def enumerate_agents(self, caller: Any) -> List[Dict[str, Any]]:
        """List every agent identity: id, status, source, registered_by,
        created_at, and live decision classes. The enumerator must hold
        DECISION_GRANT (identity inventory is privileged: it reveals
        which agents exist and what they may do)."""
        self.check(caller, DECISION_GRANT, op_name="enumerate_agents")
        cur = self.reg._conn.cursor()
        cur.execute("SELECT producer_id, status, source, created_at "
                    "FROM ob_producers WHERE producer_type='agent' "
                    "ORDER BY producer_id ASC")
        out = []
        for r in cur.fetchall():
            live = [a["decision_class"]
                    for a in self.agent_grants(r["producer_id"])["authorizations"]]
            out.append({"agent_id": r["producer_id"], "status": r["status"],
                        "source": r["source"], "created_at": r["created_at"],
                        "live_decision_classes": live})
        return out

    def issue_grant(self, caller: Any, agent_id: str,
                    decision_class: str) -> str:
        """Grant a decision class to an agent. The issuer must hold
        DECISION_GRANT; the class must be in the grantable set; self-
        grants are refused. Returns the authorization id."""
        issuer = self.check(caller, DECISION_GRANT,
                            op_name="issue_grant", target=agent_id)
        if not self._agent_live(agent_id):
            raise AuthorizationError(
                f"issue_grant: unknown or destroyed agent {agent_id!r}")
        issuer_ctx = CallerContext.from_pair(caller)
        return self.reg.authorize_producer(agent_id, decision_class,
                                          issuer, issuer_ctx.token)

    def revoke_grant(self, caller: Any, agent_id: str,
                     decision_class: str, reason: str = "") -> str:
        """Revoke an agent's live decision-class authorization. The
        revoker must hold DECISION_GRANT. Recorded as a chained event;
        the grant row is never mutated. Returns the revocation id."""
        revoker = self.check(caller, DECISION_GRANT,
                             op_name="revoke_grant", target=agent_id)
        revoker_ctx = CallerContext.from_pair(caller)
        return self.reg.revoke_producer_authorization(
            agent_id, decision_class, revoker, revoker_ctx.token,
            reason=reason)

    # -- HTTP permissions (permission spectrum) --------------------------
    def issue_http_permission(self, caller: Any, agent_id: str,
                              scope_class: str,
                              project_id: str = "") -> str:
        """Grant an HTTP permission-spectrum class to an agent. The issuer
        must hold DECISION_GRANT (grant issuance is privileged); self-
        grants are refused, mirroring authorize_producer. allow-for-project
        requires a project_id; the other classes forbid one. Returns the
        grant id. Recorded as a chained issue event."""
        issuer = self.check(caller, DECISION_GRANT,
                            op_name="issue_http_permission",
                            target=agent_id)
        if scope_class not in HTTP_PERMISSION_SCOPES:
            raise AuthorizationError(
                f"issue_http_permission: unknown scope class "
                f"{scope_class!r} (must be one of "
                f"{', '.join(HTTP_PERMISSION_SCOPES)})")
        if scope_class == HTTP_SCOPE_PROJECT:
            if not project_id:
                raise AuthorizationError(
                    "issue_http_permission: allow-for-project requires a "
                    "project_id")
        elif project_id:
            raise AuthorizationError(
                "issue_http_permission: project_id is only meaningful for "
                f"allow-for-project, not {scope_class!r}")
        if not self._agent_live(agent_id):
            raise AuthorizationError(
                "issue_http_permission: unknown or destroyed agent "
                f"{agent_id!r}")
        if agent_id == issuer:
            raise AuthorizationError(
                f"self-grant refused: {issuer!r} cannot issue an HTTP "
                f"permission to itself")
        grant_id = "hperm_" + _digest(
            agent_id + scope_class + project_id + issuer +
            str(time.time_ns()))[:16]
        self.reg._insert_chained("ob_http_permissions", {
            "grant_id": grant_id, "agent_id": agent_id,
            "scope_class": scope_class, "project_id": project_id,
            "issued_by": issuer, "issued_at": _now(),
            "consumed": 0, "revoked": 0})
        return grant_id

    def _http_permission_heads(
            self, agent_id: str) -> Dict[str, Dict[str, Any]]:
        """Head event row per grant_id for one agent (latest seq wins)."""
        cur = self.reg._conn.cursor()
        cur.execute("SELECT * FROM ob_http_permissions WHERE agent_id=? "
                    "ORDER BY seq ASC", (agent_id,))
        heads: Dict[str, Dict[str, Any]] = {}
        for r in cur.fetchall():
            heads[r["grant_id"]] = dict(r)
        return heads

    def live_http_permissions(self, agent_id: str) -> List[Dict[str, Any]]:
        """Every live (issued, neither consumed nor revoked) HTTP
        permission held by an agent."""
        return [g for g in self._http_permission_heads(agent_id).values()
                if not g["revoked"] and not g["consumed"]]

    def _append_http_event(self, head: Dict[str, Any], actor: str,
                           consumed: int, revoked: int) -> None:
        """Append a consume/revoke event row for one permission grant.
        History is preserved; the head row decides liveness."""
        self.reg._insert_chained("ob_http_permissions", {
            "grant_id": head["grant_id"], "agent_id": head["agent_id"],
            "scope_class": head["scope_class"],
            "project_id": head["project_id"],
            "issued_by": actor, "issued_at": _now(),
            "consumed": consumed, "revoked": revoked})

    def revoke_http_permission(self, caller: Any, grant_id: str,
                               reason: str = "") -> None:
        """Retire an HTTP permission. The revoker must hold DECISION_GRANT.
        Recorded as a chained revoke event; the history is preserved."""
        revoker = self.check(caller, DECISION_GRANT,
                             op_name="revoke_http_permission",
                             target=grant_id)
        cur = self.reg._conn.cursor()
        cur.execute("SELECT * FROM ob_http_permissions WHERE grant_id=? "
                    "ORDER BY seq DESC LIMIT 1", (grant_id,))
        row = cur.fetchone()
        if row is None:
            raise AuthorizationError(
                f"revoke_http_permission: no such permission {grant_id!r}")
        head = dict(row)
        if head["revoked"]:
            raise AuthorizationError(
                f"revoke_http_permission: {grant_id!r} already revoked")
        if head["consumed"]:
            raise AuthorizationError(
                f"revoke_http_permission: {grant_id!r} already consumed")
        self._append_http_event(head, revoker, consumed=0, revoked=1)

    def _consume_http_permission(self, agent_id: str, grant_id: str) -> None:
        """Retire a single-use (allow-once) permission after its one
        authorized call. This is the defined consumption mechanic of the
        allow-once class -- not a privilege change: it only ever REMOVES
        authority, and only for a permission whose head row is live,
        allow-once, and owned by the consuming agent. Called only after
        the caller's coverage was established by http_coverage and every
        other authorization check passed (see authorize_mutating).
        Recorded as a chained event, never a mutation."""
        head = self._http_permission_heads(agent_id).get(grant_id)
        if head is None or head["revoked"] or head["consumed"]:
            raise AuthorizationError(
                f"consume_http_permission: {grant_id!r} is not a live "
                f"permission of agent {agent_id!r}")
        if head["scope_class"] != HTTP_SCOPE_ONCE:
            raise AuthorizationError(
                f"consume_http_permission: {grant_id!r} is "
                f"{head['scope_class']!r}, not single-use")
        self._append_http_event(head, agent_id, consumed=1, revoked=0)

    def http_coverage(self, caller: Any, route_name: str,
                      project_id: Optional[str] = None):
        """Authenticate + find the covering HTTP permission WITHOUT
        consuming it. Returns (agent_id, grant_id_or_None): grant_id is
        set only when the covering permission is single-use (allow-once)
        and must be consumed once the call is fully authorized.
        Raises AuthorizationError with an explicit reason (401-style
        when the credential is missing/bad, 403-style when no live
        permission covers). Separated from consumption so a route can
        verify its downstream decision classes BEFORE burning a
        one-shot grant: consumption happens on the first AUTHORIZED
        mutating call, never on a call that ends 401/403."""
        ctx = self._require_auth(caller)
        live = self.live_http_permissions(ctx.agent_id)

        def _covers(perm: Dict[str, Any]) -> bool:
            sc = perm["scope_class"]
            if sc == HTTP_SCOPE_UNLIMITED:
                return True
            if sc == HTTP_SCOPE_PROJECT:
                return (project_id is not None
                        and perm["project_id"] == project_id)
            return True  # allow-once covers any single mutating route

        covering = [p for p in live if _covers(p)]
        if not covering:
            raise AuthorizationError(
                f"agent {ctx.agent_id!r}: no live HTTP permission covering "
                f"route {route_name!r}"
                + (f" for project {project_id!r}" if project_id else "")
                + " (default-deny): mutating route refused")
        covering.sort(key=lambda p: _HTTP_SCOPE_PREFERENCE.index(
            p["scope_class"]))
        chosen = covering[0]
        grant_id = (chosen["grant_id"]
                    if chosen["scope_class"] == HTTP_SCOPE_ONCE else None)
        return ctx.agent_id, grant_id

    def consume_http_grant(self, agent_id: str, grant_id: str) -> None:
        """Consume a single-use HTTP permission after its one AUTHORIZED
        call. Fail-closed: raises when the grant is not a live
        allow-once permission of this agent (already consumed, revoked,
        or wrong scope class)."""
        self._consume_http_permission(agent_id, grant_id)

    def authorize_http(self, caller: Any, route_name: str,
                       project_id: Optional[str] = None) -> str:
        """Authenticate + HTTP-permission check for one mutating route.

        route_name is the adapter's mutating-route name; project_id is the
        project the route targets, or None for non-project routes.
        Coverage: effectively-unlimited covers every mutating route;
        allow-for-project covers only project routes whose project_id
        matches the grant (cross-project or non-project use refused);
        allow-once covers any one mutating route and is consumed by this
        call. When several live permissions cover, the most reusable wins
        (a single-use grant is never burned while a reusable one covers).
        Returns the authenticated agent_id; raises AuthorizationError with
        an explicit reason otherwise (401-style when the credential is
        missing/bad, 403-style when no live permission covers).

        NOTE: this consumes allow-once immediately. Routes with
        additional downstream decision classes must use http_coverage()
        + consume_http_grant() instead, so the one-shot grant is burned
        only after every authorization check passes."""
        agent_id, grant_id = self.http_coverage(caller, route_name,
                                                project_id)
        if grant_id is not None:
            # Consumed by the first authorized mutating call: the grant
            # covers this call, and this call only. Consumption is recorded
            # before returning, so a second use is refused even if the
            # downstream operation itself fails.
            self._consume_http_permission(agent_id, grant_id)
        return agent_id

    def _agent_live(self, agent_id: str) -> bool:
        """True iff agent_id names a registered agent whose latest
        lifecycle event is not 'destroyed'. Liveness without a token
        (for grant issuance targeting); authentication still needs the
        bearer token."""
        cur = self.reg._conn.cursor()
        cur.execute("SELECT status FROM ob_producers "
                    "WHERE producer_id=? AND producer_type='agent'",
                    (agent_id,))
        row = cur.fetchone()
        if row is None or row["status"] == "destroyed":
            return False
        return not self._destroyed(agent_id)

    # -- authentication -------------------------------------------------
    def _destroyed(self, agent_id: str) -> bool:
        cur = self.reg._conn.cursor()
        cur.execute(
            "SELECT event FROM ob_agent_events WHERE agent_id=? "
            "ORDER BY seq DESC LIMIT 1", (agent_id,))
        row = cur.fetchone()
        return bool(row) and row["event"] == "destroyed"

    def authenticate(self, agent_id: str, token: str) -> bool:
        """True iff agent_id names a live agent and token verifies.

        Causally unforgeable inside the adversarial battery: the stored
        value is sha256(token) (256-bit secret, never the token), compared
        with hmac.compare_digest; a destroyed agent fails even with the
        right token; a forged or guessed token fails the digest."""
        if not agent_id or not token:
            return False
        if agent_id == ENGINE_PRODUCER_ID:
            # The engine producer is not an agent row: it authenticates by
            # its memory-only token (rotated per process) and holds every
            # decision class as a root authorization from bootstrap.
            return self.reg.authenticate(agent_id, token)
        cur = self.reg._conn.cursor()
        cur.execute("SELECT producer_type FROM ob_producers WHERE producer_id=?",
                    (agent_id,))
        row = cur.fetchone()
        if not row or row["producer_type"] != "agent":
            return False
        cur.execute("SELECT 1 FROM ob_agents WHERE agent_id=?", (agent_id,))
        if not cur.fetchone():
            return False
        if self._destroyed(agent_id):
            return False
        return self.reg.authenticate(agent_id, token)

    def _require_auth(self, caller: Any) -> CallerContext:
        ctx = CallerContext.from_pair(caller) if caller is not None else None
        if ctx is None:
            raise AuthorizationError(
                "unauthenticated: no caller credential supplied: "
                "privileged operation refused")
        if not self.authenticate(ctx.agent_id, ctx.token):
            if self._destroyed(ctx.agent_id):
                raise AuthorizationError(
                    # 401-style: a destroyed credential fails
                    # AUTHENTICATION (it no longer authenticates), it is not
                    # "authenticated but unpermitted".
                    f"unauthenticated: agent {ctx.agent_id!r}: credential "
                    f"destroyed (revoked): privileged operation refused")
            raise AuthorizationError(
                f"agent {ctx.agent_id!r}: authentication failed (unknown "
                f"agent, forged credential, or wrong token): "
                f"privileged operation refused")
        return ctx

    # -- authorization --------------------------------------------------
    def check(self, caller: Any, decision_class: str,
              op_name: str = "operation", target: str = "") -> str:
        """Authenticate + authorize. Returns the agent_id. Raises
        AuthorizationError with an explicit reason otherwise. Default-deny:
        anything not explicitly granted is refused."""
        ctx = self._require_auth(caller)
        if not self.reg.producer_authorized(ctx.agent_id, decision_class):
            raise AuthorizationError(
                f"agent {ctx.agent_id!r}: not authorized for "
                f"'{decision_class}' (default-deny): {op_name} refused"
                + (f" on {target!r}" if target else ""))
        return ctx.agent_id


def require_authorized(registry: OracleRegistry, agents: AgentDirectory,
                       caller: Any, decision_class: str,
                       op_name: str, target: str = "") -> str:
    """Enforcement primitive for privileged operations. Returns the
    authenticated agent_id; raises AuthorizationError otherwise."""
    return agents.check(caller, decision_class, op_name=op_name,
                        target=target)


def require_all(registry: OracleRegistry, agents: AgentDirectory,
                caller: Any, decision_classes: Tuple[str, ...],
                op_name: str, target: str = "") -> str:
    """Authenticate once, require every listed decision class. Returns the
    authenticated agent_id; raises AuthorizationError naming the first
    missing class."""
    ctx = agents._require_auth(caller)
    for cls in decision_classes:
        if not registry.producer_authorized(ctx.agent_id, cls):
            raise AuthorizationError(
                f"agent {ctx.agent_id!r}: not authorized for '{cls}' "
                f"(default-deny): {op_name} refused"
                + (f" on {target!r}" if target else ""))
    return ctx.agent_id


def _engine_caller_context(engine: Any) -> CallerContext:
    """Mint the engine's own CallerContext from its live oracle handle."""
    handle = getattr(engine, "oracle", None)
    if handle is None:
        # agent_org RemorOrganization exposes engine_handle instead
        handle = getattr(engine, "engine_handle", None)
    if handle is None:
        raise AuthorizationError(
            "engine has no oracle handle: cannot mint engine caller context")
    return CallerContext.from_engine_handle(handle)
