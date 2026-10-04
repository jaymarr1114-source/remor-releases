"""The Curiosity Executive Controller: level 1 of the curiosity hierarchy.

"What boundary exists?" -> "Which loop owns it?" -> "Enter that loop."

Mirrors the Primary ExecutiveController's outline (executive function ->
loop controllers -> microcontrollers): it selects the loop owning the
active boundary, sets priority, and accepts/refuses activation requests
on kill state, grants, and fit. It NEVER operates runtime machinery --
it holds no substrate reference and consumes only LoopView aggregates
(C-6.3); the Curiosity Run Controller owns cadence and execution.

Fail-closed activation:
  - trigger fails validation -> refused (INVALID_TRIGGER).
  - enforcement state in {HARD_SHUTDOWN_RESOURCE, SUSPENDED_SAFETY,
    BANNED_6M} -> refused (KILL_STATE). WARNING_1 passes through: the
    FRM restricts the grant per the decided D-3 rules.
  - boundary class owned by a loop with no real machinery yet -> refused
    (LOOP_ABSENT): the absence is named, nothing is entered, nothing is
    faked. Unknown boundary class -> refused (UNOWNED).
  - latest roll-call attestation missing or not MET -> refused
    (ATTESTATION_INVALID): a missing or non-MET roll-call prevents
    activation. Refusing to START new work under an anomalous or absent
    attestation is not judging -- GAM still owns the attestation,
    enforcement still owns the ban.
  - FRM curiosity grant budget_s <= 0 -> refused (NO_BUDGET): an inquiry
    with no budget does not start (C-4.2).
  - FRM curiosity grant max_concurrent < 1 -> refused (NO_SLOT): a grant
    that funds no concurrency slot cannot run an inquiry. The FRM's
    restriction is taken as issued -- the executive never inflates it.
  - trigger fails the fit predicates -> refused (FIT): the failed
    predicate is named.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_GENERATIVE_PROMPT,
    BOUNDARY_HYPOTHESIS_CANDIDATE,
    BOUNDARY_IMPRECISE_QUESTION,
    BOUNDARY_NOVEL_OBSERVATION,
    BOUNDARY_NOVEL_PATTERN,
    BOUNDARY_NOVEL_TASK,
    CuriosityTrigger,
    TriggerRefused,
)
from swarm_engine.curiosity.substrate import (
    LOOP_QUESTIONING, LOOP_SCIENTIFIC_INQUIRY,
    LOOP_CREATIVE_EXPLORATION, LOOP_DISCOVERY_NOVELTY)

#: Declared ownership map: boundary class -> owning loop. Phase 2 owns
#: exactly one boundary class; "scientific_inquiry" admitted by James's
#: U-1-class decision 2026-10-01 (CUR-P3A-INT).
LOOP_OWNERSHIP: Dict[str, str] = {
    BOUNDARY_IMPRECISE_QUESTION: LOOP_QUESTIONING,
    BOUNDARY_HYPOTHESIS_CANDIDATE: LOOP_SCIENTIFIC_INQUIRY,
    BOUNDARY_NOVEL_OBSERVATION: LOOP_SCIENTIFIC_INQUIRY,
    BOUNDARY_GENERATIVE_PROMPT: LOOP_CREATIVE_EXPLORATION,
    BOUNDARY_NOVEL_PATTERN: LOOP_DISCOVERY_NOVELTY,
}

#: Boundary classes whose owning loops do not exist yet (Phase 3+).
#: The executive names the absence; it never routes them elsewhere.
ABSENT_OWNERSHIP: Dict[str, str] = {
    BOUNDARY_NOVEL_TASK: "generalization (Phase 3)",
}

#: Enforcement states that refuse new curiosity activation outright.
KILL_STATES = frozenset({
    "HARD_SHUTDOWN_RESOURCE", "SUSPENDED_SAFETY", "BANNED_6M"})

#: Priority rule (declared): a Primary-requested inquiry outranks a
#: curiosity-initiated one. Documented, not learned.
PRIORITY = {"PRIMARY_REQUESTED": 0, "CURIOUSITY_INITIATED": 1}

#: Interrogative markers: a trigger that carries none of these is not
#: question-shaped and fails the fit check.
INTERROGATIVES = (
    "?", "what", "which", "how", "why", "whether", "when", "where",
    "who", "whom", "whose", "is ", "are ", "can ", "could ", "does ",
    "do ", "did ", "will ", "would ", "should ", "has ", "have ",
)

#: A bounded objective must be bounded: overlong objectives are refused
#: as unbounded scope (the fit check, not a value judgment).
MAX_OBJECTIVE_CHARS = 280

R_INVALID_TRIGGER = "INVALID_TRIGGER"
R_KILL_STATE = "KILL_STATE"
R_LOOP_ABSENT = "LOOP_ABSENT"
R_UNOWNED = "UNOWNED"
R_ATTESTATION_INVALID = "ATTESTATION_INVALID"
R_NO_BUDGET = "NO_BUDGET"
R_NO_SLOT = "NO_SLOT"
R_FIT = "FIT"


class ActivationRefused(Exception):
    """An activation request was refused. The reason code names the
    exact refusal; the message carries the evidence (state, epoch,
    failed predicate). Refusals are loud, never silent."""


@dataclass
class ActivationDecision:
    """The executive's verdict on one trigger."""
    approved: bool
    trigger: CuriosityTrigger
    loop: Optional[str] = None
    priority: Optional[int] = None
    grant: Any = None            # FrmGrant (frozen) when approved
    epoch_id: Optional[int] = None
    enforcement_state: str = ""
    roll_call: str = ""
    notes: List[str] = field(default_factory=list)
    refusal_reason: str = ""
    refusal_detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        grant = self.grant
        return {
            "approved": self.approved,
            "trigger_id": self.trigger.trigger_id,
            "loop": self.loop,
            "priority": self.priority,
            "grant": (grant.as_dict() if grant is not None else None),
            "epoch_id": self.epoch_id,
            "enforcement_state": self.enforcement_state,
            "roll_call": self.roll_call,
            "notes": list(self.notes),
            "refusal_reason": self.refusal_reason,
            "refusal_detail": self.refusal_detail,
        }


@dataclass
class CuriosityLoopOutcome:
    """What entering a curiosity loop produced. `view` is a LoopView dict
    (loop-level aggregates only) or None when nothing ran."""
    loop: str
    entered: bool
    result: Any = None
    detail: str = ""
    view: Any = None


class CuriosityExecutive:
    """Level 1: selects the owning loop and enters it through the Run
    Controller. Constructed with the real governance machinery; the
    substrate is deliberately NOT a constructor argument (C-6.3).

    frm: FinancialResourceManager (real FRM evaluation layer).
    enforcement_state_dir: directory holding the enforcement state store;
        read pull-only via the governance read API.
    gam: GovernanceAttestationMonitor (pull-only roll_call_status).
    run_controller: CuriosityRunController (owns cadence + substrate).
    demand_budget_s / demand_concurrent: the curiosity demand stated per
        contention round when no demand_bridge is bound (the standing
        fallback, named loudly in the decision notes).
    demand_bridge: FrmBridge (optional). When bound, the FRM round's
        demands are STATED BY THE REAL CONTROLLERS via the bridge
        (primary from the Primary RunController's arbitration_status,
        curiosity from this run_controller's inquiry_views) -- SEAM-WIRE-1.
        Unbound, the standing fallback demands apply and the notes say so.
    """

    def __init__(self, *, frm: Any, enforcement_state_dir: str,
                 gam: Any, run_controller: Any,
                 demand_budget_s: float = 60.0,
                 demand_concurrent: int = 2,
                 demand_bridge: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._frm = frm
        self._enforcement_state_dir = enforcement_state_dir
        self._gam = gam
        self._run_controller = run_controller
        self._demand_budget_s = demand_budget_s
        self._demand_concurrent = demand_concurrent
        self._demand_bridge = demand_bridge
        self._clock = clock

    # -- activation -----------------------------------------------------

    def request_activation(self, trigger: CuriosityTrigger
                           ) -> ActivationDecision:
        """Run the fail-closed activation checks in order. Returns an
        approved decision or raises ActivationRefused."""
        try:
            trigger.validate()
        except TriggerRefused as exc:
            raise ActivationRefused(
                f"{R_INVALID_TRIGGER}: {exc}") from exc

        # 1. Kill state (pull-only read of the governance plane).
        enforcement_state = self._read_enforcement_state()
        if enforcement_state in KILL_STATES:
            raise ActivationRefused(
                f"{R_KILL_STATE}: enforcement state is "
                f"{enforcement_state}: no new curiosity activation while "
                "the domain is killed/suspended/banned")

        # 2. Loop ownership.
        loop = LOOP_OWNERSHIP.get(trigger.boundary_class)
        if loop is None:
            absent_owner = ABSENT_OWNERSHIP.get(trigger.boundary_class)
            if absent_owner is not None:
                raise ActivationRefused(
                    f"{R_LOOP_ABSENT}: boundary class "
                    f"{trigger.boundary_class!r} is owned by "
                    f"{absent_owner}, which has no real machinery yet: "
                    "the absence is named, nothing is entered")
            raise ActivationRefused(
                f"{R_UNOWNED}: boundary class "
                f"{trigger.boundary_class!r} is owned by no curiosity "
                "loop")

        # 3. Roll-call fact (pull-only; GAM owns the attestation).
        roll_call, roll_notes = self._check_roll_call()

        # 4. FRM grant: a real contention-round evaluation. No budget ->
        # the inquiry does not start (C-4.2). Demands are stated by the
        # real controllers when a demand_bridge is bound (SEAM-WIRE-1);
        # otherwise the standing fallback values apply, named loudly.
        from swarm_engine.curiosity.frm.policy import DomainDemand
        if self._demand_bridge is not None:
            demands = self._demand_bridge.demands()
            primary_demand = demands.primary
            curiosity_demand = demands.curiosity
            demand_note = (
                "FRM demands stated by the controllers: "
                f"primary({demands.provenance['primary']}); "
                f"curiosity({demands.provenance['curiosity']})")
        else:
            primary_demand = DomainDemand(domain="primary",
                                         budget_s=0.0, max_concurrent=0)
            curiosity_demand = DomainDemand(
                domain="curiosity", budget_s=self._demand_budget_s,
                max_concurrent=self._demand_concurrent)
            demand_note = (
                "FRM demands are the standing fallback (no demand_bridge "
                "bound): primary=0, curiosity="
                f"{self._demand_budget_s}s/{self._demand_concurrent}")
        round_ = self._frm.evaluate_round(
            enforcement_state=enforcement_state,
            primary_demand=primary_demand,
            curiosity_demand=curiosity_demand,
        )
        grant = round_.grants["curiosity"]
        notes = list(roll_notes)
        notes.append(demand_note)
        if round_.mid_epoch_refusal:
            notes.append(
                f"FRM mid-epoch: reusing active epoch {round_.epoch_id} "
                "grants (non-preemptive)")
        if grant.budget_s <= 0:
            raise ActivationRefused(
                f"{R_NO_BUDGET}: FRM epoch {round_.epoch_id} grants "
                f"curiosity {grant.budget_s:.3f}s: an inquiry with no "
                "budget does not start")
        if grant.max_concurrent < 1:
            raise ActivationRefused(
                f"{R_NO_SLOT}: FRM epoch {round_.epoch_id} grants "
                f"curiosity {grant.budget_s:.3f}s with "
                f"{grant.max_concurrent} concurrency slots: no inquiry "
                "can run under this grant (the FRM's restriction stands; "
                "the executive does not inflate it)")

        # 5. Fit: the trigger must be question-shaped and bounded.
        fit_notes = self._check_fit(trigger, loop)

        return ActivationDecision(
            approved=True,
            trigger=trigger,
            loop=loop,
            priority=PRIORITY[trigger.origin],
            grant=grant,
            epoch_id=round_.epoch_id,
            enforcement_state=enforcement_state,
            roll_call=roll_call,
            notes=notes + fit_notes,
        )

    def _read_enforcement_state(self) -> str:
        from swarm_engine.governance.curiosity_enforcement.read_api import (
            read_state)
        rec = read_state(self._enforcement_state_dir)
        if rec is None:
            # Mirrors the enforcement engine's own bootstrap semantic
            # (EnforcementEngine.current: a domain with no record is
            # implicitly RUNNING). The authority defines this, not us.
            return "RUNNING"
        state = rec.state.value if hasattr(rec.state, "value") else rec.state
        return str(state)

    def _check_roll_call(self):
        status = self._gam.roll_call_status("curiosity")
        latest = (status or {}).get("latest_attestation")
        if latest is None:
            raise ActivationRefused(
                f"{R_ATTESTATION_INVALID}: no roll-call attestation on "
                "record for the curiosity domain: missing roll-call "
                "prevents activation")
        classification = latest.get("classification", "UNKNOWN")
        if classification != "MET":
            raise ActivationRefused(
                f"{R_ATTESTATION_INVALID}: latest roll-call attestation "
                f"{latest.get('attestation_id')} classified "
                f"{classification} "
                f"({latest.get('validation_detail')}): non-MET roll-call "
                "prevents activation")
        return classification, []

    def _check_fit(self, trigger: CuriosityTrigger, loop: str
                   ) -> List[str]:
        notes: List[str] = []
        if loop == LOOP_QUESTIONING:
            text = (trigger.question_text or "")
            lowered = text.lower()
            if not any(m in lowered for m in INTERROGATIVES):
                raise ActivationRefused(
                    f"{R_FIT}: trigger {trigger.trigger_id} is not "
                    "question-shaped (no interrogative marker): the "
                    "questioning loop cannot own it")
            notes.append("fit: question-shaped (interrogative marker "
                         "present)")
            if len(trigger.bounded_objective) > MAX_OBJECTIVE_CHARS:
                raise ActivationRefused(
                    f"{R_FIT}: bounded_objective is "
                    f"{len(trigger.bounded_objective)} chars "
                    f"(>{MAX_OBJECTIVE_CHARS}): unbounded scope refused")
            notes.append("fit: bounded objective "
                         f"({len(trigger.bounded_objective)} chars)")
        elif loop == LOOP_SCIENTIFIC_INQUIRY:
            # Inquiry-shaped: the trigger's question_text carries the
            # hypothesis candidate / novel observation presentation. The
            # loop itself judges falsifiability (form_hypothesis); the
            # executive checks shape only -- mirroring the questioning
            # division of labor.
            text = (trigger.question_text or "").strip()
            if not text:
                raise ActivationRefused(
                    f"{R_FIT}: trigger {trigger.trigger_id} carries no "
                    "hypothesis/observation presentation (empty "
                    "question_text): the scientific_inquiry loop cannot "
                    "own it")
            notes.append("fit: inquiry-shaped (hypothesis/observation "
                         "presentation present)")
            if len(trigger.bounded_objective) > MAX_OBJECTIVE_CHARS:
                raise ActivationRefused(
                    f"{R_FIT}: bounded_objective is "
                    f"{len(trigger.bounded_objective)} chars "
                    f"(>{MAX_OBJECTIVE_CHARS}): unbounded scope refused")
            notes.append("fit: bounded objective "
                         f"({len(trigger.bounded_objective)} chars)")
        return notes

    # -- entry ----------------------------------------------------------

    def activate(self, decision: ActivationDecision) -> CuriosityLoopOutcome:
        """Enter the loop through the Run Controller and drive the
        inquiry to terminal. The executive never touches microcontrollers:
        the outcome's view is a LoopView aggregate."""
        if not decision.approved:
            raise ActivationRefused(
                f"cannot activate a refused decision "
                f"({decision.refusal_reason})")
        result = self._run_controller.run_inquiry(decision)
        return CuriosityLoopOutcome(
            loop=decision.loop or "",
            entered=True,
            result=result,
            detail=(f"inquiry {result.get('inquiry_id')}: "
                    f"{result.get('terminal_state')} -> evidence "
                    f"{result.get('evidence_id')}"),
            view=self._run_controller.loop_view(decision.loop or ""),
        )

    def kill_inquiry(self, inquiry_id: str, reason: str) -> Dict[str, Any]:
        """The kill switch: executive -> run controller -> loop ->
        microcontroller. Propagated, never executed here."""
        return self._run_controller.kill_inquiry(inquiry_id, reason)

    def resume_inquiry(self, inquiry_id: str) -> Dict[str, Any]:
        return self._run_controller.resume_inquiry(inquiry_id)

    # -- observable state (C-6.3: loop-level aggregates ONLY) -------------

    def executive_view(self) -> Dict[str, Any]:
        """What the executive may observe: per-inquiry loop-level
        aggregates and LoopView dicts. No microcontroller ids, purposes,
        or graph-traversal detail -- enforced by construction (the
        executive holds no substrate reference; views come from the run
        controller as LoopView dicts)."""
        return {
            "inquiries": self._run_controller.inquiry_views(),
            "loops": {
                LOOP_QUESTIONING:
                    self._run_controller.loop_view(LOOP_QUESTIONING),
                LOOP_SCIENTIFIC_INQUIRY:
                    self._run_controller.loop_view(LOOP_SCIENTIFIC_INQUIRY),
            },
            "produced_at": self._clock(),
        }

    # -- introspection for the structural proof ---------------------------

    @property
    def held_references(self) -> Dict[str, str]:
        """Exactly what this executive can reach. The proof asserts no
        substrate / microcontroller / graph handle appears here."""
        return {
            "frm": type(self._frm).__name__,
            "gam": type(self._gam).__name__,
            "run_controller": type(self._run_controller).__name__,
            "enforcement_state_dir": "path (pull-only reads)",
        }
