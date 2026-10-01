#!/usr/bin/env python3
"""FRM-STUDENT-1: the governed student serving inlet.

Wraps distill1/server_client.py (the DISTILL-2 persistent-server path)
with FRM governance, mirroring runtime/core/microcontroller/
granted_cognition.py's grant-enforcement pattern:

  * No grant -> refusal (student_refused:no_grant).
  * Non-FrmGrant -> refusal naming the confusion (U-7).
  * Grant issued under a non-RUNNING enforcement state -> refusal.
  * Estimated cost beyond remaining budget -> deferral, zero charge.
  * Concurrent turns beyond the grant's max_concurrent -> deferral.
  * After a successful turn, actual wall seconds are charged through
    the substrate's existing charge path (substrate.charge) and
    recorded against the grant id. A failed turn charges nothing.

server_client.py itself is untouched (DEVICE-SIM-1 uses it directly).
This module is the governed inlet; use it wherever student inference
must be metered.

Provenance on every served turn:
  governed-student:qwen3-0.6b@23749fefcc72300e3a2ad315e1317431b06b590a
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

import server_client
from swarm_engine.curiosity.frm.grant import FrmGrant

STUDENT_MODEL_ID = "qwen3-0.6b"
STUDENT_REVISION = "23749fefcc72300e3a2ad315e1317431b06b590a"
STUDENT_PROVENANCE = f"governed-student:{STUDENT_MODEL_ID}@{STUDENT_REVISION}"

# ---------------------------------------------------------------------------
# refusal vocabulary (stable error prefixes; mirrors granted_cognition.py)
# ---------------------------------------------------------------------------

R_NO_GRANT = "student_refused:no_grant"
R_BAD_GRANT = "student_refused:grant_not_frmgrant"
R_ENFORCEMENT = "student_refused:grant_enforcement_state"
R_DEFERRED_BUDGET = "student_deferred:insufficient_grant"
R_DEFERRED_CONCURRENT = "student_deferred:grant_max_concurrent"

GRANT_BLOCKING_STATES = frozenset({"HARD_SHUTDOWN_RESOURCE", "WARNING_1",
                                   "SUSPENDED_SAFETY", "BANNED_6M"})


class GovernedStudent:
    """The governed inlet for the local student serving path.

    substrate: an object with charge(mc_id, seconds) -> (exhausted, state),
      the same interface GrantedCognitionProvider uses (U-6: the charge
      travels the substrate's existing path, never silent).
    mc_id: the microcontroller the charge is booked against.
    """

    def __init__(self, substrate: Any, mc_id: str, *,
                 clock=time.monotonic) -> None:
        self._substrate = substrate
        self._mc_id = mc_id
        self._clock = clock
        # grant_id -> seconds consumed (per-epoch; pruned as epochs advance)
        self._consumed: Dict[str, float] = {}
        self._consumed_epoch: Dict[str, int] = {}
        # grant_id -> turns currently in flight
        self._inflight: Dict[str, int] = {}

    @staticmethod
    def estimate_cost_s(prompt: str) -> float:
        """Deterministic, documented cost estimate used for the
        insufficient-grant deferral check BEFORE serving. The student
        serves ~1-2s/turn warm; the estimate is deliberately
        conservative so deferral errs toward protecting the budget."""
        return 2.0 + 0.0005 * len(prompt or "")

    def grant_consumed_s(self, grant_id: str) -> float:
        """Seconds consumed against a grant id (for tests and gates)."""
        return self._consumed.get(grant_id, 0.0)

    def _prune_epochs(self, current_epoch: int) -> None:
        stale = [gid for gid, ep in self._consumed_epoch.items()
                 if ep < current_epoch]
        for gid in stale:
            self._consumed.pop(gid, None)
            self._consumed_epoch.pop(gid, None)

    def turn(self, prompt: str, *, frm_grant: Any,
             host: str = server_client.DEFAULT_HOST,
             port: int = server_client.DEFAULT_PORT,
             max_tokens: int = 25) -> Dict[str, Any]:
        """One governed student turn. Returns a dict with ok, and either
        text/provenance/charged_s or error. Never raises on governance
        grounds; server transport errors are returned as ok=False."""
        if frm_grant is None:
            return {"ok": False,
                    "error": f"{R_NO_GRANT}: student inference requires "
                             f"an FrmGrant; pass frm_grant=<FrmGrant>"}

        if not isinstance(frm_grant, FrmGrant):
            got = f"{type(frm_grant).__module__}.{type(frm_grant).__name__}"
            return {"ok": False,
                    "error": f"{R_BAD_GRANT}: the student inlet accepts "
                             f"only FrmGrant "
                             f"(runtime/curiosity/frm/grant.py); got {got}. "
                             f"Effect grants (primitives.Grant) and "
                             f"allocation records (attribution.Grant) are "
                             f"not resource authorizations"}

        if frm_grant.enforcement_state_at_issue != "RUNNING":
            return {"ok": False,
                    "error": f"{R_ENFORCEMENT}: grant "
                             f"{frm_grant.grant_id[:8]} was issued under "
                             f"enforcement state "
                             f"{frm_grant.enforcement_state_at_issue!r}; "
                             f"zero allocation while any enforcement state "
                             f"is active"}

        self._prune_epochs(frm_grant.epoch_id)
        gid = frm_grant.grant_id

        inflight = self._inflight.get(gid, 0)
        if inflight >= frm_grant.max_concurrent:
            return {"ok": False,
                    "error": f"{R_DEFERRED_CONCURRENT}: grant {gid[:8]} "
                             f"allows {frm_grant.max_concurrent} concurrent "
                             f"turn(s); {inflight} already in flight; "
                             f"deferred, nothing charged"}

        consumed = self._consumed.get(gid, 0.0)
        estimate = self.estimate_cost_s(prompt)
        if consumed + estimate > frm_grant.budget_s + 1e-9:
            return {"ok": False,
                    "error": f"{R_DEFERRED_BUDGET}: estimated cost "
                             f"{estimate:.3f}s plus already-consumed "
                             f"{consumed:.3f}s exceeds grant {gid[:8]} "
                             f"budget {frm_grant.budget_s:.3f}s; deferred, "
                             f"nothing charged"}

        full_prompt = f"Chat turn: {prompt}\nResponse:"
        self._inflight[gid] = inflight + 1
        try:
            text, wall_s, tps, err = server_client.turn(
                full_prompt, host=host, port=port, max_tokens=max_tokens)
        finally:
            self._inflight[gid] = self._inflight.get(gid, 1) - 1

        if err is not None:
            # The turn never happened: charge nothing, report honestly.
            return {"ok": False,
                    "error": f"student_failed:transport: {err}",
                    "wall_s": wall_s, "charged_s": 0.0}

        actual_s = float(wall_s)
        self._consumed[gid] = consumed + actual_s
        self._consumed_epoch[gid] = frm_grant.epoch_id
        # U-6: the charge travels the substrate's existing charge path.
        exhausted, charge_state = self._substrate.charge(
            self._mc_id, actual_s)
        return {"ok": True, "text": text,
                "provenance": STUDENT_PROVENANCE,
                "wall_s": wall_s, "tok_per_s": tps,
                "charged_s": actual_s,
                "grant_consumed_s": self._consumed[gid],
                "grant_budget_s": frm_grant.budget_s,
                "mc_exhausted": exhausted,
                "mc_charge_state": charge_state}
