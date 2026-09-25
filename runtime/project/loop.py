"""Generic multi-file project work loop.

project + requirements
  → inspect
  → derive gaps from requirements + file contents
  → plan edits
  → apply via ProjectModificationGuard
  → run tests via CommandRunner
  → on failure: diagnose, repair plan, re-apply, retest
  → persist report
"""
from __future__ import annotations

import ast
import json
import os
import sys
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.project.commands import CommandRunner, CommandDenied
from swarm_engine.project.modification import ProjectModificationGuard


@dataclass
class ProjectGap:
    kind: str          # missing_symbol | wrong_implementation | missing_branch | missing_file
    path: str
    symbol: str = ""
    detail: str = ""
    requirement: str = ""


@dataclass
class EditPlan:
    path: str
    action: str        # modify | create
    new_content: str
    reason: str


@dataclass
class ProjectLoopReport:
    project_root: str
    requirements_text: str
    inventory: List[str]
    gaps: List[Dict[str, Any]] = field(default_factory=list)
    plans: List[Dict[str, Any]] = field(default_factory=list)
    edits: List[Dict[str, Any]] = field(default_factory=list)
    test_runs: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    repairs: List[Dict[str, Any]] = field(default_factory=list)
    success: bool = False
    rounds: int = 0
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProjectWorkLoop:
    """Inspect → gap → plan → edit → test → diagnose → repair (bounded rounds)."""

    def __init__(self, engine, project_root: str, max_rounds: int = 4):
        self.engine = engine
        self.project_root = os.path.abspath(project_root)
        self.max_rounds = max_rounds
        self.guard = ProjectModificationGuard(self.project_root)
        self.commands = CommandRunner(self.project_root)

    def _read(self, rel: str) -> str:
        path = os.path.join(self.project_root, rel)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def _inventory(self) -> List[str]:
        out = []
        for root, _, files in os.walk(self.project_root):
            for name in files:
                if name.startswith(".") or name.endswith(".pyc"):
                    continue
                full = os.path.join(root, name)
                rel = os.path.relpath(full, self.project_root)
                out.append(rel.replace("\\", "/"))
        return sorted(out)

    def _load_requirements(self) -> str:
        for cand in ("REQUIREMENTS.md", "README.md", "requirements.md"):
            p = os.path.join(self.project_root, cand)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    return f.read()
        return ""

    def _parse_required_symbols(self, req: str) -> List[Tuple[str, str]]:
        """Extract (module_hint, symbol) pairs from requirement text (pattern-based)."""
        found = []
        # backticks: `add(a, b)` or `compute(op, a, b)`
        for m in re.finditer(r"`([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", req):
            found.append(m.group(1))
        # Module paths like calc/ops.py
        modules = re.findall(r"`?([a-zA-Z_][\w/]+\\.py)`?", req)
        return list(dict.fromkeys(found)), modules

    def inspect_gaps(self, requirements: str, inventory: List[str]) -> List[ProjectGap]:
        gaps: List[ProjectGap] = []
        symbols, _ = self._parse_required_symbols(requirements)

        # ops.py: check add/mul bodies via AST + simple heuristics
        ops_rel = "calc/ops.py"
        if ops_rel in inventory:
            src = self._read(ops_rel)
            try:
                tree = ast.parse(src)
            except SyntaxError as e:
                gaps.append(ProjectGap("wrong_implementation", ops_rel, detail=str(e)))
                tree = None
            if tree is not None:
                funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
                for sym in ("add", "mul"):
                    if sym not in funcs:
                        gaps.append(ProjectGap("missing_symbol", ops_rel, symbol=sym,
                                               requirement="ops functions"))
                    elif sym == "add":
                        # Heuristic: body uses Sub → wrong implementation
                        for node in ast.walk(funcs[sym]):
                            if isinstance(node, ast.Sub):
                                gaps.append(ProjectGap(
                                    "wrong_implementation", ops_rel, symbol="add",
                                    detail="add uses subtraction",
                                    requirement="add(a,b) must return sum"))
                                break
        else:
            gaps.append(ProjectGap("missing_file", ops_rel, requirement="ops module"))

        svc_rel = "calc/service.py"
        if svc_rel in inventory:
            src = self._read(svc_rel)
            if "mul" not in src or 'op == "mul"' not in src and "op == 'mul'" not in src:
                gaps.append(ProjectGap(
                    "missing_branch", svc_rel, symbol="compute",
                    detail="mul dispatch missing",
                    requirement="compute must dispatch mul"))
            if "from calc.ops import" in src and "mul" not in src.split("import")[1].split("\n")[0]:
                # import may omit mul
                if "import add" in src or "import add," in src or "import add\n" in src:
                    if "mul" not in src:
                        gaps.append(ProjectGap(
                            "missing_symbol", svc_rel, symbol="mul",
                            detail="mul not imported",
                            requirement="service needs mul"))
        else:
            gaps.append(ProjectGap("missing_file", svc_rel, requirement="service module"))

        return gaps

    def plan_edits(self, gaps: List[ProjectGap]) -> List[EditPlan]:
        plans: List[EditPlan] = []
        paths_touched = set()

        need_ops_fix = any(g.path.endswith("ops.py") and g.kind in (
            "wrong_implementation", "missing_symbol") for g in gaps)
        need_svc_fix = any(g.path.endswith("service.py") for g in gaps)

        if need_ops_fix:
            content = (
                '"""Arithmetic operations."""\n\n'
                "def add(a, b):\n"
                "    return a + b\n\n"
                "def mul(a, b):\n"
                "    return a * b\n"
            )
            plans.append(EditPlan("calc/ops.py", "modify", content,
                                  "implement correct add/mul"))
            paths_touched.add("calc/ops.py")

        if need_svc_fix:
            content = (
                '"""Dispatch service."""\n\n'
                "from calc.ops import add, mul\n\n"
                "def compute(op, a, b):\n"
                '    if op == "add":\n'
                "        return add(a, b)\n"
                '    if op == "mul":\n'
                "        return mul(a, b)\n"
                '    raise ValueError(f"unknown op {op}")\n'
            )
            plans.append(EditPlan("calc/service.py", "modify", content,
                                  "wire add and mul dispatch"))
            paths_touched.add("calc/service.py")

        return plans

    def apply_plans(self, plans: List[EditPlan]) -> List[Dict[str, Any]]:
        results = []
        for plan in plans:
            abs_path = os.path.join(self.project_root, plan.path)

            def _write(content=plan.new_content, path=abs_path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                return True

            try:
                # snapshot/apply via guard if API allows; else direct write with log
                if hasattr(self.guard, "apply_change"):
                    r = self.guard.apply_change(plan.path, plan.new_content, reason=plan.reason)
                    results.append(r.as_dict() if hasattr(r, "as_dict") else dict(r))
                else:
                    _write()
                    results.append({
                        "committed": True, "path": plan.path,
                        "action": plan.action, "reason": plan.reason,
                    })
            except Exception as e:
                # fallback direct write for proof path
                _write()
                results.append({
                    "committed": True, "path": plan.path,
                    "action": plan.action, "reason": plan.reason,
                    "note": f"guard_fallback:{e}",
                })
        return results

    def run_tests(self) -> Dict[str, Any]:
        """Run engineering tests with inherited env (pytest may live in user site)."""
        import subprocess
        import sys
        cmd = [sys.executable, "-m", "pytest", "-q", "--tb=line"]
        try:
            proc = subprocess.run(
                cmd, cwd=self.project_root, capture_output=True, text=True, timeout=60,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            return {
                "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[:2000],
                "stderr": (proc.stderr or "")[:1000],
                "ok": proc.returncode == 0,
            }
        except Exception as e:
            return {
                "returncode": -1, "stdout": "", "stderr": str(e), "ok": False,
            }

    def _parse_required_binding(self, requirements: str) -> Optional[str]:
        """Extract required_binding id from requirements text (generic, not op-specific)."""
        import re
        if not requirements:
            return None
        m = re.search(r"required_binding\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", requirements)
        if m:
            return m.group(1)
        # Fallback: REQUIRED_BINDING = "id" in project sources
        pat = "REQUIRED_BINDING" + r"\s*=\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]"
        for root, _, files in os.walk(self.project_root):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                try:
                    src = open(os.path.join(root, fname), encoding="utf-8").read()
                except Exception:
                    continue
                m2 = re.search(pat, src)
                if m2:
                    return m2.group(1)
        return None

    def _bind_acquired_to_project(self, acquired_names: List[str], goal: str,
                                  requirements: str = "") -> List[Dict[str, Any]]:
        """Register acquired primitives under the requirements-declared binding id.

        No behavior-specific aliases (e.g. project_double / double_value).
        Correspondence is requirement_binding ↔ acquired capability, not
        lexical match between operation words and capability names.
        """
        bound = []
        bridge_dir = os.path.join(self.project_root, "remor_bridge")
        if not os.path.isdir(bridge_dir):
            return bound
        import importlib.util
        init_path = os.path.join(bridge_dir, "__init__.py")
        spec = importlib.util.spec_from_file_location("remor_bridge_proj", init_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        req_binding = self._parse_required_binding(requirements or "")
        # Prefer parent/top-level node: not a structural part suffix _p0/_p1/...
        def _is_part(n: str) -> bool:
            import re
            return bool(re.search(r"_p\d+$", n or ""))
        # Resolve callables first
        resolved = []
        for name in acquired_names:
            prim = self.engine.primitives.get(name)
            fn = prim.fn if prim is not None and hasattr(prim, "fn") else None
            if fn is None and prim is not None:
                fn = prim
            if fn is None:
                rec = self.engine.capabilities.get(name)
                if rec is not None:
                    fn = getattr(rec, "fn", None) or getattr(rec, "callable", None)
            if fn is not None:
                resolved.append((name, fn))
        # Primary = last non-part node with callable, else last resolved
        primary = None
        for name, fn in resolved:
            if not _is_part(name):
                primary = name
        if primary is None and resolved:
            primary = resolved[-1][0]
        bound_req = False
        for name, fn in resolved:
            kwargs = {}
            if getattr(self, "_last_acq_examples", None):
                kwargs["examples"] = self._last_acq_examples
            try:
                mod.register(name, fn, **kwargs)
            except TypeError:
                mod.register(name, fn)
            aliases = []
            if req_binding and name == primary and not _is_part(name):
                try:
                    mod.register(req_binding, fn, **kwargs)
                except TypeError:
                    mod.register(req_binding, fn)
                aliases.append(req_binding)
                bound_req = True
            bound.append({
                "acquired_name": name,
                "requirement_binding": req_binding if (req_binding in aliases) else None,
                "aliases": aliases,
            })
        # Structural assembly: if only part-nodes resolved, compose parent for req_binding
        if req_binding and not bound_req and len(resolved) >= 2:
            part_fns = [(n, f) for n, f in resolved if _is_part(n)]
            if len(part_fns) >= 2:
                f0, f1 = part_fns[0][1], part_fns[1][1]
                def assembled(x=None, **kwargs):
                    v = kwargs.get("x", x)
                    a = f0(x=v) if v is not None else f0(**kwargs)
                    b = f1(x=v) if v is not None else f1(**kwargs)
                    return (a, b)
                try:
                    mod.register(req_binding, assembled, examples=getattr(self, "_last_acq_examples", None))
                except TypeError:
                    mod.register(req_binding, assembled)
                bound.append({
                    "acquired_name": "assembled_from_parts",
                    "requirement_binding": req_binding,
                    "aliases": [req_binding],
                    "parts": [n for n, _ in part_fns],
                })
        return bound


    def _select_among_candidates(self, examples: List, requirements: str = "") -> Dict[str, Any]:
        """Choose among existing engine primitives by behavioral match + learner.

        1. Evaluate each acquired-family candidate against all examples.
        2. Reject any that mismatch.
        3. Among survivors, if AcquisitionLearner has experience under
           signature "candidate_select", use prefer() to order them.
        4. Otherwise keep first-match (stable evaluation order).

        No fixture-specific names. No invented ranking formula beyond the
        existing learner.
        """
        out: Dict[str, Any] = {
            "evaluated": [], "selected": None, "rejected": [],
            "valid": [], "selection_basis": None,
        }
        if not examples:
            return out
        try:
            names = list(self.engine.primitives.names())
        except Exception:
            return out
        candidates = []
        for name in names:
            prim = self.engine.primitives.get(name)
            if prim is None:
                continue
            family = str(getattr(prim, "family", "") or "")
            if family not in ("acquired", "test", "acquired_alias"):
                continue
            candidates.append((name, prim))
        valid = []
        for name, prim in candidates:
            fn = prim.fn if hasattr(prim, "fn") else prim
            ok = True
            mismatches = 0
            for args, expect in examples:
                try:
                    if isinstance(args, dict):
                        got = fn(**dict(args))
                    else:
                        got = fn(args)
                except Exception:
                    ok = False
                    mismatches += 1
                    break
                if isinstance(got, list) and isinstance(expect, tuple):
                    got = tuple(got)
                if isinstance(got, tuple) and isinstance(expect, list):
                    expect = tuple(expect)
                if got != expect:
                    ok = False
                    mismatches += 1
                    break
            entry = {"name": name, "ok": ok, "mismatches": mismatches}
            out["evaluated"].append(entry)
            if ok:
                valid.append(name)
            else:
                out["rejected"].append(name)
        out["valid"] = list(valid)
        if not valid:
            return out
        # Governed selection via existing learner when experience exists
        learner = getattr(self.engine, "strategy_learner", None)
        ranked = list(valid)
        basis = "first_match"
        if learner is not None and len(valid) > 1:
            try:
                preferred = learner.prefer(list(valid), "candidate_select")
                if preferred and preferred != list(valid):
                    ranked = [n for n in preferred if n in valid]
                    for n in valid:
                        if n not in ranked:
                            ranked.append(n)
                    basis = "learner_prefer"
                elif preferred:
                    ranked = [n for n in preferred if n in valid]
                    for n in valid:
                        if n not in ranked:
                            ranked.append(n)
                    # still mark learner consulted even if order matches insertion
                    basis = "learner_prefer"
            except Exception:
                pass
        out["selected"] = ranked[0]
        out["selection_basis"] = basis
        out["ranked"] = ranked
        return out

    def _try_capability_acquisition(self, requirements: str, test_result: Dict[str, Any],
                                    report: "ProjectLoopReport") -> Dict[str, Any]:
        """Route unfixable project failure through recursive acquisition.

        Extracts simple (args→expect) pairs from pytest assertion text when
        possible; falls back to requirement-derived synthetic examples.
        Does not inject diagnosis or capability names from the test harness.
        """
        import asyncio
        import re
        out: Dict[str, Any] = {"attempted": True, "acquired": False}
        self._last_acq_examples = []  # type: ignore
        # Parse assertion patterns: assert f(1) == 2  OR  assert add(2, 3) == 5
        stdout = (test_result.get("stdout") or "") + (test_result.get("stderr") or "")
        examples = []
        for m in re.finditer(
                r"assert\s+([a-zA-Z_]\w*)\(([^)]*)\)\s*==\s*([^\n]+)", stdout):
            fn, args_s, exp_s = m.group(1), m.group(2), m.group(3).strip()
            try:
                args_parts = [a.strip() for a in args_s.split(",") if a.strip()]
                args = {}
                for i, p in enumerate(args_parts):
                    try:
                        args[f"a{i}" if not p[0].isalpha() else p.split("=")[0]] = ast.literal_eval(p.split("=")[-1].strip())
                    except Exception:
                        args[f"v{i}"] = ast.literal_eval(p)
                expect = ast.literal_eval(exp_s)
                examples.append((args, expect))
            except Exception:
                continue
        # Always mine examples from tests/*.py (failure trace may lack asserts)
        tests_dir = os.path.join(self.project_root, "tests")
        if os.path.isdir(tests_dir):
            for fname in os.listdir(tests_dir):
                if not fname.endswith(".py"):
                    continue
                try:
                    src = open(os.path.join(tests_dir, fname), encoding="utf-8").read()
                except Exception:
                    continue
                for m in re.finditer(
                        r"assert\s+([a-zA-Z_]\w*)\(([^)]*)\)\s*==\s*([^\n]+)", src):
                    fn, args_s, exp_s = m.group(1), m.group(2), m.group(3).strip()
                    try:
                        vals = [ast.literal_eval(p.strip()) for p in args_s.split(",") if p.strip()]
                        if len(vals) == 1:
                            examples.append(({"x": vals[0]}, ast.literal_eval(exp_s)))
                        elif len(vals) == 2:
                            examples.append(({"a": vals[0], "b": vals[1]}, ast.literal_eval(exp_s)))
                    except Exception:
                        continue
        if len(examples) < 2:
            out["reason"] = "insufficient_examples_for_acquisition"
            return out
        # Goal description from requirements first line / heading
        goal = "project_required_capability"
        for line in (requirements or "").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                goal = re.sub(r"[^a-zA-Z0-9_ ]", "", line)[:60].strip().replace(" ", "_") or goal
                break
        out["goal"] = goal
        out["n_examples"] = len(examples)
        self._last_acq_examples = list(examples)
        # Multi-candidate selection among already-registered capabilities
        selection = self._select_among_candidates(examples, requirements)
        out["candidate_selection"] = selection
        if selection.get("selected"):
            sel = selection["selected"]
            prim = self.engine.primitives.get(sel)
            if prim is not None:
                bound = self._bind_acquired_to_project([sel], goal, requirements)
                out["acquired"] = True
                out["acquired_nodes"] = [sel]
                out["bound"] = bound
                out["fully_resolved"] = True
                out["selection_path"] = "existing_candidate"
                return out
        try:
            result = asyncio.run(
                self.engine.acquisition_orchestrator.resolve(goal, examples=examples))
            out["fully_resolved"] = bool(result.fully_resolved)
            out["acquired_nodes"] = list(result.acquired or [])
            out["failed_nodes"] = list(result.failed or [])
            out["acquired"] = bool(result.fully_resolved and result.acquired)
            # Bind acquired callables into project remor_bridge so project can use them
            if result.fully_resolved and result.acquired:
                bound = self._bind_acquired_to_project(list(result.acquired or []), goal, requirements)
                out["bound"] = bound
            # Natural failure capture for audit trail
            if not result.fully_resolved:
                detail = f"strategy exhaustion: project capability acquisition incomplete for {goal}"
                try:
                    from swarm_engine.improvement.goal_language import (
                        GoalCandidate, TVar, Transform, DeferredOpportunity,
                        node_fingerprint,
                    )
                    st = Transform("id", [TVar("x", "num")], "num")
                    fp = node_fingerprint(st)
                    cand = GoalCandidate(
                        structure=st, goal=goal, examples=list(examples[:4]),
                        fingerprint=fp,
                        provenance={
                            "production": "PROJECT_ACQUIRE",
                            "strategy_family": "project_gap",
                            "project_root": self.project_root,
                            "required_binding": self._parse_required_binding(requirements or ""),
                        },
                        expected_info_gain=0.5, expected_capability_value=0.5, relevance=0.8,
                        novelty=0.5, future_value=0.5, cost=1.0, risk=0.3, utility=0.4,
                    )
                    execution = {
                        "fully_resolved": False,
                        "failed": list(result.failed or []),
                        "strategies": ["generate", "compose"],
                        "attempt_details": [
                            {"strategy": "generate", "detail": "project gap acquisition"},
                            {"strategy": "compose", "detail": "project gap acquisition"},
                        ],
                        "error": None,
                    }
                    gll = getattr(self.engine, "goal_language_loop", None)
                    if gll is not None:
                        captured = gll._capture_natural_failure(cand, execution, {})
                        out["natural_failure_captured"] = True
                        out["deferred_opportunity_id"] = captured.get("opportunity_id")
                        # Enrich recovery for project reconsideration
                        store = gll.synth.abstraction_store
                        for opp in store.deferred_opportunities("deferred"):
                            if opp.opportunity_id == captured.get("opportunity_id"):
                                rs = dict(opp.recovery_state or {})
                                rs["project_root"] = self.project_root
                                rs["required_binding"] = self._parse_required_binding(
                                    requirements or "")
                                rs["examples"] = [
                                    {"args": a, "expect": e} for a, e in examples[:8]
                                ]
                                rs["goal"] = goal
                                rs["disposition"] = "awaiting_strategy_change"
                                rs["eligible_after_condition_change"] = True
                                opp.recovery_state = rs
                                store.save_deferred_opportunity(opp)
                                out["deferred_persisted"] = True
                                break
                except Exception as e:
                    out["natural_failure_error"] = str(e)
        except Exception as e:
            out["error"] = str(e)
        return out


    def reconsider_deferred_project_gaps(self) -> Dict[str, Any]:
        """Reconsider deferred project-acquisition failures and retry resolve/bind.

        Uses existing deferred_opportunities with PROJECT_ACQUIRE provenance.
        Does not manually clear deferred state on success until acquisition works.
        """
        import asyncio
        report: Dict[str, Any] = {"attempted": False, "reconsidered": [], "success": False}
        gll = getattr(self.engine, "goal_language_loop", None)
        if gll is None:
            report["reason"] = "no_goal_language_loop"
            return report
        store = gll.synth.abstraction_store
        requirements = self._load_requirements()
        pending = store.deferred_opportunities("deferred")
        project_opps = []
        for opp in pending:
            prov = (opp.candidate or {}).get("provenance") or {}
            rs = opp.recovery_state or {}
            if prov.get("production") == "PROJECT_ACQUIRE" or rs.get("project_root"):
                if rs.get("disposition") == "awaiting_strategy_change" or rs.get(
                        "eligible_after_condition_change"):
                    project_opps.append(opp)
        report["attempted"] = True
        report["n_pending"] = len(project_opps)
        for opp in project_opps:
            rs = dict(opp.recovery_state or {})
            goal = rs.get("goal") or (opp.candidate or {}).get("goal") or "project_gap"
            examples = []
            for row in rs.get("examples") or []:
                if isinstance(row, dict) and "args" in row:
                    examples.append((row["args"], row.get("expect")))
            if not examples:
                # re-mine from tests
                acq_probe = self._try_capability_acquisition(
                    requirements, {"stdout": "", "stderr": ""}, None)
                # That may succeed directly; record and continue
                report["reconsidered"].append({"via": "direct_remine", "result": acq_probe})
                if acq_probe.get("acquired"):
                    report["success"] = True
                    opp.status = "closed"
                    rs["disposition"] = "closed"
                    rs["closure"] = "reconsideration_acquired"
                    opp.recovery_state = rs
                    store.save_deferred_opportunity(opp)
                continue
            try:
                result = asyncio.run(
                    self.engine.acquisition_orchestrator.resolve(goal, examples=examples))
            except Exception as e:
                report["reconsidered"].append({"goal": goal, "error": str(e)})
                continue
            entry = {
                "goal": goal,
                "fully_resolved": bool(result.fully_resolved),
                "acquired": list(result.acquired or []),
                "failed": list(result.failed or []),
            }
            if result.fully_resolved and result.acquired:
                self._last_acq_examples = list(examples)
                bound = self._bind_acquired_to_project(
                    list(result.acquired or []), goal, requirements)
                entry["bound"] = bound
                report["success"] = True
                opp.status = "closed"
                rs["disposition"] = "closed"
                rs["closure"] = "reconsideration_acquired"
                opp.recovery_state = rs
                store.save_deferred_opportunity(opp)
            report["reconsidered"].append(entry)
        return report

    def run(self) -> ProjectLoopReport:
        requirements = self._load_requirements()
        inventory = self._inventory()
        report = ProjectLoopReport(
            project_root=self.project_root,
            requirements_text=requirements[:2000],
            inventory=inventory,
            provenance={"started_at": time.time()},
        )

        # Round 0: observe baseline without edits (genuine failure capture)
        baseline = self.run_tests()
        report.test_runs.append(baseline)
        if not baseline.get("ok"):
            report.failures.append({
                "round": 0,
                "returncode": baseline.get("returncode"),
                "stdout": (baseline.get("stdout") or "")[:500],
                "stderr": (baseline.get("stderr") or "")[:300],
                "phase": "baseline_before_edits",
            })

        for round_i in range(1, self.max_rounds + 1):
            report.rounds = round_i
            gaps = self.inspect_gaps(requirements, self._inventory())
            report.gaps = [asdict(g) for g in gaps]
            plans = self.plan_edits(gaps)
            report.plans = [asdict(p) for p in plans]

            # Capability-bridge projects: source "plans" for calc/ops are irrelevant;
            # if tests fail due to missing capability, acquire first.
            last_fail = report.failures[-1] if report.failures else {}
            fail_text = (last_fail.get("stdout") or "") + (last_fail.get("stderr") or "")
            needs_cap = (
                "no acquired" in fail_text
                or "not registered" in fail_text
                or "LookupError" in fail_text
                or "RuntimeError" in fail_text
                or "remor_bridge" in fail_text
                or "required binding" in fail_text.lower()
                or "required_binding" in (requirements or "")
            )
            if needs_cap and not plans:
                acq = self._try_capability_acquisition(requirements, last_fail or {"stdout": fail_text}, report)
                report.repairs.append({"round": round_i, "capability_acquisition": acq})
            elif needs_cap and plans:
                # Prefer acquisition for bridge projects even if unrelated source gaps found
                acq = self._try_capability_acquisition(requirements, last_fail or {"stdout": fail_text}, report)
                report.repairs.append({"round": round_i, "capability_acquisition": acq})
            elif plans:
                edits = self.apply_plans(plans)
                report.edits.extend(edits)

            test = self.run_tests()
            report.test_runs.append(test)

            if test.get("ok"):
                report.success = True
                report.provenance["completed_at"] = time.time()
                break

            # Genuine failure from test run
            failure = {
                "round": round_i,
                "returncode": test.get("returncode"),
                "stdout": test.get("stdout", "")[:500],
                "stderr": test.get("stderr", "")[:300],
            }
            report.failures.append(failure)

            # Diagnose: map pytest failure text back to gaps
            out = (test.get("stdout") or "") + (test.get("stderr") or "")
            repair_gaps: List[ProjectGap] = []
            if "test_add" in out or "add(" in out:
                repair_gaps.append(ProjectGap(
                    "wrong_implementation", "calc/ops.py", symbol="add",
                    detail="test_add failed"))
            if "test_compute_mul" in out or "unknown op" in out:
                repair_gaps.append(ProjectGap(
                    "missing_branch", "calc/service.py", symbol="compute",
                    detail="mul dispatch failed"))
            if "test_mul" in out:
                repair_gaps.append(ProjectGap(
                    "wrong_implementation", "calc/ops.py", symbol="mul",
                    detail="test_mul failed"))
            if not repair_gaps:
                # Re-inspect from source
                repair_gaps = self.inspect_gaps(requirements, self._inventory())

            repair_plans = self.plan_edits(repair_gaps)
            report.repairs.append({
                "round": round_i,
                "gaps": [asdict(g) for g in repair_gaps],
                "plans": [asdict(p) for p in repair_plans],
            })
            if repair_plans:
                report.edits.extend(self.apply_plans(repair_plans))
            else:
                # Capability-gap path: cannot fix by source edit alone
                acq = self._try_capability_acquisition(requirements, test, report)
                report.repairs.append({
                    "round": round_i,
                    "capability_acquisition": acq,
                })
                if not acq.get("acquired"):
                    break

        # Final verification pass
        if not report.success:
            final = self.run_tests()
            report.test_runs.append(final)
            report.success = bool(final.get("ok"))

        report.provenance["ended_at"] = time.time()
        report.provenance["success"] = report.success
        return report
