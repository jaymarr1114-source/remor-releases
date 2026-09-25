
"""
Open-ended task synthesis.

Construct novel executable goals + examples from engine state (experience,
capabilities, gaps, pruning) rather than instantiating a fixed family library.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class SynthesizedTask:
    task_id: str
    goal: str
    examples: List[Tuple[Dict[str, Any], Any]]
    motivation: str
    sources: List[str]
    capability_class: str
    expected_info_gain: float
    expected_capability_value: float
    relevance: float
    novelty: float
    future_value: float
    cost: float
    risk: float
    utility: float = 0.0
    rationale: str = ""
    synthesis_trace: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["examples"] = [
            {"args": a, "expect": e, "expect_type": type(e).__name__}
            for a, e in self.examples
        ]
        return d


def task_utility(
    info: float, cap: float, relevance: float, novelty: float,
    future: float, cost: float, risk: float,
) -> float:
    ev = 0.55 * info + 0.45 * cap
    return (
        max(0.0, ev) * max(0.0, relevance) * max(0.05, novelty)
        * max(0.05, future) / (1.0 + max(0.0, cost) + max(0.0, risk))
    )


class TaskSynthesizer:
    """Synthesize novel tasks from live engine state."""

    def __init__(self, engine, seed: Optional[int] = None):
        self.engine = engine
        self._rng = random.Random(seed if seed is not None else int(time.time()) & 0xFFFF)

    # ----- state inspection -------------------------------------------------
    def _learner_groups(self) -> List[Dict[str, Any]]:
        learner = getattr(self.engine, "strategy_learner", None)
        if learner is None:
            return []
        try:
            import sqlite3
            from collections import defaultdict
            conn = sqlite3.connect(learner.db_path)
            rows = conn.execute(
                "SELECT signature, strategy, success, cost FROM acquisition_experience"
            ).fetchall()
            conn.close()
            g: Dict[tuple, Dict[str, Any]] = {}
            for sig, strat, succ, cost in rows:
                key = (sig, strat)
                e = g.setdefault(key, {
                    "signature": sig, "strategy": strat,
                    "attempts": 0, "successes": 0, "cost": 0,
                })
                e["attempts"] += 1
                e["successes"] += int(succ)
                e["cost"] += int(cost or 0)
            return list(g.values())
        except Exception:
            return []

    def _capabilities(self) -> List[str]:
        try:
            return [c.name for c in self.engine.capabilities.list()]
        except Exception:
            return []

    def _primitives(self) -> List[str]:
        try:
            return list(self.engine.primitives.names())
        except Exception:
            return []

    def _pruned(self) -> Dict[str, set]:
        p = getattr(self.engine, "acquisition_pruning", None) or {}
        return {k: set(v) for k, v in p.items()}

    def _history_goals(self) -> set:
        """Goals already synthesized/executed (novelty vs history)."""
        seen = set()
        loop = getattr(self.engine, "task_synthesis_loop", None)
        if loop is None:
            return seen
        try:
            for c in loop.load_cycles():
                g = c.get("selected_goal")
                if g:
                    seen.add(g)
        except Exception:
            pass
        return seen

    # ----- synthesis operators (grammar over state, not fixed tasks) --------
    def _synth_from_failure(self, groups: List[Dict[str, Any]], pruned: Dict[str, set],
                            history: set) -> List[SynthesizedTask]:
        """Build tasks that stress signatures with recurring failures."""
        out = []
        fails = [
            g for g in groups
            if g["successes"] == 0 and g["attempts"] >= 2
        ]
        fails.sort(key=lambda x: -x["cost"])
        for g in fails[:3]:
            sig = g["signature"]
            strat = g["strategy"]
            if strat in pruned.get(sig, set()):
                continue
            # Parse rough arity from signature fragment in:N
            arity = 2
            for part in sig.split("|"):
                if part.startswith("in:"):
                    frag = part[3:]
                    if frag.endswith("+"):
                        try:
                            arity = int(frag[:-1]) + 1
                        except ValueError:
                            arity = 3
                    else:
                        try:
                            arity = int(frag)
                        except ValueError:
                            arity = 2
            # Output kind hint
            out_kind = "str"
            for part in sig.split("|"):
                if part.startswith("out:"):
                    out_kind = part[4:] or "str"

            # Synthesize concrete examples procedurally (not a fixed template table)
            salt = self._rng.randint(1, 20)
            args_list = []
            expects = []
            for k in range(2):
                args = {f"p{i}": salt + k * (i + 1) + i for i in range(arity)}
                if out_kind in ("str", "none"):
                    expect = f"syn_{salt}_{k}_{arity}"
                elif out_kind in ("num", "int", "float"):
                    expect = sum(args.values())
                elif out_kind in ("list",):
                    expect = list(args.values())
                else:
                    expect = {"k": salt + k, "n": arity}
                args_list.append(args)
                expects.append(expect)
            examples = list(zip(args_list, expects))
            goal = f"synth_fail_{strat}_{arity}_{out_kind}_{salt}_{uuid.uuid4().hex[:6]}"
            if goal in history:
                continue
            info = min(1.3, 0.4 + math.log1p(g["cost"] / 1e9))
            task = SynthesizedTask(
                task_id=f"task_{uuid.uuid4().hex[:10]}",
                goal=goal,
                examples=examples,
                motivation=(
                    f"Recurring {strat} failure on signature {sig[:48]} "
                    f"({g['attempts']} attempts, 0 success); synthesize fresh "
                    f"arity={arity} out={out_kind} probe"
                ),
                sources=["acquisition_experience", f"strategy:{strat}"],
                capability_class=f"failure_probe:{strat}:{out_kind}",
                expected_info_gain=info,
                expected_capability_value=0.35,
                relevance=0.9,
                novelty=0.8,
                future_value=0.55,
                cost=1.0,
                risk=0.2,
                synthesis_trace={
                    "operator": "from_failure",
                    "signature": sig,
                    "strategy": strat,
                    "arity": arity,
                    "out_kind": out_kind,
                    "salt": salt,
                },
            )
            out.append(task)
        return out

    def _synth_from_capabilities(self, caps: List[str], prims: List[str],
                                 history: set) -> List[SynthesizedTask]:
        """Compose tasks that exercise acquired capabilities in new combinations."""
        out = []
        acquired = [c for c in caps if c.startswith("curriculum_") or "comp" in c or "sum" in c]
        acquired += [p for p in prims if p.startswith("acquired.") or p.startswith("curriculum_")]
        # Dedup preserve order
        seen = set()
        acq = []
        for a in acquired:
            if a not in seen:
                seen.add(a)
                acq.append(a)
        if len(acq) >= 1:
            # Task: apply numeric-like combination with fresh constants
            a, b = self._rng.randint(2, 15), self._rng.randint(2, 15)
            goal = f"synth_reuse_add_{a}_{b}_{uuid.uuid4().hex[:6]}"
            if goal not in history:
                out.append(SynthesizedTask(
                    task_id=f"task_{uuid.uuid4().hex[:10]}",
                    goal=goal,
                    examples=[
                        ({"x": a, "y": b}, a + b),
                        ({"x": a + 1, "y": b + 2}, (a + 1) + (b + 2)),
                        ({"x": 0, "y": b}, b),
                    ],
                    motivation=(
                        f"Exercise numeric combination using known acquisition "
                        f"surface; acquired_hints={acq[:3]}"
                    ),
                    sources=["capabilities", "primitives"],
                    capability_class="reuse:numeric_binop",
                    expected_info_gain=0.55,
                    expected_capability_value=0.8,
                    relevance=0.7,
                    novelty=0.65,
                    future_value=0.85,
                    cost=0.9,
                    risk=0.15,
                    synthesis_trace={
                        "operator": "from_capabilities",
                        "acquired_hints": acq[:5],
                        "pair": [a, b],
                    },
                ))
        if len(acq) >= 0:
            # Structural list task with fresh numbers
            u, v = self._rng.randint(1, 12), self._rng.randint(1, 12)
            goal = f"synth_struct_{u}_{v}_{uuid.uuid4().hex[:6]}"
            if goal not in history:
                out.append(SynthesizedTask(
                    task_id=f"task_{uuid.uuid4().hex[:10]}",
                    goal=goal,
                    examples=[
                        ({"a": u, "b": v}, [u + v, u]),
                        ({"a": u + 2, "b": v + 1}, [(u + 2) + (v + 1), u + 2]),
                    ],
                    motivation=(
                        "Synthesize structural list objective [sum, first] from "
                        "composition-shaped state pressure"
                    ),
                    sources=["capability_gaps", "composition_pressure"],
                    capability_class="synth:list_assembly",
                    expected_info_gain=0.75,
                    expected_capability_value=0.85,
                    relevance=0.8,
                    novelty=0.75,
                    future_value=0.9,
                    cost=1.15,
                    risk=0.2,
                    synthesis_trace={
                        "operator": "from_composition_pressure",
                        "values": [u, v],
                    },
                ))
        return out

    def _synth_from_coverage_holes(self, groups: List[Dict[str, Any]],
                                   history: set) -> List[SynthesizedTask]:
        """Invent arity/output shapes under-represented in experience."""
        out = []
        # Observed arities roughly from signatures
        seen_out = set()
        for g in groups:
            for part in g["signature"].split("|"):
                if part.startswith("out:"):
                    seen_out.add(part[4:])
        # If list outputs rare, synthesize one; if dict rare, synthesize one
        salt = self._rng.randint(3, 30)
        if "list" not in seen_out or self._rng.random() < 0.5:
            goal = f"synth_cov_list_{salt}_{uuid.uuid4().hex[:6]}"
            if goal not in history:
                out.append(SynthesizedTask(
                    task_id=f"task_{uuid.uuid4().hex[:10]}",
                    goal=goal,
                    examples=[
                        ({"m": salt, "n": salt + 1}, [salt, salt + 1]),
                        ({"m": salt + 2, "n": salt + 3}, [salt + 2, salt + 3]),
                    ],
                    motivation="Coverage hole: list-shaped outputs under-sampled in experience",
                    sources=["coverage_analysis"],
                    capability_class="coverage:list_pair",
                    expected_info_gain=0.6,
                    expected_capability_value=0.5,
                    relevance=0.55,
                    novelty=0.85,
                    future_value=0.6,
                    cost=0.7,
                    risk=0.1,
                    synthesis_trace={"operator": "coverage_hole", "shape": "list", "salt": salt},
                ))
        if "dict" not in seen_out:
            goal = f"synth_cov_dict_{salt}_{uuid.uuid4().hex[:6]}"
            if goal not in history:
                out.append(SynthesizedTask(
                    task_id=f"task_{uuid.uuid4().hex[:10]}",
                    goal=goal,
                    examples=[
                        ({"m": salt}, {"v": salt}),
                        ({"m": salt + 5}, {"v": salt + 5}),
                    ],
                    motivation="Coverage hole: dict-shaped outputs absent from experience",
                    sources=["coverage_analysis"],
                    capability_class="coverage:dict_wrap",
                    expected_info_gain=0.5,
                    expected_capability_value=0.4,
                    relevance=0.45,
                    novelty=0.9,
                    future_value=0.5,
                    cost=0.6,
                    risk=0.1,
                    synthesis_trace={"operator": "coverage_hole", "shape": "dict", "salt": salt},
                ))
        # Always offer a low-value near-duplicate control-style task
        goal = f"synth_low_{salt}_{uuid.uuid4().hex[:6]}"
        out.append(SynthesizedTask(
            task_id=f"task_{uuid.uuid4().hex[:10]}",
            goal=goal,
            examples=[({"z": 0}, 0)],
            motivation="Low expected information constant probe (utility floor control)",
            sources=["control"],
            capability_class="low_value",
            expected_info_gain=0.05,
            expected_capability_value=0.05,
            relevance=0.1,
            novelty=0.1,
            future_value=0.05,
            cost=0.15,
            risk=0.0,
            synthesis_trace={"operator": "low_value_control"},
        ))
        return out

    def _refuted_claim_contexts(self) -> List[Dict[str, Any]]:
        """Generic context: REFUTED hyp + its Evidence + lh_challenge I/O."""
        out: List[Dict[str, Any]] = []
        intellect = getattr(self.engine, "intellect", None)
        if intellect is None or not hasattr(intellect, "epistemic"):
            return out
        try:
            hyps = intellect.epistemic.all_hypotheses()
        except Exception:
            return out
        import sqlite3
        db = getattr(self.engine, "db_path", None)
        for h in hyps or []:
            st = getattr(getattr(h, "state", None), "value", None)
            if st != "refuted":
                continue
            try:
                evs = intellect.epistemic.evidence_for(h.hypothesis_id)
            except Exception:
                evs = []
            cids = []
            for ev in evs or []:
                cid = (getattr(ev, "content", None) or {}).get("challenge_id")
                if cid and cid not in cids:
                    cids.append(cid)
            train, heldout, goal = [], [], getattr(h, "statement", "") or ""
            example_source = None
            # Preferred: I/O already on Evidence.content
            for ev in evs or []:
                content = getattr(ev, "content", None) or {}
                rows = content.get("examples") or []
                if not rows:
                    continue
                pairs = []
                for row in rows:
                    if isinstance(row, dict) and "input" in row:
                        pairs.append([row.get("input"), row.get("expected")])
                if not pairs:
                    continue
                chn = content.get("channel")
                if chn == "heldout":
                    heldout = pairs
                    example_source = "evidence_content"
                elif chn == "resolve" and not train:
                    train = pairs
                    example_source = example_source or "evidence_content"
            # Legacy fallback only if Evidence has no I/O
            if (not train and not heldout and db and cids
                    and getattr(self, "_lh_fallback_enabled", True)
                    and getattr(self.engine, "_lh_fallback_enabled", True)):
                try:
                    conn = sqlite3.connect(db)
                    conn.row_factory = sqlite3.Row
                    for cid in cids:
                        row = conn.execute(
                            "SELECT goal, train_json, heldout_json FROM lh_challenge "
                            "WHERE challenge_id=? LIMIT 1", (cid,)).fetchone()
                        if row is None:
                            continue
                        goal = row["goal"] or goal
                        if row["train_json"]:
                            train = json.loads(row["train_json"])
                        if row["heldout_json"]:
                            heldout = json.loads(row["heldout_json"])
                        example_source = "lh_challenge"
                    conn.close()
                except Exception:
                    pass
            out.append({
                "hypothesis_id": h.hypothesis_id,
                "statement": getattr(h, "statement", ""),
                "subject_id": h.hypothesis_id.split(":", 1)[-1],
                "evidence_ids": [getattr(e, "evidence_id", "") for e in evs or []],
                "challenge_ids": cids,
                "goal": goal,
                "train": train,
                "heldout": heldout,
                "example_source": example_source,
            })
        return out

    def _pairs(self, blob) -> List[Tuple[Dict[str, Any], Any]]:
        pairs = []
        for item in blob or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                inp, out = item
                if isinstance(inp, dict):
                    pairs.append((dict(inp), out))
        return pairs

    def _synth_from_refuted_claims(self, history: set) -> List[SynthesizedTask]:
        if not getattr(self, "_claim_grounding_enabled", True):
            return []
        if not getattr(self.engine, "_claim_grounding_enabled", True):
            return []
        out: List[SynthesizedTask] = []
        for ctx in self._refuted_claim_contexts():
            held = self._pairs(ctx.get("heldout"))
            train = self._pairs(ctx.get("train"))
            hid = ctx["hypothesis_id"]
            tag = hid.replace(":", "_")[:24]
            specs = []
            if held:
                specs.append(("heldout", held, 0.85))
            if train and held:
                mix = train[:1] + held[:1]
                specs.append(("mix", mix, 0.7))
            elif train:
                specs.append(("train", train, 0.55))
            for kind, examples, info in specs:
                keys = sorted({k for inp, _ in examples for k in inp})
                goal = f"claim_{kind}_{tag}_{len(examples)}_{''.join(keys) or 'x'}"
                if goal in history:
                    continue
                out.append(SynthesizedTask(
                    task_id=f"task_{uuid.uuid4().hex[:10]}",
                    goal=goal,
                    examples=examples,
                    motivation=(
                        f"Claim-grounded {kind} probe from REFUTED {hid} "
                        f"using persisted I/O (n={len(examples)})"
                    ),
                    sources=["epistemic_refuted", "lh_challenge", hid],
                    capability_class=f"claim_probe:{kind}",
                    expected_info_gain=info,
                    expected_capability_value=0.6,
                    relevance=1.0,
                    novelty=0.9,
                    future_value=0.85,
                    cost=0.9,
                    risk=0.15,
                    synthesis_trace={
                        "operator": "from_refuted_claim",
                        "hypothesis_id": hid,
                        "evidence_ids": ctx.get("evidence_ids"),
                        "challenge_ids": ctx.get("challenge_ids"),
                        "example_kind": kind,
                        "n_examples": len(examples),
                        "input_keys": keys,
                        "example_source": ctx.get("example_source"),
                    },
                ))
        return out

    def _synth_from_unused_io_failures(self, history: set) -> List[SynthesizedTask]:
        """Validation-pressure: unused_io Evidence that does not support
        the claim becomes a broader acquire using those failed pairs plus
        any other Evidence I/O on the same hypothesis."""
        if not getattr(self, "_validation_synth_enabled", True):
            return []
        if not getattr(self.engine, "_validation_synth_enabled", True):
            return []
        intellect = getattr(self.engine, "intellect", None)
        if intellect is None:
            return []
        try:
            hyps = intellect.epistemic.all_hypotheses()
        except Exception:
            return []
        out: List[SynthesizedTask] = []
        for h in hyps or []:
            try:
                evs = intellect.epistemic.evidence_for(h.hypothesis_id)
            except Exception:
                evs = []
            failed, other, eids = [], [], []
            for ev in evs or []:
                content = getattr(ev, "content", None) or {}
                eids.append(getattr(ev, "evidence_id", ""))
                if content.get("channel") == "unused_io" and not getattr(ev, "supports", True):
                    inp, exp = content.get("input"), content.get("expected")
                    if isinstance(inp, dict):
                        failed.append((dict(inp), exp))
                for row in content.get("examples") or []:
                    if isinstance(row, dict) and "input" in row:
                        other.append((dict(row.get("input") or {}), row.get("expected")))
            related_ids = []
            related_eids = []
            if getattr(self, "_evidence_union_enabled", True) and getattr(
                    self.engine, "_evidence_union_enabled", True):
                parents = []
                for ev in evs or []:
                    parent = (getattr(ev, "content", None) or {}).get("from_hypothesis")
                    if parent and parent not in parents:
                        parents.append(parent)
                for phid in parents:
                    try:
                        pevs = intellect.epistemic.evidence_for(phid)
                    except Exception:
                        pevs = []
                    related_ids.append(phid)
                    for ev in pevs or []:
                        related_eids.append(getattr(ev, "evidence_id", ""))
                        content = getattr(ev, "content", None) or {}
                        for row in content.get("examples") or []:
                            if isinstance(row, dict) and "input" in row:
                                other.append((dict(row.get("input") or {}),
                                              row.get("expected")))
                        if content.get("channel") == "unused_io" and isinstance(content.get("input"), dict):
                            other.append((dict(content.get("input")), content.get("expected")))
            if not failed:
                continue
            seen = set()
            examples = []
            for inp, exp in failed + other:
                key = tuple(sorted((inp or {}).items()))
                if key in seen:
                    continue
                seen.add(key)
                examples.append((inp, exp))
            hid = h.hypothesis_id
            tag = hid.replace(":", "_")[:28]
            goal = f"claim_reacquire_{tag}_{len(examples)}"
            if goal in history:
                continue
            out.append(SynthesizedTask(
                task_id=f"task_{uuid.uuid4().hex[:10]}",
                goal=goal,
                examples=examples,
                motivation=(
                    f"Unused-I/O validation failed for {hid} "
                    f"({len(failed)} mismatches); reacquire on union I/O"
                ),
                sources=["unused_io", "epistemic_state", hid],
                capability_class="claim_probe:reacquire",
                expected_info_gain=0.9,
                expected_capability_value=0.7,
                relevance=1.0,
                novelty=0.85,
                future_value=0.85,
                cost=0.9,
                risk=0.15,
                synthesis_trace={
                    "operator": "from_unused_io",
                    "hypothesis_id": hid,
                    "evidence_ids": eids,
                    "n_failed": len(failed),
                    "n_examples": len(examples),
                    "example_source": "unused_io_union" if related_ids else "unused_io",
                    "related_hypotheses": related_ids,
                    "related_evidence_ids": related_eids,
                },
            ))
        return out

    def generate(self) -> List[SynthesizedTask]:
        groups = self._learner_groups()
        caps = self._capabilities()
        prims = self._primitives()
        pruned = self._pruned()
        history = self._history_goals()

        tasks: List[SynthesizedTask] = []
        tasks.extend(self._synth_from_refuted_claims(history))
        tasks.extend(self._synth_from_unused_io_failures(history))
        tasks.extend(self._synth_from_failure(groups, pruned, history))
        tasks.extend(self._synth_from_capabilities(caps, prims, history))
        tasks.extend(self._synth_from_coverage_holes(groups, history))

        # Score
        for t in tasks:
            # Novelty penalty if goal prefix pattern repeated often in history
            prefix = "_".join(t.goal.split("_")[:3])
            repeats = sum(1 for h in history if h.startswith(prefix))
            nov = t.novelty * (0.6 ** repeats)
            t.novelty = max(0.05, nov)
            t.utility = task_utility(
                t.expected_info_gain, t.expected_capability_value,
                t.relevance, t.novelty, t.future_value, t.cost, t.risk,
            )
            t.rationale = (
                f"utility={t.utility:.4f} op={t.synthesis_trace.get('operator')} "
                f"info={t.expected_info_gain:.2f} cap={t.expected_capability_value:.2f} "
                f"nov={t.novelty:.2f}"
            )
        tasks.sort(key=lambda x: (-x.utility, x.task_id))
        return tasks

    def select(self, tasks: List[SynthesizedTask]) -> Optional[SynthesizedTask]:
        viable = [t for t in tasks if t.utility >= 0.02]
        if not viable:
            return None
        return viable[0]


class TaskSynthesisLoop:
    def __init__(self, engine):
        self.engine = engine
        self.synth = TaskSynthesizer(engine)
        from swarm_engine.improvement.agenda import AgendaStore
        db = getattr(engine, "db_path", None) or "swarm_engine.db"
        self.store = AgendaStore(db)
        with self.store._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS synthesis_cycles ("
                " cycle_id INTEGER PRIMARY KEY,"
                " payload TEXT NOT NULL,"
                " at REAL NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS synthesis_seq ("
                " name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )

    def _next_id(self) -> int:
        with self.store._conn() as c:
            row = c.execute("SELECT value FROM synthesis_seq WHERE name='s'").fetchone()
            if not row:
                c.execute("INSERT INTO synthesis_seq(name,value) VALUES('s',1)")
                return 1
            v = int(row[0]) + 1
            c.execute("UPDATE synthesis_seq SET value=? WHERE name='s'", (v,))
            return v

    def _save(self, cycle_id: int, payload: Dict[str, Any]):
        with self.store._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO synthesis_cycles(cycle_id,payload,at) VALUES (?,?,?)",
                (cycle_id, json.dumps(payload), time.time()),
            )

    def load_cycles(self) -> List[Dict[str, Any]]:
        with self.store._conn() as c:
            rows = c.execute(
                "SELECT cycle_id, payload FROM synthesis_cycles ORDER BY cycle_id"
            ).fetchall()
        return [{"cycle_id": r[0], **json.loads(r[1])} for r in rows]

    def run_once(self) -> Dict[str, Any]:
        import asyncio
        report: Dict[str, Any] = {
            "candidates": [],
            "selected": None,
            "execution": None,
            "learning": None,
            "next_preview": [],
        }
        tasks = self.synth.generate()
        report["candidates"] = [t.as_dict() for t in tasks]
        chosen = self.synth.select(tasks)
        if chosen is None:
            report["note"] = "no viable synthesized task"
            return report
        report["selected"] = chosen.as_dict()
        self.store.log_event("task_synthesized_selected", chosen.task_id, {
            "goal": chosen.goal,
            "utility": chosen.utility,
            "operator": chosen.synthesis_trace.get("operator"),
            "motivation": chosen.motivation[:160],
        })

        t0 = time.perf_counter_ns()
        try:
            # v32: safe across sync callers and already-running event loops
            # (asyncio.run cannot nest). Prefer the running loop via a
            # worker thread when needed; never block the loop thread with
            # nest_asyncio hacks.
            coro = self.engine.resolve(chosen.goal, examples=chosen.examples)
            try:
                asyncio.get_running_loop()
                running = True
            except RuntimeError:
                running = False
            if running:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    result = pool.submit(asyncio.run, coro).result()
            else:
                result = asyncio.run(coro)
            err = None
        except Exception as exc:
            result = {}
            err = f"{type(exc).__name__}: {exc}"
        exec_info = {
            "goal": chosen.goal,
            "fully_resolved": (result or {}).get("fully_resolved"),
            "failed": (result or {}).get("failed"),
            "acquired": (result or {}).get("acquired"),
            "strategies": [
                a.get("strategy") for a in ((result or {}).get("attempts") or [])
            ],
            "wall_ns": time.perf_counter_ns() - t0,
            "error": err,
        }
        report["execution"] = exec_info
        report["learning"] = {
            "new_acquisition": bool(exec_info.get("acquired")),
            "new_failure": bool(exec_info.get("strategies")) and not exec_info.get("fully_resolved"),
            "informative": bool(exec_info.get("strategies")) or bool(exec_info.get("acquired")),
        }
        self.store.log_event("task_synthesized_executed", chosen.task_id, exec_info)

        # Follow up failures with experiment design when informative
        if report["learning"]["new_failure"] and not report["learning"]["new_acquisition"]:
            exp = getattr(self.engine, "experiment_design_loop", None)
            if exp is not None:
                try:
                    er = exp.run_once()
                    report["experiment_followup"] = {
                        "epistemic": er.get("epistemic"),
                        "pipeline": (er.get("improvement") or {}).get("pipeline_outcome")
                        if isinstance(er.get("improvement"), dict) else None,
                    }
                except Exception as exc:
                    report["experiment_followup"] = {"error": str(exc)}

        # Next candidates from updated state
        nxt = self.synth.generate()
        report["next_preview"] = [
            {
                "goal": t.goal,
                "utility": t.utility,
                "operator": t.synthesis_trace.get("operator"),
                "class": t.capability_class,
            }
            for t in nxt[:6]
        ]
        return report

    def run_cycles(self, max_cycles: int = 5, min_utility: float = 0.02) -> Dict[str, Any]:
        cycles = []
        for _ in range(max_cycles):
            tasks = self.synth.generate()
            if not tasks or max(t.utility for t in tasks) < min_utility:
                break
            cid = self._next_id()
            report = self.run_once()
            if not report.get("selected"):
                break
            payload = {
                "selected_goal": report["selected"]["goal"],
                "selected_utility": report["selected"]["utility"],
                "operator": report["selected"].get("synthesis_trace", {}).get("operator"),
                "capability_class": report["selected"].get("capability_class"),
                "motivation": report["selected"].get("motivation", "")[:160],
                "candidates": [
                    {
                        "goal": c["goal"],
                        "utility": c["utility"],
                        "operator": (c.get("synthesis_trace") or {}).get("operator"),
                    }
                    for c in report.get("candidates") or []
                ],
                "execution": report.get("execution"),
                "learning": report.get("learning"),
                "experiment_followup": report.get("experiment_followup"),
                "next_preview": report.get("next_preview"),
            }
            self._save(cid, payload)
            cycles.append({"cycle_id": cid, **payload})
        return {"cycles_completed": len(cycles), "cycles": cycles}
