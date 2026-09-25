"""
swarm_engine/intellect/capability_growth.py

Connects general pattern discovery to autonomous capability growth, without
knowing anything about which pattern kind, subsystem, or capability is
involved.

The mechanism, stated precisely because it is the part that must not be
category-specific:

  1. Given ANY IntellectualPattern, look at its source_observation_ids and
     find any underlying event whose raw_ref traces back to something
     REPLAYABLE — currently, an ExhaustedSearchMemory record, which now
     carries (goal, examples, param_names) because that data was missing
     and needed adding. This step does not interpret what the pattern
     MEANS; it only asks "is there a real, reproducible (goal, examples)
     pair behind this."

  2. Attempt to reproduce it via `cognition.propose_multi` — the EXACT same
     entry point every other synthesis attempt in the engine uses. If it
     succeeds, there is no capability gap. If it genuinely fails (not
     forced by this code — whatever the live search policy actually does),
     that failure IS the discovered capability insufficiency.

  3. Formulate the "capability requirement" as exactly the (goal, examples,
     param_names) already captured in step 1 — never invented.

  4. Attempt acquisition via `engine.synthesize_and_admit` — the SAME
     propose -> admit -> provenance -> learn loop used everywhere else. This
     is expected to be able to fail on the first attempt, and must not be
     reported as success if it does.

  5. On failure, recover by invoking `engine.improvement_pipeline.run_cycle()`
     — the already-built, already-validated self-improvement substrate.
     Nothing new is built to make the second attempt more likely to
     succeed; an existing, already-governed mechanism is invoked.

  6. Retry acquisition. If it now succeeds, the capability has gone through
     the same independent validation/admission/provenance/persistence path
     as any other capability.

  7. Retry the original reproduction and compare before/after — the
     measurable evidence of whether growth actually helped.

Every step above calls an existing, already-tested SWarm method. Nothing in
this file executes code, admits a capability, or claims success on its own
authority.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.intellect.patterns import IntellectualPattern


@dataclass
class CapabilityGrowthResult:
    attempted: bool
    reproduced_before: Optional[bool] = None
    first_acquisition_attempt: Optional[Dict[str, Any]] = None
    recovery_triggered: bool = False
    recovery_result: Optional[List[Dict[str, Any]]] = None
    second_acquisition_attempt: Optional[Dict[str, Any]] = None
    reproduced_after: Optional[bool] = None
    capability_id: Optional[str] = None
    improved: Optional[bool] = None
    reason: str = ""
    trace: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"attempted": self.attempted, "reproduced_before": self.reproduced_before,
                "first_acquisition_attempt": self.first_acquisition_attempt,
                "recovery_triggered": self.recovery_triggered,
                "recovery_result": self.recovery_result,
                "second_acquisition_attempt": self.second_acquisition_attempt,
                "reproduced_after": self.reproduced_after,
                "capability_id": self.capability_id, "improved": self.improved,
                "reason": self.reason, "trace": self.trace}


class CapabilityGrowthConnector:
    def __init__(self, swarm_engine, experience_log):
        self.engine = swarm_engine
        self.experience_log = experience_log
        # Each checker takes a raw_ref and returns a replayable
        # (goal, examples, param_names) dict or None. Adding a new
        # replayable source (a future harvester's raw_ref type) means
        # registering one more checker here — find_replayable_evidence
        # itself never changes, and does not know or care which source
        # eventually answers.
        self._replayable_checkers = [self._check_exhausted_search,
                                     self._check_case_memory]

    def _check_exhausted_search(self, raw_ref: str) -> Optional[Dict[str, Any]]:
        for record in self.engine.cognition.exhausted.replayable(only_expressible=None):
            if record["signature"] == raw_ref and record.get("goal"):
                return record
        return None

    def _check_case_memory(self, raw_ref: str) -> Optional[Dict[str, Any]]:
        """CaseMemory entries also carry (goal, examples, param_names) —
        added earlier for regression testing, and just as usable here. A
        pattern traced back to a solved case has nothing to grow (it already
        works), but a pattern reasoning about a RELATED, harder instance of
        that case's shape can use it as a concrete starting point — this
        checker exists so that path is not structurally unavailable."""
        try:
            case_id = int(raw_ref)
        except (TypeError, ValueError):
            return None
        for cid, entry in self.engine.cognition.cases.all():
            if cid == case_id and entry.examples and entry.param_names:
                return {"goal": entry.goal, "examples": entry.examples,
                       "param_names": entry.param_names,
                       "candidates_tried": None}
        return None

    def find_replayable_evidence(self, pattern: IntellectualPattern
                                 ) -> Optional[Dict[str, Any]]:
        for event_id in pattern.source_observation_ids:
            event = self.experience_log.get(event_id)
            if event is None or event.raw_ref is None:
                continue
            for checker in self._replayable_checkers:
                record = checker(event.raw_ref)
                if record is not None:
                    return record
        return None

    def attempt_growth(self, pattern: IntellectualPattern) -> CapabilityGrowthResult:
        result = CapabilityGrowthResult(attempted=False)
        record = self.find_replayable_evidence(pattern)
        if record is None:
            result.reason = ("no underlying event for this pattern traces back "
                             "to a replayable (goal, examples) pair; nothing to "
                             "attempt growth on")
            return result

        goal = record["goal"]
        examples = record["examples"]
        param_names = record["param_names"]
        result.attempted = True
        result.trace.append(f"reproducing goal={goal!r} param_names={param_names}")

        before = self.engine.cognition.propose_multi(goal, examples, param_names)
        result.reproduced_before = before.solved
        if before.solved:
            result.reason = "the goal already reproduces successfully; no capability gap"
            result.improved = False
            return result

        result.trace.append("reproduction failed under the current live policy — "
                            "this is the discovered capability insufficiency, not "
                            "a forced or simulated failure")

        first = self.engine.synthesize_and_admit(goal, examples, param_names=param_names)
        result.first_acquisition_attempt = {k: v for k, v in first.items()
                                            if k != "derivation"}
        if first["admitted"]:
            result.trace.append("first acquisition attempt succeeded outright")
        else:
            result.trace.append(
                "first acquisition attempt failed honestly (not reported as "
                "success) — recovering via the existing improvement pipeline")
            result.recovery_triggered = True
            recovery_reports = self.engine.improvement_pipeline.run_cycle()
            result.recovery_result = recovery_reports
            result.trace.append(f"recovery outcome(s): "
                                f"{[r['outcome'] for r in recovery_reports]}")

            second = self.engine.synthesize_and_admit(goal, examples, param_names=param_names)
            result.second_acquisition_attempt = {k: v for k, v in second.items()
                                                 if k != "derivation"}
            first = second

        result.capability_id = first.get("capability_id")

        after = self.engine.cognition.propose_multi(goal, examples, param_names)
        result.reproduced_after = after.solved
        result.improved = (not result.reproduced_before) and bool(result.reproduced_after)
        result.reason = ("capability growth measurably improved reproduction of "
                         "the original goal" if result.improved else
                         "capability growth did not change whether the goal "
                         "reproduces — reported honestly, not claimed as success")
        return result
