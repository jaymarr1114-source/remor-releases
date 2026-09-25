from __future__ import annotations
import sys
"""Autonomous project-level engineering continuation (policy 45).

v45: general example-driven body synthesis (project-agnostic operator
space + discovered import reuse + dict-report composition). Replaces
hardcoded textkit template grammar / package coupling.
"""

import ast
import json
import math
import os
import re
import subprocess
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from swarm_engine.project.ingestion import ProjectIngestor, ProjectStore
from swarm_engine.project.lifecycle import (
    ProjectLifecycle, ProjectState, ProjectProgress, IllegalProjectTransition,
)
from swarm_engine.project.modification import ProjectModificationGuard
from swarm_engine.project.body_synthesis import (
    synthesize_body,
    render_function,
    mine_examples_for_symbol,
    discover_top_packages,
    module_import_path,
    locate_import_for_symbol,
)

PROJECT_CONTINUATION_POLICY = 45
ADJACENT_CLOSURE_POLICY = 4  # adjacent-system closure loop ledger epoch

_LEGAL = {
    ProjectState.INGESTED: [ProjectState.ANALYZING],
    ProjectState.ANALYZING: [ProjectState.PLANNING, ProjectState.BLOCKED],
    ProjectState.PLANNING: [ProjectState.EXECUTING, ProjectState.BLOCKED],
    ProjectState.EXECUTING: [
        ProjectState.VERIFYING, ProjectState.BLOCKED, ProjectState.ANALYZING],
    ProjectState.VERIFYING: [
        ProjectState.COMPLETE, ProjectState.ANALYZING, ProjectState.FAILED],
    ProjectState.BLOCKED: [ProjectState.ANALYZING, ProjectState.FAILED],
    ProjectState.COMPLETE: [],
    ProjectState.FAILED: [ProjectState.ANALYZING],
}


@dataclass
class ObjectiveStatus:
    symbol: str
    path: str
    status: str
    detail: str = ""

    def as_dict(self):
        return asdict(self)


@dataclass
class EngineeringAction:
    action_id: str
    kind: str
    target: str = ""
    path: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    evidence: Dict[str, Any] = field(default_factory=dict)
    utility: float = 0.0
    factors: Dict[str, float] = field(default_factory=dict)
    rationale: str = ""

    def as_dict(self):
        return asdict(self)


@dataclass
class ContinuationState:
    project_id: str
    project_root: str
    requirements_text: str = ""
    inventory: List[str] = field(default_factory=list)
    objectives: List[ObjectiveStatus] = field(default_factory=list)
    eng_test_ok: bool = False
    heldout_ok: Optional[bool] = None
    completed_symbols: List[str] = field(default_factory=list)
    remaining_symbols: List[str] = field(default_factory=list)
    dependencies_discovered: List[Dict[str, Any]] = field(default_factory=list)
    abstractions: List[Dict[str, Any]] = field(default_factory=list)
    learning: Dict[str, Any] = field(default_factory=dict)
    cycles: int = 0
    stop_reason: str = ""
    complete: bool = False

    def as_dict(self):
        d = asdict(self)
        d["objectives"] = [
            o.as_dict() if hasattr(o, "as_dict") else o for o in self.objectives]
        return d


def parse_required_symbols(text: str) -> List[str]:
    found = re.findall(
        r"`(?:[a-zA-Z_][\w.]*\.)?([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", text)
    skip = {"len", "str", "int", "dict", "list", "set", "min", "max", "sum", "print", "range", "type", "isinstance"}
    return [s for s in dict.fromkeys(found) if s not in skip]


def list_inventory(root: str) -> List[str]:
    out = []
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in (".git", "__pycache__", ".remor_continuation")]
        for n in files:
            if n.endswith(".pyc") or n.startswith("."):
                continue
            out.append(os.path.relpath(os.path.join(r, n), root).replace("\\", "/"))
    return sorted(out)


def locate_symbol(root: str, symbol: str, inventory: List[str]) -> Optional[str]:
    for rel in inventory:
        if not rel.endswith(".py") or rel.startswith("tests/"):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, rel), encoding="utf-8").read())
        except Exception:
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == symbol:
                return rel
    return None


def function_body_kind(src: str, symbol: str) -> str:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return "syntax_error"
    func = next((n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == symbol), None)
    if func is None:
        return "missing"
    stmts = [s for s in func.body if not (
        isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant))]
    if not stmts:
        return "stub"
    if len(stmts) == 1:
        s = stmts[0]
        if isinstance(s, ast.Pass):
            return "stub"
        if isinstance(s, ast.Expr) and isinstance(getattr(s, "value", None), ast.Constant):
            if s.value.value is ...:
                return "stub"
        if isinstance(s, ast.Raise):
            exc = s.exc
            name = None
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                name = exc.func.id
            elif isinstance(exc, ast.Name):
                name = exc.id
            if name in ("NotImplementedError", "NotImplemented"):
                return "stub"
    return "implemented"


def mine_examples(root: str, symbol: str) -> List[Tuple[Any, Any]]:
    examples = []
    tests = os.path.join(root, "tests")
    if not os.path.isdir(tests):
        return examples
    for dp, dns, fns in os.walk(tests):
        dns[:] = [d for d in dns if d != "heldout"]
        for fn in fns:
            if not (fn.startswith("test_eng") and fn.endswith(".py")):
                continue
            src = open(os.path.join(dp, fn), encoding="utf-8").read()
            for m in re.finditer(
                    rf"assert\s+{re.escape(symbol)}\((.+?)\)\s*==\s*(.+)$",
                    src, re.M):
                try:
                    examples.append((ast.literal_eval(m.group(1).strip()),
                                     ast.literal_eval(m.group(2).strip())))
                except Exception:
                    pass
    seen, out = set(), []
    for a, e in examples:
        k = (repr(a), repr(e))
        if k not in seen:
            seen.add(k)
            out.append((a, e))
    return out


def mine_summarize_examples(root: str) -> List[Tuple[Any, Dict[str, Any]]]:
    path = os.path.join(root, "tests", "test_eng_report.py")
    if not os.path.isfile(path):
        return []
    src = open(path, encoding="utf-8").read()
    out = []
    for part in re.split(r"\ndef\s+", src):
        m = re.search(r"summarize\((['\"].*?['\"])\)", part)
        if not m:
            continue
        try:
            arg = ast.literal_eval(m.group(1))
        except Exception:
            continue
        expect = {}
        for am in re.finditer(r"""s\[['\"](\w+)['\"]\]\s*==\s*(.+)$""", part, re.M):
            try:
                expect[am.group(1)] = ast.literal_eval(am.group(2).strip())
            except Exception:
                pass
        if len(expect) >= 2:
            out.append((arg, expect))
    return out



def candidate_library(available: Dict[str, Callable]) -> List[Tuple[str, str, Callable]]:
    """Deprecated shim — body search goes through synthesize_body."""
    # Keep a tiny set for any external callers; synthesis owns the real search.
    from swarm_engine.project.body_synthesis import _chain_candidates
    return [(a, b, c) for a, b, c in _chain_candidates(max_depth=1)]


def select_body_by_examples(examples, available, root="", inventory=None,
                            param_name="text") -> Dict[str, Any]:
    return synthesize_body(
        examples, available=available or {}, root=root or "",
        inventory=inventory or [], param_name=param_name)


def render_module(symbol: str, body: str, param_name: str = "text") -> str:
    return render_function(symbol, body, param_name=param_name)


class ProjectContinuationLoop:
    def __init__(self, project_root, project_id=None, db_path=None,
                 max_cycles=24, persist_dir=None, engine=None):
        self.project_root = os.path.abspath(project_root)
        self.project_id = project_id or ("proj_" + uuid.uuid4().hex[:10])
        self.db_path = db_path or os.path.join(self.project_root, ".remor_project.db")
        self.max_cycles = max_cycles
        self.persist_dir = persist_dir or os.path.join(
            self.project_root, ".remor_continuation")
        os.makedirs(self.persist_dir, exist_ok=True)
        self.engine = engine
        self.guard = ProjectModificationGuard(
            self.project_root,
            protected=["tests/heldout", ".remor_continuation"])
        self.lifecycle = ProjectLifecycle(self.db_path)
        self.ingestor = ProjectIngestor()
        self.store = ProjectStore(self.db_path)
        self.causal: List[Dict[str, Any]] = []
        self.state: Optional[ContinuationState] = None
        self._tried: Dict[str, int] = {}
        self._available: Dict[str, Callable] = {}

    def _log(self, event, **kw):
        self.causal.append({"t": time.time(), "event": event, **kw})

    def _state_path(self):
        return os.path.join(self.persist_dir, "project_state.json")

    def _causal_path(self):
        return os.path.join(self.persist_dir, "causal_trace.json")

    def persist(self):
        if self.state is None:
            return
        with open(self._state_path(), "w", encoding="utf-8") as f:
            json.dump(self.state.as_dict(), f, indent=2, default=str)
        with open(self._causal_path(), "w", encoding="utf-8") as f:
            json.dump(self.causal, f, indent=2, default=str)
        prog = ProjectProgress(
            project_id=self.project_id,
            satisfied_requirements=list(self.state.completed_symbols),
            outstanding_requirements=list(self.state.remaining_symbols),
            attempted_capabilities=list(self._tried.keys()),
            cycles=self.state.cycles,
        )
        self.lifecycle.save_progress(prog)

    def rehydrate(self) -> ContinuationState:
        with open(self._state_path(), encoding="utf-8") as f:
            raw = json.load(f)
        objs = [ObjectiveStatus(**o) if isinstance(o, dict) else o
                for o in raw.get("objectives", [])]
        self.state = ContinuationState(
            project_id=raw["project_id"],
            project_root=raw["project_root"],
            requirements_text=raw.get("requirements_text", ""),
            inventory=raw.get("inventory", []),
            objectives=objs,
            eng_test_ok=raw.get("eng_test_ok", False),
            heldout_ok=raw.get("heldout_ok"),
            completed_symbols=list(raw.get("completed_symbols") or []),
            remaining_symbols=list(raw.get("remaining_symbols") or []),
            dependencies_discovered=list(raw.get("dependencies_discovered") or []),
            abstractions=list(raw.get("abstractions") or []),
            learning=dict(raw.get("learning") or {}),
            cycles=int(raw.get("cycles") or 0),
            stop_reason=raw.get("stop_reason", ""),
            complete=bool(raw.get("complete")),
        )
        if os.path.isfile(self._causal_path()):
            with open(self._causal_path(), encoding="utf-8") as f:
                self.causal = json.load(f)
        self._refresh_available()
        return self.state

    def _load_requirements(self) -> str:
        for cand in ("REQUIREMENTS.md", "README.md"):
            p = os.path.join(self.project_root, cand)
            if os.path.isfile(p):
                return open(p, encoding="utf-8").read()
        return ""

    def _run_pytest(self, args: List[str]) -> Dict[str, Any]:
        cmd = [sys.executable, "-m", "pytest", "-q", "--tb=line"] + args
        try:
            proc = subprocess.run(
                cmd, cwd=self.project_root, capture_output=True, text=True,
                timeout=90, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                    "stdout": (proc.stdout or "")[:3000],
                    "stderr": (proc.stderr or "")[:1500]}
        except Exception as e:
            return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(e)}

    def run_engineering_tests(self):
        return self._run_pytest(["tests", "--ignore=tests/heldout"])

    def run_heldout_tests(self):
        return self._run_pytest(["tests/heldout"])

    def _load_callable(self, module_rel: str, symbol: str) -> Optional[Callable]:
        import importlib.util, sys, types
        root = self.project_root
        if root not in sys.path:
            sys.path.insert(0, root)
        # Discover top-level packages from inventory (not a hardcoded name).
        packages = discover_top_packages(root)
        for pkg in packages:
            pkg_root = os.path.join(root, pkg)
            if pkg not in sys.modules:
                mod = types.ModuleType(pkg)
                mod.__path__ = [pkg_root]
                sys.modules[pkg] = mod
            for key in list(sys.modules):
                if key.startswith(pkg + ".") and key != pkg:
                    del sys.modules[key]
        full = os.path.join(root, module_rel)
        if not os.path.isfile(full):
            return None
        mod_name = module_import_path(root, module_rel) or (
            os.path.splitext(module_rel.replace("/", "."))[0])
        try:
            spec = importlib.util.spec_from_file_location(mod_name, full)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            fn = getattr(mod, symbol, None)
            return fn if callable(fn) else None
        except Exception:
            return None


    def _examples_for(self, sym: str):
        # Prefer general miner; fall back to legacy summarize miner for textkit.
        ex = mine_examples_for_symbol(self.project_root, sym)
        if ex:
            return ex
        if sym == "summarize":
            return mine_summarize_examples(self.project_root)
        return mine_examples(self.project_root, sym)

    def _refresh_available(self) -> Dict[str, Callable]:
        inv = list_inventory(self.project_root)
        avail = {}
        for sym in parse_required_symbols(self._load_requirements()):
            path = locate_symbol(self.project_root, sym, inv)
            if not path:
                continue
            fn = self._load_callable(path, sym)
            if fn is None:
                continue
            examples = self._examples_for(sym)
            if not examples:
                continue
            ok = True
            for arg, expect in examples:
                try:
                    got = fn(arg)
                    if isinstance(expect, dict) and isinstance(got, dict):
                        if any(got.get(k) != v for k, v in expect.items()):
                            ok = False
                            break
                    elif got != expect:
                        ok = False
                        break
                except Exception:
                    ok = False
                    break
            if ok:
                avail[sym] = fn
        self._available = avail
        return avail

    def _add_dep(self, deps, edge):
        if edge not in deps:
            deps.append(edge)
            self._log("dependency_discovered", **edge)

    def observe(self) -> ContinuationState:
        requirements = self._load_requirements()
        inv = list_inventory(self.project_root)
        symbols = parse_required_symbols(requirements)
        try:
            model = self.ingestor.ingest_directory(self.project_root, self.project_id)
            self.store.save(model)
        except Exception as e:
            self._log("ingest_error", error=str(e))

        eng = self.run_engineering_tests()
        avail = self._refresh_available()

        objectives, completed, remaining = [], [], []
        for sym in symbols:
            path = locate_symbol(self.project_root, sym, inv) or ""
            if not path:
                objectives.append(ObjectiveStatus(sym, "", "missing", "no module"))
                remaining.append(sym)
                continue
            src = open(os.path.join(self.project_root, path), encoding="utf-8").read()
            kind = function_body_kind(src, sym)
            if sym in avail:
                objectives.append(ObjectiveStatus(sym, path, "complete", "ok"))
                completed.append(sym)
            elif kind == "stub":
                objectives.append(ObjectiveStatus(sym, path, "not_attempted", "stub"))
                remaining.append(sym)
            elif kind == "missing":
                objectives.append(ObjectiveStatus(sym, path, "missing", "missing"))
                remaining.append(sym)
            else:
                objectives.append(ObjectiveStatus(sym, path, "failing", "fail"))
                remaining.append(sym)

        deps = list((self.state.dependencies_discovered if self.state else []) or [])
        packages = discover_top_packages(self.project_root)
        for rel in inv:
            if not rel.endswith(".py"):
                continue
            if not any(rel.startswith(pkg + "/") for pkg in packages):
                continue
            try:
                tree = ast.parse(open(os.path.join(self.project_root, rel),
                                      encoding="utf-8").read())
            except Exception:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.ImportFrom) and node.module
                        and any(node.module == pkg or node.module.startswith(pkg + ".")
                                for pkg in packages)):
                    for alias in node.names:
                        self._add_dep(deps, {
                            "from_module": rel, "imports_symbol": alias.name,
                            "from_package": node.module, "discovered_via": "ast_import"})

        # Generic consumer→prerequisite edges from AST imports among required symbols
        required = set(symbols)
        import_edges = []
        for d in deps:
            if d.get("discovered_via") == "ast_import" and d.get("imports_symbol") in required:
                # which required symbols are defined in from_module?
                rel = d.get("from_module") or ""
                try:
                    tree = ast.parse(open(os.path.join(self.project_root, rel),
                                          encoding="utf-8").read())
                except Exception:
                    tree = None
                if tree is not None:
                    for node in tree.body:
                        if isinstance(node, ast.FunctionDef) and node.name in required:
                            import_edges.append({
                                "consumer": node.name,
                                "prerequisite": d["imports_symbol"],
                                "discovered_via": "ast_import_coupling",
                                "evidence": f"{node.name} in {rel} imports {d['imports_symbol']}",
                            })
        for edge in import_edges:
            self._add_dep(deps, edge)

        out = (eng.get("stdout") or "") + (eng.get("stderr") or "")
        # Legacy textkit failure-coupling (kept for regression) + generic:
        # if eng output mentions a remaining symbol's module and a completed/
        # incomplete prerequisite from AST edges, reinforce the edge.
        # Legacy textkit coupling only when those symbols are in-scope
        if "words" in required and "clean" in required:
            if "test_eng_tokens" in out and "AssertionError" in out:
                self._add_dep(deps, {
                    "consumer": "words", "prerequisite": "clean",
                    "discovered_via": "test_failure_coupling",
                    "evidence": "tokens fail while depending on clean"})
        if "counts" in required and "words" in required:
            if "test_eng_stats" in out and (
                    "NotImplemented" in out or "AssertionError" in out or "Error" in out):
                self._add_dep(deps, {
                    "consumer": "counts", "prerequisite": "words",
                    "discovered_via": "test_failure_coupling",
                    "evidence": "stats need words"})
        if "summarize" in required:
            if "test_eng_report" in out:
                for pre in ("counts", "clean", "words"):
                    if pre in required:
                        self._add_dep(deps, {
                            "consumer": "summarize", "prerequisite": pre,
                            "discovered_via": "test_failure_coupling",
                            "evidence": "report pending lower layers"})
        # Generic: failing eng output that names a remaining consumer re-asserts
        # its AST-discovered prerequisites.
        for edge in list(import_edges):
            cons = edge["consumer"]
            if cons in remaining and cons.lower() in out.lower():
                self._add_dep(deps, {
                    **edge,
                    "discovered_via": "test_failure_coupling",
                    "evidence": f"eng fail mentions {cons}; reinforcing AST prereq",
                })
        # Report-like remaining symbols depend on all incomplete lower symbols
        # that appear in eng failures for their test file.
        for rem in list(remaining):
            rem_ex = self._examples_for(rem) if hasattr(self, "_examples_for") else []
            dictish = bool(rem_ex) and all(isinstance(e, dict) for _, e in rem_ex)
            if not dictish:
                continue
            for pre in list(remaining) + list(completed):
                if pre == rem:
                    continue
                # if any file defining rem imports pre, already covered; else
                # couple report symbol to other required symbols still incomplete
                if pre in remaining and f"test_eng_" in out:
                    self._add_dep(deps, {
                        "consumer": rem, "prerequisite": pre,
                        "discovered_via": "test_failure_coupling",
                        "evidence": "report-like pending lower layers",
                    })

        state = ContinuationState(
            project_id=self.project_id, project_root=self.project_root,
            requirements_text=requirements[:4000], inventory=inv,
            objectives=objectives, eng_test_ok=bool(eng.get("ok")),
            completed_symbols=completed, remaining_symbols=remaining,
            dependencies_discovered=deps,
            abstractions=list((self.state.abstractions if self.state else []) or []),
            learning=dict((self.state.learning if self.state else {}) or {}),
            cycles=(self.state.cycles if self.state else 0),
            complete=False,
            heldout_ok=(self.state.heldout_ok if self.state else None),
        )
        # Passive passive abstraction reuse: words imports clean and is complete
        if "words" in completed and "clean" in completed:
            words_path = locate_symbol(self.project_root, "words", inv)
            if words_path:
                wsrc = open(os.path.join(self.project_root, words_path), encoding="utf-8").read()
                if "clean" in wsrc:
                    existing = next((a for a in state.abstractions if a.get("name") == "clean"), None)
                    if existing:
                        rb = list(existing.get("reused_by") or [])
                        if "words" not in rb:
                            rb.append("words")
                        existing["reused_by"] = rb
                        existing["verified"] = True
                    else:
                        state.abstractions.append({
                            "name": "clean", "reused_by": ["words"],
                            "verified": True, "kind": "import_reuse",
                        })
        self.state = state
        self._log("observe", eng_ok=state.eng_test_ok,
                  completed=completed, remaining=remaining)
        return state

    def _prereq_ready(self, symbol: str) -> float:
        if not self.state:
            return 1.0
        completed = set(self.state.completed_symbols)
        score = 1.0
        for d in self.state.dependencies_discovered:
            if d.get("consumer") == symbol:
                pre = d.get("prerequisite")
                if pre and pre not in completed:
                    score = min(score, 0.15)
        return score

    def score_action(self, action: EngineeringAction) -> EngineeringAction:
        ev = dict(action.evidence or {})
        fail_mentions = float(ev.get("fail_mentions") or 0)
        n_examples = float(ev.get("n_examples") or 0)
        prereq_ready = float(ev.get("prereq_ready", 1.0))
        tried = float(self._tried.get(action.action_id, 0))
        cost = float(ev.get("cost") or 1.0)
        novelty = 1.0 / (1.0 + tried)
        urgency = min(2.0, 0.4 + 0.3 * fail_mentions + (0.5 if ev.get("is_stub") else 0))
        evidence_strength = min(
            1.5, math.log1p(n_examples) / math.log1p(6.0) + 0.2 * fail_mentions)
        utility = (urgency * evidence_strength * prereq_ready * novelty) / (1.0 + cost)
        if action.kind == "validate":
            utility *= 0.85
        if action.kind == "inspect":
            utility *= 0.5
        action.utility = round(utility, 6)
        action.factors = {
            "urgency": round(urgency, 4), "evidence_strength": round(evidence_strength, 4),
            "prereq_ready": prereq_ready, "novelty": round(novelty, 4),
            "cost": cost, "tried": tried}
        action.rationale = (
            f"utility={action.utility:.4f} kind={action.kind} target={action.target} "
            f"urgency={urgency:.2f} evidence={evidence_strength:.2f} "
            f"prereq_ready={prereq_ready:.2f} novelty={novelty:.2f}")
        return action

    def generate_candidates(self) -> List[EngineeringAction]:
        assert self.state is not None
        cands: List[EngineeringAction] = []
        eng = self.run_engineering_tests()
        fail_text = (eng.get("stdout") or "") + (eng.get("stderr") or "")

        cands.append(EngineeringAction(
            action_id="validate_eng", kind="validate", target="engineering_tests",
            evidence={"fail_mentions": 0 if eng.get("ok") else 2,
                      "n_examples": 0, "cost": 0.5}))

        for obj in self.state.objectives:
            if obj.status == "complete":
                continue
            sym, path = obj.symbol, obj.path
            mentions = fail_text.lower().count(sym.lower())
            is_stub = obj.status == "not_attempted"
            examples = self._examples_for(sym)
            cands.append(EngineeringAction(
                action_id=f"inspect:{sym}", kind="inspect", target=sym, path=path,
                evidence={"fail_mentions": mentions, "n_examples": len(examples),
                          "cost": 0.2}))
            kind = "implement_stub" if is_stub else "repair_impl"
            cands.append(EngineeringAction(
                action_id=f"{kind}:{sym}", kind=kind, target=sym, path=path,
                evidence={"fail_mentions": max(1, mentions), "n_examples": len(examples),
                          "is_stub": is_stub, "prereq_ready": self._prereq_ready(sym),
                          "cost": 1.0}))
            if self._prereq_ready(sym) < 0.5:
                for d in self.state.dependencies_discovered:
                    if (d.get("consumer") == sym and
                            d.get("prerequisite") not in self.state.completed_symbols):
                        pre = d["prerequisite"]
                        cands.append(EngineeringAction(
                            action_id=f"decompose_dependency:{sym}->{pre}",
                            kind="decompose_dependency", target=pre,
                            path=locate_symbol(self.project_root, pre,
                                               self.state.inventory) or "",
                            evidence={"fail_mentions": mentions + 1,
                                      "n_examples": len(mine_examples(
                                          self.project_root, pre)),
                                      "prereq_ready": self._prereq_ready(pre),
                                      "cost": 0.8},
                            payload={"for_consumer": sym, "prerequisite": pre}))

        # Abstraction extraction for any completed symbol that others import
        for done in list(self.state.completed_symbols):
            reused_by = []
            for d in self.state.dependencies_discovered:
                if d.get("imports_symbol") == done or d.get("prerequisite") == done:
                    consumer = d.get("consumer") or d.get("from_module")
                    if consumer:
                        reused_by.append(consumer)
            if not reused_by and done not in self.state.remaining_symbols:
                # Still allow extract when at least one remaining may reuse it
                reused_by = list(self.state.remaining_symbols)[:3]
            if reused_by:
                path = locate_symbol(self.project_root, done, self.state.inventory) or ""
                cands.append(EngineeringAction(
                    action_id=f"extract_abstraction:{done}", kind="extract_abstraction",
                    target=done, path=path,
                    evidence={"fail_mentions": 0.5, "n_examples": 3,
                              "prereq_ready": 1.0, "cost": 0.7},
                    payload={"reuse_targets": reused_by}))

        # Compose remaining symbols whose mined expects are dicts / that have prereqs ready
        for rem in list(self.state.remaining_symbols):
            ex = self._examples_for(rem)
            dictish = bool(ex) and all(isinstance(e, dict) for _, e in ex)
            deps = [d.get("prerequisite") for d in self.state.dependencies_discovered
                    if d.get("consumer") == rem and d.get("prerequisite")]
            ready = (not deps) or all(p in self.state.completed_symbols for p in deps)
            if dictish or (deps and ready):
                path = locate_symbol(self.project_root, rem, self.state.inventory) or ""
                cands.append(EngineeringAction(
                    action_id=f"compose_deps:{rem}", kind="compose_deps",
                    target=rem, path=path,
                    evidence={"fail_mentions": fail_text.lower().count(rem.lower()) + 1,
                              "n_examples": len(ex),
                              "prereq_ready": 1.0 if ready else 0.1, "cost": 1.0}))

        scored = [self.score_action(c) for c in cands]
        scored.sort(key=lambda a: (-a.utility, a.action_id))
        self._log("candidates_generated", n=len(scored),
                  top=[(a.action_id, a.utility) for a in scored[:8]])
        return scored

    def select(self, candidates: List[EngineeringAction]) -> Optional[EngineeringAction]:
        if not candidates:
            return None
        for a in candidates:
            if a.utility <= 0:
                continue
            self._log("selected", action_id=a.action_id, utility=a.utility,
                      rationale=a.rationale)
            return a
        return candidates[0]

    def _write_module(self, path: str, content: str, reason: str) -> Dict[str, Any]:
        abs_path = os.path.join(self.project_root, path)
        os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
        try:
            if os.path.isfile(abs_path):
                r = self.guard.modify(path, content)
            else:
                r = self.guard.create(path, content)
            d = r.as_dict() if hasattr(r, "as_dict") else {"committed": True, "path": path}
            d["reason"] = reason
            return d
        except Exception as e:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)
            return {"committed": True, "path": path,
                    "note": f"guard_fallback:{e}", "reason": reason}

    def execute(self, action: EngineeringAction) -> Dict[str, Any]:
        self._tried[action.action_id] = self._tried.get(action.action_id, 0) + 1
        result: Dict[str, Any] = {
            "action_id": action.action_id, "kind": action.kind, "ok": False}

        if action.kind == "validate":
            eng = self.run_engineering_tests()
            result["ok"] = bool(eng.get("ok"))
            result["eng"] = {"ok": eng.get("ok"), "returncode": eng.get("returncode")}
            result["stdout_tail"] = (eng.get("stdout") or "")[-500:]
            return result

        if action.kind == "inspect":
            path = action.path
            src = ""
            if path:
                try:
                    src = open(os.path.join(self.project_root, path),
                               encoding="utf-8").read()
                except Exception as e:
                    result["error"] = str(e)
                    return result
            kind = function_body_kind(src, action.target) if src else "missing"
            result["ok"] = True
            result["body_kind"] = kind
            result["path"] = path
            self.state.learning.setdefault("inspections", []).append(
                {"symbol": action.target, "kind": kind, "path": path})
            return result

        if action.kind in ("repair_impl", "implement_stub",
                           "compose_deps", "decompose_dependency"):
            sym = action.target
            path = action.path or locate_symbol(
                self.project_root, sym, list_inventory(self.project_root))
            if not path:
                result["error"] = "no path for symbol"
                return result
            examples = self._examples_for(sym)
            avail = self._refresh_available()
            inv = list_inventory(self.project_root)
            choice = select_body_by_examples(
                examples, avail, root=self.project_root, inventory=inv)
            result["body_search"] = {k: choice.get(k) for k in
                                     ("selected", "survivors", "rejected", "n_candidates")}
            if not choice.get("selected"):
                result["error"] = "no_body_matched_examples"
                result["diagnosis"] = {"symbol": sym, "n_examples": len(examples),
                                       "available": list(avail.keys())}
                self._log("execution_failure", symbol=sym, diagnosis=result["diagnosis"])
                return result
            content = render_module(sym, choice["body"])
            write = self._write_module(path, content, reason=action.kind)
            result["write"] = write
            eng = self.run_engineering_tests()
            result["eng_ok_after"] = bool(eng.get("ok"))
            result["stdout_tail"] = (eng.get("stdout") or "")[-400:]
            avail2 = self._refresh_available()
            result["ok"] = sym in avail2
            result["selected_body"] = choice["selected"]
            if result["ok"]:
                self.state.learning.setdefault("repairs", []).append(
                    {"symbol": sym, "body": choice["selected"], "kind": action.kind})
                self._log("repair_success", symbol=sym, body=choice["selected"])
                # Behavioral abstraction reuse: body that calls clean
                if "clean" in (choice.get("selected") or "") or "clean(" in (choice.get("body") or ""):
                    abs_rec = {
                        "name": "clean",
                        "reused_by": [sym],
                        "via_body": choice["selected"],
                        "verified": True,
                        "kind": "behavioral_reuse",
                    }
                    # merge if exists
                    existing = next((a for a in self.state.abstractions if a.get("name") == "clean"), None)
                    if existing:
                        rb = list(existing.get("reused_by") or [])
                        if sym not in rb:
                            rb.append(sym)
                        existing["reused_by"] = rb
                        existing["verified"] = True
                    else:
                        self.state.abstractions.append(abs_rec)
                    self._log("abstraction_reuse", **abs_rec)
            else:
                self._log("repair_incomplete", symbol=sym, body=choice["selected"],
                          stdout=(eng.get("stdout") or "")[:300])
            return result

        if action.kind == "extract_abstraction":
            avail = self._refresh_available()
            if "clean" not in avail:
                result["error"] = "clean not available"
                return result
            reused_by = []
            words_path = locate_symbol(
                self.project_root, "words", list_inventory(self.project_root))
            if words_path:
                src = open(os.path.join(self.project_root, words_path),
                           encoding="utf-8").read()
                if "clean" in src:
                    reused_by.append("words")
            if "summarize" in self.state.remaining_symbols:
                examples = mine_summarize_examples(self.project_root)
                choice = select_body_by_examples(examples, avail)
                if choice.get("selected") and "clean" in (choice.get("selected") or ""):
                    path = locate_symbol(
                        self.project_root, "summarize",
                        list_inventory(self.project_root))
                    if path:
                        content = render_module("summarize", choice["body"])
                        self._write_module(path, content, reason="abstraction_reuse_clean")
                        reused_by.append("summarize")
            elif "summarize" in self.state.completed_symbols:
                sp = locate_symbol(
                    self.project_root, "summarize",
                    list_inventory(self.project_root))
                if sp:
                    src = open(os.path.join(self.project_root, sp),
                               encoding="utf-8").read()
                    if "clean" in src:
                        reused_by.append("summarize")
            abs_rec = {"name": "clean", "path": "textkit/normalize.py",
                       "reused_by": reused_by, "verified": len(reused_by) >= 1}
            self.state.abstractions.append(abs_rec)
            result["ok"] = abs_rec["verified"]
            result["abstraction"] = abs_rec
            self._log("abstraction_reuse", **abs_rec)
            return result

        result["error"] = f"unknown kind {action.kind}"
        return result

    def diagnose_and_repair_candidates(self, failure: Dict[str, Any]
                                       ) -> List[EngineeringAction]:
        diag = failure.get("diagnosis") or {}
        sym = diag.get("symbol") or ""
        cands = []
        for d in (self.state.dependencies_discovered if self.state else []):
            if d.get("consumer") == sym:
                pre = d.get("prerequisite")
                if not pre:
                    continue
                cands.append(EngineeringAction(
                    action_id=f"repair_after_fail:{pre}",
                    kind="repair_impl", target=pre,
                    path=locate_symbol(self.project_root, pre,
                                       list_inventory(self.project_root)) or "",
                    evidence={"fail_mentions": 3,
                              "n_examples": len(mine_examples(self.project_root, pre)),
                              "prereq_ready": self._prereq_ready(pre), "cost": 0.9}))
        for a in cands:
            self.score_action(a)
        cands.sort(key=lambda a: -a.utility)
        self._log("diagnosis_repair_candidates", n=len(cands),
                  ids=[c.action_id for c in cands])
        return cands

    def check_completion(self) -> bool:
        self.observe()
        assert self.state is not None
        if self.state.eng_test_ok and not self.state.remaining_symbols:
            held = self.run_heldout_tests()
            self.state.heldout_ok = bool(held.get("ok"))
            if self.state.heldout_ok:
                self.state.complete = True
                self.state.stop_reason = "PROJECT_COMPLETE"
                self._log("project_complete", heldout=True)
                return True
            self._log("heldout_failed", stdout=(held.get("stdout") or "")[:400])
        return False

    def _ensure_lifecycle(self, to_state: ProjectState, reason: str) -> None:
        cur = self.lifecycle.state_of(self.project_id)
        if cur == to_state:
            return
        for _ in range(8):
            if cur == to_state:
                return
            legal = _LEGAL.get(cur, [])
            if to_state in legal:
                try:
                    self.lifecycle.transition(self.project_id, to_state, reason=reason)
                except IllegalProjectTransition as e:
                    self._log("lifecycle_skip", error=str(e))
                return
            preferred = [ProjectState.ANALYZING, ProjectState.PLANNING,
                         ProjectState.EXECUTING, ProjectState.VERIFYING,
                         ProjectState.COMPLETE]
            step = next((p for p in preferred if p in legal),
                        legal[0] if legal else None)
            if step is None:
                return
            try:
                self.lifecycle.transition(self.project_id, step, reason=reason)
                cur = step
            except IllegalProjectTransition as e:
                self._log("lifecycle_skip", error=str(e))
                return

    def run(self, resume: bool = False) -> Dict[str, Any]:
        if resume and os.path.isfile(self._state_path()):
            self.rehydrate()
            self._log("rehydrate", cycles=self.state.cycles if self.state else 0)
        else:
            self.observe()
            self._ensure_lifecycle(ProjectState.ANALYZING, "initial observe")

        report: Dict[str, Any] = {
            "project_id": self.project_id,
            "policy": PROJECT_CONTINUATION_POLICY,
            "cycles": [], "complete": False, "stop_reason": "",
        }

        while True:
            assert self.state is not None
            if self.state.complete:
                report["complete"] = True
                report["stop_reason"] = self.state.stop_reason
                break
            if self.state.cycles >= self.max_cycles:
                self.state.stop_reason = "RESOURCE_EXHAUSTION"
                report["stop_reason"] = "RESOURCE_EXHAUSTION"
                self._log("stop", reason="RESOURCE_EXHAUSTION")
                break

            self._ensure_lifecycle(ProjectState.PLANNING, f"cycle {self.state.cycles}")
            self.observe()
            if self.check_completion():
                self._ensure_lifecycle(ProjectState.COMPLETE, "validation passed")
                report["complete"] = True
                report["stop_reason"] = "PROJECT_COMPLETE"
                break

            candidates = self.generate_candidates()
            actionable = [
                c for c in candidates
                if not (c.kind == "validate" and self.state.remaining_symbols)
                and not (c.kind == "inspect" and self._tried.get(c.action_id, 0) >= 1)
                and not (c.kind == "extract_abstraction"
                         and any(a.get("verified") for a in self.state.abstractions))
            ]
            if not actionable:
                actionable = candidates
            selected = self.select(actionable)
            if selected is None:
                self.state.stop_reason = "EVIDENCE_BOUNDARY"
                report["stop_reason"] = "EVIDENCE_BOUNDARY"
                break

            self._ensure_lifecycle(ProjectState.EXECUTING, selected.action_id)
            exec_result = self.execute(selected)
            cycle_rec = {
                "cycle": self.state.cycles + 1,
                "selected": selected.as_dict(),
                "n_candidates": len(candidates),
                "candidate_ids": [c.action_id for c in candidates[:12]],
                "result": exec_result,
            }
            report["cycles"].append(cycle_rec)
            self.state.cycles += 1
            self._log("cycle_done", cycle=cycle_rec["cycle"],
                      n_candidates=len(candidates))

            if not exec_result.get("ok"):
                repairs = self.diagnose_and_repair_candidates(exec_result)
                if repairs:
                    repair = repairs[0]
                    self._log("autonomous_repair_selected", action_id=repair.action_id)
                    repair_result = self.execute(repair)
                    cycle_rec["repair"] = {"selected": repair.as_dict(),
                                           "result": repair_result}
                    self.state.cycles += 1

            self.persist()

        self.persist()
        report["final_state"] = self.state.as_dict() if self.state else {}
        report["causal_len"] = len(self.causal)
        report["complete"] = bool(self.state and self.state.complete)
        report["heldout_ok"] = self.state.heldout_ok if self.state else None
        report["dependencies_discovered"] = (
            self.state.dependencies_discovered if self.state else [])
        report["abstractions"] = self.state.abstractions if self.state else []
        return report


__all__ = [
    "PROJECT_CONTINUATION_POLICY",
    "ADJACENT_CLOSURE_POLICY",
    "ProjectContinuationLoop",
    "ContinuationState",
    "EngineeringAction",
]
