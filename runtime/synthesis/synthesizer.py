"""
swarm_engine/synthesis/synthesizer.py

Replaces the old stub (which returned a fixed boilerplate string for every
capability_id) and the old forge function (which returned the same box+
sphere mesh for every prompt). This version does two things the old code
never did:

1. Parses the prompt into a structural spec (body plan, limb count, scale,
   features) instead of ignoring it.
2. COMPOSES new Python source from the primitive library based on that
   spec — different structures produce genuinely different generated code,
   not just different parameters plugged into one template.

No LLM client, no external API. Synthesis happens locally by combining
known-safe building blocks according to rules. This is deliberately
inspectable: `synth.synthesize(prompt).code` is real, readable Python you
can print before it's ever executed.

Every synthesized capability is:
  - hashed to a capability_id from its *structural* spec, so semantically
    identical requests ("robot warrior" vs "warrior robot") reuse the same
    capability instead of re-synthesizing
  - checked against the KnowledgeBase before generating anything new
  - run through ASTVerifier before it is ever exec'd
  - persisted, so the engine's capability set only grows
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from swarm_engine.verification.pipeline import VerificationPipeline

# --- prompt -> structural spec ------------------------------------------

BODY_PLAN_KEYWORDS = {
    "spider": ["spider", "arachnid"],
    "quadruped": ["dog", "quadruped", "animal", "creature", "four-legged", "beast"],
    "vehicle": ["car", "vehicle", "rover", "truck", "cart"],
    "flyer": ["bird", "drone", "flyer", "flying"],
    "humanoid": ["hero", "character", "humanoid", "warrior", "robot", "bot", "person", "figure"],
}
FEATURE_KEYWORDS = {
    "wheels": ["wheel", "wheeled", "rover", "car", "cart"],
    "wings": ["wing", "fly", "flying", "bird", "drone"],
    "modular": ["modular", "segmented"],
    "armor": ["armor", "armored", "heavy", "robust"],
}
SIZE_KEYWORDS = {
    "large": ["hero", "large", "big", "main character", "boss"],
    "small": ["small", "tiny", "compact", "mini"],
}


def parse_prompt(prompt: str) -> dict:
    p = prompt.lower()

    body_plan = "humanoid"  # sensible default, matches the docs' "hero character" examples
    for plan, kws in BODY_PLAN_KEYWORDS.items():
        if any(kw in p for kw in kws):
            body_plan = plan
            break

    features = sorted({feat for feat, kws in FEATURE_KEYWORDS.items() if any(kw in p for kw in kws)})

    size = "medium"
    for s, kws in SIZE_KEYWORDS.items():
        if any(kw in p for kw in kws):
            size = s
            break

    leg_match = re.search(r"(\d+)[\s-]*leg", p)
    if body_plan == "spider":
        limb_count = int(leg_match.group(1)) if leg_match else 8
    elif body_plan == "quadruped":
        limb_count = int(leg_match.group(1)) if leg_match else 4
    elif body_plan in ("humanoid",):
        limb_count = 2
    else:
        limb_count = 0

    return {
        "body_plan": body_plan,
        "features": features,
        "size": size,
        "limb_count": limb_count,
    }


def spec_hash(spec: dict) -> str:
    canonical = json.dumps(spec, sort_keys=True)
    return "cap_" + hashlib.sha256(canonical.encode()).hexdigest()[:16]


# --- spec -> synthesized source code -------------------------------------

_SIZE_SCALE = {"small": 0.6, "medium": 1.0, "large": 1.6}

_HEADER = '''import trimesh
import numpy as np
from swarm_engine.forge import primitives as p


def generate(spec):
    """Synthesized by CapabilitySynthesizer for body_plan={body_plan!r}."""
    scale = {scale}
    parts = []
'''

_BODY_HUMANOID = '''    parts.append(p.torso(width=0.5*scale, height=0.8*scale, depth=0.3*scale))
    parts.append(p.head(radius=0.25*scale, origin=(0, 0.55*scale, 0)))
    parts.append(p.limb(radius=0.07*scale, length=0.7*scale, origin=(-0.45*scale, 0.3*scale, 0)))
    parts.append(p.limb(radius=0.07*scale, length=0.7*scale, origin=(0.45*scale, 0.3*scale, 0)))
    parts.append(p.limb(radius=0.1*scale, length=0.8*scale, origin=(-0.2*scale, -0.9*scale, 0)))
    parts.append(p.limb(radius=0.1*scale, length=0.8*scale, origin=(0.2*scale, -0.9*scale, 0)))
'''

_BODY_QUADRUPED = '''    parts.append(p.torso(width=0.9*scale, height=0.35*scale, depth=0.4*scale))
    parts.append(p.head(radius=0.18*scale, origin=(0.55*scale, 0.05*scale, 0)))
    parts += p.radial_legs(count={limb_count}, radius=0.05*scale, length=0.5*scale,
                            attach_radius=0.4*scale, z=-0.3*scale, thickness=0.05*scale)
'''

_BODY_SPIDER = '''    parts.append(p.shell(radius=0.3*scale, origin=(0, 0, 0), squash=0.7))
    parts.append(p.head(radius=0.12*scale, origin=(0.35*scale, 0, 0)))
    parts += p.radial_legs(count={limb_count}, radius=0.35*scale, length=0.5*scale,
                            attach_radius=0.25*scale, z=0, thickness=0.03*scale)
'''

_BODY_VEHICLE = '''    parts.append(p.torso(width=1.0*scale, height=0.3*scale, depth=0.5*scale))
    parts.append(p.torso(width=0.5*scale, height=0.25*scale, depth=0.45*scale, origin=(0.1*scale, 0.28*scale, 0)))
'''

_BODY_FLYER = '''    parts.append(p.shell(radius=0.2*scale, origin=(0, 0, 0)))
    parts.append(p.wing(span=0.6*scale, chord=0.2*scale, origin=(0, 0, 0.15*scale), sweep_deg=15))
    parts.append(p.wing(span=0.6*scale, chord=0.2*scale, origin=(0, 0, -0.15*scale), sweep_deg=-15))
'''

_BODY_TEMPLATES = {
    "humanoid": _BODY_HUMANOID,
    "quadruped": _BODY_QUADRUPED,
    "spider": _BODY_SPIDER,
    "vehicle": _BODY_VEHICLE,
    "flyer": _BODY_FLYER,
}

_FEATURE_WHEELS = '''    for i, x in enumerate([-0.35*scale, 0.35*scale]):
        for y in [-0.2*scale, 0.2*scale]:
            parts.append(p.wheel(radius=0.15*scale, thickness=0.08*scale, origin=(x, -0.15*scale, y)))
'''
_FEATURE_WINGS = '''    parts.append(p.wing(span=0.5*scale, chord=0.18*scale, origin=(0, 0.4*scale, 0.2*scale), sweep_deg=10))
    parts.append(p.wing(span=0.5*scale, chord=0.18*scale, origin=(0, 0.4*scale, -0.2*scale), sweep_deg=-10))
'''
_FEATURE_ARMOR = '''    parts.append(p.shell(radius=0.32*scale, origin=(0, 0.1*scale, 0), squash=1.3))
'''

_FEATURE_TEMPLATES = {
    "wheels": _FEATURE_WHEELS,
    "wings": _FEATURE_WINGS,
    "armor": _FEATURE_ARMOR,
    # "modular" is handled structurally (parts stay unmerged) not as an add-on snippet
}

_FOOTER_MERGE = '''    mesh = trimesh.util.concatenate(parts)
    return mesh
'''
_FOOTER_MODULAR = '''    # modular: return an assembly of distinct components rather than one fused mesh
    return trimesh.util.concatenate(parts) if len(parts) == 1 else parts
'''


@dataclass
class SynthesizedCapability:
    capability_id: str
    spec: dict
    code: str
    is_new: bool
    generate_fn: Optional[object] = field(default=None, repr=False)


class SynthesisError(Exception):
    pass


class CapabilitySynthesizer:
    """Just-in-time capability generation. Composes real source code from
    the primitive library based on a parsed structural spec — does not
    call out to an external model, and does not fall back to a fixed
    template regardless of input."""

    def __init__(self, knowledge_base=None):
        self.kb = knowledge_base
        self.verifier = VerificationPipeline()
        self._local_registry = {}  # in-process cache: capability_id -> code

    def synthesize(self, prompt: str) -> SynthesizedCapability:
        spec = parse_prompt(prompt)
        cap_id = spec_hash(spec)

        cached = self._lookup(cap_id)
        if cached is not None:
            return SynthesizedCapability(cap_id, spec, cached, is_new=False)

        code = self._compose_source(spec)

        audit = self.verifier.verify_code(code)
        if audit["status"] != "passed":
            raise SynthesisError(f"Synthesized code failed verification: {audit['violations']}")

        self._local_registry[cap_id] = code
        if self.kb is not None:
            self.kb.store_capability(cap_id, spec, code)

        return SynthesizedCapability(cap_id, spec, code, is_new=True)

    def _lookup(self, cap_id: str):
        if cap_id in self._local_registry:
            return self._local_registry[cap_id]
        if self.kb is not None:
            row = self.kb.get_capability(cap_id)
            if row is not None:
                self._local_registry[cap_id] = row["code"]
                return row["code"]
        return None

    def _compose_source(self, spec: dict) -> str:
        body_plan = spec["body_plan"]
        scale = _SIZE_SCALE[spec["size"]]

        parts = [_HEADER.format(body_plan=body_plan, scale=scale)]
        parts.append(_BODY_TEMPLATES[body_plan].format(limb_count=spec["limb_count"]))
        for feature in spec["features"]:
            if feature in _FEATURE_TEMPLATES:
                parts.append(_FEATURE_TEMPLATES[feature])
        parts.append(_FOOTER_MODULAR if "modular" in spec["features"] else _FOOTER_MERGE)
        return "".join(parts)

    def load(self, capability: SynthesizedCapability):
        """Executes the verified source in an isolated namespace and
        returns the callable generate(spec) -> trimesh.Trimesh."""
        namespace = {}
        exec(compile(capability.code, f"<synthesized:{capability.capability_id}>", "exec"), namespace)
        capability.generate_fn = namespace["generate"]
        return capability.generate_fn
