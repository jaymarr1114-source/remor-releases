"""
swarm_engine/agents/agent_zero.py

Agent 0: the central orchestrator that assigns work to the five specialized
roles, tracks whether each is actually producing good results, reconciles
disagreement between them, and reassigns work away from a role that is
failing.

This is the piece that was missing before: work went straight to the engine's
subsystems, one function call at a time, decided by whichever code path
happened to invoke them. Agent 0 makes that assignment an explicit, tracked
decision — every task is dispatched to a *role*, not a function; every
outcome updates that role's reliability; a role whose reliability drops below
a floor stops receiving new assignments until it recovers; and when two
roles produce conflicting answers for the same question, that conflict is
surfaced through MetaReasoner rather than silently resolved by picking one.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.agents.roles import Role, RoleOutcome, SpecializedAgent, build_roles
from swarm_engine.core.metareasoning import EscalationAction


@dataclass
class Assignment:
    role: Role
    task: str
    outcome: Optional[RoleOutcome] = None
    reassigned_from: Optional[Role] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"role": self.role.value, "task": self.task[:120],
                "outcome": self.outcome.as_dict() if self.outcome else None,
                "reassigned_from": self.reassigned_from.value
                if self.reassigned_from else None}


class AgentZero:
    """Assigns, tracks, reconciles, reassigns. Nothing here does the actual
    work — that is each role's job — Agent 0 only decides who does it and
    whether the result should be trusted."""

    RELIABILITY_FLOOR = 0.25

    def __init__(self, engine, project: str = "default"):
        self.engine = engine
        self.project = project
        self.roles = build_roles(engine)
        self.blackboard = engine.blackboard
        self.metareasoner = engine.metareasoner
        from swarm_engine.core.activation import Activator, Subsystem
        self.activator = Activator(engine)
        self._subsystem_role = {
            Subsystem.RESEARCH: Role.RESEARCHER, Subsystem.BUILD: Role.BUILDER,
            Subsystem.VERIFY: Role.VERIFIER, Subsystem.DEBUG: Role.DEBUGGER,
            Subsystem.OPTIMIZE: Role.OPTIMIZER,
        }
        self.history: List[Assignment] = []

    # -- assignment -----------------------------------------------------------
    def route(self, task: str, hint: Optional[Role] = None) -> Role:
        """Decide which role owns this task.

        A hint from the caller is honoured if that role is currently
        reliable; otherwise routing falls back to keyword-based role
        inference, and finally to whichever eligible role has the best
        recorded reliability — so an unreliable role is not handed more work
        just because it happens to be the nominal owner of that task shape.
        """
        eligible = [r for r in Role if self.roles[r].reliability >= self.RELIABILITY_FLOOR]
        if not eligible:
            eligible = list(Role)  # everyone is struggling; proceed anyway rather than stall

        if hint is not None and hint in eligible:
            return hint

        plan = self.activator.plan(task)
        for subsystem in plan.needed:
            role = self._subsystem_role.get(subsystem)
            if role in eligible:
                return role

        return max(eligible, key=lambda r: self.roles[r].reliability)

    async def assign(self, task: str, payload: Dict[str, Any],
                     hint: Optional[Role] = None,
                     retries: int = 1) -> Assignment:
        """Dispatch a task to a role.

        Earlier versions of this method reassigned a failed task to a
        *different* role when the original one failed. That is wrong for
        this architecture: the five roles are not interchangeable workers —
        each owns a distinct kind of work, and none can substitute for
        another. The concrete failure this caused: a Builder task failed,
        assign() reassigned it to the Researcher, the Researcher "succeeded"
        (gap analysis cannot fail the way acquisition can — it always
        produces *an* answer), and that success was reported as if the
        capability had been built. Nothing was built. The bug was not in
        routing; routing picked Builder correctly every time. It was in
        treating role failure as fungible across roles that do not do the
        same job.

        The fix: retry the *same* role a bounded number of times (for
        transient failures), then return the honest failed Assignment. What
        to do about a role's failure — retry differently, escalate to the
        Debugger, replan, give up — is a decision for the caller, which
        already has the context to make it (run_project explicitly dispatches
        to the Debugger on a Builder failure, which is the correct place for
        that decision to live).
        """
        role = self.route(task, hint)
        assignment = Assignment(role=role, task=task)
        agent = self.roles[role]

        for attempt in range(retries + 1):
            outcome = await agent.handle(task, payload)
            assignment.outcome = outcome
            self.blackboard.post(self.project, f"assignment:{task}",
                                 role.value, outcome.as_dict())
            if outcome.success:
                self.history.append(assignment)
                return assignment
            self.engine.failure_memory.record(task, outcome.detail)

        self.history.append(assignment)
        return assignment

    # -- reconciliation ---------------------------------------------------------
    async def reconcile(self, task: str, roles: List[Role],
                        payload: Dict[str, Any]) -> Dict[str, Any]:
        """Ask more than one role to independently answer the same question
        and reconcile the results, rather than trusting a single answer for a
        high-value decision. Disagreement is reported through MetaReasoner's
        contradiction detection, not silently averaged away.
        """
        results: List[Tuple[str, Any]] = []
        outcomes = []
        for role in roles:
            outcome = await self.roles[role].handle(task, payload)
            outcomes.append(outcome)
            if outcome.success:
                results.append((role.value, outcome.value))

        contradiction = self.metareasoner.detect_contradiction(task, results)
        if contradiction is None:
            return {"agreed": True, "value": results[0][1] if results else None,
                    "outcomes": [o.as_dict() for o in outcomes]}
        return {"agreed": False, "contradiction": contradiction.as_dict(),
                "outcomes": [o.as_dict() for o in outcomes]}

    # -- reliability ------------------------------------------------------------
    def reliability_report(self) -> Dict[str, Any]:
        return {role.value: {"assignments": agent.assignments,
                             "successes": agent.successes,
                             "reliability": round(agent.reliability, 3)}
                for role, agent in self.roles.items()}

    def as_dict(self) -> Dict[str, Any]:
        return {"project": self.project, "reliability": self.reliability_report(),
                "recent_assignments": [a.as_dict() for a in self.history[-20:]]}

    # -- continuous project loop -----------------------------------------------
    def _run_declared_checks(self, project_id: str, root: str, runner,
                             declared_checks) -> tuple:
        """Execute the project's declared check commands for real.

        Returns (ok, detail, bindings). Each check command is registered as
        an oracle (the engine attests the declaration's provenance: it read
        the checks file at ingest), authorized for 'verification', executed
        via CommandRunner, and the (command, input, output) triple bound as
        a tamper-evident evaluation. With no declared checks the fallback
        is explicit, not silent.
        """
        from swarm_engine.project.commands import CommandDenied
        handle = getattr(self.engine, "oracle", None)
        if not declared_checks:
            return (True,
                    "no declared checks; builder's independent-validation "
                    "evidence stands as the verification (explicit fallback)",
                    [])
        bindings = []
        for argv in declared_checks:
            try:
                result = runner.run(list(argv))
                denied = ""
            except CommandDenied as exc:
                result, denied = None, str(exc)
            except Exception as exc:  # noqa: BLE001 -- runner containment
                result, denied = None, f"runner raised {type(exc).__name__}: {exc}"
            if handle is not None:
                import hashlib as _hl
                check_name = ("project-check:" + project_id + ":" +
                              _hl.sha256(repr(argv).encode()).hexdigest()[:12])
                oracle_id, version = handle.register_oracle(
                    check_name,
                    {"argv": list(argv), "expect": "exit code 0",
                     "declared_by": "remor_checks.json"},
                    input_contract="argv list",
                    output_contract="CommandResult{ok, exit_code}",
                    source=f"project:{project_id}")
                handle.authorize_oracle(oracle_id, version, "verification")
                if result is not None:
                    out = {"ok": result.ok, "exit_code": result.exit_code,
                           "timed_out": result.timed_out,
                           "stdout_tail": result.stdout[-500:],
                           "stderr_tail": result.stderr[-500:]}
                else:
                    out = {"ok": False, "denied": denied}
                eval_id = handle.evaluate(
                    oracle_id, {"argv": list(argv), "cwd": root}, out,
                    input_ref=f"agent_zero:{project_id}", version=version)
                bindings.append({"oracle_id": oracle_id, "version": version,
                                 "eval_id": eval_id, "argv": list(argv)})
            if result is None or not result.ok:
                detail = (f"check {argv!r} failed: "
                          f"{denied or ('exit ' + str(result.exit_code))}")
                return False, detail, bindings
        return (True,
                f"{len(declared_checks)} declared check(s) passed; "
                f"evaluations bound in oracle registry",
                bindings)

    async def run_project(self, project_id: str, root: str, max_cycles: int = 10
                          ) -> Dict[str, Any]:
        """The continuous loop: plan -> execute -> observe -> evaluate ->
        replan, over a real ingested project, until requirements are
        satisfied, the project is genuinely blocked, or the cycle budget
        runs out.

        Every role communicates through the blackboard rather than through
        values threaded by this method: the Researcher posts its gap
        analysis, the Builder reads that post rather than being handed the
        gap directly, and Agent 0's job is to run the loop and make the
        lifecycle transitions honest, not to shuttle data between roles by
        hand. That is what makes this genuine inter-role communication
        instead of a sequence of function calls that happen to run in order.

        State is checkpointed every cycle (lifecycle transition + progress
        save), so a process killed mid-project resumes from the last
        completed cycle rather than restarting the whole project.
        """
        from swarm_engine.project.commands import CommandRunner
        from swarm_engine.project.lifecycle import ProjectProgress, ProjectState

        lifecycle = self.engine.project_lifecycle
        progress = (lifecycle.load_progress(project_id)
                   or ProjectProgress(project_id=project_id))
        state = lifecycle.state_of(project_id)
        if state is ProjectState.INGESTED:
            lifecycle.transition(project_id, ProjectState.ANALYZING,
                                 "project loop started")

        model = self.engine.projects.get(project_id)
        if model is None:
            return {"error": f"no ingested project model for {project_id!r}"}
        if not progress.outstanding_requirements and not progress.satisfied_requirements:
            progress.outstanding_requirements = [r["text"] for r in model["requirements"]]

        guard = self.engine.project_modification_guard(root)
        runner = CommandRunner(project_root=root)

        for _ in range(max_cycles):
            progress.cycles += 1
            if not progress.outstanding_requirements:
                if lifecycle.state_of(project_id) is ProjectState.ANALYZING:
                    lifecycle.transition(project_id, ProjectState.PLANNING,
                                         "all requirements already satisfied")
                    lifecycle.transition(project_id, ProjectState.EXECUTING,
                                         "nothing remaining to execute")
                lifecycle.transition(project_id, ProjectState.VERIFYING,
                                     "all requirements addressed")
                lifecycle.transition(project_id, ProjectState.COMPLETE,
                                     f"{len(progress.satisfied_requirements)} "
                                     f"requirement(s) satisfied over "
                                     f"{progress.cycles} cycle(s)")
                lifecycle.save_progress(progress)
                return {"state": ProjectState.COMPLETE.value,
                       "progress": progress.as_dict()}

            requirement = progress.outstanding_requirements[0]

            # PLAN: Researcher analyzes the requirement, posts to the
            # blackboard rather than returning straight to this loop.
            research = await self.assign(requirement, {}, hint=Role.RESEARCHER)
            self.blackboard.post(project_id, f"gap:{requirement}",
                                 Role.RESEARCHER.value, research.outcome.as_dict())

            gap_posted = self.blackboard.latest(project_id, f"gap:{requirement}")
            gap_value = gap_posted.value if gap_posted else {}
            has_gap = bool(gap_value.get("value", {}).get("gaps"))

            if not has_gap:
                # Already composable from existing primitives — no gap to
                # close, but this still passes through PLANNING/EXECUTING
                # rather than jumping straight to "satisfied", so every
                # completed requirement leaves the same trail regardless of
                # whether building was needed. A shortcut here would mean two
                # different paths through the state machine for "done",
                # which is exactly the kind of skipped-stage ambiguity this
                # machinery exists to prevent.
                lifecycle.transition(project_id, ProjectState.PLANNING,
                                     f"{requirement!r} already satisfiable; "
                                     f"no acquisition needed")
                lifecycle.transition(project_id, ProjectState.EXECUTING,
                                     "nothing to build")
                lifecycle.transition(project_id, ProjectState.VERIFYING,
                                     f"{requirement!r} confirmed satisfiable")
                progress.satisfied_requirements.append(requirement)
                progress.outstanding_requirements.remove(requirement)
                lifecycle.transition(project_id, ProjectState.ANALYZING,
                                     f"{requirement!r} satisfied; continuing")
                lifecycle.save_progress(progress)
                continue

            # EXECUTE: Builder reads the same blackboard entry the Researcher
            # posted (not a value Agent 0 hands it directly) and attempts to
            # close the gap.
            lifecycle.transition(project_id, ProjectState.PLANNING,
                                 f"gap found for {requirement!r}")
            lifecycle.transition(project_id, ProjectState.EXECUTING,
                                 "dispatching to builder")

            build_payload = {"examples_by_node": {}}
            build = await self.assign(requirement, build_payload, hint=Role.BUILDER)
            self.blackboard.post(project_id, f"build:{requirement}",
                                 Role.BUILDER.value, build.outcome.as_dict())
            progress.attempted_capabilities.append(requirement)

            if not build.outcome.success:
                # OBSERVE + EVALUATE: Debugger diagnoses, failure memory
                # records it, and the project is marked blocked on this
                # requirement rather than silently dropping it.
                debug = await self.assign(
                    requirement, {"error": build.outcome.detail, "args": {}},
                    hint=Role.DEBUGGER)
                self.blackboard.post(project_id, f"debug:{requirement}",
                                     Role.DEBUGGER.value, debug.outcome.as_dict())
                lifecycle.transition(project_id, ProjectState.BLOCKED,
                                     f"could not resolve {requirement!r}: "
                                     f"{build.outcome.detail[:150]}")
                lifecycle.save_progress(progress)
                return {"state": ProjectState.BLOCKED.value,
                       "blocked_on": requirement, "progress": progress.as_dict(),
                       "debug": debug.outcome.as_dict()}

            # VERIFY: run the project's declared check commands (from
            # remor_checks.json at ingest), if any. Each check is executed
            # for real via CommandRunner, its result captured, and the
            # command/input/output bound as an oracle evaluation in the
            # tamper-evident registry. A failed check routes to the
            # DEBUGGER and BLOCKS -- the requirement is never marked
            # satisfied by state-machine motion alone. With no declared
            # checks the fallback is recorded explicitly.
            lifecycle.transition(project_id, ProjectState.VERIFYING,
                                 f"checking {requirement!r}")
            declared_checks = model.get("checks") or []
            verify_ok, verify_detail, verify_bindings = \
                self._run_declared_checks(project_id, root, runner,
                                          declared_checks)
            if not verify_ok:
                debug = await self.assign(
                    requirement, {"error": verify_detail, "args": {}},
                    hint=Role.DEBUGGER)
                self.blackboard.post(project_id, f"verify:{requirement}",
                                     Role.DEBUGGER.value,
                                     {"detail": verify_detail,
                                      "bindings": verify_bindings,
                                      "debug": debug.outcome.as_dict()})
                lifecycle.transition(project_id, ProjectState.BLOCKED,
                                     f"verification failed for {requirement!r}: "
                                     f"{verify_detail[:150]}")
                lifecycle.save_progress(progress)
                return {"state": ProjectState.BLOCKED.value,
                       "blocked_on": requirement, "progress": progress.as_dict(),
                       "verify_detail": verify_detail,
                       "verify_bindings": verify_bindings,
                       "debug": debug.outcome.as_dict()}
            self.blackboard.post(project_id, f"verify:{requirement}",
                                 "agent_zero",
                                 {"detail": verify_detail,
                                  "bindings": verify_bindings})
            progress.satisfied_requirements.append(requirement)
            progress.outstanding_requirements.remove(requirement)
            lifecycle.transition(project_id, ProjectState.ANALYZING,
                                 f"{requirement!r} satisfied; continuing")
            lifecycle.save_progress(progress)

        lifecycle.save_progress(progress)
        return {"state": lifecycle.state_of(project_id).value,
               "progress": progress.as_dict(),
               "reason": f"cycle budget ({max_cycles}) exhausted"}
