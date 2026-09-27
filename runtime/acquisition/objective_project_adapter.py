"""Production bridge from a raw objective to an executable project graph.

This module deliberately consumes the existing objective decomposition and
relation-inference outputs.  It does not recognise operations, provide graph
shapes, supply capability names, or infer an execution order by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarm_engine.acquisition.lexical import Lexicon
from swarm_engine.acquisition.objective_bridge import (
    DiscoveredRequirement,
    ObjectiveDecomposition,
    decompose_objective,
)
from swarm_engine.acquisition.project_bridge import (
    ProjectExecutor,
    ProjectObjective,
    ProjectRequirement,
    ProjectRunReport,
)
from swarm_engine.acquisition.relation_bridge import (
    DependencyGraph,
    RelationType,
    build_dependency_graph,
)


Example = Tuple[Dict[str, Any], Any]


@dataclass
class ObjectiveProjectRun:
    """Inspectable result of the raw-objective production path."""

    objective: str
    decomposition: ObjectiveDecomposition
    dependency_graph: DependencyGraph
    project: Optional[ProjectObjective] = None
    project_report: Optional[ProjectRunReport] = None
    bridge_error: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return bool(
            self.project_report
            and self.project_report.executed
            and self.project_report.verified is True
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "objective": self.objective,
            "decomposition": self.decomposition.as_dict(),
            "dependency_graph": self.dependency_graph.as_dict(),
            "project": {
                "description": self.project.description,
                "requirements": [r.as_dict() for r in self.project.requirements],
                "leaf_names": self.project.leaf_names(),
            } if self.project is not None else None,
            "project_report": self.project_report.as_dict() if self.project_report else None,
            "bridge_error": self.bridge_error,
            "succeeded": self.succeeded,
            "provenance": self.provenance,
        }


class ObjectiveProjectAdapter:
    """Construct and execute a project from existing objective/graph outputs.

    Root payload is structurally associated with every inferred source node.
    Behavioral examples, when supplied, are associated only with a sole source
    node; this is a graph-derived rule and avoids guessing which branch an
    example would describe in a multi-root graph.
    """

    def __init__(self, engine):
        self.engine = engine

    def _lexicon(self) -> Lexicon:
        return Lexicon(db_path=self.engine.db_path)

    @staticmethod
    def _source_ids(graph: DependencyGraph) -> List[str]:
        consumed = {
            edge.target_requirement
            for edge in graph.edges
            if edge.relation_type in (
                RelationType.PRODUCES_INPUT_FOR,
                RelationType.DEPENDS_ON,
            )
        }
        return [rid for rid in graph.requirement_ids if rid not in consumed]

    @staticmethod
    def _dependencies(graph: DependencyGraph) -> Dict[str, List[str]]:
        result = {rid: [] for rid in graph.requirement_ids}
        for edge in graph.edges:
            if edge.relation_type in (
                RelationType.PRODUCES_INPUT_FOR,
                RelationType.DEPENDS_ON,
            ):
                result.setdefault(edge.target_requirement, []).append(
                    edge.source_requirement
                )
        return result

    @staticmethod
    def _default_verify(leaf_results: Dict[str, Any]) -> bool:
        """Structural execution check when no external oracle is supplied.

        Process-family results carry ok/returncode; a failed command must not
        certify overall objective success (M+29.00).
        """
        if not leaf_results:
            return False
        for value in leaf_results.values():
            if value is None:
                return False
            if isinstance(value, dict) and "ok" in value and value.get("ok") is False:
                return False
            if isinstance(value, dict) and "returncode" in value and value.get("returncode") not in (0, None):
                return False
        return True


    def _try_markdown_project_synthesis(self, objective_text: str):
        """M+29.05: MD spec → implementable project → structured construction objective."""
        from swarm_engine.acquisition.spec_synthesis import (
            looks_like_markdown_spec, markdown_to_project,
        )
        if not looks_like_markdown_spec(objective_text):
            # Also accept path reference ending in .md if file readable
            text = objective_text
            import re as _re
            m = _re.search(r"(/[^\s]+\.md)", objective_text or "")
            if m:
                from pathlib import Path as _P
                path = _P(m.group(1))
                if path.exists():
                    text = path.read_text()
                else:
                    return None
            else:
                return None
        else:
            text = objective_text
        projects_dir = getattr(self.engine, "projects_dir", None) or "/tmp/swarm_projects"
        synthesized = markdown_to_project(text, str(projects_dir))
        if synthesized is None:
            return None
        root = synthesized.root
        # Build structured multi-clause objective for existing ProjectExecutor path
        clauses = [f"Create directory {root}"]
        # ensure package dirs
        seen_dirs = {root}
        for f in synthesized.files:
            from pathlib import Path as _P
            full = str(_P(root) / f.relative_path)
            parent = str(_P(full).parent)
            if parent not in seen_dirs:
                clauses.append(f"Create directory {parent}")
                seen_dirs.add(parent)
            # Escape content carefully: use actual content in Create file ... containing
            content = f.content
            clauses.append(f"Create file {full} containing {content}")
        test_path = str(_P(root) / synthesized.test_command_rel)
        clauses.append(f"run python {test_path}")
        structured = " then ".join(clauses)
        return {
            "structured_objective": structured,
            "synthesized": synthesized,
        }


    async def _run_synthesized_project(
        self, objective_text, synthesized, root_payload, verify,
    ):
        """Execute SynthesizedProject via ProjectRequirement graph (no clause split)."""
        from pathlib import Path as _P
        import hashlib
        from swarm_engine.acquisition.objective_bridge import ObjectiveDecomposition
        from swarm_engine.acquisition.relation_bridge import DependencyGraph

        root = _P(synthesized.root)
        requirements: List[ProjectRequirement] = []
        prev_name = None
        dirs = {str(root)}
        for f in synthesized.files:
            dirs.add(str((_P(root) / f.relative_path).parent))
        for d in sorted(dirs, key=lambda x: (x.count("/"), x)):
            name = "dir_" + hashlib.sha256(d.encode()).hexdigest()[:12]
            requirements.append(ProjectRequirement(
                name=name,
                description=f"Create directory {d}",
                depends_on=[prev_name] if prev_name else [],
                capability_id="make_dir",
                primitive_name="make_dir",
                validation_level="reused",
            ))
            prev_name = name
        file_names = []
        for f in synthesized.files:
            full = str(_P(root) / f.relative_path)
            name = "file_" + hashlib.sha256(full.encode()).hexdigest()[:12]
            requirements.append(ProjectRequirement(
                name=name,
                description=f"Create file {full} containing {f.content}",
                depends_on=[prev_name] if prev_name else [],
                capability_id="write_text",
                primitive_name="write_text",
                validation_level="reused",
            ))
            file_names.append(name)
            prev_name = name
        test_full = str(_P(root) / synthesized.test_command_rel)
        test_name = "test_" + hashlib.sha256(test_full.encode()).hexdigest()[:12]
        requirements.append(ProjectRequirement(
            name=test_name,
            description=f"run python {test_full}",
            depends_on=list(file_names),
            capability_id="run_python",
            primitive_name="run_python",
            validation_level="reused",
        ))
        project = ProjectObjective(
            description=(objective_text or "")[:500],
            requirements=requirements,
            verify=verify or self._default_verify,
            provenance={
                "builder": "ObjectiveProjectAdapter.spec_synthesis",
                "spec_synthesis": synthesized.as_dict(),
            },
        )
        decomp = ObjectiveDecomposition(
            objective_id="spec_synth",
            source_text=(objective_text or "")[:200],
            requirements=[],
        )
        graph = DependencyGraph(
            requirement_ids=[r.name for r in requirements],
            edges=[],
            rejected=[],
        )
        result = ObjectiveProjectRun(
            objective=(objective_text or "")[:500],
            decomposition=decomp,
            dependency_graph=graph,
            project=project,
            provenance={
                "spec_synthesis": synthesized.as_dict(),
                "spec_synthesis_applied": True,
            },
        )
        payload = dict(root_payload or {})
        control = {k: payload.pop(k) for k in list(payload.keys())
                   if str(k).startswith("__") and str(k).endswith("__")}
        if control:
            project.provenance = dict(project.provenance or {})
            project.provenance["control"] = control
        result.project_report = await ProjectExecutor(self.engine).run(project, {})
        if result.project_report is not None:
            result.project_report.provenance = dict(
                result.project_report.provenance or {})
            result.project_report.provenance["spec_synthesis"] = synthesized.as_dict()
        return result

    def construct(
        self,
        objective_text: str,
        root_payload: Optional[Dict[str, Any]] = None,
        examples: Optional[Sequence[Example]] = None,
        verify: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> ObjectiveProjectRun:
        decomposition = decompose_objective(self._lexicon(), objective_text)
        graph = build_dependency_graph(decomposition.requirements)
        result = ObjectiveProjectRun(
            objective=objective_text,
            decomposition=decomposition,
            dependency_graph=graph,
            provenance={
                "builder": "ObjectiveProjectAdapter.v1",
                "objective_input": "raw_text",
                "dependency_source": "DependencyGraph.accepted_edges",
            },
        )
        if graph.has_cycle:
            result.bridge_error = "dependency inference produced a cycle; refusing execution"
            return result
        if not decomposition.requirements:
            result.bridge_error = "objective produced no executable requirement candidates"
            return result

        by_id = {req.requirement_id: req for req in decomposition.requirements}
        dependencies = self._dependencies(graph)
        source_ids = self._source_ids(graph)
        normalized_examples = list(examples or [])
        requirements: List[ProjectRequirement] = []
        for req in decomposition.requirements:
            root_examples = normalized_examples if (
                len(source_ids) == 1 and req.requirement_id == source_ids[0]
            ) else []
            requirements.append(ProjectRequirement(
                name=req.requirement_id,
                description=req.description,
                semantic_structure_id=req.semantic_structure_id,
                depends_on=list(dependencies.get(req.requirement_id, [])),
                root_examples=root_examples,
            ))

        result.project = ProjectObjective(
            description=objective_text,
            requirements=requirements,
            verify=verify or self._default_verify,
            provenance={
                "builder": "ObjectiveProjectAdapter.v1",
                "decomposition_id": decomposition.objective_id,
                "dependency_graph": graph.as_dict(),
                "source_requirement_ids": source_ids,
            },
        )
        return result

    async def run(
        self,
        objective_text: str,
        root_payload: Optional[Dict[str, Any]] = None,
        examples: Optional[Sequence[Example]] = None,
        verify: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> ObjectiveProjectRun:
        # M+29.05: attempt generic MD → implementable project synthesis first
        synth_meta = self._try_markdown_project_synthesis(objective_text)
        if synth_meta is not None:
            synthesized = synth_meta["synthesized"]
            result = self._run_synthesized_project(
                objective_text, synthesized, root_payload, verify)
            return await result

        # M+29.23: the specialized markdown-synthesis pipeline (atomic
        # operator composition, obligation-driven synthesis, class
        # synthesizers) found nothing. Before falling back to the older,
        # differently-shaped clause-decomposition path below, try the
        # M+29.22 symbolic bridge -- but only when there is a real,
        # objective-derived behavioral contract (worked numeric examples
        # parsed from the objective text itself) to drive it, and only
        # after confirming the specialized arithmetic inducer genuinely
        # cannot solve it. This supplies no solution of its own -- it
        # only routes the objective's own already-extracted examples to
        # an existing, unmodified search engine.
        symbolic_result = self._try_symbolic_synthesis_dispatch(
            objective_text, root_payload)
        if symbolic_result is not None:
            return symbolic_result

        result = self.construct(objective_text, root_payload, examples, verify)
        if result.project is None:
            return result
        payload = dict(root_payload or {})
        control = {k: payload.pop(k) for k in list(payload.keys())
                   if str(k).startswith("__") and str(k).endswith("__")}
        if result.project is not None and control:
            result.project.provenance = dict(result.project.provenance or {})
            result.project.provenance["control"] = control
        source_inputs = {
            source_id: dict(payload)
            for source_id in self._source_ids(result.dependency_graph)
        }
        result.project_report = await ProjectExecutor(self.engine).run(
            result.project, source_inputs
        )
        return result

    def _try_symbolic_synthesis_dispatch(
        self, objective_text: str, root_payload: Optional[Dict[str, Any]],
    ) -> Optional["ObjectiveProjectRun"]:
        from swarm_engine.acquisition.spec_synthesis import looks_like_markdown_spec
        from swarm_engine.acquisition.atomic_operators import (
            extract_numeric_io_examples, induce_arithmetic_from_examples,
            symbolic_synthesis_bridge, verify_symbolic_candidate,
            learned_symbolic_synthesis_bridge,
        )
        if not looks_like_markdown_spec(objective_text):
            return None
        num_ex = extract_numeric_io_examples(objective_text)
        if not num_ex:
            return None
        specialized = induce_arithmetic_from_examples(num_ex)
        if specialized and specialized.get("status") == "ok":
            return None  # the specialized path should have already solved this
        param_names = sorted(num_ex[0]["fields"].keys())
        cognition = getattr(self.engine, "cognition", None)
        from swarm_engine.synthesis.acquisition_learning import AcquisitionLearner
        learner = AcquisitionLearner(self.engine.db_path)
        bridged = learned_symbolic_synthesis_bridge(
            num_ex, param_names, objective_text, cognition, learner)
        if bridged is None:
            return None
        plan = bridged["plan"]
        if not verify_symbolic_candidate(plan, num_ex, self.engine.composer):
            return None  # GeneralSynthesizer's own candidate failed re-verification

        admitted_id = None
        if hasattr(self.engine, "admit_as_engine"):
            from swarm_engine.synthesis.admission import SmokeTest
            ex0 = num_ex[0]
            smoke = SmokeTest(args=dict(ex0["fields"]), expect=ex0["output"],
                               name="symbolic_bridge_smoke")
            try:
                res = self.engine.admit_as_engine(objective_text, plan,
                                                  smoke=smoke)
                if getattr(res, "ok", False):
                    admitted_id = res.capability_id
            except Exception:
                admitted_id = None

        decomposition = decompose_objective(self._lexicon(), objective_text)
        graph = build_dependency_graph(decomposition.requirements)
        exec_result: Dict[str, Any] = {}
        verified_ok = True
        if root_payload:
            try:
                exec_result = self.engine.composer.execute_sync(plan, dict(root_payload))
                verified_ok = bool(exec_result.get("success"))
            except Exception:
                verified_ok = False
        report = ProjectRunReport(
            objective=objective_text,
            resolved=[{
                "name": "symbolic_bridge", "description": objective_text,
                "capability_id": admitted_id or "", "primitive_name": "",
                "validation_level": "symbolic_bridge",
                "lifecycle_state": "ACQUIRED" if admitted_id else "UNPERSISTED",
                "depends_on": [], "semantic_structure_id": "",
                "semantic_evidence_gaps": [],
            }],
            execution_order=["symbolic_bridge"],
            result=exec_result.get("value"),
            leaf_results={"symbolic_bridge": exec_result.get("value")},
            executed=True,
            verified=verified_ok,
            error="" if verified_ok else "symbolic candidate failed on root payload",
        )
        run_result = ObjectiveProjectRun(
            objective=objective_text, decomposition=decomposition,
            dependency_graph=graph, project=None, project_report=report,
            provenance={
                "symbolic_bridge": {
                    "candidates_tried": bridged["candidates_tried"],
                    "plan": plan, "capability_id": admitted_id,
                }
            },
        )
        return run_result


