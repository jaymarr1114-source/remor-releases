"""
swarm_engine/acquisition/project_bridge.py

M+28.26 — bridges a multi-requirement project objective to the existing,
already-proven single-capability machinery (gap detection, acquisition,
independent validation, admission, persistence, reuse).

Nothing here decides WHAT a project needs — that is declared by the caller,
exactly the same way worked examples are legitimate evidence rather than an
answer injection. What SWarm does autonomously with that declaration is
real: checking each requirement against the actual capability inventory,
acquiring what is genuinely missing through the same synthesis machinery
proven in M+28.24/M+28.25, ordering execution from the requirements' own
declared dependency edges (a real topological sort, supporting fan-in —
multiple dependencies feeding one requirement — not just a linear chain),
wiring each capability's real output into the next capability's real
input using the RESOLVED capability's own declared param names (never
guessed by the caller, since the caller cannot know in advance which
capability will end up satisfying a requirement), executing the composed
plan for actual values, and verifying the result independently of
whatever the planner itself reports.

This deliberately does NOT extend the hardcoded _COMPOUND_SHAPES lookup
table in gap_reasoner.py — that table is a small, fixed set of
regex-matched pre-anticipated project patterns, exactly the kind of
hardcoded-workflow shortcut this milestone must not rely on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from swarm_engine.acquisition.gap_reasoner import CapabilityGapReasoner
from swarm_engine.acquisition.orchestrator import AcquisitionOrchestrator


# M+29.05: the "test"/"assert" markers match as whole tokens (word
# boundaries), so names like ".../contest/...", "latest_run.py" or
# "looptest" don't divert source files into test_paths.
_TEST_TOKEN_RE = re.compile(r"(?<![a-z0-9])test(?![a-z0-9])")
_ASSERT_TOKEN_RE = re.compile(r"(?<![a-z0-9])assert(?![a-z0-9])")


@dataclass
class ProjectRequirement:
    """One node in a project's requirement graph.

    depends_on declares WIRING TOPOLOGY (which other requirements' outputs
    feed this one, and in what order) — structurally the same kind of
    caller-declared fact as an example's own input values, not an
    operation or an answer. A requirement with len(depends_on) > 1 is a
    real fan-in node (A,B -> C): its dependencies' outputs are bound
    POSITIONALLY, in depends_on's own order, to the resolved capability's
    own declared parameter names — never to names the caller guessed.
    """
    name: str
    description: str
    # Existing persisted non-executable lexical semantic structure. It is
    # observability metadata only and is not used to select or run a capability.
    semantic_structure_id: str = ""
    depends_on: List[str] = field(default_factory=list)
    root_examples: List[Tuple[Dict[str, Any], Any]] = field(default_factory=list)
    capability_id: str = ""
    primitive_name: str = ""
    resolved_plan: Optional[Dict[str, Any]] = None
    validation_level: str = "unresolved"  # unresolved|reused|acquired|failed
    lifecycle_state: str = "UNRESOLVED"
    # Diagnostic records only: unresolved semantic evidence is separate from
    # capability identity, execution, and lifecycle state.
    semantic_evidence_gaps: List[Dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "semantic_structure_id": self.semantic_structure_id,
                "depends_on": self.depends_on,
                "capability_id": self.capability_id,
                "primitive_name": self.primitive_name,
                "validation_level": self.validation_level,
                "lifecycle_state": self.lifecycle_state,
                "semantic_evidence_gaps": list(self.semantic_evidence_gaps)}


@dataclass
class ProjectObjective:
    description: str
    requirements: List[ProjectRequirement]
    verify: Callable[[Dict[str, Any]], bool]  # receives ALL leaf/terminal
                                              # results keyed by requirement
                                              # name -- generic across
                                              # linear/fan-in/fan-out/mixed
    provenance: Dict[str, Any] = field(default_factory=dict)

    def leaf_names(self) -> List[str]:
        """Terminal requirements: those nothing else depends on. There can
        be more than one (fan-out, mixed graphs) -- this is derived from
        graph structure, never assumed to be a single 'last' node."""
        consumed = set()
        for r in self.requirements:
            consumed.update(r.depends_on)
        return [r.name for r in self.requirements if r.name not in consumed]

    def execution_order(self) -> List[ProjectRequirement]:
        """Real topological sort over depends_on edges, supporting fan-in
        (multiple dependencies per requirement) — generic graph ordering,
        not a project-specific sequence."""
        by_name = {r.name: r for r in self.requirements}
        visited: Dict[str, int] = {}
        order: List[ProjectRequirement] = []

        def visit(r: ProjectRequirement):
            state = visited.get(r.name, 0)
            if state == 2:
                return
            if state == 1:
                raise ValueError(f"dependency cycle at {r.name!r}")
            visited[r.name] = 1
            for dep_name in r.depends_on:
                dep = by_name.get(dep_name)
                if dep is None:
                    raise ValueError(f"{r.name!r} depends on unknown {dep_name!r}")
                visit(dep)
            visited[r.name] = 2
            order.append(r)

        for r in self.requirements:
            visit(r)
        return order

    def snapshot_names(self) -> List[str]:
        return [r.name for r in self.requirements]

    def insert_requirement_before(self, before_name: str, new_req: "ProjectRequirement") -> bool:
        """Graph mutation: insert a requirement before an existing node."""
        for i, r in enumerate(self.requirements):
            if r.name == before_name:
                if new_req.name not in r.depends_on:
                    r.depends_on = list(r.depends_on) + [new_req.name]
                self.requirements.insert(i, new_req)
                return True
        return False

    def graph_signature(self) -> List[tuple]:
        """Observable topology for before/after comparison."""
        return [(r.name, r.description[:80], list(r.depends_on), r.capability_id or r.primitive_name)
                for r in self.requirements]


@dataclass
class ProjectRunReport:
    objective: str
    resolved: List[Dict[str, Any]] = field(default_factory=list)
    execution_order: List[str] = field(default_factory=list)
    result: Any = None
    leaf_results: Dict[str, Any] = field(default_factory=dict)
    resolution_events: List[Dict[str, Any]] = field(default_factory=list)
    lifecycle_events: List[Dict[str, Any]] = field(default_factory=list)
    semantic_evidence_gaps: List[Dict[str, Any]] = field(default_factory=list)
    executed: bool = False
    verified: Optional[bool] = None
    error: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"objective": self.objective, "resolved": self.resolved,
                "execution_order": self.execution_order,
                "result": self.result, "leaf_results": self.leaf_results,
                                "resolution_events": self.resolution_events,
                "lifecycle_events": self.lifecycle_events,
                "semantic_evidence_gaps": self.semantic_evidence_gaps,
                "executed": self.executed, "verified": self.verified, "error": self.error,
                "provenance": dict(self.provenance or {})}



class ProjectExecutor:
    """Resolves every requirement (reuse-or-acquire), orders execution from
    the declared dependency edges, wires real outputs into real inputs,
    executes, and verifies independently. No project-specific branching."""

    def __init__(self, engine):
        self.engine = engine
        self.orchestrator = AcquisitionOrchestrator(engine)
        self.reasoner = CapabilityGapReasoner(
            engine.primitives, engine.planner, engine.composer,
            engine.provenance, engine.acquired_specs,
            capabilities=engine.capabilities)

    @staticmethod
    def _graph_fingerprint(project: ProjectObjective) -> str:
        edges = [(req.name, list(req.depends_on)) for req in project.requirements]
        raw = json.dumps(edges, sort_keys=True, separators=(",", ":"))
        return "graph_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def _artifact_state(self, req: ProjectRequirement, graph_fingerprint: str,
                        provisional_context: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        rec = self.engine.capabilities.get(req.capability_id) if req.capability_id else None
        if rec is not None:
            context = (provisional_context or {}).get(rec.capability_id)
            if context is None:
                context = self.engine.provenance.binding_fingerprint(
                    rec.capability_id, graph_fingerprint)
            return {"requirement": req.name, "capability_id": rec.capability_id,
                    "version": rec.version, "plan_fingerprint": rec.capability_id,
                    "primitive_name": "", "dependency_context_fingerprint": context}
        primitive = req.primitive_name or req.capability_id
        return {"requirement": req.name, "capability_id": "", "version": None,
                "plan_fingerprint": None, "primitive_name": primitive,
                "dependency_context_fingerprint": None}

    def _prerequisite_states(self, req: ProjectRequirement,
                             by_name: Dict[str, ProjectRequirement],
                             graph_fingerprint: str,
                             provisional_context: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        return [self._artifact_state(by_name[name], graph_fingerprint, provisional_context)
                for name in req.depends_on]

    def _lifecycle_decision(self, req: ProjectRequirement, graph_fingerprint: str,
                            by_name: Dict[str, ProjectRequirement],
                            provisional_context: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if not req.depends_on:
            return {"decision": "not_applicable"}
        rec = self.engine.capabilities.get(req.capability_id) if req.capability_id else None
        if rec is None:
            return {"decision": "not_persisted"}
        return self.engine.provenance.compare_prerequisites(
            rec.capability_id, graph_fingerprint,
            self._prerequisite_states(req, by_name, graph_fingerprint, provisional_context))

    def _remember_provisional_context(self, req: ProjectRequirement,
                                      by_name: Dict[str, ProjectRequirement],
                                      graph_fingerprint: str,
                                      provisional_context: Dict[str, str]) -> None:
        if not req.depends_on or not req.capability_id:
            return
        rec = self.engine.capabilities.get(req.capability_id)
        if rec is None:
            return
        prerequisites = self._prerequisite_states(
            req, by_name, graph_fingerprint, provisional_context)
        provisional_context[rec.capability_id] = (
            self.engine.provenance.prerequisite_context_fingerprint(
                rec.capability_id, graph_fingerprint, prerequisites))

    def _record_verified_bindings(self, project: ProjectObjective,
                                  graph_fingerprint: str,
                                  by_name: Dict[str, ProjectRequirement],
                                  behavioral_evidence: Dict[str, Dict[str, Any]],
                                  provisional_context: Dict[str, str]) -> None:
        for req in project.requirements:
            if not req.depends_on:
                continue
            rec = self.engine.capabilities.get(req.capability_id) if req.capability_id else None
            if rec is None:
                continue
            self.engine.provenance.record_prerequisites(
                rec.capability_id, graph_fingerprint,
                self._prerequisite_states(req, by_name, graph_fingerprint,
                                          provisional_context),
                validation_context={"project_verified": True,
                                    "requirement": req.name,
                                    "dependency_count": len(req.depends_on)},
                behavioral_evidence=behavioral_evidence.get(req.name, {}))



    def _infer_process_effect_binding(self, description: str):
        """Map process-required goals to run_command / run_python by effect family."""
        from swarm_engine.acquisition.intent import infer_required_effects
        from swarm_engine.primitives.core import Effect
        if "process" not in infer_required_effects(description):
            return None
        names = list(self.engine.primitives.names()) if hasattr(self.engine.primitives, "names") else []
        low = (description or "").lower()
        if ".py" in low and "run_python" in names:
            return "run_python"
        if "run_command" in names:
            return "run_command"
        for name in names:
            prim = self.engine.primitives.get(name)
            if prim is None:
                continue
            pe = tuple(getattr(prim, "effects", ()) or ())
            if Effect.PROCESS in pe or any(getattr(x, "value", x) == "process" for x in pe):
                return prim.name
        return None

    def _workspace_root(self) -> str:
        return str(getattr(self.engine, "projects_dir", None)
                   or "/tmp/swarm_projects")

    @staticmethod
    def _scoped_target(raw: str, root: str) -> str:
        """Normalize an execution target to a workspace-scoped path.

        The target may be a bare path or a command string containing a
        path (e.g. "python /tmp/swarm_projects/x.py"). Returns the longest
        path-like token contained in the workspace root, or "" when the
        target does not resolve inside the root. Callers must treat ""
        as refusal -- never fall back to a wildcard.
        """
        import re
        root = root.rstrip("/")
        best = ""
        for tok in re.findall(r"[^\s\"']+", raw):
            t = tok.rstrip(",;:")
            if t == root or t.startswith(root + "/"):
                if len(t) > len(best):
                    best = t
        return best

    def issue_workspace_grants(self, scope_note: str = "") -> None:
        """Deliberate, recorded, scoped grant decision (oracle binding).

        Called ONCE when the engine takes on project work (run start /
        resume) -- never per requirement, never from attacker-influenced
        bound args. Issues PROCESS and WRITE_FS scoped to the engine's
        project workspace root through the governed registry path:
        attributed to remor:engine, hash-chained, revocable. Never issues
        a wildcard. Idempotent: effects already covered by a live grant
        are skipped.
        """
        from swarm_engine.primitives.core import Effect
        root = self._workspace_root().rstrip("/") + "/*"
        note = f"project workspace [{scope_note}]" if scope_note \
            else "project workspace"
        for effect in (Effect.PROCESS, Effect.WRITE_FS):
            ok, _ = self.engine.governor.allows(effect, root)
            if ok:
                continue
            self.engine.governor.grant(effect, root, note=note)

    def _check_workspace_grant(self, effect, target: str) -> bool:
        """Check-only authorization: is the target contained in the
        workspace root and covered by a live governed grant? Never mints
        a grant -- refusal is conservative and final."""
        from swarm_engine.primitives.core import Effect  # noqa: F401
        if not target:
            return False
        norm = self._scoped_target(str(target), self._workspace_root())
        if not norm:
            return False
        ok, _ = self.engine.governor.allows(effect, norm)
        return ok

    def _authorize_workspace_process(self, target: str) -> bool:
        """Check-only now (oracle binding): the old implementation minted
        PROCESS grants -- including a "*" wildcard -- at call time from
        attacker-influenced bound args. Scoped grants are issued once by
        issue_workspace_grants(); this only verifies coverage."""
        from swarm_engine.primitives.core import Effect
        return self._check_workspace_grant(Effect.PROCESS, target)

    def _authorize_workspace_write(self, path: str) -> bool:
        """Check-only now (oracle binding): see _authorize_workspace_process."""
        from swarm_engine.primitives.core import Effect
        return self._check_workspace_grant(Effect.WRITE_FS, path)

    def _bind_process_args_from_description(self, description: str, bound: dict) -> dict:
        import re as _re
        out = {}
        for k, v in (bound or {}).items():
            if isinstance(v, (str, int, float, list)) or v is None:
                out[k] = v
        desc = description or ""
        if not isinstance(out.get("path"), str):
            out.pop("path", None)
            m = _re.search(r"(/[^\s]+\.py)", desc)
            if m:
                out["path"] = m.group(1)
        if not isinstance(out.get("command"), str):
            out.pop("command", None)
            if isinstance(out.get("path"), str):
                out["command"] = "python " + out["path"]
        if not isinstance(out.get("cwd"), str) and isinstance(out.get("path"), str):
            from pathlib import Path as _P
            out["cwd"] = str(_P(out["path"]).parent)
        return out

    def _infer_fs_effect_binding(self, description: str):
        """Map write_fs-required goals to registry primitives by effect family.

        Selection uses Effect.WRITE_FS membership and input shape, not
        task-specific natural-language branches. Returns primitive name or None.
        """
        from swarm_engine.acquisition.intent import infer_required_effects
        from swarm_engine.primitives.core import Effect
        effects = infer_required_effects(description)
        if "write_fs" not in effects:
            return None
        candidates = []
        names = list(self.engine.primitives.names()) if hasattr(self.engine.primitives, "names") else []
        for name in names:
            prim = self.engine.primitives.get(name)
            if prim is None:
                continue
            pe = tuple(getattr(prim, "effects", ()) or ())
            if Effect.WRITE_FS in pe or any(getattr(x, "value", x) == "write_fs" for x in pe):
                candidates.append(prim)
        if not candidates:
            return None
        low = (description or "").lower()
        dir_cues = ("directory", "mkdir", "make dir", "make_dir", "folder")
        wants_dir = any(c in low for c in dir_cues) and "file" not in low
        # Exclude destructive ops from create/write binding
        candidates = [p for p in candidates if p.name not in ("delete_file", "move_file")]
        if wants_dir:
            for prim in candidates:
                if prim.name == "make_dir":
                    return prim.name
            for prim in candidates:
                inputs = getattr(prim, "inputs", {}) or {}
                if "path" in inputs and "content" not in inputs:
                    return prim.name
        # Prefer canonical write_text over append_text for create/write-file.
        preferred = []
        for prim in candidates:
            inputs = getattr(prim, "inputs", {}) or {}
            if "path" in inputs and "content" in inputs:
                preferred.append(prim)
        if preferred:
            for prim in preferred:
                if prim.name == "write_text":
                    return prim.name
            return preferred[0].name
        for prim in candidates:
            inputs = getattr(prim, "inputs", {}) or {}
            if "path" in inputs:
                return prim.name
        return candidates[0].name

    def _bind_fs_args_from_description(self, description: str, bound: dict) -> dict:
        """Fill path/content from goal text when payload omitted keys."""
        from swarm_engine.acquisition.intent import (
            _extract_quoted_or_path, infer_required_effects,
        )
        import re as _re
        out = dict(bound or {})
        if "write_fs" not in infer_required_effects(description):
            return out
        desc = description or ""
        # Dependency outputs must not override explicit path/content in the goal
        if "path" in out and not isinstance(out.get("path"), str):
            out.pop("path", None)
        if "content" in out and not isinstance(out.get("content"), str):
            out.pop("content", None)
        if "path" not in out:
            m = _re.search(
                r"(?i)\b(?:create\s+file|create\s+directory|mkdir|write(?:\s+text)?(?:\s+file)?)\s+"
                r"(/[^\s]+)",
                desc,
            )
            if m:
                out["path"] = m.group(1).rstrip(".,;")
            if "path" not in out:
                m = _re.search(r"(?i)\bat\s+(/[^\s]+)", desc)
                if m:
                    out["path"] = m.group(1).rstrip(".,;")
            if "path" not in out:
                hits = _extract_quoted_or_path(desc)
                fileish = [
                    h for h in hits
                    if h.startswith("/") and "." in h.rstrip("/").split("/")[-1]
                ]
                if fileish and "containing" in desc.lower():
                    out["path"] = fileish[0]
                else:
                    for h in hits:
                        if h.startswith("/"):
                            out["path"] = h
                            break
        if "content" not in out:
            m = _re.search(r"(?i)prints?\s+['\"]([^'\"]+)['\"]", desc)
            if m:
                out["content"] = "print(%r)\n" % m.group(1)
            else:
                m2 = _re.search(r"(?i)\bcontaining\b\s+(.+)$", desc, _re.S)
                if m2:
                    body = m2.group(1)
                    s = body.strip()
                    # Unwrap only ONE pair of quotes around a short one-line
                    # string. Never touch triple-quoted or multi-line bodies.
                    triple = s.startswith('"""') or s.startswith("'''")
                    simple = (
                        len(s) >= 2 and s[0] == s[-1] and s[0] in "'\""
                        and not triple and "\n" not in s and s.count(s[0]) == 2
                    )
                    if simple:
                        body = s[1:-1]
                    else:
                        body = body.lstrip(" \t")
                    if body:
                        out["content"] = body if body.endswith("\n") else body + "\n"
        return out

    async def resolve_requirement(self, req: ProjectRequirement) -> None:
        # Effect-family binding: WRITE_FS → registry FS primitives (generic).
        fs_prim = self._infer_fs_effect_binding(req.description)
        if fs_prim:
            req.capability_id = fs_prim
            req.primitive_name = fs_prim
            req.validation_level = "reused"
            return
        proc_prim = self._infer_process_effect_binding(req.description)
        if proc_prim:
            req.capability_id = proc_prim
            req.primitive_name = proc_prim
            req.validation_level = "reused"
            return
        graph = self.reasoner.analyze(req.description)
        nodes = graph.acquisition_order()
        if not nodes or not any(n.is_gap for n in nodes):
            req.validation_level = "reused"
            # Satisfaction can come from three real, distinct places, and
            # each was found necessary by actually hitting it: the
            # capability store's goal binding (M+28.24-A's repair); the
            # primitive registry directly (a built-in, or a previously
            # acquired capability GENERATE registered as a real primitive
            # under its own node name — M+28.25's target capabilities land
            # here); or on-demand template composition (SatisfiedBy.
            # COMPOSITION), where nothing was ever persisted under any
            # name because the planner can just build the plan fresh each
            # time — that plan is reconstructed and executed directly.
            rec = self.engine.capabilities.resolve_goal(req.description)
            if rec is not None:
                req.capability_id = rec.capability_id
                return
            satisfied_node = next(
                (n for n in graph.nodes.values() if not n.is_gap), None)
            if satisfied_node is not None and satisfied_node.name in self.engine.primitives:
                req.capability_id = satisfied_node.name
                req.primitive_name = satisfied_node.name
                return
            proposal = self.engine.planner.best(req.description)
            if proposal is not None and self.engine.composer.analyze(proposal.plan).ok:
                # A template/composed plan is executable immediately, but it
                # must also become a governed persisted capability.  Otherwise
                # a raw-objective project can run only while its planner is
                # available, and a later selective-loss test has no concrete
                # artifact to inspect, revoke, or re-admit.  Admission is
                # generic over the resolved plan and requirement description;
                # it does not depend on topology, operation identity, or the
                # originating objective.
                verdict = self.engine.admit_as_engine(req.description, proposal.plan)
                if verdict.ok and verdict.capability_id:
                    req.capability_id = verdict.capability_id
                    req.validation_level = (
                        "reused" if verdict.verdict == "reused" else "admitted"
                    )
                    return
                # Preserve the previous executable fallback if governance
                # declines persistence for a reason unrelated to plan validity.
                req.resolved_plan = proposal.plan
                req.validation_level = "reused"
                return
            req.validation_level = "failed"
            return

        result = await self.orchestrator.resolve(
            req.description,
            examples_by_node={nodes[0].name: req.root_examples} if req.root_examples else {})
        req.semantic_evidence_gaps = list(getattr(result, "semantic_evidence_gaps", []) or [])
        accepted = [a for a in result.attempts if a.accepted]
        # Only retry with examples attached if the first call genuinely
        # needed them and didn't have them yet (attempts exist but none
        # accepted, and no examples were supplied the first time around).
        # Found directly: retrying unconditionally whenever examples exist
        # meant a first call that ALREADY succeeded (examples included
        # from the start) got a needless second call, which correctly
        # found the just-registered capability already satisfied — zero
        # attempts logged — and that empty list was then misread as
        # failure, rather than as the success it actually was.
        if not accepted and result.attempts and not req.root_examples:
            pass  # no examples available at all; nothing more to try
        node_names = [a.node for a in result.attempts]
        if not accepted and node_names and req.root_examples:
            result = await self.orchestrator.resolve(
                req.description, examples_by_node={node_names[0]: req.root_examples})
            accepted = [a for a in result.attempts if a.accepted]
        if not accepted:
            req.validation_level = "failed"
            return
        req.validation_level = "acquired"
        req.capability_id = accepted[-1].capability_id
        req.primitive_name = accepted[-1].node
        if not req.capability_id:
            rec = self.engine.capabilities.resolve_goal(req.description)
            req.capability_id = rec.capability_id if rec else ""

    def _resolved_param_names(self, req: ProjectRequirement) -> List[str]:
        if req.resolved_plan is not None:
            return list(req.resolved_plan.get("params", {}).keys())
        rec = self.engine.capabilities.get(req.capability_id)
        if rec is not None:
            return list(rec.plan.get("params", {}).keys())
        prim = self.engine.primitives.get(req.primitive_name or req.capability_id)
        if prim is not None:
            return list(prim.inputs.keys())
        return []

    def _resolved_param_types(self, req: ProjectRequirement) -> Dict[str, str]:
        if req.resolved_plan is not None:
            return dict(req.resolved_plan.get("params", {}))
        rec = self.engine.capabilities.get(req.capability_id)
        if rec is not None:
            return dict(rec.plan.get("params", {}))
        prim = self.engine.primitives.get(req.primitive_name or req.capability_id)
        if prim is not None:
            return {k: str(v) for k, v in prim.inputs.items()}
        return {}

    def _run_capability(self, req: ProjectRequirement, bound_args: Dict[str, Any]) -> Any:
        if req.resolved_plan is not None:
            out = self.engine.composer.execute_sync(req.resolved_plan, bound_args)
            return out.get("value")
        rec = self.engine.capabilities.get(req.capability_id)
        if rec is not None:
            out = self.engine.composer.execute_sync(rec.plan, bound_args)
            return out.get("value")
        prim = self.engine.primitives.get(req.primitive_name or req.capability_id)
        if prim is not None:
            from swarm_engine.primitives.core import ExecContext
            ctx = ExecContext(self.engine.governor, self.engine.primitives)
            path_target = (bound_args.get("path") or bound_args.get("destination")
                           or bound_args.get("command") or "")
            for eff in (getattr(prim, "effects", ()) or ()):
                # Normalize command strings to workspace-scoped paths so the
                # scoped workspace grant (not a wildcard) is what covers
                # PROCESS/WRITE_FS execution. Targets outside the workspace
                # fail closed here. Other effects keep their raw target.
                from swarm_engine.primitives.core import Effect as _Eff
                target = str(path_target)
                if eff in (_Eff.PROCESS, _Eff.WRITE_FS):
                    target = self._scoped_target(target,
                                                 self._workspace_root())
                try:
                    ctx.governor.check(eff, target, prim.name)
                except TypeError:
                    ctx.governor.check(eff, target)
            # Drop unexpected kwargs for strict primitives
            allowed = set(prim.inputs.keys())
            call_args = {k: v for k, v in bound_args.items() if k in allowed}
            if getattr(prim, "needs_ctx", False):
                return prim.fn(ctx, **call_args)
            return prim.fn(**call_args)
        raise RuntimeError(f"requirement {req.name!r} has no resolved plan or "
                           f"capability to execute")

    async def run(self, project: ProjectObjective,
                  root_inputs: Dict[str, Any]) -> ProjectRunReport:
        report = ProjectRunReport(objective=project.description)
        # Oracle binding: the deliberate, recorded, scoped grant decision
        # for this project run. Per-requirement authorization below is
        # check-only; nothing mints grants from bound args anymore.
        self.issue_workspace_grants(
            scope_note=f"run:{getattr(project, 'description', '')[:60]}")
        try:
            order = project.execution_order()
        except ValueError as exc:
            report.error = str(exc)
            return report
        report.execution_order = [r.name for r in order]
        by_name = {req.name: req for req in project.requirements}
        graph_fingerprint = self._graph_fingerprint(project)
        lifecycle: Dict[str, Dict[str, Any]] = {}
        behavioral_evidence: Dict[str, Dict[str, Any]] = {}
        # Current-run binding identities are deliberately provisional until
        # project.verify approves all terminal results.  They nevertheless
        # let later DAG nodes detect an upstream context change in this run.
        provisional_context: Dict[str, str] = {}

        for req in order:
            started = time.perf_counter()
            await self.resolve_requirement(req)
            if req.validation_level == "failed":
                req.lifecycle_state = "FAILED"
            elif req.validation_level == "acquired":
                req.lifecycle_state = "REACQUIRED"
            elif req.validation_level == "admitted":
                req.lifecycle_state = "RE_ADMITTED"
            else:
                req.lifecycle_state = "REUSED"
            decision = self._lifecycle_decision(
                req, graph_fingerprint, by_name, provisional_context)
            lifecycle[req.name] = decision
            if decision["decision"] in ("stale_graph_context", "stale_prerequisites", "invalid_provenance"):
                req.validation_level = "stale_dependency"
                req.lifecycle_state = "STALE_DEPENDENCY"
            report.resolved.append(req.as_dict())
            for semantic_gap in req.semantic_evidence_gaps:
                if semantic_gap not in report.semantic_evidence_gaps:
                    report.semantic_evidence_gaps.append(semantic_gap)
            event = {
                "requirement": req.name,
                "validation_level": req.validation_level,
                "lifecycle_state": req.lifecycle_state,
                "capability_id": req.capability_id,
                "primitive_name": req.primitive_name,
                "lifecycle_decision": decision["decision"],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            report.resolution_events.append(event)
            report.lifecycle_events.append({"phase": "resolution", **event})
            if req.validation_level == "failed":
                report.error = f"could not resolve requirement {req.name!r}"
                return report

        outputs: Dict[str, Any] = {}
        for req in order:
            # Resolution happens before execution so plans can be selected
            # generically.  A descendant's *nested* context, however, exists
            # only after its predecessors have actually run.  Recompare here
            # against provisional current-run identities before allowing reuse.
            if req.depends_on and req.lifecycle_state != "STALE_DEPENDENCY":
                runtime_decision = self._lifecycle_decision(
                    req, graph_fingerprint, by_name, provisional_context)
                if runtime_decision["decision"] in (
                        "stale_graph_context", "stale_prerequisites", "invalid_provenance"):
                    req.validation_level = "stale_dependency"
                    req.lifecycle_state = "STALE_DEPENDENCY"
                    lifecycle[req.name] = runtime_decision
                    for resolved in reversed(report.resolved):
                        if resolved.get("name") == req.name:
                            resolved["validation_level"] = req.validation_level
                            resolved["lifecycle_state"] = req.lifecycle_state
                            break
                    report.lifecycle_events.append({
                        "phase": "execution_dependency_check",
                        "requirement": req.name,
                        "validation_level": req.validation_level,
                        "lifecycle_state": req.lifecycle_state,
                        "capability_id": req.capability_id,
                        "primitive_name": req.primitive_name,
                        "lifecycle_decision": runtime_decision["decision"]})
            param_names = self._resolved_param_names(req)
            if not req.depends_on:
                root_value = root_inputs.get(req.name, {})
                if (isinstance(root_value, dict) and param_names
                        and set(root_value).issubset(set(param_names))):
                    bound_args = dict(root_value)
                else:
                    key = param_names[0] if param_names else req.name
                    bound_args = {key: root_value}
            else:
                param_types = self._resolved_param_types(req)
                if (len(param_types) == 1 and len(req.depends_on) > 1
                    and "list" in str(next(iter(param_types.values()))).lower()):
                    only_param = next(iter(param_types.keys()))
                    bound_args = {only_param: [outputs[d] for d in req.depends_on]}
                else:
                    bound_args = {}
                    for i, dep_name in enumerate(req.depends_on):
                        if i >= len(param_names):
                            break
                        bound_args[param_names[i]] = outputs[dep_name]

            # Effectful FS: bind path/content from description; workspace grant
            prim = self.engine.primitives.get(req.primitive_name or req.capability_id)
            from swarm_engine.primitives.core import Effect
            if prim is not None and (
                Effect.WRITE_FS in (getattr(prim, "effects", ()) or ())
                or any(getattr(x, "value", x) == "write_fs"
                       for x in (getattr(prim, "effects", ()) or ()))
            ):
                if not isinstance(bound_args, dict):
                    bound_args = {}
                if not req.depends_on:
                    root_value = root_inputs.get(req.name)
                    if isinstance(root_value, dict):
                        for k, v in root_value.items():
                            if k in ("path", "content", "encoding") and k not in bound_args:
                                bound_args[k] = v
                bound_args = self._bind_fs_args_from_description(
                    req.description, bound_args)
                path = bound_args.get("path")
                if path:
                    if not self._authorize_workspace_write(str(path)):
                        report.lifecycle_events.append({
                            "phase": "authorization_refused",
                            "requirement": req.name,
                            "reason": f"WRITE_FS on {path!r} is outside the "
                                      f"workspace or ungranted -- refusing"})
                        req.validation_level = "failed"
                        continue

            prim_p = self.engine.primitives.get(req.primitive_name or req.capability_id)
            from swarm_engine.primitives.core import Effect as _Effect
            if prim_p is not None and (
                _Effect.PROCESS in (getattr(prim_p, "effects", ()) or ())
                or any(getattr(x, "value", x) == "process"
                       for x in (getattr(prim_p, "effects", ()) or ()))
            ):
                if not isinstance(bound_args, dict):
                    bound_args = {}
                if not req.depends_on:
                    root_value = root_inputs.get(req.name)
                    if isinstance(root_value, dict):
                        for k, v in root_value.items():
                            if k in ("path", "command", "cwd", "args", "timeout") and k not in bound_args:
                                bound_args[k] = v
                bound_args = self._bind_process_args_from_description(
                    req.description, bound_args)
                target = bound_args.get("path") or bound_args.get("command") or ""
                if target:
                    if not self._authorize_workspace_process(str(target)):
                        report.lifecycle_events.append({
                            "phase": "authorization_refused",
                            "requirement": req.name,
                            "reason": f"PROCESS on {str(target)[:120]!r} is "
                                      f"outside the workspace or ungranted "
                                      f"-- refusing"})
                        req.validation_level = "failed"
                        continue

            if req.lifecycle_state == "STALE_DEPENDENCY":
                stale_id = req.capability_id
                started = time.perf_counter()
                try:
                    value = self._run_capability(req, bound_args)
                    compatible = value is not None
                    failure = "" if compatible else "revalidation produced null output"
                except Exception as exc:
                    value, compatible, failure = None, False, str(exc)
                if compatible:
                    req.validation_level = "reused"
                    req.lifecycle_state = "BEHAVIORALLY_REVALIDATED"
                    outputs[req.name] = value
                    behavioral_evidence[req.name] = {
                        "state": "behaviorally_revalidated",
                        "current_upstream_execution": True,
                        "result_type": type(value).__name__}
                    self._remember_provisional_context(
                        req, by_name, graph_fingerprint, provisional_context)
                    report.lifecycle_events.append({
                        "phase": "revalidation", "requirement": req.name,
                        "lifecycle_state": "BEHAVIORALLY_REVALIDATED",
                        "capability_id": req.capability_id,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
                    continue

                # A child that fails against current predecessors may not remain
                # an exact-goal winner.  Quarantine only this artifact and try
                # the existing generic resolver once; never reactivate an
                # identical plan under the appearance of recovery.
                self.engine.capabilities.set_status(stale_id, "quarantined")
                self.engine.capabilities.unbind_goal_if(req.description, stale_id)
                report.lifecycle_events.append({
                    "phase": "invalidation", "requirement": req.name,
                    "lifecycle_state": "INVALIDATED", "capability_id": stale_id,
                    "reason": failure})
                req.capability_id = ""
                req.primitive_name = ""
                req.resolved_plan = None
                await self.resolve_requirement(req)
                if req.validation_level == "failed" or not req.capability_id:
                    report.error = f"incompatible stale capability at {req.name!r}: {failure}"
                    return report
                if req.capability_id == stale_id:
                    self.engine.capabilities.set_status(stale_id, "quarantined")
                    self.engine.capabilities.unbind_goal_if(req.description, stale_id)
                    report.error = (f"no distinct validated replacement for stale capability "
                                    f"at {req.name!r}: {failure}")
                    return report
                try:
                    value = self._run_capability(req, bound_args)
                    if value is None:
                        raise RuntimeError("replacement produced null output")
                except Exception as exc:
                    self.engine.capabilities.set_status(req.capability_id, "quarantined")
                    self.engine.capabilities.unbind_goal_if(req.description, req.capability_id)
                    report.error = f"replacement failed at {req.name!r}: {exc}"
                    return report
                req.lifecycle_state = "REACQUIRED"
                req.validation_level = "acquired"
                outputs[req.name] = value
                behavioral_evidence[req.name] = {"state": "reacquired",
                                                   "current_upstream_execution": True,
                                                   "result_type": type(value).__name__}
                self._remember_provisional_context(
                    req, by_name, graph_fingerprint, provisional_context)
                report.lifecycle_events.append({
                    "phase": "reacquisition", "requirement": req.name,
                    "lifecycle_state": "REACQUIRED", "capability_id": req.capability_id,
                    "replaced_capability_id": stale_id})
                continue

            try:
                outputs[req.name] = self._run_capability(req, bound_args)
                self._remember_provisional_context(
                    req, by_name, graph_fingerprint, provisional_context)
            except Exception as exc:
                report.error = f"execution failed at {req.name!r}: {exc}"
                return report

        leaves = project.leaf_names()
        report.leaf_results = {name: outputs[name] for name in leaves}
        report.executed = True
        if len(leaves) == 1:
            report.result = outputs[leaves[0]]
        try:
            report.verified = bool(project.verify(report.leaf_results))
        except Exception as exc:
            report.error = f"verification raised: {exc}"
            report.verified = False

        # M+29.01: graph-changing REPLAN on process/test failure (one pass)
        if not report.verified and not report.provenance.get("replan_attempted"):
            replan = await self._attempt_graph_replan(
                project, order, outputs, report, graph_fingerprint, by_name,
                provisional_context, behavioral_evidence, root_inputs)
            if replan is not None:
                return replan

        if report.verified:
            self._record_verified_bindings(project, graph_fingerprint, by_name,
                                           behavioral_evidence, provisional_context)
            for req in order:
                if req.depends_on:
                    report.lifecycle_events.append({
                        "phase": "binding", "requirement": req.name,
                        "lifecycle_state": req.lifecycle_state,
                        "graph_fingerprint": graph_fingerprint,
                        "prerequisite_count": len(req.depends_on)})
        return report

    def _process_failures(self, outputs: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
        fails = []
        for name, value in outputs.items():
            if isinstance(value, dict) and value.get("ok") is False:
                fails.append((name, value))
        return fails

    @staticmethod
    def _verifier_rejection_detail(report) -> str:
        """Build the diagnoser input for a verifier-channel rejection.

        The independent verifier is a caller-supplied ``project.verify``
        callable returning bool, so its "reasons" are whatever it
        communicated: a False verdict over the leaf results, or an
        exception captured in report.error. Both are folded into the
        detail string so the diagnoser -- and the recorded diagnosis
        event's detail -- consume the upstream output rather than a
        placeholder.
        """
        parts = [
            "independent verifier rejected the project result while all "
            "processes were green: project.verify(leaf_results) did not "
            "accept the terminal outputs",
        ]
        try:
            summary = {}
            for name, value in (report.leaf_results or {}).items():
                try:
                    blob = json.dumps(value, sort_keys=True, default=str)
                except Exception:
                    blob = str(value)
                summary[str(name)] = blob[:400]
            parts.append("leaf_results=" + json.dumps(summary, sort_keys=True)[:1500])
        except Exception:
            pass
        if getattr(report, "error", ""):
            parts.append("verifier error: " + str(report.error)[:500])
        return "; ".join(parts)

    def _find_repair_capability(self, path: str):
        """Select an active capability whose plan targets this path (generic)."""
        store = getattr(self.engine, "capabilities", None)
        if store is None:
            return None
        path = str(path)
        for rec in store.list(status="active", limit=200):
            blob = json.dumps(rec.plan, default=str) + " " + (rec.goal or "")
            if path in blob and "write" in blob.lower():
                return rec
        return None

    def _extract_path_from_req(self, req: "ProjectRequirement") -> str:
        import re as _re
        m = _re.search(r"(/[^\s]+\.py)", req.description or "")
        return m.group(1) if m else ""

    async def _attempt_graph_replan(
            self, project, order, outputs, report, graph_fingerprint, by_name,
            provisional_context, behavioral_evidence, root_inputs):
        """Diagnose process failure, mutate project graph, re-execute once.

        D3 repair: when no process failed but the independent verifier
        rejected the result (report.verified is False with all-green
        outputs), the verifier channel routes into diagnosis on the
        verifier's own verdict/reasons and continues down the same bounded
        replan path instead of returning None silently. The process-failure
        path below is behavior-identical; only the verifier-rejection case
        gains the diagnosis -> replan -> rerun chain.
        """
        from swarm_engine.improvement.loop import RecoveryAction
        fails = self._process_failures(outputs)
        verifier_channel = False
        if not fails:
            # Verifier channel (D3): every process is green, so the failure
            # signal is the independent verifier's rejection alone
            # (project.verify returned False or raised). Fail-closed is not
            # enough -- it must enter diagnosis like a process failure.
            if report.verified is not False:
                return None
            # Bounded to one verifier-channel replan per project: the rerun
            # below builds a fresh report (no replan_attempted flag), so
            # without this project-level marker a deterministically
            # rejecting verifier would recurse without bound. The
            # process-failure path never sets or reads this marker.
            if (project.provenance or {}).get("verifier_replan_done"):
                return None
            verifier_channel = True
            # The verifier judges the terminal results: anchor the repair on
            # the last leaf requirement (mirrors fails[-1] on the process
            # path).
            leaves = project.leaf_names()
            if leaves:
                failed_name = leaves[-1]
            elif order:
                failed_name = order[-1].name
            else:
                failed_name = ""
            err = self._verifier_rejection_detail(report)
        else:
            failed_name, fail_val = fails[-1]
            err = fail_val.get("stderr") or fail_val.get("stdout") or "process failed"
        diagnoser = getattr(self.engine, "diagnoser", None)
        recovery = getattr(self.engine, "recovery", None)
        if diagnoser is None:
            return None
        diagnosis = diagnoser.diagnose(err)
        diagnosis_event = {
            "phase": "diagnosis",
            "requirement": failed_name,
            "kind": getattr(diagnosis.kind, "value", str(diagnosis.kind)),
            "action": getattr(diagnosis.action, "value", str(diagnosis.action)),
            "detail": (diagnosis.detail or "")[:300],
        }
        if verifier_channel:
            # Label the channel: this diagnosis came from the independent
            # verifier's rejection, not from a failed process.
            diagnosis_event["channel"] = "verifier"
        report.lifecycle_events.append(diagnosis_event)
        action = diagnosis.action
        action_val = getattr(action, "value", str(action))
        if action_val not in ("replan", "substitute", "resynthesize"):
            return None

        # Call RecoveryEngine for provenance (may fail; mutation still proceeds)
        if recovery is not None:
            try:
                ok, val, attempts = await recovery.recover(
                    next(r.description for r in order if r.name == failed_name),
                    {}, err)
                report.lifecycle_events.append({
                    "phase": "recovery_engine",
                    "ok": ok,
                    "attempts": [getattr(a, "action", a).value if hasattr(getattr(a, "action", None), "value")
                                 else str(getattr(a, "action", a)) for a in (attempts or [])],
                })
            except Exception as exc:
                report.lifecycle_events.append({
                    "phase": "recovery_engine", "error": str(exc)})

        graph_before = project.graph_signature() if hasattr(project, "graph_signature") else []
        report.provenance = dict(report.provenance or {})
        report.provenance["graph_before"] = graph_before
        report.provenance["replan_attempted"] = True

        # Find upstream .py write requirements to repair
        repair_rec = None
        repair_path = ""
        source_req = None
        for req in order:
            if (req.primitive_name == "write_text" or req.capability_id == "write_text"):
                path = self._extract_path_from_req(req)
                if path and path.endswith(".py") and "test" not in Path_name(path):
                    rec = self._find_repair_capability(path)
                    if rec is not None:
                        repair_rec, repair_path, source_req = rec, path, req
                        break
        if repair_rec is None:
            # Any .py write upstream
            for req in order:
                if req.primitive_name == "write_text" or req.capability_id == "write_text":
                    path = self._extract_path_from_req(req)
                    if path and path.endswith(".py"):
                        rec = self._find_repair_capability(path)
                        if rec is not None:
                            repair_rec, repair_path, source_req = rec, path, req
                            break
        synthesis_provenance = None
        content = None
        repair_cap_id = ""

        if repair_rec is not None and source_req is not None:
            content = _content_from_plan(repair_rec.plan)
            repair_cap_id = repair_rec.capability_id
            if not content:
                report.lifecycle_events.append({
                    "phase": "replan_aborted", "reason": "repair_capability_has_no_content"})
                return None
        else:
            # M+29.03: no pre-registered repair → example-driven synthesis
            synth = self._synthesize_repair_candidate(project, order, outputs, failed_name)
            if synth is None:
                report.lifecycle_events.append({
                    "phase": "replan_aborted",
                    "reason": "no_repair_capability_and_synthesis_failed"})
                return None
            content = synth.content
            repair_path = synth.path
            synthesis_provenance = synth.as_dict()
            report.lifecycle_events.append({
                "phase": "repair_synthesis",
                "path": synth.path,
                "expression": synth.expression,
                "examples_satisfied": synth.examples_satisfied,
                "provenance": synth.provenance,
            })
            # Independent validation: examples already satisfied by synthesizer;
            # admit as capability via existing store (governed write plan).
            admitted = self._admit_synthesized_repair(synth)
            report.lifecycle_events.append({
                "phase": "repair_admission",
                "ok": admitted.get("ok"),
                "capability_id": admitted.get("capability_id"),
                "reasons": admitted.get("reasons"),
            })
            if not admitted.get("ok"):
                report.lifecycle_events.append({
                    "phase": "replan_aborted",
                    "reason": "synthesized_repair_not_admitted"})
                return None
            repair_cap_id = admitted.get("capability_id") or ""
            # Ensure source_req points at the repaired file's original write
            if source_req is None:
                for req in order:
                    if (req.primitive_name == "write_text" or req.capability_id == "write_text"):
                        path = self._extract_path_from_req(req)
                        if path == repair_path:
                            source_req = req
                            break
            if source_req is None:
                # Create synthetic dependency anchor on failed node
                source_req = next(r for r in order if r.name == failed_name)

        repair_name = f"repair_{source_req.name}"
        repair_req = ProjectRequirement(
            name=repair_name,
            description=f"Create file {repair_path} containing {content.strip()}",
            depends_on=list(source_req.depends_on) if source_req.name != failed_name else [],
            capability_id="write_text",
            primitive_name="write_text",
            validation_level="acquired" if synthesis_provenance else "reused",
            lifecycle_state="REPLAN_INSERTED",
        )
        if synthesis_provenance:
            report.provenance = dict(report.provenance or {})
            report.provenance["synthesized_repair"] = synthesis_provenance
            report.provenance["repair_capability_id"] = repair_cap_id
        if not project.insert_requirement_before(failed_name, repair_req):
            # Fallback: append before end
            project.requirements.insert(len(project.requirements) - 1, repair_req)
            failed_req = next(r for r in project.requirements if r.name == failed_name)
            if repair_name not in failed_req.depends_on:
                failed_req.depends_on = list(failed_req.depends_on) + [repair_name]

        graph_after = project.graph_signature()
        report.provenance["graph_after"] = graph_after
        report.lifecycle_events.append({
            "phase": "graph_mutation",
            "action": action_val,
            "inserted": repair_name,
            "repair_capability_id": repair_cap_id,
            "repair_path": repair_path,
            "graph_before_names": [g[0] for g in graph_before],
            "graph_after_names": [g[0] for g in graph_after],
        })

        # M+29.02: persist changed graph BEFORE repair execution
        objective_id = self._persist_replan_checkpoint(
            project=project,
            objective_text=project.description,
            root_inputs=root_inputs,
            completed_outputs=outputs,
            graph_before=graph_before,
            graph_after=graph_after,
            failed_name=failed_name,
            repair_name=repair_name,
            lifecycle_events=report.lifecycle_events,
        )
        report.provenance["objective_id"] = objective_id
        report.provenance["replan_persisted"] = True
        report.lifecycle_events.append({
            "phase": "replan_persist",
            "objective_id": objective_id,
            "completed_nodes": list(outputs.keys()),
            "pending_nodes": [r.name for r in project.requirements
                              if r.name not in outputs or r.name == repair_name
                              or r.name == failed_name],
        })

        def _flag(key):
            ri = root_inputs or {}
            if ri.get(key):
                return True
            for v in ri.values():
                if isinstance(v, dict) and v.get(key):
                    return True
            ctrl = (project.provenance or {}).get("control") or {}
            if ctrl.get(key):
                return True
            return False
        pause = _flag("__pause_after_replan__")
        if pause:
            # Process boundary: stop after persist; repair runs after rehydrate
            report.executed = True
            report.verified = False
            report.provenance["replan_paused"] = True
            report.error = "replan_paused_for_cross_process_resume"
            return report

        # Same-process path: resume execution of mutated graph
        report.provenance["replan_rerun"] = True
        if verifier_channel:
            # Bound the verifier channel to a single re-execution (see the
            # marker check at the top of this method).
            project.provenance = dict(project.provenance or {})
            project.provenance["verifier_replan_done"] = True
        new_report = await self.run(project, root_inputs)
        if verifier_channel:
            # The rerun builds a fresh report, so the verifier-channel
            # diagnosis would otherwise be visible only inside
            # parent_lifecycle: carry it into the final report's own
            # lifecycle events, in chronological position (the diagnosis
            # preceded the rerun).
            new_report.lifecycle_events.insert(0, dict(diagnosis_event))
        new_report.provenance = dict(new_report.provenance or {})
        new_report.provenance["graph_before"] = graph_before
        new_report.provenance["graph_after"] = graph_after
        new_report.provenance["replan_attempted"] = True
        new_report.provenance["replan_persisted"] = True
        new_report.provenance["objective_id"] = objective_id
        new_report.provenance["parent_lifecycle"] = report.lifecycle_events
        # Mark objective completed
        self._complete_replan_objective(objective_id, new_report)
        return new_report


    def _synthesize_repair_candidate(self, project, order, outputs, failed_name):
        """Example-driven repair synthesis from project test artifacts (M+29.03)."""
        from swarm_engine.acquisition.repair_synthesis import synthesize_repair_for_paths
        test_paths = []
        source_paths = []
        for req in order:
            path = self._extract_path_from_req(req)
            if not path:
                continue
            desc = (req.description or "").lower()
            # M+29.05: match the "test" marker on the path BASENAME only
            # (existing Path_name convention, lowercase), as a whole token.
            # The description usually embeds the full path, so strip it
            # before the word checks -- otherwise a scratch dir like
            # ".../contest/..." re-triggers the quirk via the description.
            desc_words = desc.replace(path.lower(), "", 1)
            if path.endswith(".py") and (_TEST_TOKEN_RE.search(Path_name(path))
                                         or _TEST_TOKEN_RE.search(desc_words)
                                         or _ASSERT_TOKEN_RE.search(desc_words)):
                test_paths.append(path)
            elif path.endswith(".py"):
                source_paths.append(path)
        for name, val in (outputs or {}).items():
            if isinstance(val, dict) and val.get("ok") is False:
                err = (val.get("stderr") or "") + (val.get("stdout") or "")
                import re as _re
                for m in _re.finditer(r"(/[^\s]+\.py)", err):
                    p = m.group(1)
                    if p not in test_paths:
                        test_paths.append(p)
        for sp in source_paths:
            cand = synthesize_repair_for_paths(sp, test_paths)
            if cand is not None:
                return cand
        return None

    def _admit_synthesized_repair(self, candidate) -> dict:
        """Admit a synthesized repair as a write_text capability plan.

        2026-09-27 (Worker 2, item 6): this previously stored the
        CapabilityRecord DIRECTLY, bypassing admission entirely -- the
        row's id (cap_synth_<sha>) was not even fingerprint-bound, so
        the repair plan rode an unadmitted identity with only
        examples_satisfied >= 1 and non-empty content as gates. It now
        goes through the real admission path
        (engine.admit_as_engine, the trust-spine choke point):
        engine-attributed caller auth ('agent:admit_capability'),
        type check, effect ceiling, permission grants,
        quarantine-stickiness guards (0b/0c), fingerprint-bound id,
        goal binding, and acquired.* primitive registration.

        The candidate-level evidence gates (non-empty content, >=1
        satisfied example from the synthesizer's real test runs) stay
        as pre-checks. No smoke test is supplied: the evidence is the
        synthesizer's example runs (recorded in the report's lifecycle
        events), and executing a filesystem write as a "smoke test"
        would be a redundant side effect rather than new evidence --
        the orchestrator likewise admits with smoke=None in places.
        The plan's write_text effect must be covered by the bridge's
        scoped workspace grants (issued once at run start); without a
        grant admission fails closed at the permission stage, which is
        the honest outcome.
        """
        import hashlib
        from swarm_engine.synthesis.capability_store import plan_fingerprint
        store = getattr(self.engine, "capabilities", None)
        if store is None:
            return {"ok": False, "reasons": ["no_capability_store"]}
        plan = {
            "name": "synthesized_repair_write",
            "params": {},
            "steps": [{
                "id": "s1",
                "op": "write_text",
                "args": {"path": candidate.path, "content": candidate.content},
            }],
            "output": {"$step": "s1"},
        }
        goal = f"Create file {candidate.path} containing {candidate.content.strip()}"
        if not candidate.content.strip():
            return {"ok": False, "reasons": ["empty_content"]}
        if candidate.examples_satisfied < 1:
            return {"ok": False, "reasons": ["no_examples_satisfied"]}
        # Migration bridge: pre-repair rows used the cap_synth_<sha>
        # namespace, which was never fingerprint-bound. If an identical
        # repair (same path+content) was DELIBERATELY quarantined under
        # that legacy identity, admitting the same bytes fresh under a
        # fingerprint id would silently resurrect it -- refuse,
        # preserving deliberate-quarantine stickiness across the
        # namespace migration. (admit()'s own 0c guard covers the new
        # fingerprint id.)
        legacy_cid = "cap_synth_" + hashlib.sha256(
            (candidate.path + candidate.content).encode()).hexdigest()[:14]
        legacy = store.get(legacy_cid)
        if (legacy is not None and legacy.status == "quarantined"
                and store._quarantine_was_deliberate(legacy_cid)):
            store.log(legacy_cid, "resurrection_refused",
                      "synthesized-repair admission refused: identical "
                      "repair is deliberately quarantined under its legacy "
                      "identity; deliberate revocations are sticky -- "
                      "restore via integrity.restore_everywhere with "
                      "trust:transition authority")
            return {"ok": False,
                    "reasons": ["deliberately_quarantined_sticky: identical "
                                "repair capability is deliberately "
                                "quarantined; re-admission refused"]}
        admit = getattr(self.engine, "admit_as_engine", None)
        if admit is None:
            return {"ok": False, "reasons": ["no_admission_path"]}
        try:
            res = admit(goal, plan, name=f"repair_{candidate.func_name}")
        except Exception as exc:
            return {"ok": False, "reasons": [f"admission raised: {exc!r}"]}
        if res.ok:
            return {"ok": True, "capability_id": res.capability_id,
                    "reasons": [f"admitted via admission path "
                                f"(verdict={res.verdict}): "
                                + "; ".join(res.reasons)]}
        return {"ok": False, "reasons": list(res.reasons)}

    def _persist_replan_checkpoint(
            self, project, objective_text, root_inputs, completed_outputs,
            graph_before, graph_after, failed_name, repair_name, lifecycle_events):
        """Persist post-REPLAN project graph via ObjectiveStore (M+29.02)."""
        import time as _time
        import uuid
        from swarm_engine.core.autonomy import Objective, ObjectiveState
        store = getattr(self.engine, "objectives", None)
        if store is None:
            return ""
        objective_id = "replan_" + uuid.uuid4().hex[:16]
        # Serialize outputs (JSON-safe)
        safe_outputs = {}
        for k, v in (completed_outputs or {}).items():
            try:
                json.dumps(v)
                safe_outputs[k] = v
            except (TypeError, ValueError):
                safe_outputs[k] = str(v)
        # Strip control keys from root_inputs for resume
        clean_root = {k: v for k, v in (root_inputs or {}).items()
                      if not str(k).startswith("__")}
        payload = {
            "kind": "project_replan",
            "objective_text": objective_text,
            "requirements": [_serialize_requirement(r) for r in project.requirements],
            "completed_outputs": safe_outputs,
            "completed_nodes": list(safe_outputs.keys()),
            "graph_before": graph_before,
            "graph_after": graph_after,
            "failed_name": failed_name,
            "repair_name": repair_name,
            "root_inputs": clean_root,
            "lifecycle_events": list(lifecycle_events or []),
            "replan_pending": True,
        }
        obj = Objective(
            objective_id=objective_id,
            goal=objective_text[:500] if objective_text else "project_replan",
            payload=payload,
            state=ObjectiveState.PAUSED,
            steps_done=list(safe_outputs.keys()),
            last_error="",
            attempts=1,
            result=None,
            created_at=_time.time(),
            updated_at=_time.time(),
            diagnosis={},
        )
        store.save(obj)
        return objective_id

    def _complete_replan_objective(self, objective_id: str, report) -> None:
        from swarm_engine.core.autonomy import ObjectiveState
        store = getattr(self.engine, "objectives", None)
        if not store or not objective_id:
            return
        try:
            obj = store.get(objective_id) if hasattr(store, "get") else None
            if obj is None and hasattr(store, "_load"):
                obj = store._load(objective_id)
            if obj is None:
                # rebuild from unfinished list
                for o in store.unfinished():
                    if o.objective_id == objective_id:
                        obj = o
                        break
            if obj is None:
                return
            obj.state = (ObjectiveState.COMPLETED if getattr(report, "verified", False)
                       else ObjectiveState.FAILED)
            obj.result = {
                "verified": getattr(report, "verified", None),
                "leaf_results": getattr(report, "leaf_results", {}),
            }
            obj.payload = dict(obj.payload or {})
            obj.payload["replan_pending"] = False
            store.save(obj)
        except Exception:
            pass

    async def resume_replan(self, objective_id: str):
        """Rehydrate a persisted post-REPLAN project graph and continue (M+29.02)."""
        from swarm_engine.core.autonomy import ObjectiveState
        store = self.engine.objectives
        obj = None
        if hasattr(store, "get"):
            try:
                obj = store.get(objective_id)
            except Exception:
                obj = None
        if obj is None:
            for o in store.unfinished():
                if o.objective_id == objective_id:
                    obj = o
                    break
            if obj is None:
                # also search all states via direct SQL if needed
                with store._conn() as conn:
                    row = conn.execute(
                        "SELECT * FROM objectives WHERE objective_id=?",
                        (objective_id,)).fetchone()
                if row is not None:
                    obj = store._row(row)
        if obj is None:
            raise RuntimeError(f"replan objective {objective_id!r} not found")
        payload = obj.payload or {}
        if payload.get("kind") != "project_replan":
            raise RuntimeError(f"objective {objective_id!r} is not a project_replan")
        reqs = [_deserialize_requirement(d) for d in payload.get("requirements") or []]
        if not reqs:
            raise RuntimeError("replan payload has no requirements")
        from swarm_engine.acquisition.objective_project_adapter import ObjectiveProjectAdapter
        project = ProjectObjective(
            description=payload.get("objective_text") or obj.goal,
            requirements=reqs,
            verify=ObjectiveProjectAdapter._default_verify,
        )

        completed = dict(payload.get("completed_outputs") or {})
        root_inputs = dict(payload.get("root_inputs") or {})
        # Mark objective running
        obj.state = ObjectiveState.RUNNING
        obj.updated_at = time.time()
        store.save(obj)

        report = await self._run_from_checkpoint(
            project, root_inputs, completed,
            graph_before=payload.get("graph_before"),
            graph_after=payload.get("graph_after"),
            parent_lifecycle=payload.get("lifecycle_events") or [],
            objective_id=objective_id,
        )
        self._complete_replan_objective(objective_id, report)
        return report

    async def _run_from_checkpoint(
            self, project, root_inputs, completed_outputs,
            graph_before=None, graph_after=None, parent_lifecycle=None,
            objective_id=""):
        """Execute only nodes not already completed; reuse prior outputs."""
        report = ProjectRunReport(objective=project.description)
        # Oracle binding: deliberate, recorded, scoped grant decision for
        # this resumed run (see run()).
        self.issue_workspace_grants(
            scope_note=f"resume:{str(objective_id)[:40]}")
        report.provenance = {
            "resumed": True,
            "objective_id": objective_id,
            "graph_before": graph_before,
            "graph_after": graph_after,
            "replan_attempted": True,
            "replan_persisted": True,
            "completed_before_resume": list(completed_outputs.keys()),
            "parent_lifecycle": parent_lifecycle or [],
        }
        try:
            order = project.execution_order()
        except ValueError as exc:
            report.error = str(exc)
            return report
        report.execution_order = [r.name for r in order]
        by_name = {req.name: req for req in project.requirements}
        graph_fingerprint = self._graph_fingerprint(project)
        provisional_context: Dict[str, str] = {}
        behavioral_evidence: Dict[str, Dict[str, Any]] = {}
        outputs = dict(completed_outputs)
        skipped = []

        # Nodes that must re-execute: repair_* and anything depending on them,
        # plus any prior process failure (ok:False).
        force_rerun = set()
        for req in order:
            if str(req.name).startswith("repair_"):
                force_rerun.add(req.name)
            val = outputs.get(req.name)
            if isinstance(val, dict) and val.get("ok") is False:
                force_rerun.add(req.name)
        # Propagate: dependents of force_rerun
        changed = True
        while changed:
            changed = False
            for req in order:
                if req.name in force_rerun:
                    continue
                if any(d in force_rerun for d in (req.depends_on or [])):
                    force_rerun.add(req.name)
                    changed = True

        for req in order:
            if req.name in outputs and req.name not in force_rerun:
                skipped.append(req.name)
                req.lifecycle_state = "RESUMED_SKIPPED"
                report.resolved.append(req.as_dict())
                report.lifecycle_events.append({
                    "phase": "resume_skip", "requirement": req.name})
                continue
            # Force re-execution of repair and failed test nodes
            started = time.perf_counter()
            await self.resolve_requirement(req)
            req.lifecycle_state = "RESUMED_EXECUTE"
            report.resolved.append(req.as_dict())
            param_names = self._resolved_param_names(req)
            if not req.depends_on:
                root_value = root_inputs.get(req.name, {})
                if (isinstance(root_value, dict) and param_names
                        and set(root_value).issubset(set(param_names))):
                    bound_args = dict(root_value)
                else:
                    key = param_names[0] if param_names else req.name
                    bound_args = {key: root_value} if root_value != {} else {}
            else:
                param_types = self._resolved_param_types(req)
                if (len(param_types) == 1 and len(req.depends_on) > 1
                    and "list" in str(next(iter(param_types.values()))).lower()):
                    only_param = next(iter(param_types.keys()))
                    bound_args = {only_param: [outputs[d] for d in req.depends_on
                                               if d in outputs]}
                else:
                    bound_args = {}
                    for i, dep_name in enumerate(req.depends_on):
                        if i >= len(param_names):
                            break
                        if dep_name in outputs:
                            bound_args[param_names[i]] = outputs[dep_name]

            prim = self.engine.primitives.get(req.primitive_name or req.capability_id)
            from swarm_engine.primitives.core import Effect
            if prim is not None and (
                Effect.WRITE_FS in (getattr(prim, "effects", ()) or ())
                or any(getattr(x, "value", x) == "write_fs"
                       for x in (getattr(prim, "effects", ()) or ()))
            ):
                if not isinstance(bound_args, dict):
                    bound_args = {}
                bound_args = self._bind_fs_args_from_description(
                    req.description, bound_args)
                path = bound_args.get("path")
                if path:
                    if not self._authorize_workspace_write(str(path)):
                        report.lifecycle_events.append({
                            "phase": "authorization_refused",
                            "requirement": req.name,
                            "reason": f"WRITE_FS on {path!r} is outside the "
                                      f"workspace or ungranted -- refusing"})
                        req.validation_level = "failed"
                        continue
            if prim is not None and (
                Effect.PROCESS in (getattr(prim, "effects", ()) or ())
                or any(getattr(x, "value", x) == "process"
                       for x in (getattr(prim, "effects", ()) or ()))
            ):
                if not isinstance(bound_args, dict):
                    bound_args = {}
                bound_args = self._bind_process_args_from_description(
                    req.description, bound_args)
                target = bound_args.get("path") or bound_args.get("command") or ""
                if target:
                    if not self._authorize_workspace_process(str(target)):
                        report.lifecycle_events.append({
                            "phase": "authorization_refused",
                            "requirement": req.name,
                            "reason": f"PROCESS on {str(target)[:120]!r} is "
                                      f"outside the workspace or ungranted "
                                      f"-- refusing"})
                        req.validation_level = "failed"
                        continue

            try:
                outputs[req.name] = self._run_capability(req, bound_args)
                report.lifecycle_events.append({
                    "phase": "resume_execute", "requirement": req.name,
                    "capability_id": req.capability_id or req.primitive_name})
            except Exception as exc:
                report.error = f"resume execution failed at {req.name!r}: {exc}"
                return report

        leaves = project.leaf_names()
        report.leaf_results = {name: outputs[name] for name in leaves if name in outputs}
        report.executed = True
        report.provenance["skipped_nodes"] = skipped
        try:
            report.verified = bool(project.verify(report.leaf_results))
        except Exception as exc:
            report.error = f"verification raised: {exc}"
            report.verified = False
        if report.verified:
            self._record_verified_bindings(project, graph_fingerprint, by_name,
                                           behavioral_evidence, provisional_context)
        return report



def _serialize_requirement(req: "ProjectRequirement") -> Dict[str, Any]:
    return {
        "name": req.name,
        "description": req.description,
        "depends_on": list(req.depends_on),
        "capability_id": req.capability_id,
        "primitive_name": req.primitive_name,
        "validation_level": req.validation_level,
        "lifecycle_state": req.lifecycle_state,
        "root_examples": [],
        "semantic_structure_id": getattr(req, "semantic_structure_id", "") or "",
    }


def _deserialize_requirement(d: Dict[str, Any]) -> "ProjectRequirement":
    return ProjectRequirement(
        name=d["name"],
        description=d.get("description", ""),
        depends_on=list(d.get("depends_on") or []),
        capability_id=d.get("capability_id") or "",
        primitive_name=d.get("primitive_name") or "",
        validation_level=d.get("validation_level") or "unresolved",
        lifecycle_state=d.get("lifecycle_state") or "UNRESOLVED",
        semantic_structure_id=d.get("semantic_structure_id") or "",
    )

def Path_name(path: str) -> str:
    return path.rstrip("/").split("/")[-1].lower()


def _content_from_plan(plan) -> str:
    """Pull write content from a stored capability plan structure."""
    if not isinstance(plan, dict):
        return ""
    # Direct params
    for key in ("content", "text", "body"):
        if key in plan and isinstance(plan[key], str):
            return plan[key]
    steps = plan.get("steps") or plan.get("ops") or []
    if isinstance(steps, list):
        for step in steps:
            if not isinstance(step, dict):
                continue
            args = step.get("args") or step.get("params") or {}
            if isinstance(args, dict):
                for key in ("content", "text", "body"):
                    if key in args and isinstance(args[key], str):
                        return args[key]
            if step.get("op") in ("write_text", "write_json") or step.get("primitive") == "write_text":
                c = (args or {}).get("content")
                if isinstance(c, str):
                    return c
    # Nested
    for v in plan.values():
        if isinstance(v, dict):
            c = _content_from_plan(v)
            if c:
                return c
    return ""
