"""runtime/core/microcontroller/granted_cognition.py

The governed cognition inlet: GrantedCognitionProvider.

BRAIN-SCAFFOLD-1 (James, 2026-09-30): keep scaffolding the brain so REMOR
has something to grow into. Borrowed models are scaffolding with a
measured half-life; REMOR's own intelligence is what remains when the
model is taken away.

v3 retained core, enforced here:
  * ONE cognition inlet on the executive-owned substrate: install with
    substrate.set_cognition_provider(GrantedCognitionProvider(...)).
  * Cognition is exceptional: NATIVE FIRST. The native tier (REMOR's own
    deterministic machinery) is tried before any borrow, needs no grant,
    and never charges the FRM -- it is not borrowed intelligence.
  * Borrow only after a NAMED NATIVE REFUSAL, preserved as
    context["native_refusal"] on the borrow record.
  * NO GRANT means NO BORROW (refusal); INSUFFICIENT GRANT means DEFERRAL
    (never a silent partial charge). cognize never silently charges the
    FRM (U-6): every borrow consumes authorized resources under an
    explicit FrmGrant, through the substrate's existing charge path.
  * Every result records provenance: "native:<mechanism>" or
    "borrowed:<model>@<revision>".
  * Every call emits one telemetry event: borrow ratio by purpose and
    target profile, deferral rate, proposal survival rate -- the
    measurement of the borrow-to-native loop.

U-7 settlement -- three grant concepts, three distinct domains; the
inlet accepts exactly one:
  * FrmGrant (runtime/curiosity/frm/grant.py): the FRM resource grant --
    frozen, epoch-bounded. THE canonical grant contract. The only grant
    this inlet accepts.
  * primitives.Grant (runtime/primitives/core.py): a Governor EFFECT
    grant -- which effects a primitive may perform (Effect x target
    pattern). A permission, not a resource allocation. Never valid here.
  * attribution.Grant (runtime/curiosity/attribution/grants.py): an
    allocation-site metering RECORD (grant -> controller binding).
    Bookkeeping, not an authorization. Never valid here.
Passing any non-FrmGrant as "frm_grant" is refused fail-closed, with the
confusion named in the error.

The teacher behind the provider is a STUB until Qwen3 arrives
(QWEN3-ACQUIRE-1 is queued, not this mission): StubTeacher returns
honestly marked stub text -- never plausible-looking reasoning. The
governance (grant gating, charging, provenance, telemetry) is proven
without depending on any model weights.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from swarm_engine.core.microcontroller.substrate import (
    CognitionProvider,
    CognitionResult,
)
from swarm_engine.curiosity.frm.grant import FrmGrant


# ---------------------------------------------------------------------------
# refusal vocabulary (stable error prefixes; the reason after the colon)
# ---------------------------------------------------------------------------

R_NO_GRANT = "cognition_refused:no_grant"
R_BAD_GRANT = "cognition_refused:grant_not_frmgrant"
R_ENFORCEMENT = "cognition_refused:grant_enforcement_state"
R_DEFERRED = "cognition_deferred:insufficient_grant"
R_NATIVE_ERROR = "cognition_failed:native_error"

#: Enforcement states under which a grant authorizes nothing. Mirrors the
#: FRM's frozen rule: zero allocation while any enforcement state other
#: than RUNNING is active.
GRANT_BLOCKING_STATES = frozenset({"HARD_SHUTDOWN_RESOURCE", "WARNING_1",
                                   "SUSPENDED_SAFETY", "BANNED_6M"})


class NativeRefusal(Exception):
    """A NAMED refusal from the native tier: the native mechanism declares,
    by name, why it cannot handle this prompt. Only a NativeRefusal
    unlocks the borrow path. A native ok=False (malformed request) or an
    unexpected native exception fails closed WITHOUT borrowing -- a
    broken native mechanism must never silently become a model call."""

    def __init__(self, name: str, detail: str = "") -> None:
        super().__init__(f"native_refusal:{name}: {detail}")
        self.name = name
        self.detail = detail


# ---------------------------------------------------------------------------
# the teacher: an honest stub until Qwen3 arrives
# ---------------------------------------------------------------------------

class StubTeacher:
    """Placeholder reasoning teacher. Returns honestly marked stub text --
    never plausible-looking reasoning output. Exists to prove the
    governance (grant gating, charging, provenance, telemetry) without
    depending on any model weights. QWEN3-ACQUIRE-1 replaces it."""

    model_id = "stub-teacher"
    revision = "0.0.0"

    def estimate_cost_s(self, prompt: str) -> float:
        """Deterministic, documented cost estimate used for the
        insufficient-grant deferral check BEFORE any borrow happens."""
        return 0.5 + 0.001 * len(prompt or "")

    def complete(self, prompt: str,
                 context: Dict[str, Any]) -> Tuple[str, float]:
        """Returns (marked stub text, actual seconds). The text is
        unmistakably a stub: no caller can mistake it for reasoning."""
        start = time.monotonic()
        text = (
            f"[STUB-TEACHER {self.revision}] borrowed cognition is not "
            f"available: no model weights are installed behind this inlet. "
            f"This is a governance-proving stub, not a reasoning result. "
            f"prompt_chars={len(prompt or '')}")
        actual_s = max(time.monotonic() - start, 0.001)
        return text, actual_s


# ---------------------------------------------------------------------------
# telemetry: the measurement of the borrow-to-native loop
# ---------------------------------------------------------------------------

OUTCOME_NATIVE = "native"
OUTCOME_BORROWED = "borrowed"
OUTCOME_REFUSED_NO_GRANT = "refused_no_grant"
OUTCOME_REFUSED_BAD_GRANT = "refused_bad_grant"
OUTCOME_REFUSED_ENFORCEMENT = "refused_enforcement"
OUTCOME_DEFERRED = "deferred_insufficient_grant"
OUTCOME_NATIVE_ERROR = "native_error"


@dataclass
class CognitionEvent:
    seq: int
    at: float
    mc_id: str
    purpose: str
    target_profile: str
    outcome: str
    native_refusal: str = ""
    model_id: str = ""
    revision: str = ""
    charged_s: float = 0.0
    result_id: str = ""
    detail: str = ""


class CognitionTelemetry:
    """One event per cognize call, emitted from the real path. Queryable:
    borrow_ratio / deferral_rate / proposal_survival_rate, each filterable
    by purpose and target profile.

    The borrow-to-native loop is measured here: as REMOR grows its own
    intelligence (verified techniques, promoted native primitives), the
    borrow ratio for a purpose must FALL. A purpose whose ratio never
    falls is a standing gap, not a success."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: List[CognitionEvent] = []
        self._seq = 0
        # result_id -> survived (proposal survival is recorded by the
        # caller after it evaluates the borrowed result).
        self._proposals: Dict[str, bool] = {}
        self._proposal_purpose: Dict[str, str] = {}
        self._proposal_profile: Dict[str, str] = {}

    def record(self, *, mc_id: str, purpose: str, target_profile: str,
               outcome: str, native_refusal: str = "",
               model_id: str = "", revision: str = "",
               charged_s: float = 0.0, result_id: str = "",
               detail: str = "") -> CognitionEvent:
        self._seq += 1
        ev = CognitionEvent(
            seq=self._seq, at=self._clock(), mc_id=mc_id,
            purpose=purpose or "", target_profile=target_profile or "",
            outcome=outcome, native_refusal=native_refusal,
            model_id=model_id, revision=revision,
            charged_s=float(charged_s), result_id=result_id,
            detail=detail)
        self._events.append(ev)
        return ev

    def record_proposal_outcome(self, result_id: str, survived: bool, *,
                                purpose: str = "",
                                target_profile: str = "") -> None:
        """The caller reports whether a borrowed result survived its
        evaluation (admitted/used) or not. Only borrowed results with a
        recorded outcome count toward the survival rate."""
        self._proposals[result_id] = bool(survived)
        self._proposal_purpose[result_id] = purpose or ""
        self._proposal_profile[result_id] = target_profile or ""

    def _select(self, purpose: Optional[str],
                target_profile: Optional[str]) -> List[CognitionEvent]:
        return [e for e in self._events
                if (purpose is None or e.purpose == purpose)
                and (target_profile is None
                     or e.target_profile == target_profile)]

    def borrow_ratio(self, *, purpose: Optional[str] = None,
                     target_profile: Optional[str] = None
                     ) -> Optional[float]:
        """Borrowed / (native + borrowed) over resolved cognition calls.
        None when no resolved calls match (honest, not zero)."""
        evs = [e for e in self._select(purpose, target_profile)
               if e.outcome in (OUTCOME_NATIVE, OUTCOME_BORROWED)]
        if not evs:
            return None
        borrowed = sum(1 for e in evs if e.outcome == OUTCOME_BORROWED)
        return borrowed / len(evs)

    def deferral_rate(self, *, purpose: Optional[str] = None,
                      target_profile: Optional[str] = None) -> float:
        evs = self._select(purpose, target_profile)
        if not evs:
            return 0.0
        deferred = sum(1 for e in evs if e.outcome == OUTCOME_DEFERRED)
        return deferred / len(evs)

    def proposal_survival_rate(self, *, purpose: Optional[str] = None,
                               target_profile: Optional[str] = None
                               ) -> Optional[float]:
        """Survived / recorded proposals. None when no proposal outcome
        has been recorded (honest, not zero)."""
        ids = [rid for rid, p in self._proposal_purpose.items()
               if (purpose is None or p == purpose)
               and (target_profile is None
                    or self._proposal_profile.get(rid) == target_profile)]
        if not ids:
            return None
        survived = sum(1 for rid in ids if self._proposals.get(rid))
        return survived / len(ids)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for e in self._events:
            out[e.outcome] = out.get(e.outcome, 0) + 1
        return out

    @property
    def events(self) -> List[CognitionEvent]:
        return list(self._events)


# ---------------------------------------------------------------------------
# the governed inlet
# ---------------------------------------------------------------------------

class GrantedCognitionProvider:
    """The one governed cognition inlet behind the executive-owned
    substrate. Install with substrate.set_cognition_provider(...).

    Per-call flow:
      1. NATIVE FIRST: the native tier (REMOR's own deterministic
         machinery) is tried without any grant and never charges the FRM
         -- it is not borrowed intelligence. A native ok (or a native
         fail-closed ok=False) is terminal: no borrow follows.
      2. Only a NativeRefusal(name) unlocks the borrow path. The name is
         preserved as context["native_refusal"] on the borrow record.
      3. BORROW (grant-gated): context["frm_grant"] must be an FrmGrant
         issued under RUNNING enforcement. No grant -> refusal.
         Non-FrmGrant -> refusal naming the confusion (U-7). Estimated
         cost beyond the grant's remaining budget -> deferral, with no
         charge of any kind.
      4. The teacher completes; provenance "borrowed:<model>@<revision>"
         is recorded on the result; actual seconds are consumed from the
         grant's ledger AND charged through the substrate's existing
         charge path (U-6: never silent).
      5. Every call emits exactly one telemetry event.
    """

    def __init__(self, substrate: Any, *,
                 native: Optional[CognitionProvider] = None,
                 teacher: Optional[StubTeacher] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._substrate = substrate
        self._native = native
        self._teacher = teacher or StubTeacher()
        self._clock = clock
        self._telemetry = CognitionTelemetry(clock=clock)
        # grant_id -> seconds consumed (per-epoch; pruned as epochs advance)
        self._consumed: Dict[str, float] = {}
        self._consumed_epoch: Dict[str, int] = {}

    @property
    def telemetry(self) -> CognitionTelemetry:
        return self._telemetry

    @property
    def teacher(self) -> StubTeacher:
        return self._teacher

    # -- the inlet ----------------------------------------------------

    def request_cognition(self, *, mc_id: str, prompt: str,
                          context: Dict[str, Any]) -> CognitionResult:
        ctx = dict(context or {})
        purpose = str(ctx.get("purpose") or "")
        target_profile = str(ctx.get("target_profile") or "")
        result_id = uuid.uuid4().hex[:12]

        if self._native is not None:
            try:
                nres = self._native.request_cognition(
                    mc_id=mc_id, prompt=prompt, context=ctx)
            except NativeRefusal as nr:
                return self._borrow(
                    mc_id, prompt, ctx, result_id, purpose, target_profile,
                    native_refusal=nr.name, refusal_detail=nr.detail)
            except Exception as exc:  # noqa: BLE001 -- fail closed, never borrow
                self._telemetry.record(
                    mc_id=mc_id, purpose=purpose,
                    target_profile=target_profile,
                    outcome=OUTCOME_NATIVE_ERROR, result_id=result_id,
                    detail=f"{type(exc).__name__}: {exc}")
                return CognitionResult(
                    ok=False,
                    error=f"{R_NATIVE_ERROR}: native tier raised "
                          f"{type(exc).__name__}; failing closed without "
                          f"borrowing",
                    result_id=result_id)
            # Native answered (ok, or fail-closed ok=False for a malformed
            # request): terminal. A malformed native request is not a
            # borrow trigger.
            self._telemetry.record(
                mc_id=mc_id, purpose=purpose,
                target_profile=target_profile, outcome=OUTCOME_NATIVE,
                result_id=result_id)
            nres.provenance = f"native:{type(self._native).__name__}"
            nres.result_id = result_id
            return nres

        refusal = str(ctx.get("native_refusal") or "no_native_tier_registered")
        return self._borrow(mc_id, prompt, ctx, result_id, purpose,
                            target_profile, native_refusal=refusal,
                            refusal_detail="")

    # -- the borrow path (grant-gated) ---------------------------------

    def _borrow(self, mc_id: str, prompt: str, ctx: Dict[str, Any],
                result_id: str, purpose: str, target_profile: str, *,
                native_refusal: str, refusal_detail: str) -> CognitionResult:
        grant = ctx.get("frm_grant")

        if grant is None:
            self._telemetry.record(
                mc_id=mc_id, purpose=purpose,
                target_profile=target_profile,
                outcome=OUTCOME_REFUSED_NO_GRANT,
                native_refusal=native_refusal, result_id=result_id,
                detail="borrow requires context['frm_grant']")
            return CognitionResult(
                ok=False,
                error=f"{R_NO_GRANT}: borrowing cognition requires "
                      f"context['frm_grant'] holding an FrmGrant; native "
                      f"refused as {native_refusal!r}",
                native_refusal=native_refusal, result_id=result_id)

        if not isinstance(grant, FrmGrant):
            self._telemetry.record(
                mc_id=mc_id, purpose=purpose,
                target_profile=target_profile,
                outcome=OUTCOME_REFUSED_BAD_GRANT,
                native_refusal=native_refusal, result_id=result_id,
                detail=f"got {type(grant).__module__}."
                       f"{type(grant).__name__}")
            return CognitionResult(
                ok=False,
                error=f"{R_BAD_GRANT}: the cognition inlet accepts only "
                      f"FrmGrant (runtime/curiosity/frm/grant.py); got "
                      f"{type(grant).__module__}.{type(grant).__name__}. "
                      f"Effect grants (primitives.Grant) and allocation "
                      f"records (attribution.Grant) are not resource "
                      f"authorizations",
                native_refusal=native_refusal, result_id=result_id)

        if grant.enforcement_state_at_issue != "RUNNING":
            self._telemetry.record(
                mc_id=mc_id, purpose=purpose,
                target_profile=target_profile,
                outcome=OUTCOME_REFUSED_ENFORCEMENT,
                native_refusal=native_refusal, result_id=result_id,
                detail=f"enforcement_state_at_issue="
                       f"{grant.enforcement_state_at_issue}; known "
                       f"blocking states={sorted(GRANT_BLOCKING_STATES)}; "
                       f"unknown states also block (fail closed)")
            return CognitionResult(
                ok=False,
                error=f"{R_ENFORCEMENT}: grant {grant.grant_id[:8]} was "
                      f"issued under enforcement state "
                      f"{grant.enforcement_state_at_issue!r}; zero "
                      f"allocation while any enforcement state is active",
                native_refusal=native_refusal, result_id=result_id)

        self._prune_epochs(grant.epoch_id)
        consumed = self._consumed.get(grant.grant_id, 0.0)
        estimate = self._teacher.estimate_cost_s(prompt)
        if consumed + estimate > grant.budget_s + 1e-9:
            self._telemetry.record(
                mc_id=mc_id, purpose=purpose,
                target_profile=target_profile, outcome=OUTCOME_DEFERRED,
                native_refusal=native_refusal, result_id=result_id,
                detail=f"estimate {estimate:.3f}s + consumed "
                       f"{consumed:.3f}s > budget {grant.budget_s:.3f}s; "
                       f"no charge made")
            return CognitionResult(
                ok=False,
                error=f"{R_DEFERRED}: estimated cost {estimate:.3f}s plus "
                      f"already-consumed {consumed:.3f}s exceeds grant "
                      f"{grant.grant_id[:8]} budget {grant.budget_s:.3f}s; "
                      f"deferred, nothing charged",
                native_refusal=native_refusal, result_id=result_id)

        text, actual_s = self._teacher.complete(prompt, ctx)
        self._consumed[grant.grant_id] = consumed + actual_s
        self._consumed_epoch[grant.grant_id] = grant.epoch_id
        # U-6: the charge travels the substrate's existing charge path --
        # visible on the microcontroller's budget, never silent.
        exhausted, charge_state = self._substrate.charge(mc_id, actual_s)
        provenance = (f"borrowed:{self._teacher.model_id}"
                      f"@{self._teacher.revision}")
        detail = (f"mc_charge_state={charge_state} exhausted={exhausted}")
        if refusal_detail:
            detail += f" native_refusal_detail={refusal_detail}"
        self._telemetry.record(
            mc_id=mc_id, purpose=purpose, target_profile=target_profile,
            outcome=OUTCOME_BORROWED, native_refusal=native_refusal,
            model_id=self._teacher.model_id,
            revision=self._teacher.revision, charged_s=actual_s,
            result_id=result_id, detail=detail)
        return CognitionResult(
            ok=True, text=text, provenance=provenance,
            native_refusal=native_refusal, result_id=result_id)

    def grant_consumed_s(self, grant_id: str) -> float:
        """Seconds consumed against a grant id (for tests and gates)."""
        return self._consumed.get(grant_id, 0.0)

    def _prune_epochs(self, current_epoch: int) -> None:
        stale = [gid for gid, ep in self._consumed_epoch.items()
                 if ep < current_epoch]
        for gid in stale:
            self._consumed.pop(gid, None)
            self._consumed_epoch.pop(gid, None)
