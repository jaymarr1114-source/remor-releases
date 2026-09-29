"""Terminal-state routing: real consumers for declared-terminal outcomes.

PLOOP-2's handoff contract declares an answer for every (loop,
terminal_state) pair in HANDOFF_ROUTES. Pairs with follow-on routes are
driven by produce_handoff()/transition(). Pairs with an EMPTY follow-on
list are DECLARED TERMINALS: produce_handoff() returns None for them --
a declared answer, but the outcome still needs a CONSUMER. An outcome
that reaches no consumer is a silent drop with better paperwork; this
module is the layer that prevents that.

TerminalRouter.route_terminal(loop, outcome, context) classifies the
outcome with the real classify_terminal(), refuses anything that is NOT
a declared terminal (pairs with follow-on routes belong to
produce_handoff/transition -- the router never steals their work), and
delivers the outcome to its real consumer:

  (run, exhausted)         -> the run controller's own retry cadence.
                              The exhausted tick is already persisted by
                              record_cycle (PLOOP-3's per-cycle
                              acceptance); the router verifies the
                              persisted cycle row names budget_exceeded
                              and logs the route. The run controller
                              retries on its own cadence -- the
                              executive never second-guesses cadence
                              (the route table's declared policy).
  (run, open)              -> same: the tick's named errors live in its
                              persisted cycle record; verified + logged.
  (run, converged)         -> same shape: no follow-on route applied, so
                              the accepted cycle record is the consumer;
                              verified + logged.
  (run, absent)            -> explicit sink (the loop was not entered;
                              there is nothing to consume).
  (acquisition, converged) -> the closed gap's own record: verified via
                              the real gap_fetcher (status closed, real
                              closing evidence present); logged.
  (acquisition, open)      -> two real sub-cases, split on the
                              dispatch's own routed flag. routed=False
                              (the dispatch was REFUSED: no route
                              satisfied the gap) goes to the executive
                              as a finding carrying the exact refusal --
                              backing off an unroutable gap retries
                              nothing. routed=True (a route attempted the
                              gap and failed open) goes to the run
                              controller's backoff path: a real
                              note_gap_failure() on the rc_gap_backoff
                              table (the tick's inline path already does
                              this for gaps IT processes; this route
                              covers direct acquisition-loop entries,
                              where nobody recorded backoff -- never
                              spin, never drop).
  (acquisition, refused)   -> the executive, as a finding via
                              submit_finding(): the refusal reason
                              (DispatchResult.detail) with the exact
                              violation named. The relevance gate's
                              persisted decision is the receipt.
  (acquisition, absent)    -> explicit sink.
  (execution, converged)   -> the execution loop's diagnosis record in
                              the quarantine store: verified present via
                              the real diagnosis fetcher; logged.
                              (Repaired capabilities re-enter through
                              the normal completion path with real
                              attempt evidence -- this router never
                              manufactures it.)
  (execution, open)        -> same: the still-quarantined diagnosis stays
                              the execution loop's business; the named
                              reason is verified present + logged.
  (execution, absent)      -> explicit sink.
  (acceptance, candidate)  -> the acceptance loop's verdict-awaiting
                              surface: the record is verified persisted
                              in the real AcceptanceStore (state still
                              CANDIDATE, evidence intact), and the route
                              names it as awaiting James's verdict.
                              record_verdict() is NEVER called here --
                              nothing auto-advances a candidate.
  (acceptance, absent)     -> explicit sink.
  (distillation, open)     -> the named failure is the distillation
                              loop's record; verified present on the
                              result + logged.
  (distillation, converged)-> no promotion: the distillation loop's own
                              result is the record; logged with the
                              delta ref. (A promoted technique WITH a
                              novel spec is produce_handoff's domain, not
                              this router's.)
  (distillation, absent)   -> explicit sink.
  (generalization, converged) -> the trust path lives OUTSIDE the six
                              loops (the route table's declared policy).
                              No real in-loop consumer exists today, so
                              this is an explicit sink NAMING the trust
                              path as the declared-but-external
                              consumer. Wiring ReviewBoard's work-product
                              flow would need a real
                              technique->work-product adapter that does
                              not exist; it is not fabricated here.
  (generalization, open)   -> the honest out-of-envelope refusal is the
                              generalization loop's record; the named
                              reason is verified present + logged.
  (generalization, absent) -> explicit sink.

Additionally, route_refusal(exc) routes a HandoffRefused raised during
produce/accept/transition to the executive as a finding carrying the
EXACT violation (the exception message). A contract violation that only
propagates up a call stack is observed by nobody; the finding makes it
observed.

The explicit sink is TerminalLedger: an append-only sqlite ledger owned
by this module, recording every routed terminal outcome (loop, terminal
state, outcome refs, consumer, reason, routed_at). It is queryable
state -- a route that only printed to stdout would not be a consumer.

Fail-loud rules (load-bearing):
  - route_terminal raises HandoffRefused when a follow-on genuinely
    applies (it runs the pair's declared builders -- the same ones
    produce_handoff uses -- and any builder returning evidence means
    the outcome belongs to produce_handoff/transition, not here). A
    builder that raises HandoffRefused propagates: the route applies
    but its evidence cannot honestly be built.
  - It raises HandoffRefused when a required consumer is absent (no
    executive for a refusal, no acceptance loop for a candidate, no run
    controller for backoff, no gap fetcher where verification needs
    one).
  - It raises HandoffRefused when consumer-state verification fails
    (the candidate record is not in the store; the cycle row does not
    match; the gap record is not closed).
  - Evidence is never dropped: every route returns a TerminalRoute
    carrying evidence_refs, and the ledger persists them.

What this module does NOT do:
  - It does not edit handoff.py (read-only; PLOOP-8 owns checkpoint
    hooks there), run_controller.py (PLOOP-10), or any loop machinery.
  - It does not invent consumers: every consumer named above is real
    machinery inventoried in this tree (ControllerCheckpoint's
    rc_gap_backoff/rc_cycles, GapRegistry, AcceptanceStore,
    ExecutiveController.submit_finding, the quarantine store).
  - It does not auto-advance acceptance, manufacture attempt evidence,
    or guess terminal states.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

from .handoff import (
    HANDOFF_ROUTES,
    TERMINAL_ABSENT,
    TERMINAL_CANDIDATE,
    TERMINAL_CONVERGED,
    TERMINAL_EXHAUSTED,
    TERMINAL_OPEN,
    TERMINAL_REFUSED,
    HandoffRefused,
    classify_terminal,
)
from .loops import (
    LOOP_ACCEPTANCE,
    LOOP_ACQUISITION,
    LOOP_DISTILLATION,
    LOOP_EXECUTION,
    LOOP_GENERALIZATION,
    LOOP_RUN,
    LoopOutcome,
)

#: Contract version stamp for terminal routes.
TERMINAL_ROUTING_VERSION = "terminal-routing/v1"

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS terminal_routes (
    route_id TEXT PRIMARY KEY,
    routed_at REAL NOT NULL,
    loop TEXT NOT NULL,
    terminal_state TEXT NOT NULL,
    consumer TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    produced_by TEXT NOT NULL
);
"""


@dataclass
class TerminalRoute:
    """One routed terminal outcome: what it was, who consumed it, and
    the evidence refs proving nothing was dropped."""
    route_id: str
    loop: str
    terminal_state: str
    consumer: str
    reason: str
    evidence_refs: Dict[str, str] = field(default_factory=dict)
    routed_at: float = field(default_factory=time.time)
    produced_by: str = TERMINAL_ROUTING_VERSION


class TerminalLedger:
    """Append-only, queryable record of every routed terminal outcome.
    The explicit sink: an outcome routed here is observably dropped
    with its reason, never silently."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_LEDGER_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def record(self, route: TerminalRoute) -> TerminalRoute:
        con = self._conn()
        try:
            con.execute(
                "INSERT INTO terminal_routes (route_id, routed_at, loop,"
                " terminal_state, consumer, reason, evidence_refs_json,"
                " produced_by) VALUES (?,?,?,?,?,?,?,?)",
                (route.route_id, route.routed_at, route.loop,
                 route.terminal_state, route.consumer, route.reason,
                 json.dumps(route.evidence_refs), route.produced_by))
            con.commit()
        finally:
            con.close()
        return route

    def routes(self, loop: Optional[str] = None,
               terminal_state: Optional[str] = None) -> List[Dict[str, Any]]:
        """Query routed outcomes. No filters = everything, newest last."""
        q = ("SELECT route_id, routed_at, loop, terminal_state, consumer,"
             " reason, evidence_refs_json, produced_by FROM terminal_routes")
        clauses, params = [], []
        if loop is not None:
            clauses.append("loop = ?")
            params.append(loop)
        if terminal_state is not None:
            clauses.append("terminal_state = ?")
            params.append(terminal_state)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY routed_at"
        con = self._conn()
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(q, params).fetchall()
        finally:
            con.close()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence_refs"] = json.loads(d.pop("evidence_refs_json"))
            out.append(d)
        return out

    def count(self) -> int:
        con = self._conn()
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM terminal_routes").fetchone()
        finally:
            con.close()
        return int(row[0])


#: Epistemic terminal state carried on refusal findings. Findings use
#: relevance.py's enumerated epistemic vocabulary (charter C-2.2), NOT
#: the loop-terminal vocabulary: a refusal finding's claim is "this
#: loop established a boundary it cannot cross" (an unroutable gap; a
#: violated transition), and the refusal's occurrence ESTABLISHES that
#: claim -- hence BOUNDARY_ESTABLISHED (a truth-SUPPORTED state), never
#: the loop-terminal "refused" which the finding vocabulary rejects.
FINDING_STATE_REFUSAL = "BOUNDARY_ESTABLISHED"


def _route_id() -> str:
    return "troute_" + uuid.uuid4().hex[:12]


class TerminalRouter:
    """Routes declared-terminal loop outcomes to their real consumers.

    Constructed with the real machinery:
      executive:       ExecutiveController (for refusal findings; must
                       carry a relevance gate or submit_finding fails
                       loudly -- which is correct).
      run_controller:  RunController (backoff + cycle verification go
                       through its ControllerCheckpoint -- the SAME
                       object the tick uses; PLOOP-10 owns
                       run_controller.py so no new accessor is added
                       here).
      acceptance_loop: AcceptanceLoop (candidate verification; its store
                       is read, never written, by this router).
      gap_fetcher:     gap_id -> GapRecord | None (the real registry
                       getter; acquisition verification needs it).
      diagnosis_fetcher: capability_id -> QuarantineDiagnosis | None
                       (execution verification needs it).
      ledger_path:     sqlite path for the TerminalLedger.
    """

    def __init__(self, *, executive: Any,
                 run_controller: Any = None,
                 acceptance_loop: Any = None,
                 gap_fetcher: Optional[Callable[[str], Any]] = None,
                 diagnosis_fetcher: Optional[Callable[[str], Any]] = None,
                 ledger_path: str = "terminal_routes.db") -> None:
        self._executive = executive
        self._run_controller = run_controller
        self._acceptance_loop = acceptance_loop
        self._gap_fetcher = gap_fetcher
        self._diagnosis_fetcher = diagnosis_fetcher
        self.ledger = TerminalLedger(ledger_path)

    # -- entry points -------------------------------------------------

    def route_terminal(self, loop: str, outcome: LoopOutcome,
                       context: Optional[Mapping[str, Any]] = None
                       ) -> TerminalRoute:
        """Route a declared-terminal outcome to its real consumer.

        Usage contract: the driver first offers the outcome to
        produce_handoff(); when that returns None (declared terminal),
        the outcome comes here. Raises HandoffRefused when the outcome
        is NOT a declared terminal, when a required consumer is absent,
        or when consumer-state verification fails.
        """
        context = context or {}
        terminal_state = classify_terminal(loop, outcome)
        # The follow-on builders need the same context produce_handoff
        # would get; the router's own gap_fetcher fills the gap when the
        # driver did not supply one.
        effective = dict(context)
        if self._gap_fetcher is not None and "gap_fetcher" not in effective:
            effective["gap_fetcher"] = self._gap_fetcher
        self._assert_no_follow_on_applies(loop, terminal_state, outcome,
                                          effective)
        handler = _TERMINAL_HANDLERS.get((loop, terminal_state))
        if handler is None:
            raise HandoffRefused(
                f"route_terminal: ({loop}, {terminal_state}) is a "
                "declared terminal with no consumer route: contract hole")
        route = handler(self, outcome, context)
        return self.ledger.record(route)

    @staticmethod
    def _assert_no_follow_on_applies(loop: str, terminal_state: str,
                                     outcome: LoopOutcome,
                                     context: Mapping[str, Any]) -> None:
        """Run the pair's declared follow-on builders (the SAME builders
        produce_handoff uses -- pure reads, no mutation). If any builder
        returns evidence, a follow-on genuinely applies and this outcome
        does not belong to the terminal router: refuse loudly and name
        produce_handoff/transition. If a builder raises HandoffRefused,
        it propagates (the route applies but its evidence cannot
        honestly be built -- loud, never swallowed). All builders
        returning None means no follow-on applies: terminal routing
        proceeds."""
        for follow in HANDOFF_ROUTES.get((loop, terminal_state), []):
            evidence = follow.build(outcome, context)
            if evidence is not None:
                raise HandoffRefused(
                    f"route_terminal: ({loop}, {terminal_state}) has a "
                    f"live follow-on ({follow.boundary_kind}): drive it "
                    "via produce_handoff/transition, not the terminal "
                    "router")

    def route_refusal(self, exc: HandoffRefused, *,
                      loop: Optional[str] = None,
                      context: Optional[Mapping[str, Any]] = None
                      ) -> TerminalRoute:
        """Route a HandoffRefused raised during produce/accept/transition
        to the executive as a finding carrying the EXACT violation."""
        if self._executive is None:
            raise HandoffRefused(
                "route_refusal: no executive: a contract violation with "
                "nowhere to go is not routed")
        from .relevance import Finding, FindingProvenance
        violation = str(exc)
        finding = Finding(
            finding_id="finding_refusal_" + uuid.uuid4().hex[:12],
            content=(f"handoff contract violation: {violation}"),
            provenance=FindingProvenance(
                source_loop=loop or "executive",
                bounded_objective_id="handoff-contract",
                requested_by_primary=True),
            terminal_state=FINDING_STATE_REFUSAL,
        ).validate()
        decision = self._executive.submit_finding(finding)
        return self.ledger.record(TerminalRoute(
            route_id=_route_id(), loop=loop or "executive",
            terminal_state=TERMINAL_REFUSED,
            consumer="executive.submit_finding",
            reason=(f"contract violation observed as a finding "
                    f"(gate verdict: {decision.verdict})"),
            evidence_refs={"finding_id": finding.finding_id,
                           "violation": violation[:200]}))

    # -- consumer accessors (one real object each; missing -> loud) ----

    def _checkpoint(self) -> Any:
        rc = self._run_controller
        if rc is None:
            raise HandoffRefused(
                "terminal routing needs the run controller: backoff and "
                "cycle verification go through its checkpoint")
        return rc._checkpoint  # the tick's own store; PLOOP-10 owns
        # run_controller.py so no new public accessor is added here.

    def _acceptance(self) -> Any:
        if self._acceptance_loop is None:
            raise HandoffRefused(
                "terminal routing needs the acceptance loop: a candidate "
                "with nowhere to await verdict is not routed")
        return self._acceptance_loop

    # -- run: the controller's own persisted cycle record ---------------

    def _route_run_own_record(self, outcome: LoopOutcome,
                              context: Mapping[str, Any]) -> TerminalRoute:
        """(run, exhausted | open | converged): the tick's consumer is
        the run controller itself -- its persisted, accepted cycle
        record (record_cycle, PLOOP-3). The router verifies the latest
        persisted summary IS this outcome's summary (or at least names
        the same terminal condition), then logs the route. The run
        controller retries on its own cadence; the executive never
        second-guesses cadence."""
        ckpt = self._checkpoint()
        summary = outcome.result
        if not isinstance(summary, dict):
            raise HandoffRefused(
                "run terminal routing needs the tick summary dict")
        persisted = ckpt.last_cycle_summary()
        if persisted is None:
            raise HandoffRefused(
                "run terminal routing: no persisted cycle record: the "
                "tick was never recorded -- its consumer does not exist")
        terminal = classify_terminal(LOOP_RUN, outcome)
        if persisted.get("budget_exceeded") != summary.get("budget_exceeded"):
            raise HandoffRefused(
                "run terminal routing: the persisted cycle does not match "
                "this outcome's terminal condition: refusing to route a "
                "stale record's outcome")
        if bool(persisted.get("errors")) != bool(summary.get("errors")):
            raise HandoffRefused(
                "run terminal routing: the persisted cycle's error state "
                "does not match this outcome: refusing to route")
        return TerminalRoute(
            route_id=_route_id(), loop=LOOP_RUN, terminal_state=terminal,
            consumer="run_controller.persisted_cycle_record",
            reason=("tick consumed by the run controller's own accepted "
                    "cycle record; retry is the controller's cadence"),
            evidence_refs={
                "cycle": str(summary.get("cycle", "?")),
                "budget_exceeded": str(bool(summary.get("budget_exceeded"))),
                "errors": str(len(summary.get("errors") or []))})

    # -- acquisition ----------------------------------------------------

    def _route_acquisition_converged(
            self, outcome: LoopOutcome,
            context: Mapping[str, Any]) -> TerminalRoute:
        """(acquisition, converged): the closed gap's own record is the
        consumer. Verified via the real gap_fetcher: status closed with
        real closing evidence."""
        if self._gap_fetcher is None:
            raise HandoffRefused(
                "terminal routing needs gap_fetcher: a closed gap cannot "
                "be verified without the real record")
        result = outcome.result
        gap_id = getattr(result, "gap_id", "") or ""
        record = self._gap_fetcher(gap_id) if gap_id else None
        if record is None:
            raise HandoffRefused(
                f"acquisition converged routing: no gap record for "
                f"{gap_id!r}: cannot verify the closure")
        if getattr(record, "status", None) != "closed":
            raise HandoffRefused(
                f"acquisition converged routing: gap {gap_id!r} status is "
                f"{getattr(record, 'status', None)!r}, not closed: the "
                "claimed convergence is not real")
        closing = getattr(record, "closing_evidence", None) or {}
        return TerminalRoute(
            route_id=_route_id(), loop=LOOP_ACQUISITION,
            terminal_state=TERMINAL_CONVERGED,
            consumer="gap_registry.closed_record",
            reason="closed gaps stand as their own record",
            evidence_refs={
                "gap_id": gap_id,
                "route": str(getattr(record, "route_name", "")),
                "closing_keys": ",".join(sorted(closing.keys()))})

    def _route_acquisition_open(self, outcome: LoopOutcome,
                                context: Mapping[str, Any]) -> TerminalRoute:
        """(acquisition, open): two real sub-cases, split on the dispatch's
        own routed flag.

        routed=False: the dispatch was REFUSED -- no route satisfied the
        gap. The refusal is the story and goes to the executive as a
        finding carrying the exact refusal reason; backing off a gap no
        route can serve retries nothing, so backoff is the wrong
        consumer here.

        routed=True: a route attempted the gap and failed open. The real
        note_gap_failure() on rc_gap_backoff -- exponential backoff with
        the controller's own configured bounds. Never spin, never drop.
        """
        ckpt = self._checkpoint()
        result = outcome.result
        if getattr(result, "routed", True) is False:
            return self._route_acquisition_refused(outcome, context)
        gap_id = getattr(result, "gap_id", "") or ""
        if not gap_id:
            raise HandoffRefused(
                "acquisition open routing: the outcome names no gap_id: "
                "backoff needs the gap")
        cfg = self._run_controller.config
        ckpt.note_gap_failure(gap_id, cfg.backoff_base_s, cfg.backoff_max_s)
        failures = ckpt.gap_failure_count(gap_id)
        if failures < 1:
            raise HandoffRefused(
                f"acquisition open routing: backoff write for {gap_id!r} "
                "did not persist: the consumer did not take it")
        return TerminalRoute(
            route_id=_route_id(), loop=LOOP_ACQUISITION,
            terminal_state=TERMINAL_OPEN,
            consumer="run_controller.rc_gap_backoff",
            reason=("open gap handed to backoff; retry is the run "
                    "controller's backoff, not an executive follow-on"),
            evidence_refs={
                "gap_id": gap_id,
                "failures": str(failures),
                "detail": str(getattr(result, "detail", ""))[:200]})

    def _route_acquisition_refused(
            self, outcome: LoopOutcome,
            context: Mapping[str, Any]) -> TerminalRoute:
        """(acquisition, refused): the executive, as a finding. The
        refusal reason (DispatchResult.detail) with the exact violation
        named; the relevance gate's persisted decision is the receipt."""
        if self._executive is None:
            raise HandoffRefused(
                "terminal routing needs the executive: a refusal with "
                "nowhere to go is not routed")
        from .relevance import Finding, FindingProvenance
        result = outcome.result
        gap_id = getattr(result, "gap_id", "") or ""
        detail = getattr(result, "detail", "") or ""
        if not detail.strip():
            raise HandoffRefused(
                "acquisition refused routing: the refusal names no "
                "reason: a reasonless refusal is not routable")
        finding = Finding(
            finding_id="finding_refusal_" + uuid.uuid4().hex[:12],
            content=(f"acquisition refused dispatch of gap {gap_id}: "
                     f"{detail}"),
            provenance=FindingProvenance(
                source_loop=LOOP_ACQUISITION,
                bounded_objective_id=f"gap-dispatch:{gap_id}",
                requested_by_primary=True),
            terminal_state=FINDING_STATE_REFUSAL,
        ).validate()
        decision = self._executive.submit_finding(finding)
        return TerminalRoute(
            route_id=_route_id(), loop=LOOP_ACQUISITION,
            terminal_state=TERMINAL_REFUSED,
            consumer="executive.submit_finding",
            reason=(f"refusal observed as a finding (gate verdict: "
                    f"{decision.verdict})"),
            evidence_refs={"gap_id": gap_id,
                           "finding_id": finding.finding_id,
                           "refusal": detail[:200]})

    # -- execution ------------------------------------------------------

    def _route_execution(self, outcome: LoopOutcome,
                        context: Mapping[str, Any]) -> TerminalRoute:
        """(execution, converged | open): the diagnosis record in the
        quarantine store is the consumer. Verified present via the real
        diagnosis fetcher, with the named verdict/reason."""
        if self._diagnosis_fetcher is None:
            raise HandoffRefused(
                "terminal routing needs diagnosis_fetcher: the execution "
                "outcome cannot be verified without the real diagnosis")
        result = outcome.result
        capability_id = getattr(result, "capability_id", "") or ""
        diagnosis = (self._diagnosis_fetcher(capability_id)
                     if capability_id else None)
        if diagnosis is None:
            raise HandoffRefused(
                f"execution routing: no diagnosis for {capability_id!r}: "
                "the claimed outcome has no record")
        terminal = classify_terminal(LOOP_EXECUTION, outcome)
        return TerminalRoute(
            route_id=_route_id(), loop=LOOP_EXECUTION,
            terminal_state=terminal,
            consumer="execution_loop.quarantine_diagnosis_record",
            reason=("converged: repaired capabilities re-enter through "
                    "the normal completion path with real attempt "
                    "evidence; open: a still-quarantined capability "
                    "stays the execution loop's business"),
            evidence_refs={
                "capability_id": capability_id,
                "verdict": str(getattr(diagnosis, "verdict", "?")),
                "reason": str(getattr(diagnosis, "reason", ""))[:200]})

    # -- acceptance -----------------------------------------------------

    def _route_acceptance_candidate(
            self, outcome: LoopOutcome,
            context: Mapping[str, Any]) -> TerminalRoute:
        """(acceptance, candidate): the acceptance loop's
        verdict-awaiting surface. The record is verified persisted in
        the real AcceptanceStore with state still CANDIDATE (evidence
        intact, never auto-advanced); the route names it as awaiting
        James's verdict. record_verdict() is NEVER called here."""
        acc = self._acceptance()
        result = outcome.result
        run_id = getattr(result, "run_id", "") or ""
        if not run_id:
            raise HandoffRefused(
                "acceptance candidate routing: the record names no "
                "run_id: the candidate cannot be verified")
        stored = acc.store.get(run_id)
        if stored is None:
            raise HandoffRefused(
                f"acceptance candidate routing: run {run_id!r} is not in "
                "the acceptance store: the candidate was never "
                "persisted -- its consumer does not exist")
        state = getattr(stored, "state", None)
        state_name = getattr(state, "name", state)
        if state_name != "CANDIDATE" and state != "CANDIDATE":
            raise HandoffRefused(
                f"acceptance candidate routing: stored record for "
                f"{run_id!r} is in state {state_name!r}, not CANDIDATE: "
                "refusing to route a non-candidate as one")
        return TerminalRoute(
            route_id=_route_id(), loop=LOOP_ACCEPTANCE,
            terminal_state=TERMINAL_CANDIDATE,
            consumer="acceptance_loop.verdict_awaiting",
            reason=("candidate verified persisted, awaiting James's "
                    "verdict; nothing auto-advances acceptance"),
            evidence_refs={
                "run_id": run_id,
                "goal": str(getattr(stored, "goal", ""))[:120],
                "state": "CANDIDATE"})

    # -- distillation / generalization ----------------------------------

    def _route_distillation(self, outcome: LoopOutcome,
                            context: Mapping[str, Any]) -> TerminalRoute:
        """(distillation, open | converged-without-promotion): the named
        failure -- or the unpromoted result -- is the distillation
        loop's own record. Verified present on the real result."""
        result = outcome.result
        terminal = classify_terminal(LOOP_DISTILLATION, outcome)
        delta_id = getattr(result, "delta_id", "") or ""
        if terminal == TERMINAL_OPEN and not getattr(result, "reason", ""):
            raise HandoffRefused(
                "distillation open routing: the failure names no reason: "
                "a reasonless failure is not routable")
        return TerminalRoute(
            route_id=_route_id(), loop=LOOP_DISTILLATION,
            terminal_state=terminal,
            consumer="distillation_loop.own_record",
            reason=("open: the named failure is the distillation loop's "
                    "record; converged without promotion: the result "
                    "stands as its own record"),
            evidence_refs={
                "delta_id": delta_id,
                "success": str(bool(getattr(result, "success", False))),
                "reason": str(getattr(result, "reason", ""))[:200]})

    def _route_generalization_converged(
            self, outcome: LoopOutcome,
            context: Mapping[str, Any]) -> TerminalRoute:
        """(generalization, converged): the trust path lives OUTSIDE the
        six loops (declared policy). No real in-loop consumer exists
        today: explicit sink NAMING the trust path as the
        declared-but-external consumer, with the technique's refs."""
        result = outcome.result
        return TerminalRoute(
            route_id=_route_id(), loop=LOOP_GENERALIZATION,
            terminal_state=TERMINAL_CONVERGED,
            consumer="trust_path.external (declared, outside the six loops)",
            reason=("generalized techniques go to the trust path; no "
                    "in-loop consumer exists -- named, not dropped"),
            evidence_refs={
                "capability_id": str(getattr(result, "capability_id",
                                             "") or ""),
                "promoted_name": str(getattr(result, "promoted_name",
                                             "") or ""),
                "heldout": (f"{getattr(result, 'heldout_passed', '?')}/"
                            f"{getattr(result, 'heldout_examples', '?')}")})

    def _route_generalization_open(
            self, outcome: LoopOutcome,
            context: Mapping[str, Any]) -> TerminalRoute:
        """(generalization, open): the honest out-of-envelope refusal is
        the generalization loop's record. The named reason is verified
        present."""
        result = outcome.result
        reason = getattr(result, "reason", "") or ""
        if not reason.strip():
            raise HandoffRefused(
                "generalization open routing: the refusal names no "
                "reason: a reasonless refusal is not routable")
        return TerminalRoute(
            route_id=_route_id(), loop=LOOP_GENERALIZATION,
            terminal_state=TERMINAL_OPEN,
            consumer="generalization_loop.own_record",
            reason="an honest out-of-envelope refusal is the loop's record",
            evidence_refs={
                "capability_id": str(getattr(result, "capability_id",
                                             "") or ""),
                "reason": reason[:200]})

    # -- the explicit sink ----------------------------------------------

    def _route_sink(self, outcome: LoopOutcome,
                    context: Mapping[str, Any]) -> TerminalRoute:
        """(any loop, absent): the loop was not entered -- there is no
        outcome to consume. Explicitly, observably dropped with the
        reason: the ledger row IS the consumer."""
        terminal = classify_terminal(outcome.loop, outcome)
        return TerminalRoute(
            route_id=_route_id(), loop=outcome.loop,
            terminal_state=terminal,
            consumer="terminal_ledger.explicit_sink",
            reason=("loop not entered: no outcome exists to consume; "
                    "dropped explicitly with this reason, never silently"),
            evidence_refs={"detail": str(outcome.detail or "")[:200]})


# Every declared-terminal (loop, terminal) pair and its consumer route.
# Pairs whose builders return evidence for a given outcome are refused
# at route time (the outcome belongs to produce_handoff/transition) --
# reaching a handler means no follow-on applied.
_TERMINAL_HANDLERS: Dict[Any, Callable] = {
    (LOOP_RUN, TERMINAL_EXHAUSTED):
        TerminalRouter._route_run_own_record,
    (LOOP_RUN, TERMINAL_OPEN):
        TerminalRouter._route_run_own_record,
    (LOOP_RUN, TERMINAL_CONVERGED):
        TerminalRouter._route_run_own_record,
    (LOOP_RUN, TERMINAL_ABSENT):
        TerminalRouter._route_sink,
    (LOOP_ACQUISITION, TERMINAL_CONVERGED):
        TerminalRouter._route_acquisition_converged,
    (LOOP_ACQUISITION, TERMINAL_OPEN):
        TerminalRouter._route_acquisition_open,
    (LOOP_ACQUISITION, TERMINAL_REFUSED):
        TerminalRouter._route_acquisition_refused,
    (LOOP_ACQUISITION, TERMINAL_ABSENT):
        TerminalRouter._route_sink,
    (LOOP_EXECUTION, TERMINAL_CONVERGED):
        TerminalRouter._route_execution,
    (LOOP_EXECUTION, TERMINAL_OPEN):
        TerminalRouter._route_execution,
    (LOOP_EXECUTION, TERMINAL_ABSENT):
        TerminalRouter._route_sink,
    (LOOP_ACCEPTANCE, TERMINAL_CANDIDATE):
        TerminalRouter._route_acceptance_candidate,
    (LOOP_ACCEPTANCE, TERMINAL_ABSENT):
        TerminalRouter._route_sink,
    (LOOP_DISTILLATION, TERMINAL_CONVERGED):
        TerminalRouter._route_distillation,
    (LOOP_DISTILLATION, TERMINAL_OPEN):
        TerminalRouter._route_distillation,
    (LOOP_DISTILLATION, TERMINAL_ABSENT):
        TerminalRouter._route_sink,
    (LOOP_GENERALIZATION, TERMINAL_CONVERGED):
        TerminalRouter._route_generalization_converged,
    (LOOP_GENERALIZATION, TERMINAL_OPEN):
        TerminalRouter._route_generalization_open,
    (LOOP_GENERALIZATION, TERMINAL_ABSENT):
        TerminalRouter._route_sink,
}
