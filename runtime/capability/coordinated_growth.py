"""
swarm_engine/capability/coordinated_growth.py

CoordinatedGrowthOrchestrator: extends single-artifact growth
(GenericCapabilityGrowthEngine) to a PROJECT of interdependent artifacts.

Honest scope: this is NOT general program synthesis. It does not decompose
an objective into files on its own — the caller supplies a
ProjectRequirement naming each file-level CapabilityRequirement and which
others it depends on (a real, structural fact a project's own file
references usually make explicit). What this adds:

  1. DEPENDENCY ORDERING via a real topological sort — not the order the
     caller happened to list requirements in.
  2. SYSTEM-LEVEL VALIDATION: after each file grows and is individually
     validated, check whether cross-file references actually resolve —
     via real ast parsing of the actual generated content, not a guess.
  3. HONEST FAILURE PROPAGATION: if a dependency fails, everything
     depending on it is marked blocked rather than attempted against
     something that doesn't exist.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.capability.growth_engine import GenericCapabilityGrowthEngine, GrowthResult
from swarm_engine.capability.requirement import CapabilityRequirement


@dataclass
class FileRequirement:
    key: str
    requirement: CapabilityRequirement
    depends_on: List[str] = field(default_factory=list)
    expects_symbols: List[str] = field(default_factory=list)


@dataclass
class ProjectRequirement:
    objective: str
    files: List[FileRequirement]


@dataclass
class CoordinatedGrowthReport:
    objective: str
    order: List[str] = field(default_factory=list)
    results: Dict[str, GrowthResult] = field(default_factory=dict)
    blocked: List[str] = field(default_factory=list)
    system_validation: Optional[Dict[str, Any]] = None
    succeeded: bool = False
    fully_objective_verified: bool = False
    has_unverified_components: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"objective": self.objective, "order": self.order,
                "results": {k: v.as_dict() for k, v in self.results.items()},
                "blocked": self.blocked,
                "system_validation": self.system_validation,
                "succeeded": self.succeeded,
                "fully_objective_verified": self.fully_objective_verified,
                "has_unverified_components": self.has_unverified_components}


class DependencyCycleError(Exception):
    pass


class CoordinatedGrowthOrchestrator:
    def __init__(self, growth_engine: GenericCapabilityGrowthEngine,
                project_root: str):
        self.growth = growth_engine
        self.project_root = project_root

    def _topological_order(self, files: List[FileRequirement]) -> List[str]:
        by_key = {f.key: f for f in files}
        visited: Dict[str, int] = {}
        order: List[str] = []

        def visit(key: str, stack: List[str]):
            if visited.get(key) == 1:
                return
            if visited.get(key) == 0:
                raise DependencyCycleError(
                    f"dependency cycle: {' -> '.join(stack + [key])}")
            visited[key] = 0
            for dep in by_key[key].depends_on:
                if dep not in by_key:
                    raise DependencyCycleError(
                        f"{key!r} depends on {dep!r}, which is not in this project")
                visit(dep, stack + [key])
            visited[key] = 1
            order.append(key)

        for f in files:
            visit(f.key, [])
        return order

    def grow(self, project: ProjectRequirement) -> CoordinatedGrowthReport:
        report = CoordinatedGrowthReport(objective=project.objective)
        by_key = {f.key: f for f in project.files}

        try:
            order = self._topological_order(project.files)
        except DependencyCycleError as exc:
            report.system_validation = {"error": str(exc)}
            return report
        report.order = order

        blocked: set = set()
        for key in order:
            f = by_key[key]
            unmet = [d for d in f.depends_on if d in blocked or
                    (d in report.results and not report.results[d].succeeded)]
            if unmet:
                blocked.add(key)
                report.blocked.append(key)
                continue
            if f.depends_on:
                # Generalized from the single-dependency case to N, via
                # positional correspondence for IMPORT rewriting. A real,
                # stated heuristic — not guaranteed correct if retrieved
                # content orders its imports differently than the project
                # declares its dependencies.
                dep_symbols, dep_examples = [], []
                for dep_key in f.depends_on:
                    dep_result = report.results.get(dep_key)
                    dep_file = by_key[dep_key].requirement.constraints.get(
                        "target_path", "")
                    file_stem = dep_file.rsplit(".", 1)[0] if dep_file else None
                    # The function name is NOT reliably the file's own stem
                    # — found directly: that assumption is only true for
                    # capabilities _materialize_internal_capability wrote
                    # itself (which does name the function after the file),
                    # and silently wrong for external-knowledge-route
                    # dependencies, whose actual function name is whatever
                    # the retrieved reference material happened to call it
                    # (e.g. doubler.py containing `def double_it(...)`, not
                    # a function named `doubler`). Parse the dependency's
                    # REAL admitted content when it exists, rather than
                    # guess from its filename.
                    real_symbol = file_stem
                    if dep_result is not None and dep_result.artifact_path:
                        dep_path = os.path.join(self.project_root, dep_result.artifact_path)
                        if os.path.exists(dep_path):
                            try:
                                with open(dep_path) as fh:
                                    dep_tree = ast.parse(fh.read())
                                defined_funcs = [n.name for n in ast.walk(dep_tree)
                                                if isinstance(n, (ast.FunctionDef,
                                                                 ast.AsyncFunctionDef))]
                                if len(defined_funcs) == 1:
                                    real_symbol = defined_funcs[0]
                                elif file_stem in defined_funcs:
                                    real_symbol = file_stem
                                # Ambiguous (0 or 2+ candidates, none
                                # matching the stem): fall back to the
                                # stem guess rather than pick arbitrarily.
                            except (SyntaxError, OSError):
                                pass
                    dep_symbols.append(real_symbol)
                    dep_ex = by_key[dep_key].requirement.examples
                    dep_examples.append(list(dep_ex[0][0].values()) if dep_ex else None)
                f.requirement.constraints["real_dependencies"] = list(f.depends_on)
                f.requirement.constraints["dependency_symbols"] = dep_symbols
                f.requirement.constraints["dependency_examples"] = dep_examples
                if len(f.depends_on) == 1:
                    # Kept for backward compatibility with any caller still
                    # reading the singular keys directly.
                    f.requirement.constraints["dependency_symbol"] = dep_symbols[0]
                    f.requirement.constraints["dependency_example"] = dep_examples[0]
            result = self.growth.grow(f.requirement)
            report.results[key] = result
            if not result.succeeded:
                blocked.add(key)
                report.blocked.append(key)

        report.system_validation = self._validate_system(project, report)
        # A project with ZERO files is not "done" — found directly: an
        # empty ProjectRequirement.files (e.g. from a decomposition that
        # matched no headings) produced report.blocked=[] and a trivially
        # {"valid": True, "checks": []} system_validation, so an empty
        # project reported succeeded=True having done nothing at all. This
        # is the same class of false-success the objective-level
        # validation fix targets, one level up: absence of failure is not
        # evidence of success.
        report.succeeded = (bool(project.files) and not report.blocked and
                            report.system_validation.get("valid", False))

        # Honest, per-file validation level, finalized here because only
        # the orchestrator knows whether a file actually has an oracle
        # (its own worked examples) AND whether the system-level execution
        # check ran successfully — growth_engine.py can only establish
        # STRUCTURALLY_VALID for the external-knowledge route on its own.
        exec_ok = report.system_validation.get("execution_valid")
        for f in project.files:
            result = report.results.get(f.key)
            if result is None or not result.succeeded:
                continue
            if result.validation_level == "objective_verified":
                continue  # already the strongest level, real oracle checked
            if exec_ok is True:
                result.validation_level = "executable"
            # Regardless of executing cleanly, a file with no oracle of
            # its own can never be upgraded to objective_verified here —
            # doing so would be exactly the "pretend semantics can be
            # inferred magically" this fix must not do.
            if not f.requirement.is_example_driven():
                result.validation_level = ("unverified" if exec_ok is not False
                                          else result.validation_level)

        report.fully_objective_verified = (
            report.succeeded and
            all(r.validation_level == "objective_verified"
               for r in report.results.values() if r.succeeded))
        report.has_unverified_components = any(
            r.validation_level == "unverified" for r in report.results.values())
        return report

    def _validate_system(self, project: ProjectRequirement,
                         report: CoordinatedGrowthReport) -> Dict[str, Any]:
        import ast
        import os

        checks: List[Dict[str, Any]] = []
        all_valid = True
        for f in project.files:
            if f.key not in report.results or not report.results[f.key].succeeded:
                continue
            if not f.depends_on:
                continue
            f_path = os.path.join(self.project_root, report.results[f.key].artifact_path)
            f_source = open(f_path).read() if os.path.exists(f_path) else ""
            for dep_key in f.depends_on:
                dep_result = report.results.get(dep_key)
                dep_module_name = None
                if dep_result is not None and dep_result.artifact_path:
                    dep_module_name = os.path.splitext(
                        os.path.basename(dep_result.artifact_path))[0]
                if dep_module_name and dep_module_name not in f_source:
                    # A dependent file that never even references its
                    # dependency's module name cannot possibly be using it
                    # correctly, regardless of whether the dependency
                    # itself is valid — found necessary directly: a file
                    # that received unrelated content (via a since-fixed
                    # queue matching bug) still passed every OTHER system
                    # check, because those checks only verified the
                    # dependency exports what's needed, never that the
                    # dependent actually imports or references it at all.
                    checks.append({"file": f.key, "depends_on": dep_key,
                                  "valid": False,
                                  "reason": f"{f.key}'s generated content "
                                           f"never references "
                                           f"{dep_module_name!r} at all — it "
                                           f"cannot be using this dependency "
                                           f"regardless of what the "
                                           f"dependency itself provides"})
                    all_valid = False

        for f in project.files:
            if f.key not in report.results or not report.results[f.key].succeeded:
                continue
            if not f.expects_symbols or not f.depends_on:
                continue
            for dep_key in f.depends_on:
                dep_result = report.results.get(dep_key)
                if dep_result is None or not dep_result.succeeded:
                    checks.append({"file": f.key, "depends_on": dep_key,
                                  "valid": False,
                                  "reason": "dependency artifact was never admitted"})
                    all_valid = False
                    continue
                dep_path = os.path.join(self.project_root, dep_result.artifact_path)
                if not os.path.exists(dep_path):
                    checks.append({"file": f.key, "depends_on": dep_key,
                                  "valid": False,
                                  "reason": f"artifact file missing on disk: {dep_path}"})
                    all_valid = False
                    continue
                with open(dep_path) as fh:
                    dep_source = fh.read()
                try:
                    dep_tree = ast.parse(dep_source)
                except SyntaxError as exc:
                    checks.append({"file": f.key, "depends_on": dep_key,
                                  "valid": False,
                                  "reason": f"dependency artifact has invalid "
                                           f"syntax: {exc}"})
                    all_valid = False
                    continue
                defined = {node.name for node in ast.walk(dep_tree)
                          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                              ast.ClassDef))}
                defined |= {t.id for node in ast.walk(dep_tree)
                           if isinstance(node, ast.Assign)
                           for t in node.targets if isinstance(t, ast.Name)}
                missing = [s for s in f.expects_symbols if s not in defined]
                if missing:
                    checks.append({"file": f.key, "depends_on": dep_key,
                                  "valid": False,
                                  "reason": f"expected symbol(s) {missing} not "
                                           f"found in {dep_key}'s actual "
                                           f"generated content — checked via "
                                           f"real ast parsing, not assumed"})
                    all_valid = False
                else:
                    checks.append({"file": f.key, "depends_on": dep_key,
                                  "valid": True,
                                  "resolved_symbols": f.expects_symbols})

        static_valid = all_valid
        execution_check = self._execution_check(project, report)
        if execution_check is not None:
            checks.append(execution_check)
            all_valid = all_valid and execution_check["valid"]

        return {"valid": all_valid, "static_checks_valid": static_valid,
               "execution_valid": (execution_check["valid"]
                                   if execution_check is not None else None),
               "checks": checks}

    def _execution_check(self, project: ProjectRequirement,
                         report: CoordinatedGrowthReport) -> Optional[Dict[str, Any]]:
        """A strictly stronger check than the static ast pass above: it
        does not just confirm a symbol is DEFINED, it actually imports the
        generated modules in a real sandboxed subprocess (reusing
        CommandRunner — no new execution path) and confirms importing
        every admitted Python-domain file raises nothing. Only runs when
        every file in the project is Python-domain — a mixed project (e.g.
        a Python script alongside a GDScript file this environment cannot
        execute) only gets the static check for the parts that can't
        actually run here, which is stated honestly rather than skipped
        silently."""
        import os

        python_files = [f for f in project.files
                        if f.requirement.target_domain in ("python", "bpy")]
        if len(python_files) != len(project.files) or not python_files:
            return None
        if any(f.key not in report.results or not report.results[f.key].succeeded
              for f in python_files):
            return None

        from swarm_engine.project.commands import CommandRunner
        runner = CommandRunner(self.project_root)
        import_lines = []
        for f in python_files:
            rel_path = report.results[f.key].artifact_path
            module_name = os.path.splitext(os.path.basename(rel_path))[0]
            import_lines.append(f"import {module_name}")
        script = "\n".join(import_lines) + "\nprint('IMPORT_OK')"

        script_path = os.path.join(self.project_root, "_system_validation_probe.py")
        with open(script_path, "w") as fh:
            fh.write(script)
        try:
            # The materialized wrapper files this validation is importing
            # (see growth_engine.py's _materialize_internal_capability)
            # genuinely need swarm_engine importable to run at all — found
            # directly, via this same execution check catching its own
            # dependency gap. Passed as an explicit env_override for this
            # call site specifically, not a change to CommandRunner's
            # general sandbox defaults, which stay minimal for every other
            # caller.
            import swarm_engine as _swarm_engine_pkg
            swarm_engine_parent = os.path.dirname(os.path.dirname(_swarm_engine_pkg.__file__))
            result = runner.run(["python3", "_system_validation_probe.py"],
                               env_overrides={"PYTHONPATH": swarm_engine_parent})
            ok = result.exit_code == 0 and "IMPORT_OK" in (result.stdout or "")
            return {"kind": "execution_check", "valid": ok,
                   "stdout": result.stdout, "stderr": result.stderr,
                   "reason": ("all generated modules import successfully in a "
                             "real sandboxed subprocess" if ok else
                             "importing the generated modules together raised "
                             "an error — a real runtime failure, not a static "
                             "guess")}
        except Exception as exc:
            return {"kind": "execution_check", "valid": False,
                   "reason": f"execution check itself failed: {exc}"}
        finally:
            if os.path.exists(script_path):
                os.remove(script_path)
