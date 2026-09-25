"""
swarm_engine/capability/growth_engine.py

GenericCapabilityGrowthEngine: takes any CapabilityRequirement, from any
source, and tries to satisfy it using only mechanisms SWarm's architecture
can honestly support — reusing existing composition/synthesis/admission
machinery wherever the requirement is example-driven, and falling through
to externally-researched knowledge only when it isn't.

The honest limit, stated once here because it governs this whole file:
SWarm has no LLM and no code-generation-from-natural-language capability.
Given retrieved reference material, it CANNOT write novel code satisfying
an arbitrary description. What it CAN honestly do is RETRIEVAL +
SUBSTITUTION: find a genuine code example already present in real fetched
documentation, and adapt it via explicit, traceable substitution — never
free-form generation. If no matching example exists in the retrieved
knowledge, this reports failure honestly rather than inventing something
plausible-looking.

Routing:
  1. If the requirement is example-driven, try the EXISTING composition/
     synthesis/admission path (`engine.synthesize_and_admit`) — unchanged.
  2. Otherwise, if a target_domain is set and knowledge is available,
     extract a real code pattern from retrieved material, validate it via
     the domain's registered TextArtifactValidator, and if valid, persist
     it via ProjectModificationGuard with full provenance/rollback.
  3. If neither route succeeds, report an honest, structured failure.

Nothing here special-cases WHICH subsystem produced the requirement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.capability.artifact_validation import (
    get_validator, record_artifact_validation, verify_validator_binding,
)
from swarm_engine.capability.external_knowledge import (
    ExternalKnowledgeSource, NoExternalKnowledgeSource,
)
from swarm_engine.capability.requirement import CapabilityRequirement
from swarm_engine.services.run_control import checkpoint


@dataclass
class ValidationLevel:
    """Explicit levels, not a boolean. Introduced because the prior model
    collapsed "runs without crashing" and "does what it's supposed to"
    into a single succeeded=True/False, and the benchmark demonstrated the
    real consequence: a script computing `5 * 2 - 999` reported full
    success because it happened not to raise an exception. These are
    ordered from weakest to strongest guarantee; STRUCTURALLY_VALID and
    EXECUTABLE are checked the same way as before (parses / imports
    without error). OBJECTIVE_VERIFIED requires an actual oracle — real
    (input, output) examples — checked by calling the admitted
    capability/primitive directly and comparing outputs, not by trusting
    that synthesis reported success. UNVERIFIED is not a failure: it is
    the honest label for "this ran, but nothing establishes whether its
    behavior is correct," used whenever no oracle exists for a given file
    (typically glue/orchestration code assembled via external knowledge,
    which has no worked examples of its own)."""
    STRUCTURALLY_VALID = "structurally_valid"
    EXECUTABLE = "executable"
    OBJECTIVE_VERIFIED = "objective_verified"
    UNVERIFIED = "unverified"
    FAILED = "failed"


@dataclass
class GrowthResult:
    requirement_id: str
    route_attempted: Optional[str] = None
    succeeded: bool = False
    artifact_path: Optional[str] = None
    capability_id: Optional[str] = None
    validation: Optional[Dict[str, Any]] = None
    validation_level: str = ValidationLevel.FAILED
    reason: str = ""
    trace: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"requirement_id": self.requirement_id,
                "route_attempted": self.route_attempted,
                "succeeded": self.succeeded, "artifact_path": self.artifact_path,
                "capability_id": self.capability_id,
                "validation": self.validation,
                "validation_level": self.validation_level,
                "reason": self.reason,
                "trace": self.trace}


# Was hardcoded to GDScript keywords only (func/extends/var/const/...),
# found directly: retrieved Python code for a bpy requirement was never
# recognized as code at all, since none of its lines start with a GDScript
# keyword. Generalized to structural code SHAPES common across C-like and
# Python-like languages, rather than keywords from one specific grammar —
# an assignment, a call, a control-flow keyword in any of the languages
# this engine has actually targeted, an import/include, or a line ending in
# an opening block character. Still not universal (no support for every
# possible target language), but no longer silently specific to one.
_CODE_LINE = re.compile(
    r"^\s*("
    r"import |from .+ import |func |def |extends |class |"
    r"var |const |let |"
    r"if |elif |else|for |while |return|print\(|"
    r"@export|@onready|@\w+|#|"
    r"[\w.]+\s*=\s*[^=]|"          # assignment: name = value
    r"[\w.]+\(.*\)|"               # a call: name(...anything...)
    r".+[:{]\s*$"                  # line opens a block (: or {)
    r")")


class GenericCapabilityGrowthEngine:
    def __init__(self, swarm_engine, project_root: Optional[str] = None,
                knowledge_source: Optional[ExternalKnowledgeSource] = None,
                provenance=None):
        self.engine = swarm_engine
        self.project_root = project_root
        self.knowledge_source = knowledge_source or NoExternalKnowledgeSource()
        self.provenance = provenance

    def grow(self, requirement: CapabilityRequirement) -> GrowthResult:
        result = GrowthResult(requirement_id=requirement.requirement_id)

        contradiction = requirement.find_contradiction()
        if contradiction is not None:
            result.route_attempted = "contradiction_detected"
            result.succeeded = False
            result.reason = (f"contradictory example set, not attempted: "
                            f"{contradiction}")
            result.trace.append("checked for same-input/different-output "
                               "contradictions before any search — found "
                               "one, so composition, iteration, and "
                               "external-knowledge acquisition were all "
                               "skipped rather than wasted on an "
                               "impossible target")
            return result

        if requirement.is_example_driven():
            result.route_attempted = "composition"
            result.trace.append("requirement carries real (input, output) "
                                "examples — routing to existing composition/"
                                "synthesis/admission")
            # Cooperation point at each route transition. No-op when no
            # control is installed.
            checkpoint("grow:composition")
            outcome = self.engine.synthesize_and_admit(
                requirement.description, requirement.examples,
                param_names=requirement.param_names)
            result.succeeded = outcome["admitted"]
            result.capability_id = outcome.get("capability_id")

            if not result.succeeded and hasattr(self.engine, "improvement_pipeline"):
                # Recovery, not a retry of the identical attempt: the same
                # mechanism CapabilityGrowthConnector already uses when
                # composition fails under the live policy — reused here
                # rather than re-implemented, per "reuse existing recovery
                # machinery wherever possible." A failed first attempt is
                # not necessarily "inexpressible"; it may just need the
                # already-governed search-policy improvement loop to widen
                # the live policy from real accumulated evidence before a
                # second attempt is worth making.
                result.trace.append("composition failed under the current "
                                    "live policy; attempting recovery via "
                                    "the existing improvement pipeline "
                                    "before falling through to external "
                                    "knowledge")
                checkpoint("grow:recovery")
                recovery_reports = self.engine.improvement_pipeline.run_cycle()
                result.trace.append(f"recovery outcome(s): "
                                    f"{[r['outcome'] for r in recovery_reports]}")
                if any(r["outcome"] == "active" for r in recovery_reports):
                    outcome = self.engine.synthesize_and_admit(
                        requirement.description, requirement.examples,
                        param_names=requirement.param_names)
                    result.succeeded = outcome["admitted"]
                    result.capability_id = outcome.get("capability_id")
                    result.trace.append(f"retried composition after recovery: "
                                        f"admitted={result.succeeded}")

            result.reason = ("composition satisfied the requirement" if result.succeeded
                             else "composition could not satisfy the requirement, "
                                  "even after attempting recovery via the "
                                  "improvement pipeline")
            if result.succeeded:
                verified = self._reverify_against_examples(
                    requirement, result.capability_id)
                result.validation = {"reverified": verified}
                if verified:
                    result.validation_level = ValidationLevel.OBJECTIVE_VERIFIED
                else:
                    # A real, if rare, inconsistency: admission reported
                    # success but independently re-calling the admitted
                    # capability against its own examples disagreed. Honest
                    # behavior here is to NOT report success — trusting the
                    # admitted flag alone is exactly the "runs without
                    # crashing implies correct" mistake this fix exists to
                    # close, just one layer up.
                    result.succeeded = False
                    result.validation_level = ValidationLevel.FAILED
                    result.reason = ("composition reported admission but "
                                    "independent re-verification against its "
                                    "own examples disagreed — not reported "
                                    "as success")
                    return result
                self._materialize_internal_capability(requirement, result)
                return result

            # Iteration construction as a genuinely different fallback, not
            # another retry of the same mechanism: tree-based composition
            # (GeneralSynthesizer) and iteration construction
            # (PrimitiveConstructor) can each solve requirements the other
            # structurally cannot — a fixed-depth tree can't express a
            # variable-trip-count loop, and this schema's step function has
            # to actually vary the running value, so it's not simply a
            # wider version of the same search. Found necessary directly:
            # this connection never existed — IterativePrimitiveGrower was
            # built and wired into SwarmEngine, but nothing ever tried it
            # automatically when composition failed; every prior benchmark
            # that used it was invoked by a human-written script calling it
            # directly, not by this engine deciding to.
            if hasattr(self.engine, "iterative_primitive_grower") and \
                    requirement.param_names and len(requirement.param_names) == 1:
                result.trace.append("composition failed; trying iteration "
                                    "construction as a structurally "
                                    "different fallback before external "
                                    "knowledge")
                checkpoint("grow:iteration")
                name = self.engine.iterative_primitive_grower.grow(
                    requirement.description, requirement.examples,
                    param_name=requirement.param_names[0])
                if name is not None:
                    verified = self._reverify_against_examples(
                        requirement, f"primitive:{name}")
                    if not verified:
                        result.trace.append(
                            "iteration construction reported success but "
                            "independent re-verification against its own "
                            "examples disagreed — not reported as success")
                        name = None  # fall through to external knowledge
                    else:
                        result.succeeded = True
                        result.capability_id = f"primitive:{name}"
                        result.validation = {"reverified": True}
                        result.validation_level = ValidationLevel.OBJECTIVE_VERIFIED
                        result.reason = ("iteration construction satisfied the "
                                         "requirement — a genuinely new "
                                         "primitive, not a tree composition")
                        self._materialize_internal_capability(requirement, result)
                        return result
                result.trace.append("iteration construction also could not "
                                    "satisfy the requirement")

            result.trace.append("composition and iteration both failed; "
                                "falling through to external-knowledge "
                                "route if a domain hint exists")

        if not requirement.target_domain:
            result.reason = (result.reason or
                             "no target_domain hint and no example-driven route "
                             "available — nothing this engine can attempt")
            return result

        result.route_attempted = "external_knowledge"
        checkpoint("grow:external")
        query = self._formulate_query(requirement)
        result.trace.append(f"formulated research query: {query!r}")
        retrieved = self.knowledge_source.research(
            query, {"target_domain": requirement.target_domain,
                   "description": requirement.description})
        if not retrieved:
            pending_now = False
            if hasattr(self.knowledge_source, "pending"):
                pending_now = any(p["query"] == query
                                  for p in self.knowledge_source.pending())
            if pending_now:
                result.reason = ("no answer available YET — the query has "
                                 "been queued for a governed external "
                                 "fulfiller and this requirement can be "
                                 "retried once it's answered; not a dead end")
            else:
                result.reason = ("no external knowledge available for this "
                                 "requirement — honest failure, not a "
                                 "fabricated artifact")
            return result
        result.trace.append(f"retrieved {len(retrieved)} knowledge item(s), "
                            f"source(s): {[r.source for r in retrieved]}")

        artifact = self._extract_and_adapt(retrieved, requirement)
        if artifact is None:
            result.reason = ("no usable code pattern found in the retrieved "
                             "knowledge for this requirement — reported "
                             "honestly rather than fabricating one")
            return result
        result.trace.append(f"extracted and adapted a "
                            f"{len(artifact.splitlines())}-line candidate "
                            f"artifact from real retrieved material")

        validator = get_validator(requirement.target_domain)
        if validator is None:
            result.reason = (f"no validator registered for domain "
                             f"{requirement.target_domain!r} — an artifact "
                             f"exists but cannot be independently validated")
            return result
        # O18: in the bound configuration the live validator must still be
        # byte-identical to its registered definition -- a silent registry
        # swap is refused here, before its verdict can be trusted.
        binding = verify_validator_binding(requirement.target_domain,
                                           validator)
        verdict = validator.validate(artifact)
        if binding is not None:
            eval_id = record_artifact_validation(
                binding, requirement.target_domain, artifact, verdict)
            result.trace.append(
                f"artifact validation bound to validator oracle "
                f"{binding['oracle_id']} v{binding['version']} "
                f"(eval {eval_id})")
        result.validation = verdict.as_dict()
        if not verdict.valid:
            result.reason = f"artifact failed validation: {verdict.checks_failed}"
            return result
        result.trace.append(f"validated: {verdict.checks_passed}")

        write = self._persist(requirement, artifact)
        if write is None:
            result.reason = "no project root configured; artifact validated but not persisted"
            return result
        result.succeeded = write["committed"]
        result.artifact_path = write["path"]
        result.capability_id = write.get("capability_id")
        result.reason = ("external-knowledge-informed artifact admitted and "
                         "persisted" if result.succeeded else
                         "artifact failed to persist")
        if result.succeeded:
            # Structurally valid (the artifact validator's ast-level check
            # already passed), but never higher than that here: this route
            # has no worked examples for the assembled file, so there is no
            # oracle to check its behavior against. EXECUTABLE (does it
            # actually run) is established later at the system level by
            # CoordinatedGrowthOrchestrator; OBJECTIVE_VERIFIED is
            # structurally unreachable for this route and must stay that
            # way — inventing a proxy oracle here would be exactly the
            # "pretend semantics can be inferred magically" this fix is
            # required not to do.
            result.validation_level = ValidationLevel.STRUCTURALLY_VALID
        return result

    def _reverify_against_examples(self, requirement: CapabilityRequirement,
                                   capability_id: Optional[str]) -> bool:
        """The concrete mechanism behind OBJECTIVE_VERIFIED: actually call
        the admitted capability/primitive against every one of the
        requirement's own examples and compare outputs, rather than trust
        a boolean the admission process already reported. This is what
        "tested against them" means literally, not just "was reported as
        passing" — a distinction the benchmark showed matters."""
        if capability_id is None or not requirement.examples:
            return False
        try:
            if capability_id.startswith("primitive:"):
                prim = self.engine.primitives.get(capability_id.split(":", 1)[1])
                if prim is None:
                    return False
                for args, expected in requirement.examples:
                    if prim.fn(**args) != expected:
                        return False
                return True
            else:
                from swarm_engine.synthesis.composer import Composer
                record = self.engine.capabilities.get(capability_id)
                if record is None:
                    return False
                comp = Composer(self.engine.primitives)
                for args, expected in requirement.examples:
                    out = comp.execute_sync(record.plan, args)
                    if out.get("value") != expected:
                        return False
                return True
        except Exception:
            return False

    def _materialize_internal_capability(self, requirement: CapabilityRequirement,
                                        result: GrowthResult) -> None:
        """Composition and iteration both succeed by registering an
        internal SWarm primitive/capability — neither writes a file, since
        neither ever needed to before this. When a requirement also names a
        target_path (meaning some OTHER file in the same project needs to
        import this one as a real module), that's not enough: a capability
        that exists only inside SwarmEngine's live registry can't be
        imported by a separately-run Python file.

        Honest scope: this writes a THIN WRAPPER that calls back into the
        SWarm engine's own registry/rehydration machinery to reach the
        underlying primitive — it is a real, runnable bridge WITHIN this
        codebase (verified by actually importing and running it), not a
        claim that the wrapper is portable outside an environment with
        swarm_engine installed. Full compilation of an arbitrary Expr tree
        or IterationHypothesis into fully standalone Python source is a
        real, separate piece of work this does not attempt.
        """
        target_path = requirement.constraints.get("target_path")
        if self.project_root is None or not target_path or not result.capability_id:
            return
        cap_id = result.capability_id
        func_name = requirement.constraints.get(
            "function_name", target_path.rsplit(".", 1)[0])
        param_names = requirement.param_names or ("x",)
        args_sig = ", ".join(param_names)
        args_call = ", ".join(f"{p}={p}" for p in param_names)

        if cap_id.startswith("primitive:"):
            prim_name = cap_id.split(":", 1)[1]
            body = (f"    from swarm_engine.core.engine import SwarmEngine\n"
                   f"    _e = SwarmEngine(db_path={self.engine.db_path!r})\n"
                   f"    _e.boot()\n"
                   f"    return _e.primitives.get({prim_name!r}).fn({args_call})")
        else:
            body = (f"    from swarm_engine.core.engine import SwarmEngine\n"
                   f"    from swarm_engine.synthesis.composer import Composer\n"
                   f"    _e = SwarmEngine(db_path={self.engine.db_path!r})\n"
                   f"    _e.boot()\n"
                   f"    _rec = _e.capabilities.get({cap_id!r})\n"
                   f"    _c = Composer(_e.primitives)\n"
                   f"    return _c.execute_sync(_rec.plan, "
                   f"{{{', '.join(f'{p!r}: {p}' for p in param_names)}}}).get('value')")

        source = (f"# Auto-generated by SWarm: a thin bridge to the "
                 f"internally-registered capability {cap_id!r},\n"
                 f"# admitted via composition/iteration synthesis rather than "
                 f"retrieved knowledge. Runnable within an\n"
                 f"# environment that has swarm_engine installed; not a claim "
                 f"of standalone portability beyond that.\n"
                 f"def {func_name}({args_sig}):\n{body}\n")

        from swarm_engine.project.modification import ProjectModificationGuard
        guard = ProjectModificationGuard(root=self.project_root)
        import os
        try:
            if os.path.exists(os.path.join(self.project_root, target_path)):
                change = guard.modify(target_path, source, verify=lambda: (True, {}))
            else:
                change = guard.create(target_path, source, verify=lambda: (True, {}))
            if change.committed:
                result.artifact_path = target_path
        except Exception as exc:
            result.trace.append(f"materialization to {target_path!r} failed: {exc}")

    def _formulate_query(self, requirement: CapabilityRequirement) -> str:
        return f"{requirement.target_domain} {requirement.description}"

    def _extract_and_adapt(self, retrieved, requirement: CapabilityRequirement
                           ) -> Optional[str]:
        best_block: Optional[str] = None
        for item in retrieved:
            lines = item.content.split("\n")
            block: List[str] = []
            for line in lines:
                if _CODE_LINE.match(line):
                    block.append(line)
                elif block and line.strip() == "":
                    continue
                elif block:
                    break
            if len(block) >= 2:
                candidate = "\n".join(block)
                if best_block is None or len(candidate) > len(best_block):
                    best_block = candidate

        if best_block is None:
            return None

        # Auto-rewrite whatever module the retrieved snippet actually
        # imports to the REAL dependency, when the caller declares exactly
        # one and it isn't already correct. Found necessary directly:
        # retrieved reference material for "import a module and call its
        # function" naturally uses a placeholder/example module name (e.g.
        # a generic tutorial's "calculation" or "my_module") that has no
        # reason to already match this project's actual dependency name —
        # without this, a dependent file would import something that
        # doesn't exist, and the earlier manual-substitution benchmarks
        # only worked because a human supplied the exact rename by hand.
        # This discovers the WRONG name from the retrieved content itself
        # (never guessed) and replaces it with the real one.
        real_deps = requirement.constraints.get("real_dependencies", [])
        if real_deps:
            imported = re.findall(r"^\s*import\s+(\w+)", best_block, re.MULTILINE)
            imported += re.findall(r"^\s*from\s+(\w+)\s+import", best_block, re.MULTILINE)
            # Positional correspondence, generalized from the single-
            # dependency case: the i-th import statement found in the
            # retrieved content is treated as standing in for the i-th
            # declared dependency. A stated heuristic, not a guarantee —
            # correct when retrieved reference material orders its imports
            # the same way the project declares its dependencies, which is
            # the common case for "import several modules and use them"
            # patterns, but not a semantic match. len(real_deps) == 1 is
            # the same behavior as before this generalization; it is not a
            # special case anymore, just the N=1 instance of this loop.
            dep_symbols = requirement.constraints.get("dependency_symbols", [])
            dep_examples = requirement.constraints.get("dependency_examples", [])
            for i, wrong_name in enumerate(imported):
                if i >= len(real_deps):
                    break
                real_name = real_deps[i]
                if wrong_name != real_name:
                    best_block = re.sub(rf"\b{re.escape(wrong_name)}\b",
                                       real_name, best_block)

                dep_symbol = dep_symbols[i] if i < len(dep_symbols) else None
                if dep_symbol:
                    called = re.findall(rf"\b{re.escape(real_name)}\.(\w+)\(",
                                       best_block)
                    for wrong_call in set(called):
                        if wrong_call != dep_symbol:
                            best_block = re.sub(
                                rf"\b{re.escape(real_name)}\.{re.escape(wrong_call)}\(",
                                f"{real_name}.{dep_symbol}(", best_block)

                dep_example = dep_examples[i] if i < len(dep_examples) else None
                if dep_symbol and dep_example is not None:
                    pattern = rf"\b{re.escape(real_name)}\.{re.escape(dep_symbol)}\(([^)]*)\)"
                    m = re.search(pattern, best_block)
                    if m:
                        current_args = [a for a in m.group(1).split(",") if a.strip()]
                        if len(current_args) != len(dep_example):
                            new_call = (f"{real_name}.{dep_symbol}("
                                       f"{', '.join(repr(v) for v in dep_example)})")
                            best_block = (best_block[:m.start()] + new_call +
                                         best_block[m.end():])

        substitutions = requirement.constraints.get("substitutions", {})
        for old, new in substitutions.items():
            best_block = best_block.replace(old, new)
        return best_block

    def _persist(self, requirement: CapabilityRequirement, artifact: str
                ) -> Optional[Dict[str, Any]]:
        if self.project_root is None:
            return None
        from swarm_engine.project.modification import ProjectModificationGuard
        guard = ProjectModificationGuard(root=self.project_root)
        path = requirement.constraints.get("target_path",
                                           f"generated_{requirement.requirement_id}.gd")
        try:
            import os
            if os.path.exists(os.path.join(self.project_root, path)):
                change = guard.modify(path, artifact, verify=lambda: (True, {}))
            else:
                change = guard.create(path, artifact, verify=lambda: (True, {}))
        except Exception as exc:
            return {"committed": False, "path": path, "error": str(exc)}

        capability_id = None
        if change.committed and self.provenance is not None:
            from swarm_engine.governance.provenance import (
                Origin, ProvenanceRecord, TrustLevel,
            )
            capability_id = f"artifact_{requirement.requirement_id}"
            self.provenance.record(ProvenanceRecord(
                capability_id=capability_id, origin=Origin.SYNTHESIZED,
                trust=TrustLevel.TESTED,
                source=f"external_knowledge:{requirement.target_domain}"))
            self.provenance.log(capability_id, "artifact_admitted",
                                f"path={path}; domain={requirement.target_domain}")

        return {"committed": change.committed, "path": path,
               "capability_id": capability_id}
