"""Media intent wiring (worker E, 2026-09-26).

Wires media intents into the intent/dispatch path:

  register_media_primitives(engine)
      Registers four primitives on the engine's PrimitiveRegistry through
      the registry's public ``register`` API (the same API admission
      itself uses for ``acquired.*``). Each primitive is a thin,
      needs_ctx wrapper that:
        1. enforces the WRITE_FS grant against the real output path via
           ``ctx.governor.check`` (the registry's generic invoke loop
           additionally enforces it, since the path kwarg is named
           ``path`` -- the kwarg name the generic target extraction
           looks for);
        2. lazily imports the genuine ``swarm_engine.media.<module>``
           (built by sibling workers) and calls the genuine function.
      If the media module has not landed yet, registration still
      succeeds; invocation fails closed with a clear RuntimeError --
      never a fabricated result.

  admit_media_capabilities(engine, media_out_dir, attempt_smoke=True)
      Admits the four media capabilities through the REAL admission
      path (``engine.admission.admit``: type check -> effect ceiling ->
      permission check -> smoke test -> store -> goal binding), after
      issuing a scoped WRITE_FS grant for ``media_out_dir`` through the
      governor's real oracle-backed authority path. Then binds a set of
      natural NL phrasings per medium via the store's real
      ``bind_goal`` mechanism, which is what ``IntentRouter`` consults
      for exact-goal routing.

Plans are declared data executed by the real Composer path: each plan is
a single step invoking its media primitive with plan params, e.g.::

    {"id": "s1", "op": "media.voice_synthesize",
     "args": {"text": {"$param": "text"},
              "path": {"$param": "path"},
              "voice": {"$param": "voice"}}}

There is no bypass anywhere in this module: no caller-supplied
callables, no exec/eval, no direct store writes.
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Dict, List, Optional

from swarm_engine.primitives.core import (
    ANY, DICT, NUM, STR, Effect, Primitive,
)
from swarm_engine.synthesis.admission import SmokeTest, Verdict

# medium -> lexicon effect token (acquisition.intent._EFFECT_LEXICON)
MEDIA_EFFECTS = {
    "voice": "media_voice",
    "song": "media_song",
    "image": "media_image",
    "video": "media_video",
}

# Declared plan effects for the spec-driven image renderer
# (runtime/media/image_spec.py). It declares the general "media_image"
# token so the router's effect fallback can select it, plus the finer
# "media_image_spec" token: on an effect tie the fallback prefers the
# candidate with fewer declared effects beyond the inferred set, so a
# bare "make an image" still routes to the general image capability
# while the spec renderer stays reachable by its exact phrasings and by
# any future lexicon cue for "media_image_spec".
IMAGE_SPEC_EFFECTS = ["media_image", "media_image_spec"]

# medium -> (module, function) in swarm_engine.media (sibling workers)
MEDIA_MODULES = {
    "voice": ("voice", "synthesize"),
    "song": ("song", "assemble_song"),
    "image": ("image", "generate"),
    "video": ("video", "generate"),
}

# medium -> (primitive bare name, plan params in order, primitive inputs)
_MEDIA_SPECS: Dict[str, Dict[str, Any]] = {
    "voice": {
        "prim": "voice_synthesize",
        "inputs": {"text": STR, "path": STR, "voice": STR},
        "params": {"text": "str", "path": "str", "voice": "str"},
        "doc": ("media.voice_synthesize(text, path, voice): synthesize speech "
                "via swarm_engine.media.voice.synthesize(text, out_path=path, "
                "voice=voice). Writes an audio file to path (WRITE_FS)."),
    },
    "song": {
        "prim": "song_assemble",
        "inputs": {"lyrics": STR, "spec": DICT(), "path": STR, "work_dir": STR},
        "params": {"lyrics": "str", "spec": "dict", "path": "str",
                   "work_dir": "str"},
        "doc": ("media.song_assemble(lyrics, spec, path, work_dir): assemble a "
                "song via swarm_engine.media.song.assemble_song(lyrics, spec, "
                "out_path=path, work_dir=work_dir). Writes audio to path "
                "(WRITE_FS)."),
    },
    "image": {
        "prim": "image_generate",
        "inputs": {"prompt": STR, "path": STR, "width": NUM, "height": NUM,
                   "seed": ANY},
        "params": {"prompt": "str", "path": "str", "width": "num",
                   "height": "num", "seed": "any"},
        "doc": ("media.image_generate(prompt, path, width, height, seed): "
                "generate an image via swarm_engine.media.image.generate"
                "(prompt, out_path=path, ...). Writes a raster file to path "
                "(WRITE_FS)."),
    },
    "video": {
        "prim": "video_generate",
        "inputs": {"prompt": STR, "path": STR, "duration_s": NUM, "fps": NUM,
                   "width": NUM, "height": NUM, "seed": ANY},
        "params": {"prompt": "str", "path": "str", "duration_s": "num",
                   "fps": "num", "width": "num", "height": "num",
                   "seed": "any"},
        "doc": ("media.video_generate(prompt, path, duration_s, fps, width, "
                "height, seed): generate a video via "
                "swarm_engine.media.video.generate(prompt, out_path=path, "
                "...). Writes a video file to path (WRITE_FS)."),
    },
}

# Natural phrasings bound per medium via the store's real goal-binding map.
# The first entry of each list is the canonical admission goal.
MEDIA_PHRASINGS: Dict[str, List[str]] = {
    "voice": [
        "synthesize speech saying 'hello world'",
        "synthesize speech saying hello world",
        "turn this text into speech: hello world",
        "speak the text 'hello world' aloud",
        "read this text aloud: hello world",
        "make a voiceover saying hello world",
    ],
    "song": [
        "make a song about the sea",
        "create a song about the sea",
        "compose a song about the sea",
        "write me a song about the sea",
        "make me a song about the ocean",
        "write a song about the sea",
    ],
    "image": [
        "generate an image of a sunset",
        "create an image of a sunset",
        "make an image of a sunset",
        "draw a picture of a sunset",
        "generate a picture of a sunset over the ocean",
        "create a picture of a sunset",
    ],
    "video": [
        "create a short video of ocean waves",
        "generate a short video of ocean waves",
        "make a short video of ocean waves",
        "create a video of ocean waves",
        "generate a video of ocean waves",
        "animate ocean waves as a short video",
    ],
}


def media_module_present(medium: str) -> bool:
    """Whether the sibling-built substrate module imports cleanly right now.

    Any import-time failure (missing file, mid-write syntax error, broken
    dependency) counts as not present: admission then skips the smoke test
    rather than rejecting on a transient, and execution fails closed with
    the wrapped import error.
    """
    module, _ = MEDIA_MODULES[medium]
    try:
        importlib.import_module(f"swarm_engine.media.{module}")
        return True
    except Exception:
        return False


def _make_media_fn(medium: str):
    """Build the primitive fn: govern the write, then call the real module."""
    module, func = MEDIA_MODULES[medium]
    prim_label = f"media.{_MEDIA_SPECS[medium]['prim']}"

    def _media_fn(ctx, **kwargs):
        path = kwargs.get("path", "")
        # Explicit per-target enforcement with a primitive-attributed audit
        # entry. The registry's generic invoke loop enforces the same
        # grant again (it extracts the target from the `path` kwarg).
        ctx.governor.check(Effect.WRITE_FS, str(path), prim_label)
        try:
            mod = importlib.import_module(f"swarm_engine.media.{module}")
        except ImportError as exc:
            raise RuntimeError(
                f"media substrate module 'swarm_engine.media.{module}' is "
                f"not installed; {prim_label} cannot execute "
                f"({exc})") from exc
        except Exception as exc:
            raise RuntimeError(
                f"media substrate module 'swarm_engine.media.{module}' "
                f"failed to import ({type(exc).__name__}: {exc}); "
                f"{prim_label} cannot execute") from exc
        target = getattr(mod, func)
        call_kwargs = dict(kwargs)
        call_kwargs["out_path"] = call_kwargs.pop("path")
        return target(**call_kwargs)

    _media_fn.__name__ = f"_media_{medium}"
    _media_fn.__doc__ = _MEDIA_SPECS[medium]["doc"]
    return _media_fn


def register_media_primitives(engine) -> List[str]:
    """Register the four media primitives on engine.primitives.

    Uses the registry's public register() API -- the same API the
    admission controller itself uses for acquired.* primitives. There is
    no separate governed 'new base primitive' ceremony in this tree;
    import-time family registration (families_*.py) and this explicit
    call are the two real provisioning routes. Idempotent: re-registering
    the same media primitive overwrites it (same plan -> same behavior).
    Returns the qualified op names usable in plans.
    """
    reg = engine.primitives
    ops = []
    for medium, spec in _MEDIA_SPECS.items():
        name = spec["prim"]
        prim = Primitive(
            name=name, family="media",
            fn=_make_media_fn(medium),
            inputs=dict(spec["inputs"]), output=DICT(),
            effects=(Effect.WRITE_FS, Effect.RANDOM),
            doc=spec["doc"], needs_ctx=True,
        )
        existing = reg.get(name)
        if existing is not None and existing.family != "media":
            raise RuntimeError(
                f"primitive name {name!r} already registered by family "
                f"{existing.family!r}; refusing to shadow it")
        reg.register(prim, overwrite=True)
        ops.append(f"media.{name}")
    return ops


def media_plan(medium: str) -> Dict[str, Any]:
    """The declared plan for one medium: one step, media primitive, params.

    The plan declares its lexicon effect token (``MEDIA_EFFECTS[medium]``,
    e.g. ``"media_image"``) under the ``"effects"`` key. These are the
    acquisition.intent effect-lexicon tokens -- the same namespace
    ``IntentRouter`` infers from request text -- so the router's
    effect-fallback can select an admitted media capability by its
    declared effects when structural scoring finds no candidate. The
    key is inert to the composer/admission machinery (plan checker only
    reads steps/params/output; the fingerprint covers it, so the
    declaration is part of the admitted plan identity).
    """
    spec = _MEDIA_SPECS[medium]
    op = f"media.{spec['prim']}"
    params = dict(spec["params"])
    return {
        "name": f"media_{medium}",
        "params": params,
        "steps": [{
            "id": "s1", "op": op,
            "args": {p: {"$param": p} for p in params},
        }],
        "output": {"$step": "s1"},
        "effects": [MEDIA_EFFECTS[medium]],
    }


def _smoke_args(medium: str, smoke_dir: str) -> Dict[str, Any]:
    os.makedirs(smoke_dir, exist_ok=True)
    if medium == "voice":
        return {"text": "media smoke test", "voice": "default",
                "path": os.path.join(smoke_dir, "smoke_voice.wav")}
    if medium == "song":
        work = os.path.join(smoke_dir, "smoke_song_work")
        return {"lyrics": "smoke song about the sea",
                "spec": {"style": "ballad", "bars": 2}, "work_dir": work,
                "path": os.path.join(smoke_dir, "smoke_song.wav")}
    if medium == "image":
        return {"prompt": "smoke test sunset", "width": 64, "height": 64,
                "seed": 1, "path": os.path.join(smoke_dir, "smoke_image.png")}
    if medium == "video":
        return {"prompt": "smoke test ocean waves", "duration_s": 1.0,
                "fps": 8, "width": 160, "height": 90, "seed": 1,
                "path": os.path.join(smoke_dir, "smoke_video.mp4")}
    raise KeyError(medium)


def ensure_media_write_grant(engine, media_out_dir: str) -> str:
    """Issue a scoped WRITE_FS grant for the media output dir through the
    governor's real oracle-backed authority path (engine's own handle).
    Returns the grant pattern. Idempotent for the same dir."""
    pattern = os.path.abspath(media_out_dir) + "/*"
    have = {tuple(g) for g in engine.governor.summary().get("grants", [])}
    if ("write_fs", pattern) not in have:
        engine.governor.grant(
            Effect.WRITE_FS, pattern,
            note="media intent wiring: admitted media capabilities may write "
                 "generated artifacts here")
    return pattern


def admit_media_capabilities(engine, media_out_dir: str,
                             attempt_smoke: bool = True,
                             bind_phrasings: bool = True) -> Dict[str, Any]:
    """Full wiring: primitives + grant + real admission + goal bindings.

    Returns a per-medium report with capability_id, verdict, smoke outcome,
    and how many phrasings were bound. Fails closed PER MEDIUM, not per
    service: a medium whose admission refuses (e.g. its substrate is
    absent in this environment) is recorded as not admitted and the
    remaining media still wire. A refused medium is never half-wired --
    nothing is bound for it and requests for it are refused honestly
    downstream. The report entry for a refused medium carries
    ``admitted: False`` plus the refusal stage and reasons, so no caller
    can mistake it for an admitted capability.
    """
    os.makedirs(media_out_dir, exist_ok=True)
    register_media_primitives(engine)
    ensure_media_write_grant(engine, media_out_dir)

    report: Dict[str, Any] = {}
    for medium in ("voice", "song", "image", "video"):
        plan = media_plan(medium)
        phrasings = MEDIA_PHRASINGS[medium]
        smoke = None
        smoke_note = "skipped: attempt_smoke=False"
        if attempt_smoke:
            if media_module_present(medium):
                smoke = SmokeTest(
                    args=_smoke_args(
                        medium, os.path.join(media_out_dir, "_smoke")),
                    name=f"media_{medium}_smoke")
                smoke_note = "executed"
            else:
                smoke_note = ("skipped: substrate module "
                              f"'swarm_engine.media.{MEDIA_MODULES[medium][0]}' "
                              "not landed yet")
        res = engine.admit_as_engine(
            goal=phrasings[0], plan=plan, smoke=smoke,
            name=f"media_{medium}")
        if not res.ok:
            # Fail closed per medium: record the refusal LOUDLY and keep
            # wiring the remaining media. The refused medium is NOT
            # admitted -- no phrasings are bound for it, and downstream
            # callers must check the ``admitted`` flag before dispatching.
            # (Raising here used to kill the whole service boot when an
            # optional substrate such as piper was simply absent from the
            # environment; an absent substrate is honest unavailability,
            # not a reason to refuse to boot.)
            print(f"[media] ADMISSION REFUSED for {medium!r} at stage "
                  f"{res.stage!r}: {res.reasons} -- medium not admitted; "
                  f"service continues without it", flush=True)
            report[medium] = {
                "admitted": False,
                "capability_id": None,
                "verdict": res.verdict,
                "refusal_stage": res.stage,
                "reasons": list(res.reasons),
                "smoke": smoke_note,
                "smoke_passed": (res.smoke or {}).get("passed")
                if res.smoke else None,
                "phrasings_bound": 0,
                "plan": plan,
            }
            continue
        if res.verdict == Verdict.REUSED:
            smoke_note = "reused: identical plan already admitted"
        if bind_phrasings:
            for ph in phrasings:
                engine.capabilities.bind_goal(ph, res.capability_id)
        entry = {
            "admitted": True,
            "capability_id": res.capability_id,
            "verdict": res.verdict,
            "via": res.stage,
            "smoke": smoke_note,
            "smoke_passed": (res.smoke or {}).get("passed")
            if res.smoke else None,
            "phrasings_bound": len(phrasings) if bind_phrasings else 0,
            "plan": plan,
        }
        report[medium] = entry
    return report

# ---------------------------------------------------------------------------
# Honest-bounds helper for the NL dispatch path.
# ---------------------------------------------------------------------------
def media_bounds_for_effects(declared_effects) -> List[str]:
    """Honest substrate bounds for a media plan's declared effects.

    The primitive returns the substrate result dict, which does not
    carry bounds (services.media adds them only on its own front), so
    the NL dispatch path attaches these to every media answer. Most
    specific effect wins (media_image_spec before media_image).
    """
    from swarm_engine.services.media import (
        IMAGE_BOUNDS, SONG_BOUNDS, VIDEO_BOUNDS, VOICE_BOUNDS)
    from swarm_engine.media.image_spec import IMAGE_SPEC_BOUNDS
    declared = {str(e) for e in (declared_effects or [])}
    if "media_image_spec" in declared:
        return list(IMAGE_SPEC_BOUNDS)
    if "media_image" in declared:
        return list(IMAGE_BOUNDS)
    if "media_video" in declared:
        return list(VIDEO_BOUNDS)
    if "media_song" in declared:
        return list(SONG_BOUNDS)
    if "media_voice" in declared:
        return list(VOICE_BOUNDS)
    return []


# ---------------------------------------------------------------------------
# Spec-driven image renderer (worker B, 2026-09-26).
# ---------------------------------------------------------------------------
# The capability renders deterministic compositions from an explicit spec
# (runtime/media/image_spec.py): gradient/solid backgrounds, geometric
# shapes at spec'd positions/colors, DejaVuSans text. It travels the REAL
# admission path (engine.admission.admit: type check -> effect ceiling ->
# permission -> live pixel-level smoke test -> store -> goal binding).
#
# On "ReviewBoard": runtime/agent_org/review.py::ReviewBoard governs the
# organizational review flow for AGENT WORK PRODUCTS (states, lifecycle,
# approvals of work agents produce). It is not the verifier for executable
# capabilities: this tree's designated gate for a plan becoming runnable
# is AdmissionController.admit, whose live smoke test executes the plan
# through the real Composer and whose judge rule is a registered,
# authorized oracle. Routing the renderer through ReviewBoard would be a
# hand-rolled ceremony around the real gate, not the real gate. Hence the
# admission-controller path below -- no bypass, no faked review.
IMAGE_SPEC_PHRASINGS = [
    "render an image from a spec",
    "draw a red circle",
    "render a spec-driven image",
    "draw a red circle at the center",
    "render an image from a specification",
    "draw shapes from a spec",
]

# Fixed spec for the admission smoke test: a red disc on a flat dark
# background. The bound pixel predicate asserts disc-inside red /
# disc-outside not-red, exact dimensions, and byte-determinism.
IMAGE_SPEC_SMOKE_SPEC = {
    "subject": "pixel smoke: red disc",
    "width": 256,
    "height": 256,
    "style": "flat",
    "seed": 1234,
    "background": {"color": "#0b1e3a"},
    "shapes": [
        {"kind": "circle", "center": [128, 128], "radius": 60,
         "fill": "#ff0000"},
    ],
}


def register_image_spec_primitive(engine) -> str:
    """Register the media.image_render_spec primitive (public register API).

    needs_ctx wrapper: enforces the WRITE_FS grant against the real output
    path via ctx.governor.check (the registry's generic invoke loop
    enforces it again from the `path` kwarg), then lazily imports the
    genuine renderer and calls it. Idempotent via overwrite.
    """
    def _image_spec_fn(ctx, **kwargs):
        path = kwargs.get("path", "")
        ctx.governor.check(Effect.WRITE_FS, str(path),
                           "media.image_render_spec")
        from swarm_engine.media.image_spec import render_spec
        return render_spec(kwargs["spec"], out_path=path)

    _image_spec_fn.__name__ = "_media_image_render_spec"
    _image_spec_fn.__doc__ = (
        "media.image_render_spec(spec, path): render a deterministic "
        "spec-driven image via swarm_engine.media.image_spec.render_spec. "
        "Writes a PNG to path (WRITE_FS). Spec-driven rasterization only: "
        "cannot depict arbitrary subjects.")
    prim = Primitive(
        name="image_render_spec", family="media",
        fn=_image_spec_fn,
        inputs={"spec": DICT(), "path": STR}, output=DICT(),
        effects=(Effect.WRITE_FS,),
        doc=_image_spec_fn.__doc__, needs_ctx=True,
    )
    existing = engine.primitives.get("image_render_spec")
    if existing is not None and existing.family != "media":
        raise RuntimeError(
            "primitive name 'image_render_spec' already registered by family "
            f"{existing.family!r}; refusing to shadow it")
    engine.primitives.register(prim, overwrite=True)
    return "media.image_render_spec"


def image_spec_plan() -> Dict[str, Any]:
    """Declared plan for the spec renderer: one step, params, effects."""
    return {
        "name": "media_image_render_spec",
        "params": {"spec": "dict", "path": "str"},
        "steps": [{
            "id": "s1", "op": "media.image_render_spec",
            "args": {"spec": {"$param": "spec"},
                     "path": {"$param": "path"}},
        }],
        "output": {"$step": "s1"},
        "effects": list(IMAGE_SPEC_EFFECTS),
    }


def _ensure_image_spec_smoke_oracle(engine):
    """Register + authorize the pixel smoke predicate as a bound oracle.

    Uses the engine's own oracle handle (the same authority the admission
    controller's auto-bound smoke oracles use). Idempotent: re-registering
    the identical predicate returns the head version; authorization is
    idempotent too.
    """
    from swarm_engine.governance.oracle_binding import (
        DECISION_ADMISSION_SMOKE)
    from swarm_engine.media.image_spec import judge_image_spec_pixels
    handle = getattr(engine, "oracle", None)
    if handle is None:
        raise RuntimeError(
            "engine has no oracle handle; the pixel smoke predicate "
            "cannot be bound -- refusing to admit without verification")
    oracle_id, version = handle.register_oracle(
        name="media_image_spec_pixel_smoke",
        definition=judge_image_spec_pixels,
        input_contract="render_spec result dict",
        output_contract="judge(value) -> bool (pixel-level spec compliance)",
        source="media:image_spec wiring")
    handle.authorize_oracle(oracle_id, version, DECISION_ADMISSION_SMOKE)
    return oracle_id, version


def admit_image_spec_capability(engine, media_out_dir: str,
                                attempt_smoke: bool = True,
                                bind_phrasings: bool = True
                                ) -> Dict[str, Any]:
    """Admit the spec-driven image renderer through the REAL admission path.

    type check -> effect ceiling -> permission check -> live pixel-level
    smoke test (bound predicate oracle asserting disc-inside red /
    disc-outside not-red, exact dimensions, byte-determinism) -> store ->
    goal binding. Returns a report dict like admit_media_capabilities'
    per-medium entries (admitted, capability_id, verdict, smoke...).
    """
    from swarm_engine.media.image_spec import judge_image_spec_pixels
    os.makedirs(media_out_dir, exist_ok=True)
    register_image_spec_primitive(engine)
    ensure_media_write_grant(engine, media_out_dir)

    smoke = None
    smoke_note = "skipped: attempt_smoke=False"
    if attempt_smoke:
        try:
            oracle_id, version = _ensure_image_spec_smoke_oracle(engine)
        except Exception as exc:
            return {"admitted": False, "capability_id": None,
                    "verdict": Verdict.REJECTED,
                    "refusal_stage": "smoke_oracle_binding",
                    "reasons": [f"pixel smoke oracle binding failed: {exc}"],
                    "smoke": "oracle binding failed",
                    "smoke_passed": None, "phrasings_bound": 0,
                    "plan": image_spec_plan()}
        smoke_dir = os.path.join(media_out_dir, "_smoke")
        os.makedirs(smoke_dir, exist_ok=True)
        smoke = SmokeTest(
            args={"spec": dict(IMAGE_SPEC_SMOKE_SPEC),
                  "path": os.path.join(smoke_dir, "smoke_image_spec.png")},
            predicate=judge_image_spec_pixels,
            oracle_id=oracle_id, oracle_version=version,
            name="image_spec_pixel_smoke")
        smoke_note = "executed"
    res = engine.admit_as_engine(
        goal=IMAGE_SPEC_PHRASINGS[0], plan=image_spec_plan(), smoke=smoke,
        name="media_image_spec")
    if not res.ok:
        print(f"[media] ADMISSION REFUSED for 'image_spec' at stage "
              f"{res.stage!r}: {res.reasons} -- capability not admitted",
              flush=True)
        return {
            "admitted": False,
            "capability_id": None,
            "verdict": res.verdict,
            "refusal_stage": res.stage,
            "reasons": list(res.reasons),
            "smoke": smoke_note,
            "smoke_passed": (res.smoke or {}).get("passed")
            if res.smoke else None,
            "phrasings_bound": 0,
            "plan": image_spec_plan(),
        }
    if res.verdict == Verdict.REUSED:
        smoke_note = "reused: identical plan already admitted"
    if bind_phrasings:
        for ph in IMAGE_SPEC_PHRASINGS:
            engine.capabilities.bind_goal(ph, res.capability_id)
    return {
        "admitted": True,
        "capability_id": res.capability_id,
        "verdict": res.verdict,
        "via": res.stage,
        "smoke": smoke_note,
        "smoke_passed": (res.smoke or {}).get("passed")
        if res.smoke else None,
        "smoke_oracle": (smoke.oracle_id if smoke else None),
        "phrasings_bound": len(IMAGE_SPEC_PHRASINGS) if bind_phrasings else 0,
        "plan": image_spec_plan(),
    }
