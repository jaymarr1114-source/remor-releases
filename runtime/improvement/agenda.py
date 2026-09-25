
"""
Autonomous improvement agenda formation.

Agenda questions are generated from accumulated experience BEFORE improvement
candidates exist. Selection chooses what to investigate; investigation produces
new evidence; only then do existing observers/pipelines form candidates.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


class AgendaStatus(Enum):
    PROPOSED = "proposed"
    SELECTED = "selected"
    INVESTIGATING = "investigating"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass
class ImprovementAgendaItem:
    agenda_id: str
    question: str
    scope: str
    candidate_subsystems: List[str] = field(default_factory=list)
    evidence_sources: List[str] = field(default_factory=list)
    evidence_strength: float = 0.0
    estimated_impact: float = 0.0
    recurrence: float = 0.0
    uncertainty: float = 0.5
    investigation_cost: float = 1.0
    capability_relevance: float = 0.5
    priority: float = 0.0
    status: AgendaStatus = AgendaStatus.PROPOSED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    parent_agenda_id: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    raw_evidence: Dict[str, Any] = field(default_factory=dict)
    selection_rationale: str = ""
    sequence: int = 0

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ImprovementAgendaItem":
        d = dict(d)
        d["status"] = AgendaStatus(d.get("status", "proposed"))
        return ImprovementAgendaItem(**{k: d[k] for k in ImprovementAgendaItem.__dataclass_fields__ if k in d})


@dataclass
class InvestigationRecord:
    investigation_id: str
    agenda_id: str
    actions: List[str] = field(default_factory=list)
    resources_used: List[str] = field(default_factory=list)
    cost_ns: int = 0
    evidence_collected: Dict[str, Any] = field(default_factory=dict)
    result: str = ""
    candidate_ids: List[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    sequence: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AgendaStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS improvement_agenda ("
                " agenda_id TEXT PRIMARY KEY,"
                " payload TEXT NOT NULL,"
                " sequence INTEGER NOT NULL,"
                " status TEXT NOT NULL,"
                " priority REAL NOT NULL,"
                " updated_at REAL NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS agenda_investigations ("
                " investigation_id TEXT PRIMARY KEY,"
                " agenda_id TEXT NOT NULL,"
                " payload TEXT NOT NULL,"
                " sequence INTEGER NOT NULL,"
                " completed_at REAL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS agenda_events ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " kind TEXT NOT NULL,"
                " ref_id TEXT,"
                " payload TEXT,"
                " at REAL NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS agenda_seq ("
                " name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def next_seq(self) -> int:
        with self._conn() as c:
            row = c.execute("SELECT value FROM agenda_seq WHERE name='global'").fetchone()
            if not row:
                c.execute("INSERT INTO agenda_seq(name,value) VALUES('global',1)")
                return 1
            v = int(row[0]) + 1
            c.execute("UPDATE agenda_seq SET value=? WHERE name='global'", (v,))
            return v

    def log_event(self, kind: str, ref_id: str = "", payload: Optional[Dict] = None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO agenda_events(kind, ref_id, payload, at) VALUES (?,?,?,?)",
                (kind, ref_id, json.dumps(payload or {}), time.time()),
            )

    def save_item(self, item: ImprovementAgendaItem):
        item.updated_at = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO improvement_agenda"
                "(agenda_id,payload,sequence,status,priority,updated_at) VALUES (?,?,?,?,?,?)",
                (item.agenda_id, json.dumps(item.as_dict()), item.sequence,
                 item.status.value, item.priority, item.updated_at),
            )

    def all_items(self, status: Optional[str] = None) -> List[ImprovementAgendaItem]:
        with self._conn() as c:
            if status:
                rows = c.execute(
                    "SELECT payload FROM improvement_agenda WHERE status=? ORDER BY priority DESC",
                    (status,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT payload FROM improvement_agenda ORDER BY priority DESC"
                ).fetchall()
        return [ImprovementAgendaItem.from_dict(json.loads(r[0])) for r in rows]

    def save_investigation(self, inv: InvestigationRecord):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO agenda_investigations"
                "(investigation_id,agenda_id,payload,sequence,completed_at) VALUES (?,?,?,?,?)",
                (inv.investigation_id, inv.agenda_id, json.dumps(inv.as_dict()),
                 inv.sequence, inv.completed_at),
            )

    def investigations(self, agenda_id: Optional[str] = None) -> List[InvestigationRecord]:
        with self._conn() as c:
            if agenda_id:
                rows = c.execute(
                    "SELECT payload FROM agenda_investigations WHERE agenda_id=?",
                    (agenda_id,),
                ).fetchall()
            else:
                rows = c.execute("SELECT payload FROM agenda_investigations").fetchall()
        out = []
        for r in rows:
            d = json.loads(r[0])
            out.append(InvestigationRecord(**{k: d[k] for k in InvestigationRecord.__dataclass_fields__ if k in d}))
        return out

    def events(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, kind, ref_id, payload, at FROM agenda_events ORDER BY id"
            ).fetchall()
        return [
            {"id": r[0], "kind": r[1], "ref_id": r[2],
             "payload": json.loads(r[3] or "{}"), "at": r[4]}
            for r in rows
        ]


def priority_score(
    evidence_strength: float,
    estimated_impact: float,
    recurrence: float,
    uncertainty: float,
    capability_relevance: float,
    investigation_cost: float,
) -> float:
    """Identity-independent priority; no subsystem-name branches."""
    return (
        max(0.0, evidence_strength)
        * max(0.0, estimated_impact)
        * max(0.0, recurrence)
        * max(0.05, uncertainty)  # higher uncertainty → more need to investigate
        * max(0.0, capability_relevance)
        / (1.0 + max(0.0, investigation_cost))
    )


class AgendaFormer:
    """Form investigation questions from experience — not improvement candidates."""

    def __init__(self, engine, store: AgendaStore):
        self.engine = engine
        self.store = store

    def _learner_groups(self) -> List[Dict[str, Any]]:
        learner = getattr(self.engine, "strategy_learner", None)
        if learner is None:
            return []
        try:
            conn = sqlite3.connect(learner.db_path)
            rows = conn.execute(
                "SELECT signature, strategy, success, cost FROM acquisition_experience"
            ).fetchall()
            conn.close()
        except Exception:
            return []
        from collections import defaultdict
        g: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for signature, strategy, success, cost in rows:
            key = (signature, strategy)
            e = g.setdefault(key, {
                "signature": signature, "strategy": strategy,
                "attempts": 0, "successes": 0, "total_cost_ns": 0,
            })
            e["attempts"] += 1
            e["successes"] += int(success)
            e["total_cost_ns"] += int(cost or 0)
        return list(g.values())

    def form_questions(self) -> List[ImprovementAgendaItem]:
        groups = self._learner_groups()
        items: List[ImprovementAgendaItem] = []
        # Question type A: why does strategy S never succeed for signature?
        for g in groups:
            if g["attempts"] < 3 or g["successes"] > 0:
                continue
            wasted_s = g["total_cost_ns"] / 1e9
            if wasted_s < 0.05 and g["strategy"] != "generate":
                # allow compose with lower threshold
                if g["total_cost_ns"] < 50_000:
                    continue
            es = min(1.5, math.log1p(g["attempts"]) / math.log1p(10))
            impact = min(2.0, math.log1p(wasted_s) / math.log1p(5))
            rec = min(1.5, g["attempts"] / 5.0)
            unc = 0.8 if g["successes"] == 0 else 0.3
            cost = 1.0 if g["strategy"] == "generate" else 0.5
            rel = 0.9
            # Already-solved: active pruning removes residual impact of that strategy
            pruning = getattr(self.engine, "acquisition_pruning", None) or {}
            pruned = pruning.get(g["signature"]) or set()
            if g["strategy"] in pruned:
                impact *= 0.05
                unc = 0.15
                rec *= 0.2
            pr = priority_score(es, impact, rec, unc, rel, cost)
            seq = self.store.next_seq()
            q = (
                f"What explains recurring failure of strategy {g['strategy']!r} "
                f"on signature {g['signature']} "
                f"({g['attempts']} attempts, 0 successes, {wasted_s:.2f}s wasted)?"
            )
            item = ImprovementAgendaItem(
                agenda_id=f"agenda_{seq}_{uuid.uuid4().hex[:8]}",
                question=q,
                scope="strategy_failure",
                candidate_subsystems=self._subsystems_for_strategy(g["strategy"]),
                evidence_sources=["acquisition_experience"],
                evidence_strength=es,
                estimated_impact=impact,
                recurrence=rec,
                uncertainty=unc,
                investigation_cost=cost,
                capability_relevance=rel,
                priority=pr,
                sequence=seq,
                raw_evidence=dict(g),
            )
            items.append(item)

        # Question type B: is verification the dominant cost for generate failures?
        gen = [g for g in groups if g["strategy"] == "generate" and g["successes"] == 0 and g["attempts"] >= 3]
        if gen:
            top = max(gen, key=lambda x: x["total_cost_ns"])
            wasted_s = top["total_cost_ns"] / 1e9
            if wasted_s >= 0.5:
                es = min(1.5, math.log1p(top["attempts"]) / math.log1p(10))
                impact = min(2.0, math.log1p(wasted_s) / math.log1p(5))
                rec = min(1.5, top["attempts"] / 5.0)
                pruning = getattr(self.engine, "acquisition_pruning", None) or {}
                pruned = pruning.get(top["signature"]) or set()
                if "generate" in pruned:
                    impact *= 0.05
                    rec *= 0.2
                pr = priority_score(es, impact, rec, 0.9 if "generate" not in pruned else 0.15, 0.85, 1.2)
                seq = self.store.next_seq()
                item = ImprovementAgendaItem(
                    agenda_id=f"agenda_{seq}_{uuid.uuid4().hex[:8]}",
                    question=(
                        f"Is independent verification dominating cost for generate "
                        f"failures on {top['signature']} ({wasted_s:.2f}s wasted)?"
                    ),
                    scope="verification_cost",
                    candidate_subsystems=["verification.strategy_policy", "acquisition.strategy_policy"],
                    evidence_sources=["acquisition_experience"],
                    evidence_strength=es,
                    estimated_impact=impact,
                    recurrence=rec,
                    uncertainty=0.9,
                    investigation_cost=1.2,
                    capability_relevance=0.85,
                    priority=pr,
                    sequence=seq,
                    raw_evidence=dict(top),
                )
                items.append(item)

        items.sort(key=lambda x: -x.priority)
        for it in items:
            self.store.save_item(it)
            self.store.log_event("agenda_proposed", it.agenda_id, {
                "question": it.question, "priority": it.priority, "sequence": it.sequence,
            })
        return items

    @staticmethod
    def _subsystems_for_strategy(strategy: str) -> List[str]:
        if strategy == "generate":
            return ["acquisition.strategy_policy", "verification.strategy_policy"]
        if strategy == "compose":
            return ["representation.search_policy", "acquisition.strategy_policy"]
        return ["acquisition.strategy_policy"]

    def select(self, items: Optional[List[ImprovementAgendaItem]] = None) -> Optional[ImprovementAgendaItem]:
        pool = items if items is not None else self.form_questions()
        # Prefer open proposed items
        open_items = [i for i in pool if i.status == AgendaStatus.PROPOSED]
        if not open_items:
            open_items = [i for i in self.store.all_items() if i.status == AgendaStatus.PROPOSED]
        if not open_items:
            return None
        open_items.sort(key=lambda x: (-x.priority, x.sequence))
        winner = open_items[0]
        if winner.priority <= 0:
            return None
        winner.status = AgendaStatus.SELECTED
        winner.selection_rationale = (
            f"highest priority {winner.priority:.4f} among {len(open_items)} proposed "
            f"questions; evidence_strength={winner.evidence_strength:.3f} "
            f"impact={winner.estimated_impact:.3f} recurrence={winner.recurrence:.3f}"
        )
        winner.updated_at = time.time()
        self.store.save_item(winner)
        self.store.log_event("agenda_selected", winner.agenda_id, {
            "priority": winner.priority, "rationale": winner.selection_rationale,
            "competitors": [
                {"agenda_id": i.agenda_id, "priority": i.priority, "question": i.question[:80]}
                for i in open_items[:5]
            ],
        })
        return winner


class AgendaInvestigator:
    """Execute real investigation work that yields NEW evidence."""

    def __init__(self, engine, store: AgendaStore):
        self.engine = engine
        self.store = store

    def investigate(self, item: ImprovementAgendaItem) -> InvestigationRecord:
        item.status = AgendaStatus.INVESTIGATING
        self.store.save_item(item)
        seq = self.store.next_seq()
        inv = InvestigationRecord(
            investigation_id=f"inv_{seq}_{uuid.uuid4().hex[:8]}",
            agenda_id=item.agenda_id,
            sequence=seq,
            started_at=time.time(),
        )
        t0 = time.perf_counter_ns()
        actions = []
        evidence: Dict[str, Any] = {"pre_existing": dict(item.raw_evidence)}

        # Controlled micro-benchmark: re-run a synthetic goal of the same
        # structural class with/without candidate interventions to measure cost.
        sig = (item.raw_evidence or {}).get("signature", "")
        strategy = (item.raw_evidence or {}).get("strategy", "")
        try:
            import asyncio
            from swarm_engine.acquisition.orchestrator import requirement_signature

            # Probe 1: measure current attempt mix cost on a fresh tiny goal
            examples = [
                ({"a": 1, "b": 2, "c": 3}, "probe_a"),
                ({"a": 2, "b": 3, "c": 4}, "probe_b"),
            ]
            t1 = time.perf_counter_ns()
            res = asyncio.get_event_loop().run_until_complete(
                self.engine.resolve("agenda_probe_baseline", examples=examples)
            ) if False else None
            # Use asyncio.run for cleanliness
            async def _probe(goal):
                return await self.engine.resolve(goal, examples=examples)

            res = asyncio.run(_probe(f"agenda_probe_{seq}"))
            probe_ns = time.perf_counter_ns() - t1
            attempts = [
                {"strategy": a.get("strategy"), "accepted": a.get("accepted"),
                 "detail": str(a.get("detail", ""))[:80]}
                for a in (res.get("attempts") or [])
            ]
            actions.append("probe_resolve_baseline")
            evidence["probe_attempts"] = attempts
            evidence["probe_cost_ns"] = probe_ns
            evidence["probe_fully_resolved"] = res.get("fully_resolved")

            # Probe 2: strategy-attribution — count cost share of the blamed strategy
            blamed = [a for a in attempts if a.get("strategy") == strategy]
            evidence["blamed_strategy_present"] = bool(blamed)
            evidence["blamed_strategy_accepted"] = any(a.get("accepted") for a in blamed)
            actions.append("strategy_attribution")

            # Probe 3: counterfactual — if acquisition_pruning would drop strategy
            pruning_before = dict(getattr(self.engine, "acquisition_pruning", None) or {})
            if strategy and sig:
                trial = {sig: set(pruning_before.get(sig, set())) | {strategy}}
                setattr(self.engine, "acquisition_pruning", {
                    k: set(v) for k, v in {**pruning_before, **trial}.items()
                })
                t2 = time.perf_counter_ns()
                res2 = asyncio.run(_probe(f"agenda_probe_prune_{seq}"))
                probe2_ns = time.perf_counter_ns() - t2
                # restore
                setattr(self.engine, "acquisition_pruning", pruning_before)
                evidence["counterfactual_prune_cost_ns"] = probe2_ns
                evidence["counterfactual_attempts"] = [
                    a.get("strategy") for a in (res2.get("attempts") or [])
                ]
                evidence["cost_delta_ns"] = probe_ns - probe2_ns
                actions.append("counterfactual_prune_probe")
            evidence["new"] = True  # marks investigation-produced evidence
        except Exception as exc:
            evidence["investigation_error"] = f"{type(exc).__name__}: {exc}"
            actions.append("investigation_error")
            item.status = AgendaStatus.BLOCKED
            inv.result = "blocked"
            inv.actions = actions
            inv.evidence_collected = evidence
            inv.cost_ns = time.perf_counter_ns() - t0
            inv.completed_at = time.time()
            self.store.save_item(item)
            self.store.save_investigation(inv)
            self.store.log_event("investigation_blocked", inv.investigation_id, evidence)
            return inv

        inv.actions = actions
        inv.resources_used = ["SwarmEngine.resolve", "acquisition_pruning_counterfactual"]
        inv.evidence_collected = evidence
        inv.cost_ns = time.perf_counter_ns() - t0
        inv.completed_at = time.time()
        inv.result = "completed"
        item.status = AgendaStatus.COMPLETED
        self.store.save_item(item)
        self.store.save_investigation(inv)
        self.store.log_event("investigation_completed", inv.investigation_id, {
            "agenda_id": item.agenda_id,
            "cost_ns": inv.cost_ns,
            "actions": actions,
            "cost_delta_ns": evidence.get("cost_delta_ns"),
        })
        return inv


class AgendaLoop:
    """Full loop: form → select → investigate → candidates → improve → reassess."""

    def __init__(self, engine):
        db = getattr(engine, "db_path", None) or "swarm_engine.db"
        self.engine = engine
        self.store = AgendaStore(db)
        self.former = AgendaFormer(engine, self.store)
        self.investigator = AgendaInvestigator(engine, self.store)
        engine.improvement_agenda = self

    def run_once(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {
            "agenda_items": [],
            "selected": None,
            "investigation": None,
            "candidates_after": [],
            "selection_report": None,
            "pipeline_outcome": None,
            "agenda_after": [],
            "events_tail": [],
        }
        # 1. Form questions BEFORE candidates
        items = self.former.form_questions()
        report["agenda_items"] = [i.as_dict() for i in items]
        self.store.log_event("agenda_formation_complete", "", {
            "count": len(items),
            "ids": [i.agenda_id for i in items],
        })

        # 2. Select investigation
        selected = self.former.select(items)
        if selected is None:
            report["note"] = "no agenda item selected"
            return report
        report["selected"] = selected.as_dict()

        # 3. Investigate (new evidence)
        inv = self.investigator.investigate(selected)
        report["investigation"] = inv.as_dict()
        if inv.result != "completed":
            return report

        # 4. Only AFTER investigation: run existing observers → multi selector
        selector = getattr(self.engine, "multi_improvement_selector", None)
        if selector is None:
            report["note"] = "no multi_improvement_selector"
            return report
        # Stamp investigation linkage into event log before candidate generation
        self.store.log_event("pre_candidate_generation", selected.agenda_id, {
            "investigation_id": inv.investigation_id,
            "sequence": inv.sequence,
        })
        sel_report = selector.select_and_process()
        report["selection_report"] = sel_report
        report["candidates_after"] = sel_report.get("candidates") or []
        report["pipeline_outcome"] = sel_report.get("pipeline_outcome")

        # Link candidate ids onto investigation
        if sel_report.get("selected"):
            inv.candidate_ids = [sel_report["selected"]["improvement_id"]]
            self.store.save_investigation(inv)
            self.store.log_event("candidate_from_investigation", inv.investigation_id, {
                "candidate_id": sel_report["selected"]["improvement_id"],
            })

        # 5. Reassess agenda
        after = self.former.form_questions()
        report["agenda_after"] = [i.as_dict() for i in after]
        report["events_tail"] = self.store.events()[-20:]
        return report


class CycleRecord:
    def __init__(self, cycle_id: int, data: Dict[str, Any]):
        self.cycle_id = cycle_id
        self.data = data

    def as_dict(self) -> Dict[str, Any]:
        return {"cycle_id": self.cycle_id, **self.data}


def _ensure_cycle_table(store: AgendaStore):
    with store._conn() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS agenda_cycles ("
            " cycle_id INTEGER PRIMARY KEY,"
            " payload TEXT NOT NULL,"
            " at REAL NOT NULL)"
        )


def save_cycle(store: AgendaStore, cycle_id: int, payload: Dict[str, Any]):
    _ensure_cycle_table(store)
    with store._conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO agenda_cycles(cycle_id, payload, at) VALUES (?,?,?)",
            (cycle_id, json.dumps(payload), time.time()),
        )


def load_cycles(store: AgendaStore) -> List[Dict[str, Any]]:
    _ensure_cycle_table(store)
    with store._conn() as c:
        rows = c.execute("SELECT cycle_id, payload FROM agenda_cycles ORDER BY cycle_id").fetchall()
    return [{"cycle_id": r[0], **json.loads(r[1])} for r in rows]


# Monkey-patch AgendaLoop with run_cycles
def run_cycles(self, max_cycles: int = 5, min_priority: float = 0.01) -> Dict[str, Any]:
    """Evidence-driven multi-cycle agenda loop.

    Stops when no credible agenda item remains (priority < min_priority)
    or max_cycles is reached. No external improvement sequence.
    """
    cycles = []
    for n in range(1, max_cycles + 1):
        start_pruning = {
            k: sorted(list(v)) for k, v in (getattr(self.engine, "acquisition_pruning", None) or {}).items()
        }
        # Snapshot open priorities before cycle
        pre_items = self.former.form_questions()
        pre_snap = [
            {"agenda_id": i.agenda_id, "priority": i.priority, "scope": i.scope,
             "question": i.question[:100], "status": i.status.value}
            for i in pre_items
        ]
        if not pre_items or max(i.priority for i in pre_items) < min_priority:
            self.store.log_event("multi_cycle_stop", "", {
                "reason": "no_credible_agenda", "cycle": n, "priorities": [i.priority for i in pre_items],
            })
            break

        report = self.run_once()
        selected = report.get("selected") or {}
        inv = report.get("investigation") or {}
        pipe = report.get("pipeline_outcome") or {}
        end_pruning = {
            k: sorted(list(v)) for k, v in (getattr(self.engine, "acquisition_pruning", None) or {}).items()
        }
        post_items = report.get("agenda_after") or self.former.form_questions()
        if isinstance(post_items, list) and post_items and hasattr(post_items[0], "as_dict"):
            post_snap = [i.as_dict() for i in post_items]
        else:
            post_snap = post_items if isinstance(post_items, list) else []

        cycle_payload = {
            "start_pruning": start_pruning,
            "end_pruning": end_pruning,
            "agenda_before": pre_snap,
            "selected_agenda_id": selected.get("agenda_id"),
            "selected_question": (selected.get("question") or "")[:160],
            "selected_priority": selected.get("priority"),
            "selection_rationale": selected.get("selection_rationale"),
            "investigation_id": inv.get("investigation_id"),
            "investigation_result": inv.get("result"),
            "investigation_actions": inv.get("actions"),
            "new_evidence_keys": list((inv.get("evidence_collected") or {}).keys()),
            "candidates": [
                {"id": c.get("improvement_id"), "utility": c.get("utility"),
                 "subsystem": c.get("subsystem")}
                for c in (report.get("candidates_after") or [])
            ],
            "pipeline_outcome": pipe,
            "agenda_after": [
                {"agenda_id": a.get("agenda_id"), "priority": a.get("priority"),
                 "scope": a.get("scope"), "question": (a.get("question") or "")[:100]}
                for a in post_snap
            ] if post_snap and isinstance(post_snap[0], dict) else [],
            "behavioral_delta": {
                "pruning_added": {
                    k: sorted(set(end_pruning.get(k, [])) - set(start_pruning.get(k, [])))
                    for k in set(end_pruning) | set(start_pruning)
                    if set(end_pruning.get(k, [])) - set(start_pruning.get(k, []))
                }
            },
        }
        save_cycle(self.store, n, cycle_payload)
        self.store.log_event("cycle_completed", str(n), {
            "selected": selected.get("agenda_id"),
            "pipeline": (pipe or {}).get("outcome"),
        })
        cycles.append({"cycle_id": n, **cycle_payload})

        # If nothing activated and no pruning change, still continue if agenda remains
        # Stop if pipeline rejected and no behavioral change and priorities collapsed
        if not cycle_payload["behavioral_delta"]["pruning_added"]:
            # still allow cycle if investigation completed; next form may shift
            pass

    return {
        "cycles_completed": len(cycles),
        "cycles": cycles,
        "final_pruning": {
            k: sorted(list(v)) for k, v in (getattr(self.engine, "acquisition_pruning", None) or {}).items()
        },
        "events_tail": self.store.events()[-30:],
    }


AgendaLoop.run_cycles = run_cycles
AgendaLoop.load_cycles = lambda self: load_cycles(self.store)
