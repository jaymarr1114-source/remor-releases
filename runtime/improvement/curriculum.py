
"""
Self-directed workload / curriculum selection.

REMOR generates candidate workloads from current experience and capability
state, scores them with an explicit utility, selects one, executes it for
real, updates experience, and reselects from the new state.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class WorkloadCandidate:
    workload_id: str
    family: str  # gap_probe | composition | verification | novelty | low_value
    description: str
    goal: str
    examples: List[Tuple[Dict[str, Any], Any]]
    expected_info_gain: float
    expected_capability_value: float
    relevance: float
    novelty: float
    future_value: float
    cost: float
    risk: float
    utility: float = 0.0
    rationale: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # examples may contain non-JSON types; keep summary only in dict form
        d["examples"] = [
            {"args": ex[0], "expect_type": type(ex[1]).__name__}
            for ex in (self.examples or [])
        ]
        return d


def workload_utility(
    expected_info_gain: float,
    expected_capability_value: float,
    relevance: float,
    novelty: float,
    future_value: float,
    cost: float,
    risk: float,
) -> float:
    expected_value = 0.6 * expected_info_gain + 0.4 * expected_capability_value
    return (
        max(0.0, expected_value)
        * max(0.0, relevance)
        * max(0.05, novelty)
        * max(0.05, future_value)
        / (1.0 + max(0.0, cost) + max(0.0, risk))
    )


class WorkloadDesigner:
    """Generate and score candidate workloads from engine state."""

    def __init__(self, engine):
        self.engine = engine

    def _learner_summary(self) -> Dict[str, Any]:
        learner = getattr(self.engine, "strategy_learner", None)
        out = {"groups": [], "n_rows": 0}
        if learner is None:
            return out
        try:
            import sqlite3
            from collections import defaultdict
            conn = sqlite3.connect(learner.db_path)
            rows = conn.execute(
                "SELECT signature, strategy, success, cost FROM acquisition_experience"
            ).fetchall()
            conn.close()
            out["n_rows"] = len(rows)
            g = defaultdict(lambda: {"attempts": 0, "successes": 0, "cost": 0})
            for sig, strat, succ, cost in rows:
                key = (sig, strat)
                g[key]["signature"] = sig
                g[key]["strategy"] = strat
                g[key]["attempts"] += 1
                g[key]["successes"] += int(succ)
                g[key]["cost"] += int(cost or 0)
            out["groups"] = list(g.values())
        except Exception:
            pass
        return out

    def _pruned(self) -> Dict[str, set]:
        p = getattr(self.engine, "acquisition_pruning", None) or {}
        return {k: set(v) for k, v in p.items()}

    def _family_counts(self) -> Dict[str, int]:
        """How often each family has been executed (curriculum history)."""
        counts: Dict[str, int] = {}
        # Prefer curriculum_cycles table when available via engine.curriculum_loop
        loop = getattr(self.engine, "curriculum_loop", None)
        if loop is not None:
            try:
                for c in loop.load_cycles():
                    fam = c.get("selected_family") or ""
                    counts[fam] = counts.get(fam, 0) + 1
            except Exception:
                pass
        return counts

    def generate(self) -> List[WorkloadCandidate]:
        summary = self._learner_summary()
        pruned = self._pruned()
        n_rows = summary["n_rows"]
        fam_counts = self._family_counts()
        cands: List[WorkloadCandidate] = []

        # --- gap_probe: exercise signatures with recurring generate failures ---
        gen_fails = [
            g for g in summary["groups"]
            if g.get("strategy") == "generate"
            and g.get("successes", 0) == 0
            and g.get("attempts", 0) >= 2
        ]
        gen_fails.sort(key=lambda x: -x.get("cost", 0))
        if gen_fails:
            top = gen_fails[0]
            sig = top["signature"]
            already = "generate" in pruned.get(sig, set())
            n_gap = fam_counts.get("gap_probe", 0)
            # Diminishing returns: each prior gap_probe execution reduces novelty/info
            decay = 0.55 ** n_gap
            info = (0.25 if already else min(1.4, 0.5 + math.log1p(top["cost"] / 1e9))) * decay
            nov = (0.15 if already else 0.85) * decay
            cands.append(WorkloadCandidate(
                workload_id=f"wl_gap_{uuid.uuid4().hex[:8]}",
                family="gap_probe",
                description=f"Probe 3-input string-class goals matching high-waste generate signature",
                goal=f"curriculum_gap_{uuid.uuid4().hex[:6]}",
                examples=[
                    ({"a": 3, "b": 4, "c": 5}, "cx_a"),
                    ({"a": 6, "b": 7, "c": 8}, "cx_b"),
                ],
                expected_info_gain=info,
                expected_capability_value=(0.4 if not already else 0.1) * decay,
                relevance=0.95 if not already else 0.25,
                novelty=max(0.05, nov),
                future_value=(0.7 if not already else 0.15) * decay,
                cost=1.0,
                risk=0.15,
                provenance={"source": "generate_failure_rows", "signature": sig,
                            "already_pruned": already, "prior_gap_runs": n_gap},
            ))

        # --- composition: list-output structural tasks (exercises pair assembly) ---
        cands.append(WorkloadCandidate(
            workload_id=f"wl_comp_{uuid.uuid4().hex[:8]}",
            family="composition",
            description="Two-part list composition [a+b, a] — exercises hierarchical assembly",
            goal=f"curriculum_comp_{uuid.uuid4().hex[:6]}",
            examples=[
                ({"a": 1, "b": 2}, [3, 1]),
                ({"a": 4, "b": 5}, [9, 4]),
                ({"a": 7, "b": 1}, [8, 7]),
            ],
            expected_info_gain=(0.9 if n_rows < 20 else 0.55) * (0.6 ** fam_counts.get("composition", 0)),
            expected_capability_value=0.85 * (0.7 ** fam_counts.get("composition", 0)),
            relevance=0.8,
            novelty=max(0.08, (0.7 if n_rows < 15 else 0.4) * (0.55 ** fam_counts.get("composition", 0))),
            future_value=0.9 * (0.7 ** fam_counts.get("composition", 0)),
            cost=1.2,
            risk=0.2,
            provenance={"source": "composition_opportunity", "prior_runs": fam_counts.get("composition", 0)},
        ))

        # --- verification-oriented: multi-arg tasks that stress independent validation ---
        cands.append(WorkloadCandidate(
            workload_id=f"wl_ver_{uuid.uuid4().hex[:8]}",
            family="verification",
            description="Numeric add-like tasks with held-out generalization pressure",
            goal=f"curriculum_ver_{uuid.uuid4().hex[:6]}",
            examples=[
                ({"x": 2, "y": 3}, 5),
                ({"x": 10, "y": 4}, 14),
                ({"x": 0, "y": 7}, 7),
            ],
            expected_info_gain=0.7 * (0.6 ** fam_counts.get("verification", 0)),
            expected_capability_value=0.75 * (0.7 ** fam_counts.get("verification", 0)),
            relevance=0.75,
            novelty=max(0.08, (0.6 if n_rows < 25 else 0.35) * (0.55 ** fam_counts.get("verification", 0))),
            future_value=0.8 * (0.7 ** fam_counts.get("verification", 0)),
            cost=0.9,
            risk=0.15,
            provenance={"source": "verification_coverage", "prior_runs": fam_counts.get("verification", 0)},
        ))

        # --- novelty: unexplored arity / output shape ---
        cands.append(WorkloadCandidate(
            workload_id=f"wl_nov_{uuid.uuid4().hex[:8]}",
            family="novelty",
            description="Single-input identity-like probe (under-explored arity)",
            goal=f"curriculum_nov_{uuid.uuid4().hex[:6]}",
            examples=[
                ({"v": 1}, 1),
                ({"v": 9}, 9),
            ],
            expected_info_gain=0.5,
            expected_capability_value=0.3,
            relevance=0.4,
            novelty=0.9 if n_rows > 0 else 0.5,
            future_value=0.4,
            cost=0.4,
            risk=0.1,
            provenance={"source": "arity_coverage"},
        ))

        # --- low_value control candidate ---
        cands.append(WorkloadCandidate(
            workload_id=f"wl_low_{uuid.uuid4().hex[:8]}",
            family="low_value",
            description="Empty-ish re-probe of already-saturated trivial constant",
            goal=f"curriculum_low_{uuid.uuid4().hex[:6]}",
            examples=[
                ({"z": 0}, 0),
            ],
            expected_info_gain=0.05,
            expected_capability_value=0.05,
            relevance=0.1,
            novelty=0.05,
            future_value=0.05,
            cost=0.2,
            risk=0.0,
            provenance={"source": "low_value_control"},
        ))

        for c in cands:
            c.utility = workload_utility(
                c.expected_info_gain, c.expected_capability_value,
                c.relevance, c.novelty, c.future_value, c.cost, c.risk,
            )
            c.rationale = (
                f"utility={c.utility:.4f} from info={c.expected_info_gain:.2f} "
                f"cap={c.expected_capability_value:.2f} rel={c.relevance:.2f} "
                f"nov={c.novelty:.2f} fut={c.future_value:.2f} "
                f"/ (1+cost={c.cost:.2f}+risk={c.risk:.2f})"
            )
        cands.sort(key=lambda x: (-x.utility, x.workload_id))
        return cands

    def select(self, candidates: List[WorkloadCandidate]) -> Optional[WorkloadCandidate]:
        if not candidates:
            return None
        # Only consider above a floor so low_value does not win by default
        viable = [c for c in candidates if c.utility >= 0.02]
        if not viable:
            return None
        return viable[0]


class CurriculumLoop:
    """Autonomous workload selection loop with state-driven reselection."""

    def __init__(self, engine):
        self.engine = engine
        self.designer = WorkloadDesigner(engine)
        from swarm_engine.improvement.agenda import AgendaStore
        db = getattr(engine, "db_path", None) or "swarm_engine.db"
        self.store = AgendaStore(db)
        with self.store._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS curriculum_cycles ("
                " cycle_id INTEGER PRIMARY KEY,"
                " payload TEXT NOT NULL,"
                " at REAL NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS curriculum_seq ("
                " name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )

    def _next_cycle_id(self) -> int:
        with self.store._conn() as c:
            row = c.execute("SELECT value FROM curriculum_seq WHERE name='c'").fetchone()
            if not row:
                c.execute("INSERT INTO curriculum_seq(name,value) VALUES('c',1)")
                return 1
            v = int(row[0]) + 1
            c.execute("UPDATE curriculum_seq SET value=? WHERE name='c'", (v,))
            return v

    def _save_cycle(self, cycle_id: int, payload: Dict[str, Any]):
        with self.store._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO curriculum_cycles(cycle_id,payload,at) VALUES (?,?,?)",
                (cycle_id, json.dumps(payload), time.time()),
            )

    def load_cycles(self) -> List[Dict[str, Any]]:
        with self.store._conn() as c:
            rows = c.execute(
                "SELECT cycle_id, payload FROM curriculum_cycles ORDER BY cycle_id"
            ).fetchall()
        return [{"cycle_id": r[0], **json.loads(r[1])} for r in rows]

    def run_once(self) -> Dict[str, Any]:
        import asyncio
        report: Dict[str, Any] = {
            "candidates": [],
            "selected": None,
            "execution": None,
            "learning": None,
            "next_candidates_preview": [],
        }
        cands = self.designer.generate()
        report["candidates"] = [c.as_dict() for c in cands]
        chosen = self.designer.select(cands)
        if chosen is None:
            report["note"] = "no viable workload"
            return report
        report["selected"] = chosen.as_dict()
        self.store.log_event("workload_selected", chosen.workload_id, {
            "family": chosen.family,
            "utility": chosen.utility,
            "goal": chosen.goal,
            "competitors": [
                {"id": c.workload_id, "family": c.family, "utility": c.utility}
                for c in cands[:6]
            ],
        })

        t0 = time.perf_counter_ns()
        try:
            result = asyncio.run(
                self.engine.resolve(chosen.goal, examples=chosen.examples)
            )
            err = None
        except Exception as exc:
            result = {}
            err = f"{type(exc).__name__}: {exc}"
        wall = time.perf_counter_ns() - t0
        exec_info = {
            "goal": chosen.goal,
            "family": chosen.family,
            "fully_resolved": result.get("fully_resolved") if result else False,
            "failed": result.get("failed") if result else [],
            "acquired": result.get("acquired") if result else [],
            "strategies": [
                a.get("strategy") for a in (result.get("attempts") or [])
            ] if result else [],
            "wall_ns": wall,
            "error": err,
        }
        report["execution"] = exec_info
        self.store.log_event("workload_executed", chosen.workload_id, exec_info)

        # Learning assessment from actual result
        learned = {
            "new_failure_evidence": bool(exec_info["strategies"]) and not exec_info["fully_resolved"],
            "new_acquisition": bool(exec_info.get("acquired")),
            "informative": bool(exec_info["strategies"]) or bool(exec_info.get("acquired")),
        }
        report["learning"] = learned

        # Feed informative failures into proven experiment-design → improvement path
        if learned["new_failure_evidence"] and not learned["new_acquisition"]:
            try:
                exp = getattr(self.engine, "experiment_design_loop", None)
                if exp is not None:
                    exp_report = exp.run_once()
                    report["experiment_followup"] = {
                        "agenda": (exp_report.get("agenda_question") or {}).get("question", "")[:100],
                        "experiment_family": (exp_report.get("selected_experiment") or {}).get("family"),
                        "epistemic": exp_report.get("epistemic"),
                        "pipeline": (exp_report.get("improvement") or {}).get("pipeline_outcome")
                        if isinstance(exp_report.get("improvement"), dict) else None,
                    }
            except Exception as exc:
                report["experiment_followup"] = {"error": f"{type(exc).__name__}: {exc}"}

        # Preview next candidates from UPDATED state (post-execution experience)
        next_cands = self.designer.generate()
        report["next_candidates_preview"] = [
            {"family": c.family, "utility": c.utility, "id": c.workload_id}
            for c in next_cands[:6]
        ]
        return report

    def run_cycles(self, max_cycles: int = 5, min_utility: float = 0.02) -> Dict[str, Any]:
        cycles = []
        for _ in range(max_cycles):
            cands = self.designer.generate()
            if not cands or max(c.utility for c in cands) < min_utility:
                self.store.log_event("curriculum_stop", "", {
                    "reason": "low_utility",
                    "top": max((c.utility for c in cands), default=0),
                })
                break
            cycle_id = self._next_cycle_id()
            report = self.run_once()
            if report.get("selected") is None:
                break
            payload = {
                "selected_family": (report.get("selected") or {}).get("family"),
                "selected_utility": (report.get("selected") or {}).get("utility"),
                "selected_goal": (report.get("selected") or {}).get("goal"),
                "candidates": [
                    {"family": c["family"], "utility": c["utility"]}
                    for c in (report.get("candidates") or [])
                ],
                "execution": report.get("execution"),
                "learning": report.get("learning"),
                "next_preview": report.get("next_candidates_preview"),
            }
            self._save_cycle(cycle_id, payload)
            cycles.append({"cycle_id": cycle_id, **payload})
            # Slight novelty decay is implicit via more learner rows / acquisitions
        return {
            "cycles_completed": len(cycles),
            "cycles": cycles,
            "stopping": "max_cycles" if len(cycles) >= max_cycles else "evidence",
        }
