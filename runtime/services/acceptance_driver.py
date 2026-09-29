"""V10-P6 -- the production acceptance driver.

James's architectural decision (2026-09-27, standing): the REMOR Core Run
Controller OWNS acceptance-loop invocation; Q8's present() is an inlet into
this mechanism, not the owner of the loop.

What this file builds (only the missing wiring):
  - AcceptanceDriver: the production mechanism behind the Controller's
    invoke_acceptance inlet. Takes a produced result -> runs the REAL
    authentication gate (claim re-execution, held-out tests, negative
    controls, counterfactuals where applicable) -> present() as CANDIDATE
    (completed != accepted, recorded) -> verdict -> on dissatisfaction:
    near-miss record -> pool expansion steered by the near-miss -> steered
    retry -> re-presented as the next round with the evidence chain intact
    -> verdict ... until ACCEPTED, terminal, or an honest stop.
  - present_result(): Q8's inlet -- the function a scheduler-completion hook
    calls when a run's result enters the acceptance/review pathway.
  - VerdictSource: the verdict interface. Production verdicts arrive from
    the user (chat_handler's accept/reject kinds call submit_verdict);
    the bench proof uses ScriptedVerdicts (seeded, explicitly labeled --
    the mechanism is what's proven, not the verdict).

What it reuses (called, never rebuilt):
  - AcceptanceLoop (M6, runtime/services/acceptance.py): present /
    record_verdict / characterize_near_miss / persist_near_miss /
    expand_pool / attempt / close_terminal, the acceptance overlay store,
    the completed-vs-accepted distinction, near-miss epistemic records.
  - The planner's own hint-token steering: the steered retry plans the
    goal text with the near-miss's steers_toward tokens appended
    ("<goal> steered toward <unmet...>") -- probe-verified: the binding
    follows the steering tokens through the planner's real _hint_steers.
    The exclusion list is enforced as a backstop by loop.attempt().
  - record_experience (V10-P1): every driver transition is an observation
    when a controller is attached.

Honesty rules (load-bearing, not decorative):
  - Vacuous dissatisfaction (no feedback AND no unmet criteria) is
    recorded but the retry is WITHHELD: the driver never invents a
    gradient. Status: awaiting_feedback.
  - A steered retry is never presented without its own held-out
    expectations: submit_verdict requires retry_held_out when a retry is
    triggered, else the retry is blocked (fail-closed).
  - Fabricated acceptance (verdict for a run with no acceptance record,
    or a result whose claimed output does not re-execute) is refused.
  - Dissatisfaction after acceptance reopens honestly (the user's latest
    word is ground truth); terminal stays terminal.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.services.acceptance import (
    AcceptanceLoop, AcceptanceRecord, AcceptanceState, AcceptanceStore,
    Attempt, AuthReport, PoolVerdict, SYSTEM_COMPLETED)


# ---------------------------------------------------------------------------
# verdict sources
# ---------------------------------------------------------------------------

class VerdictSource:
    """Where verdicts come from. Production: the user. Bench: scripted."""

    def verdict(self, record) -> Tuple[bool, str, List[str]]:
        """Return (satisfied, feedback_text, unmet_criteria) for the record
        currently in CANDIDATE state."""
        raise NotImplementedError

    def retry_held_out(self, record) -> Optional[List[Dict[str, Any]]]:
        """Held-out expectations for the steered retry round, if any."""
        return None


class ScriptedVerdicts(VerdictSource):
    """Seeded verdicts for the bench proof. SIMULATED USER -- the mechanism
    is what's proven, not the verdict."""

    def __init__(self, script: List[Dict[str, Any]]):
        self._script = list(script)
        self._i = 0

    def verdict(self, record):
        if self._i >= len(self._script):
            raise RuntimeError(
                "ScriptedVerdicts: script exhausted; the proof script "
                "must cover every round")
        step = self._script[self._i]
        self._i += 1
        return (bool(step.get("satisfied", False)),
                str(step.get("feedback", "")),
                list(step.get("unmet_criteria", [])))

    def retry_held_out(self, record):
        # The held-out for the round about to be retried comes from the
        # last consumed script step (the dissatisfaction that triggered it).
        step = self._script[max(0, self._i - 1)]
        return step.get("retry_held_out")


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------

class AcceptanceDriver:
    """The production acceptance mechanism behind the Controller's inlet."""

    def __init__(self, loop: AcceptanceLoop, controller: Any = None):
        self.loop = loop
        self.controller = controller

    @classmethod
    def from_controller(cls, controller, store_path: str,
                        scheduler: Any = None) -> "AcceptanceDriver":
        loop = AcceptanceLoop(
            AcceptanceStore(store_path),
            controller.epistemic,
            engine=controller.engine,
            scheduler=scheduler)
        return cls(loop, controller=controller)

    # -- observations ----------------------------------------------------

    def _observe(self, kind: str, content: str,
                 raw: Optional[Dict[str, Any]] = None) -> str:
        if self.controller is None:
            return ""
        try:
            return self.controller._observe(kind, content, raw)
        except Exception:
            return ""

    # -- the real authentication gate -------------------------------------

    def _auth_gate(self, attempt: Attempt,
                   held_out: List[Dict[str, Any]],
                   negative_control: Optional[Dict[str, Any]] = None,
                   alt_plan: Optional[Dict[str, Any]] = None) -> AuthReport:
        """Re-verify a produced attempt with real executions. The producer's
        claim is never trusted: the plan is re-executed on the original
        args (claim check) and on held-out args the attempt was not built
        against; the negative control must fail closed; the counterfactual
        (where an alternative plan exists) must diverge."""
        eng = self.loop.engine
        if eng is None:
            raise ValueError("_auth_gate: no engine injected")
        held_results = {}
        held_ok = True
        for i, spec in enumerate(held_out or []):
            r = eng.composer.execute_sync(attempt.plan, spec["args"])
            ok = bool(r.get("success")) and r.get("value") == spec["expected"]
            held_results[f"held_out_{i}"] = {
                "args": spec["args"], "expected": spec["expected"],
                "observed": r.get("value"), "passed": ok}
            held_ok = held_ok and ok
        # Claim check: the produced result must re-execute.
        claim = eng.composer.execute_sync(attempt.plan, attempt.args)
        claim_ok = (bool(claim.get("success"))
                    and claim.get("value") == attempt.result_summary)
        neg_results = {}
        neg_ok = True
        if negative_control is not None:
            rn = eng.composer.execute_sync(attempt.plan,
                                           negative_control["args"])
            neg_ok = not rn.get("success")  # must fail closed
            neg_results = {"args": negative_control["args"],
                           "expected": "fail closed",
                           "observed_success": rn.get("success"),
                           "passed": neg_ok}
        cf = None
        cf_ok = True
        if alt_plan is not None:
            ra = eng.composer.execute_sync(alt_plan, attempt.args)
            diverges = (ra.get("success")
                        and ra.get("value") != claim.get("value"))
            cf = {"alternative_value": ra.get("value"),
                  "diverges": bool(diverges)}
            cf_ok = bool(diverges)
        return AuthReport(
            held_out={**held_results,
                      "claim_check": {
                          "args": attempt.args,
                          "expected": attempt.result_summary,
                          "observed": claim.get("value"),
                          "passed": claim_ok}},
            negative_controls=neg_results,
            counterfactuals=cf,
            passed=bool(held_ok and claim_ok and neg_ok and cf_ok))

    # -- Q8's inlet: a produced result enters the pathway ------------------

    @staticmethod
    def _require(result: Dict[str, Any], key: str) -> Any:
        v = result.get(key)
        if v is None or v == "" or v == [] or v == {}:
            raise ValueError(
                f"present_result: result is missing required field {key!r}; "
                "a fabricated or incomplete result cannot enter acceptance")
        return v

    def present_result(self, result: Dict[str, Any]):
        """Q8's inlet into the acceptance mechanism. A produced result
        enters the acceptance/review pathway: it is authenticated with
        real executions and presented as a CANDIDATE. Completed is a
        candidate state, never terminal, never accepted-by-default."""
        run_id = self._require(result, "run_id")
        goal = self._require(result, "goal")
        attempt = Attempt(
            approach_signature=list(self._require(
                result, "approach_signature")),
            plan=dict(self._require(result, "plan")),
            args=dict(result.get("args") or {}),
            result_summary=result.get("result_summary"),
            exec_ok=bool(result.get("exec_ok")))
        held_out = result.get("held_out") or []
        if not held_out:
            raise ValueError(
                "present_result: no held-out expectations supplied; the "
                "authentication gate cannot run without them")
        auth = self._auth_gate(
            attempt, held_out,
            negative_control=result.get("negative_control"),
            alt_plan=result.get("alt_plan"))
        rec = self.loop.present(run_id, goal, attempt, auth)
        self._observe(
            "acceptance_presented",
            f"AcceptanceDriver: run {run_id} presented as candidate "
            f"(auth passed: {auth.passed}); completed != accepted.",
            {"kind": "acceptance_presented", "run_id": run_id,
             "goal": goal, "state": rec.state.value})
        return rec

    # -- verdicts ----------------------------------------------------------

    @staticmethod
    def _actionable(feedback: str, unmet_criteria) -> bool:
        return bool((feedback or "").strip()) or bool(unmet_criteria)

    def submit_verdict(self, run_id: str, satisfied: bool,
                       feedback: str = "",
                       unmet_criteria: Optional[List[str]] = None,
                       retry_held_out: Optional[List[Dict[str, Any]]] = None
                       ) -> Dict[str, Any]:
        """A verdict arrives for a presented run. satisfied=True closes the
        loop (ACCEPTED). Dissatisfaction persists the near-miss and drives
        the steered retry inline: expand -> steered attempt -> re-present
        as the next round with the evidence chain intact."""
        try:
            rec = self.loop.record_verdict(
                run_id, satisfied, feedback=feedback,
                unmet_criteria=unmet_criteria)
        except KeyError:
            return {"status": "refused",
                    "reason": f"no acceptance record for run {run_id!r}; "
                              "a verdict for a never-presented run is "
                              "fabricated acceptance"}
        if rec.state is AcceptanceState.ACCEPTED:
            self._observe(
                "acceptance_accepted",
                f"AcceptanceDriver: run {run_id} ACCEPTED "
                f"(rounds: {rec.rounds}).",
                {"kind": "acceptance_accepted", "run_id": run_id,
                 "rounds": rec.rounds,
                 "near_miss_ids": rec.near_miss_ids})
            return {"status": "accepted", "run_id": run_id,
                    "rounds": rec.rounds,
                    "chain": self.evidence_chain(run_id)}
        # Dissatisfied: the near-miss is persisted by record_verdict.
        if not self._actionable(feedback, unmet_criteria):
            self._observe(
                "acceptance_awaiting_feedback",
                f"AcceptanceDriver: run {run_id} dissatisfied with no "
                f"actionable feedback; retry WITHHELD (no invented "
                f"gradient).",
                {"kind": "acceptance_awaiting_feedback",
                 "run_id": run_id})
            return {"status": "awaiting_feedback", "run_id": run_id,
                    "reason": "dissatisfaction recorded; no feedback or "
                              "unmet criteria to steer by, so no retry "
                              "was attempted"}
        nms = [x for x in self.loop.near_misses_for_goal(rec.goal)
               if x.run_id == run_id]
        # The retry steers by the LATEST dissatisfaction, not the first:
        # exclusions accumulate across rounds, but the steering target is
        # always the user's most recent word.
        nm = (max(enumerate(nms), key=lambda ix: (ix[1].at, ix[0]))[1]
              if nms else None)
        if nm is None:
            return {"status": "refused",
                    "reason": "near-miss record missing after rejection; "
                              "refusing to steer blind"}
        exp = self.loop.expand_pool(rec.goal, nm)
        if exp.verdict is PoolVerdict.GENUINELY_ABSENT:
            self._observe(
                "acceptance_genuinely_absent",
                f"AcceptanceDriver: run {run_id}: steered expansion finds "
                f"nothing new -- the gap is genuine, not pool-limited.",
                {"kind": "acceptance_genuinely_absent",
                 "run_id": run_id, "excluded": exp.excluded})
            return {"status": "genuinely_absent", "run_id": run_id,
                    "excluded": exp.excluded, "reason": exp.reason}
        if retry_held_out is None:
            return {"status": "retry_blocked", "run_id": run_id,
                    "reason": "steered retry withheld: no held-out "
                              "expectations supplied for the retry round; "
                              "a retry is never presented unauthenticated"}
        # Steered retry: the near-miss's steers_toward tokens steer the
        # planner's binding through its real hint interface; the excluded
        # signatures are enforced as a backstop.
        steered_goal = (rec.goal + " steered toward "
                        + " ".join(nm.steers_toward))
        try:
            attempt2 = self.loop.attempt(
                steered_goal, rec.attempt.args,
                exclude=exp.excluded)
        except RuntimeError as e:
            return {"status": "retry_refused", "run_id": run_id,
                    "reason": f"planner re-bound the excluded approach: "
                              f"{e}"}
        auth2 = self._auth_gate(
            attempt2, retry_held_out,
            negative_control=None,
            alt_plan=rec.attempt.plan)
        rec2 = self.loop.present_retry(run_id, attempt2, auth2)
        self._observe(
            "acceptance_retry_presented",
            f"AcceptanceDriver: run {run_id} round {rec2.rounds} presented "
            f"as candidate (steered toward {nm.steers_toward}; excluded "
            f"{exp.excluded}).",
            {"kind": "acceptance_retry_presented", "run_id": run_id,
             "round": rec2.rounds,
             "steered_toward": nm.steers_toward,
             "excluded": exp.excluded,
             "near_miss_id": nm.near_miss_id})
        return {"status": "retry_presented", "run_id": run_id,
                "round": rec2.rounds,
                "steered_toward": nm.steers_toward,
                "excluded": exp.excluded,
                "near_miss_id": nm.near_miss_id}

    # -- synchronous end-to-end (bench proof; no operator) -------------------

    def drive(self, result: Dict[str, Any],
              verdicts: VerdictSource) -> Dict[str, Any]:
        """Run the whole acceptance pathway for one produced result:
        present -> verdict -> (near-miss -> steered expansion -> steered
        retry -> re-present)* -> terminal state. Returns the transcript
        and the evidence chain."""
        rec = self.present_result(result)
        transcript = [("presented", rec.state.value, rec.rounds)]
        while True:
            satisfied, feedback, unmet = verdicts.verdict(rec)
            out = self.submit_verdict(
                rec.run_id, satisfied, feedback, unmet,
                retry_held_out=verdicts.retry_held_out(rec))
            rec = self.loop.store.get(rec.run_id)
            transcript.append((out["status"], rec.state.value, rec.rounds))
            if out["status"] in ("accepted", "awaiting_feedback",
                                 "genuinely_absent", "retry_blocked",
                                 "retry_refused", "refused"):
                return {"run_id": rec.run_id,
                        "final_state": rec.state.value,
                        "transcript": transcript,
                        "last": out,
                        "chain": self.evidence_chain(rec.run_id)}

    # -- per-cycle acceptance (PLOOP-3): the run controller's own cycles --

    @staticmethod
    def evaluate_cycle(cycle_n: int, presented: Dict[str, Any],
                       persisted: Optional[Dict[str, Any]]
                       ) -> Tuple[bool, Dict[str, Dict[str, Any]]]:
        """Deterministic, evidence-derived verdict criteria for one
        controller cycle, evaluated against the PERSISTED checkpoint
        summary (never trusting the in-memory dict alone).

        Single source of truth: the driver and any independent re-check
        import this; nobody copies it. A cycle passes only when every
        named check passes; a failed cycle is a failed cycle, never a
        skipped or auto-passed one."""
        checks: Dict[str, Dict[str, Any]] = {}

        def _check(name: str, passed: bool,
                   expected: Any, observed: Any) -> None:
            checks[name] = {"expected": expected, "observed": observed,
                            "passed": bool(passed)}

        _check("row_exists", persisted is not None,
               "rc_cycles row present", "present" if persisted else "missing")
        if persisted is None:
            return False, checks
        _check("cycle_number_matches", persisted.get("cycle") == cycle_n,
               cycle_n, persisted.get("cycle"))
        _check("claims_match_persisted",
               (persisted.get("errors", []) == presented.get("errors", [])
                and bool(persisted.get("budget_exceeded"))
                == bool(presented.get("budget_exceeded"))),
               "presented claims == persisted row",
               {"errors": persisted.get("errors", []),
                "budget_exceeded": bool(persisted.get("budget_exceeded",
                                                     False))})
        _check("no_tick_error", "tick_error" not in persisted,
               "no tick_error", persisted.get("tick_error", "absent"))
        errs = persisted.get("errors") or []
        _check("no_step_errors", len(errs) == 0, "errors == []", errs)
        _check("budget_honored", not persisted.get("budget_exceeded", False),
               "budget_exceeded falsy",
               persisted.get("budget_exceeded", False))
        return all(c["passed"] for c in checks.values()), checks

    def accept_cycle(self, controller: Any, cycle_n: int,
                     summary: Dict[str, Any]) -> Dict[str, Any]:
        """Per-cycle acceptance evidence for the run controller's own
        cadence -- the ACCEPT stage of the unified loop, driven every
        tick, not only when an operator presents a result.

        This is deliberately NOT present_result(): that inlet's auth gate
        re-executes synthesis plans, and a cycle summary is not a plan --
        forcing it through that gate would be gaming the mechanism. The
        operational authentication here re-reads the PERSISTED rc_cycles
        row for this cycle and verifies the cycle's claims against what's
        on disk; the verdict is deterministic and evidence-derived (there
        is no user inside the autonomous loop).

        A failed cycle is recorded REJECTED with the failed checks named
        and a near-miss persisted -- never skipped, never auto-passed.
        loop.present() rightly refuses unauthenticated attempts, which is
        why the failed path records directly through the store instead of
        through present()."""
        con = sqlite3.connect(controller.checkpoint_path)
        try:
            row = con.execute(
                "SELECT summary_json FROM rc_cycles WHERE n=?",
                (cycle_n,)).fetchone()
        finally:
            con.close()
        persisted = json.loads(row[0]) if row else None
        passed, checks = self.evaluate_cycle(cycle_n, summary, persisted)
        failed = [k for k, c in checks.items() if not c["passed"]]
        goal = (f"run-controller cycle {cycle_n}: execute the authorized "
                f"cadence honestly")
        run_id = f"{controller._run_id}:cycle:{cycle_n}"
        attempt = Attempt(
            approach_signature=["gap_queue", "distill_sweep",
                                "quarantine_sweep"],
            plan={"cycle": cycle_n,
                  "steps": ["gap_queue", "distill_sweep",
                            "quarantine_sweep"]},
            args={},
            result_summary={
                "errors": (persisted or {}).get("errors", []),
                "budget_exceeded": bool(
                    (persisted or {}).get("budget_exceeded", False)),
                "gaps_processed": len((persisted or {}).get("gaps", []))},
            exec_ok=passed)
        auth = AuthReport(
            held_out={k: {"expected": v["expected"],
                          "observed": v["observed"],
                          "passed": v["passed"]}
                      for k, v in checks.items()},
            passed=passed)
        if passed:
            rec = self.loop.present(run_id, goal, attempt, auth)
            # Evidence-derived accept, not a user verdict: set the terminal
            # fields directly with an honest reason instead of routing
            # through record_verdict(satisfied=True), which would
            # mislabel the closer as "user_satisfied".
            rec.state = AcceptanceState.ACCEPTED
            rec.decided_at = time.time()
            rec.close_reason = (
                "cycle_accepted: autonomous evidence-derived verdict; "
                "no user in the loop, checks re-verified from the "
                "persisted checkpoint row")
            self.loop.store.save(rec)
            state = "accepted"
        else:
            rec = AcceptanceRecord(
                run_id=run_id, goal=goal,
                system_status=SYSTEM_COMPLETED, auth=auth,
                state=AcceptanceState.REJECTED, attempt=attempt,
                decided_at=time.time(),
                close_reason="cycle_failed: " + ",".join(failed))
            self.loop.store.save(rec)
            nm = self.loop.characterize_near_miss(
                rec,
                feedback_text=("autonomous cycle verdict FAILED: "
                               + ", ".join(failed)),
                unmet_criteria=failed)
            self.loop.persist_near_miss(nm)
            rec.near_miss_ids.append(nm.near_miss_id)
            self.loop.store.save(rec)
            state = "rejected"
        self._observe(
            "cycle_acceptance",
            f"AcceptanceDriver: cycle {cycle_n} {state} "
            f"({len(checks)} checks, failed: {failed or 'none'}).",
            {"kind": "cycle_acceptance", "cycle": cycle_n, "state": state,
             "run_id": run_id, "failed_checks": failed})
        return {"cycle": cycle_n, "state": state, "run_id": run_id,
                "checks": {k: v["passed"] for k, v in checks.items()},
                "failed_checks": failed}

    # -- evidence ------------------------------------------------------------

    def evidence_chain(self, run_id: str) -> Dict[str, Any]:
        """The full chain, queryable: the acceptance record (with its
        near-miss ids and rounds intact across re-presents) plus the
        near-miss records from the epistemic store."""
        rec = self.loop.store.get(run_id)
        if rec is None:
            return {"run_id": run_id, "record": None, "near_misses": []}
        nms = [nm for nm in self.loop.near_misses_for_goal(rec.goal)
               if nm.run_id == run_id]
        return {"run_id": run_id, "record": rec.as_dict(),
                "near_misses": [nm.as_dict() for nm in nms]}
