"""
swarm_engine/forge/module.py

The ForgeModule your ARCHITECTURE.md / API_REFERENCE.md already specify:
  "ForgeModule - 3D asset generation" in Layer 6 (Tool/Module System),
  invoked via engine.submit_task(prompt, metadata={"module": "forge"}).

This replaces the standalone swarm_forge_generate_asset() function, which
never went through the engine, never used the prompt for anything beyond
a couple of keyword checks in critique_mesh(), and produced the same
mesh regardless of input.

Flow, matching the engine's governed lifecycle:
  1. CapabilitySynthesizer.synthesize(prompt) — reuse if this structural
     capability already exists, otherwise write and verify new code.
  2. Load + execute the synthesized generator (never anything unverified).
  3. Critique the result (existing quality checks, reused as-is).
  4. Log outcome to KnowledgeBase. On failure, attempt one bounded
     self-correction (bump scale/detail) and retry before giving up —
     this is the "self-improving" part: failures become data, not
     dead ends, and results are cached so the same request gets cheaper
     and more reliable every time it recurs.
"""
import copy

from swarm_engine.synthesis.synthesizer import CapabilitySynthesizer, SynthesisError
from swarm_engine.memory.knowledge_base import KnowledgeBase


def critique_mesh(prompt: str, mesh) -> dict:
    """Unchanged quality gate from the original notebook — kept as-is
    since it was already reasonable, just never fed real geometry."""
    assessment = {"pass": True, "feedback": []}
    prompt_lower = prompt.lower()

    if not mesh.is_watertight:
        assessment["pass"] = False
        assessment["feedback"].append("Critical: Mesh is not watertight.")

    if not mesh.is_winding_consistent:
        assessment["feedback"].append("Warning: Mesh winding is not consistent.")

    if mesh.volume < 0.001 and len(mesh.vertices) > 10:
        assessment["feedback"].append("Warning: Mesh volume is very small.")

    if "hero" in prompt_lower or "main character" in prompt_lower:
        if len(mesh.vertices) < 3000:
            assessment["feedback"].append("Suggestion: Hero character needs higher vertex count.")

    if any("Critical" in fb for fb in assessment["feedback"]):
        assessment["pass"] = False
    elif not assessment["feedback"]:
        assessment["feedback"].append("Initial quality checks passed.")

    return assessment


class ForgeModule:
    def __init__(self, db_path="swarm_engine.db"):
        self.kb = KnowledgeBase(db_path=db_path)
        self.synthesizer = CapabilitySynthesizer(knowledge_base=self.kb)

    def generate_3d_asset(self, prompt: str, output_file: str = "forge_asset.obj", max_retries: int = 1) -> dict:
        cap = self.synthesizer.synthesize(prompt)
        generate_fn = self.synthesizer.load(cap)

        spec = copy.deepcopy(cap.spec)
        attempt = 0
        result = None
        while True:
            mesh = generate_fn(spec)
            if isinstance(mesh, list):  # modular assemblies stay as parts
                import trimesh
                mesh_for_check = trimesh.util.concatenate(mesh)
            else:
                mesh_for_check = mesh

            critique = critique_mesh(prompt, mesh_for_check)
            self.kb.log_generation(cap.capability_id, prompt, cap.spec["body_plan"],
                                    critique["pass"], critique["feedback"])

            if critique["pass"] or attempt >= max_retries:
                self.kb.record_use(cap.capability_id, success=critique["pass"])
                if critique["pass"]:
                    if isinstance(mesh, list):
                        import trimesh
                        trimesh.util.concatenate(mesh).export(output_file)
                    else:
                        mesh.export(output_file)
                result = {
                    "status": "passed" if critique["pass"] else "failed",
                    "output_file": output_file if critique["pass"] else None,
                    "capability_id": cap.capability_id,
                    "capability_reused": not cap.is_new or attempt > 0,
                    "spec": spec,
                    "feedback": critique["feedback"],
                    "attempts": attempt + 1,
                }
                break

            # bounded self-correction: not watertight / too small -> bump scale & retry once
            spec["size"] = "medium" if spec.get("size") == "small" else "large"
            attempt += 1

        return result

    def stats(self) -> dict:
        """Exposes how much the engine has actually learned — growing
        capability count and per-attempt outcomes, for the admin dashboard."""
        return self.kb.stats()
