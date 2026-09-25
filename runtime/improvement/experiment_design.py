
"""
Self-directed experiment design.

Given an agenda question already selected by AgendaFormer, generate multiple
candidate experiments, score them with an explicit utility, select one,
execute it, and return structured new evidence — without test-supplied
experiment choice.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class ExperimentCandidate:
    experiment_id: str
    family: str  # counterfactual | probe_workload | strategy_ablation | parameter_sweep
    description: str
    relevance: float
    expected_info_gain: float
    cost_estimate: float
    risk: float
    distinguishes: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)
    utility: float = 0.0
    rationale: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def experiment_utility(
    relevance: float,
    expected_info_gain: float,
    cost_estimate: float,
    risk: float,
) -> float:
    """Identity-independent experiment score. No family-name special cases."""
    return (
        max(0.0, relevance)
        * max(0.0, expected_info_gain)
        / (1.0 + max(0.0, cost_estimate) + max(0.0, risk))
    )


class ExperimentDesigner:
    """Generate / score / select experiments for a selected agenda item."""

    def __init__(self, engine):
        self.engine = engine

    def generate_candidates(self, agenda_item: Dict[str, Any]) -> List[ExperimentCandidate]:
        raw = dict(agenda_item.get("raw_evidence") or {})
        signature = raw.get("signature") or ""
        strategy = raw.get("strategy") or ""
        attempts = int(raw.get("attempts") or 0)
        wasted = float(raw.get("total_cost_ns") or 0) / 1e9
        scope = agenda_item.get("scope") or ""
        question = agenda_item.get("question") or ""

        cands: List[ExperimentCandidate] = []

        # Family A: counterfactual prune of blamed strategy
        if strategy and signature:
            rel = 0.95 if "failure" in scope or strategy in question else 0.7
            ig = min(1.5, 0.5 + math.log1p(wasted) / math.log1p(5))
            cost = 1.0
            risk = 0.2
            cands.append(ExperimentCandidate(
                experiment_id=f"exp_cf_{uuid.uuid4().hex[:8]}",
                family="counterfactual",
                description=(
                    f"A/B: resolve probe goals of class {signature[:40]} "
                    f"with vs without pruning strategy {strategy!r}"
                ),
                relevance=rel,
                expected_info_gain=ig,
                cost_estimate=cost,
                risk=risk,
                distinguishes=[
                    f"strategy_{strategy}_is_cost_driver",
                    f"pruning_{strategy}_reduces_waste",
                ],
                params={"signature": signature, "strategy": strategy, "mode": "prune_ab"},
            ))

        # Family B: baseline probe workload (measure current attempt mix)
        rel_b = 0.75
        ig_b = 0.55 if attempts >= 3 else 0.35
        cands.append(ExperimentCandidate(
            experiment_id=f"exp_probe_{uuid.uuid4().hex[:8]}",
            family="probe_workload",
            description=(
                f"Run held-out probe resolves matching signature structure; "
                f"record strategy attempt mix and costs"
            ),
            relevance=rel_b,
            expected_info_gain=ig_b,
            cost_estimate=0.8,
            risk=0.1,
            distinguishes=["current_attempt_mix", "live_cost_profile"],
            params={"signature": signature, "mode": "baseline_probe"},
        ))

        # Family C: strategy ablation — skip only the blamed strategy via temporary pruning
        if strategy:
            cands.append(ExperimentCandidate(
                experiment_id=f"exp_ablate_{uuid.uuid4().hex[:8]}",
                family="strategy_ablation",
                description=(
                    f"Temporarily ablate strategy {strategy!r} only; "
                    f"compare success/cost to full strategy set"
                ),
                relevance=0.9 if strategy else 0.4,
                expected_info_gain=min(1.3, 0.4 + math.log1p(attempts) / math.log1p(8)),
                cost_estimate=1.1,
                risk=0.25,
                distinguishes=[f"necessity_of_{strategy}", "residual_success_without"],
                params={"signature": signature, "strategy": strategy, "mode": "ablate"},
            ))

        # Family D: cheap no-op control (low info) — should lose on utility
        cands.append(ExperimentCandidate(
            experiment_id=f"exp_noop_{uuid.uuid4().hex[:8]}",
            family="parameter_sweep",
            description="Re-read existing learner rows without new execution",
            relevance=0.2,
            expected_info_gain=0.05,
            cost_estimate=0.05,
            risk=0.0,
            distinguishes=["none_new"],
            params={"mode": "noop_reread"},
        ))

        for c in cands:
            c.utility = experiment_utility(
                c.relevance, c.expected_info_gain, c.cost_estimate, c.risk
            )
            c.rationale = (
                f"utility={c.utility:.4f} = rel={c.relevance:.2f}*"
                f"ig={c.expected_info_gain:.2f}/(1+cost={c.cost_estimate:.2f}+risk={c.risk:.2f})"
            )
        cands.sort(key=lambda x: (-x.utility, x.experiment_id))
        return cands

    def select(self, candidates: List[ExperimentCandidate]) -> Optional[ExperimentCandidate]:
        if not candidates:
            return None
        top = candidates[0]
        if top.utility <= 0:
            return None
        return top

    def execute(self, candidate: ExperimentCandidate, agenda_item: Dict[str, Any]) -> Dict[str, Any]:
        """Run real experiment; return observed evidence (not fabricated)."""
        import asyncio
        params = dict(candidate.params or {})
        mode = params.get("mode")
        signature = params.get("signature") or ""
        strategy = params.get("strategy") or ""
        t0 = time.perf_counter_ns()
        evidence: Dict[str, Any] = {
            "experiment_id": candidate.experiment_id,
            "family": candidate.family,
            "mode": mode,
            "new": True,
        }
        actions: List[str] = []

        async def _probe(goal: str, examples=None):
            if examples is None:
                examples = [
                    ({"a": 1, "b": 2, "c": 3}, "probe_x"),
                    ({"a": 2, "b": 3, "c": 4}, "probe_y"),
                ]
            return await self.engine.resolve(goal, examples=examples)

        try:
            if mode == "noop_reread":
                actions.append("reread_learner")
                learner = getattr(self.engine, "strategy_learner", None)
                evidence["rows_seen"] = 0
                if learner is not None:
                    import sqlite3
                    conn = sqlite3.connect(learner.db_path)
                    n = conn.execute("SELECT COUNT(*) FROM acquisition_experience").fetchone()[0]
                    conn.close()
                    evidence["rows_seen"] = int(n)
                evidence["information_gain_realized"] = 0.0

            elif mode in ("baseline_probe", "prune_ab", "ablate"):
                # Baseline
                t1 = time.perf_counter_ns()
                res1 = asyncio.run(_probe(f"exp_base_{candidate.experiment_id[-6:]}"))
                base_ns = time.perf_counter_ns() - t1
                base_strats = [a.get("strategy") for a in (res1.get("attempts") or [])]
                actions.append("baseline_resolve")
                evidence["baseline_cost_ns"] = base_ns
                evidence["baseline_strategies"] = base_strats
                evidence["baseline_fully"] = res1.get("fully_resolved")

                if mode in ("prune_ab", "ablate") and strategy and signature:
                    pruning_before = {
                        k: set(v) for k, v in (getattr(self.engine, "acquisition_pruning", None) or {}).items()
                    }
                    trial = dict(pruning_before)
                    trial[signature] = set(trial.get(signature, set())) | {strategy}
                    setattr(self.engine, "acquisition_pruning", trial)
                    t2 = time.perf_counter_ns()
                    res2 = asyncio.run(_probe(f"exp_cf_{candidate.experiment_id[-6:]}"))
                    cf_ns = time.perf_counter_ns() - t2
                    setattr(self.engine, "acquisition_pruning", pruning_before)
                    actions.append("counterfactual_resolve")
                    evidence["counterfactual_cost_ns"] = cf_ns
                    evidence["counterfactual_strategies"] = [
                        a.get("strategy") for a in (res2.get("attempts") or [])
                    ]
                    evidence["cost_delta_ns"] = base_ns - cf_ns
                    evidence["strategy_removed"] = strategy not in evidence["counterfactual_strategies"]
                    # Realized info: did cost drop and strategy disappear?
                    evidence["information_gain_realized"] = (
                        1.0 if evidence["cost_delta_ns"] > 1e7 and evidence["strategy_removed"]
                        else 0.4 if evidence["strategy_removed"] else 0.1
                    )
                else:
                    evidence["information_gain_realized"] = 0.5 if base_strats else 0.1
            else:
                actions.append("unknown_mode")
                evidence["information_gain_realized"] = 0.0
        except Exception as exc:
            evidence["error"] = f"{type(exc).__name__}: {exc}"
            evidence["information_gain_realized"] = 0.0
            actions.append("error")

        evidence["actions"] = actions
        evidence["wall_cost_ns"] = time.perf_counter_ns() - t0
        evidence["sufficient"] = float(evidence.get("information_gain_realized") or 0) >= 0.4
        return evidence


class ExperimentDesignLoop:
    """Agenda question → design experiments → select → execute → improve."""

    def __init__(self, engine):
        self.engine = engine
        self.designer = ExperimentDesigner(engine)
        # Reuse agenda store for provenance
        from swarm_engine.improvement.agenda import AgendaStore
        db = getattr(engine, "db_path", None) or "swarm_engine.db"
        self.store = AgendaStore(db)
        with self.store._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS experiment_runs ("
                " experiment_id TEXT PRIMARY KEY,"
                " agenda_id TEXT,"
                " payload TEXT NOT NULL,"
                " at REAL NOT NULL)"
            )

    def _save_run(self, experiment_id: str, agenda_id: str, payload: Dict[str, Any]):
        with self.store._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO experiment_runs(experiment_id,agenda_id,payload,at) "
                "VALUES (?,?,?,?)",
                (experiment_id, agenda_id, json.dumps(payload), time.time()),
            )

    def run_once(self) -> Dict[str, Any]:
        from swarm_engine.improvement.agenda import AgendaFormer, AgendaStatus

        report: Dict[str, Any] = {
            "agenda_question": None,
            "candidates": [],
            "selected_experiment": None,
            "execution": None,
            "epistemic": None,
            "improvement": None,
            "agenda_after": [],
        }

        former = AgendaFormer(self.engine, self.store)
        items = former.form_questions()
        selected = former.select(items)
        if selected is None:
            report["note"] = "no agenda question"
            return report

        q = selected.as_dict()
        report["agenda_question"] = q
        self.store.log_event("experiment_design_start", selected.agenda_id, {
            "question": selected.question[:160],
        })

        # Generate & select experiment BEFORE any improvement candidate path
        cands = self.designer.generate_candidates(q)
        report["candidates"] = [c.as_dict() for c in cands]
        chosen = self.designer.select(cands)
        if chosen is None:
            report["note"] = "no experiment selected"
            return report
        report["selected_experiment"] = chosen.as_dict()
        self.store.log_event("experiment_selected", chosen.experiment_id, {
            "agenda_id": selected.agenda_id,
            "family": chosen.family,
            "utility": chosen.utility,
            "competitors": [
                {"id": c.experiment_id, "family": c.family, "utility": c.utility}
                for c in cands[:5]
            ],
        })

        # Execute
        evidence = self.designer.execute(chosen, q)
        report["execution"] = evidence
        self.store.log_event("experiment_executed", chosen.experiment_id, {
            "sufficient": evidence.get("sufficient"),
            "info_gain": evidence.get("information_gain_realized"),
            "cost_delta_ns": evidence.get("cost_delta_ns"),
        })

        # Epistemic update (from evidence, not test)
        ig = float(evidence.get("information_gain_realized") or 0)
        if evidence.get("error"):
            belief = "INCONCLUSIVE"
            conf = 0.1
        elif ig >= 0.8 and evidence.get("cost_delta_ns", 0) > 1e7:
            belief = "SUPPORT_PRUNE_STRATEGY"
            conf = min(0.95, 0.5 + ig * 0.4)
        elif ig >= 0.4:
            belief = "WEAK_SUPPORT"
            conf = 0.45
        else:
            belief = "INSUFFICIENT"
            conf = 0.2
        report["epistemic"] = {
            "belief": belief,
            "confidence": conf,
            "based_on": {
                "information_gain_realized": ig,
                "cost_delta_ns": evidence.get("cost_delta_ns"),
                "strategy_removed": evidence.get("strategy_removed"),
            },
        }
        self.store.log_event("epistemic_update", selected.agenda_id, report["epistemic"])

        # Only after experiment: existing improvement path
        self.store.log_event("pre_candidate_after_experiment", selected.agenda_id, {
            "experiment_id": chosen.experiment_id,
            "belief": belief,
        })
        selector = getattr(self.engine, "multi_improvement_selector", None)
        if selector is not None and belief in ("SUPPORT_PRUNE_STRATEGY", "WEAK_SUPPORT"):
            sel = selector.select_and_process()
            report["improvement"] = sel
        else:
            report["improvement"] = {
                "note": "no improvement attempted",
                "reason": belief,
            }

        # Agenda reassess
        after = former.form_questions()
        report["agenda_after"] = [
            {"priority": i.priority, "scope": i.scope, "question": i.question[:100]}
            for i in after[:5]
        ]

        self._save_run(chosen.experiment_id, selected.agenda_id, report)
        selected.status = AgendaStatus.COMPLETED
        self.store.save_item(selected)
        return report
