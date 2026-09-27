"""Media intent pickup (worker E) tests.

Causal chain under test (all real, no simulation):
  lexicon (acquisition.intent.infer_required_effects)
    -> media primitives registered on engine.primitives (public register API)
    -> four capabilities admitted via engine.admission.admit (real path:
       type check -> effect ceiling -> permission check -> store -> goal
       bindings; smoke test executed iff the substrate module has landed)
    -> IntentRouter.route (genuine router; exact-goal bindings)
    -> NLToolDispatcher.dispatch (re-verify -> arg validation -> real
       Composer execute_sync -> primitive fn -> genuine
       swarm_engine.media.* module)

If a substrate module has not landed yet, routing assertions still run
(they do not need the module); execution assertions run as far as the
real path goes and document exactly where it stops (execution_failed
naming the missing module).

Adversarial battery: non-media requests never route to media; unknown /
ambiguous / empty / oversized intents refuse fail-closed; quarantined
media capability stops routing; out-of-grant-scope output path is
denied by the governor; bad args refused; refused dispatches leave no
record.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.acquisition.intent import infer_required_effects
from swarm_engine.core.engine import SwarmEngine
from swarm_engine.media.wiring import (
    MEDIA_EFFECTS,
    MEDIA_MODULES,
    MEDIA_PHRASINGS,
    IMAGE_SPEC_EFFECTS,
    IMAGE_SPEC_PHRASINGS,
    _smoke_args,
    admit_image_spec_capability,
    admit_media_capabilities,
    media_module_present,
    media_plan,
)
from swarm_engine.synthesis.admission import Verdict
from swarm_engine.synthesis.integrity import (
    effective_status,
    quarantine_everywhere,
)
from swarm_engine.synthesis.intent_router import IntentRouter
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher

SCRATCH = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))


def judge_smoke_value(value):
    """Refusal-aware smoke judge for this suite.

    The admission smoke judge's default ("non-null") scores ANY non-null
    value as passed -- including a refusal dict {"ok": False, ...} returned
    by a substrate that cannot deliver. This judge scores a refusal dict
    as REFUSED (False, "refusal-dict"), never as a pass; anything else
    keeps the suite's non-null semantics. Returns (passed, via).
    """
    if isinstance(value, dict) and value.get("ok") is False:
        return False, "refusal-dict"
    return value is not None, "non-null"


# The four required phrasings per medium, verbatim from the mission order.
REQUIRED_PHRASINGS = {
    "voice": [
        "synthesize speech saying 'hello world'",
        "synthesize speech saying hello world",
        "turn this text into speech: hello world",
        "speak the text 'hello world' aloud",
    ],
    "song": [
        "make a song about the sea",
        "create a song about the sea",
        "compose a song about the sea",
        "write me a song about the sea",
    ],
    "image": [
        "generate an image of a sunset",
        "create an image of a sunset",
        "make an image of a sunset",
        "draw a picture of a sunset",
    ],
    "video": [
        "create a short video of ocean waves",
        "generate a short video of ocean waves",
        "make a short video of ocean waves",
        "create a video of ocean waves",
    ],
}

EXPECTED_OPS = {
    "voice": "media.voice_synthesize",
    "song": "media.song_assemble",
    "image": "media.image_generate",
    "video": "media.video_generate",
}

# Real dispatch args per medium (paths under the granted output dir).
def dispatch_args(medium, out_dir):
    if medium == "voice":
        return {"text": "hello world", "voice": "default",
                "path": os.path.join(out_dir, "hello_world.wav")}
    if medium == "song":
        return {"lyrics": "a song about the sea",
                "spec": {"style": "ballad", "tempo_bpm": 90},
                "work_dir": os.path.join(out_dir, "song_work"),
                "path": os.path.join(out_dir, "sea_song.wav")}
    if medium == "image":
        return {"prompt": "a sunset over the ocean",
                "width": 256, "height": 256, "seed": 7,
                "path": os.path.join(out_dir, "sunset.png")}
    if medium == "video":
        return {"prompt": "ocean waves rolling",
                "duration_s": 2.0, "fps": 12,
                "width": 320, "height": 180, "seed": 7,
                "path": os.path.join(out_dir, "ocean_waves.mp4")}
    raise KeyError(medium)


def main():
    base = tempfile.mkdtemp(prefix="media_pickup_", dir=SCRATCH)
    db = os.path.join(base, "eng.db")
    media_out = os.path.join(base, "media_out")

    # The refusal-aware judge itself, scored directly: a refusal dict must
    # be refused, never passed.
    for val, want_pass, label in [
        ({"ok": False, "error": "refused: no piper"}, False, "refusal dict"),
        ({"ok": False}, False, "bare ok=False dict"),
        ({"ok": True, "out_path": "x.png"}, True, "success dict"),
        (None, False, "null value"),
        ("plain string value", True, "non-dict value"),
    ]:
        got, via = judge_smoke_value(val)
        check(f"judge: {label} -> {'pass' if want_pass else 'refused'}",
              got is want_pass, f"via={via} value={val!r}")

    eng = SwarmEngine(db_path=db)

    # ---- 1. wire through the real helper ---------------------------------
    report = admit_media_capabilities(eng, media_out_dir=media_out)
    cap_ids = {}
    for medium in ("voice", "song", "image", "video"):
        entry = report[medium]
        if not entry.get("admitted"):
            # Honest refusal: the medium's substrate cannot deliver in this
            # environment (e.g. piper absent). Admission correctly refused
            # it -- assert the refusal is recorded with stage + reasons and
            # that nothing was bound for it. This is the expected outcome
            # here, not a failure.
            check(f"admit: {medium} honestly refused (not admitted)",
                  entry["capability_id"] is None
                  and entry["phrasings_bound"] == 0
                  and bool(entry.get("refusal_stage"))
                  and bool(entry.get("reasons")),
                  str(entry))
            print(f"   info: {medium}: refused at "
                  f"{entry.get('refusal_stage')}: {entry.get('reasons')}")
            continue
        cap_ids[medium] = entry["capability_id"]
        check(f"admit: {medium} ok via real admission path",
              entry["verdict"] in (Verdict.ADMITTED, Verdict.REUSED)
              and entry["capability_id"].startswith("cap_"),
              str(entry))
        check(f"admit: {medium} phrasings bound",
              entry["phrasings_bound"] == len(MEDIA_PHRASINGS[medium]),
              str(entry))
        print(f"   info: {medium}: smoke={entry['smoke']} "
              f"module_present={media_module_present(medium)}")
        if entry["smoke"] == "executed":
            # Refusal-aware re-score of the admission smoke. Admission's
            # default smoke judge ("non-null") scores ANY non-null value as
            # passed, so a substrate refusal dict would have been admitted
            # as a smoke pass. Re-run the genuine smoke through the real
            # Composer path with the same args admission used, and score
            # the value with the refusal-aware judge: a refusal dict is a
            # refusal (suite FAIL), never a pass.
            sargs = _smoke_args(medium, os.path.join(media_out, "_rescore"))
            run = eng.composer.execute_sync(
                media_plan(medium), sargs, skip_check=True)
            val = run.get("value")
            passed, via = judge_smoke_value(val)
            check(f"smoke: {medium} refusal-aware rescore not a refusal",
                  passed, f"via={via} value={str(val)[:300]}")
        else:
            print(f"   info: {medium}: smoke not executed "
                  f"({entry['smoke']}); refusal-aware rescore skipped")

    # Partition for the environment-aware contract below: admission-
    # dependent checks run only for admitted media; refused media get
    # honest non-resolution / fail-closed checks instead.
    admitted = [m for m in ("voice", "song", "image", "video")
                if m in cap_ids]
    refused = [m for m in ("voice", "song", "image", "video")
               if m not in cap_ids]
    print(f"   info: admitted={admitted} refused={refused}")

    # grant is scoped and oracle-backed
    grants = eng.governor.summary().get("grants", [])
    check("governor: scoped WRITE_FS grant for media_out",
          ("write_fs", os.path.abspath(media_out) + "/*") in
          [(g[0], g[1]) for g in grants],
          str(grants))

    # primitives really registered, plan shapes as declared (admitted only)
    for medium in admitted:
        op = EXPECTED_OPS[medium]
        prim = eng.primitives.get(op)
        check(f"registry: {op} registered",
              prim is not None and prim.family == "media"
              and not prim.pure, str(prim))
        rec = eng.capabilities.get(cap_ids[medium])
        plan = rec.plan
        steps = plan.get("steps") or []
        check(f"plan: {medium} single media step",
              len(steps) == 1 and steps[0].get("op") == op
              and plan.get("output") == {"$step": "s1"},
              str(plan))
        check(f"plan: {medium} params match declared surface",
              set(plan.get("params") or {}) ==
              set(media_plan(medium)["params"]),
              str(plan.get("params")))
        check(f"record: {medium} declares write_fs effect",
              "write_fs" in (rec.effects or []), str(rec.effects))
        eff = effective_status(eng, cap_ids[medium])
        check(f"integrity: {medium} effectively active",
              eff.get("effective") == "active" and eff.get("consistent"),
              str(eff))

    # ---- 2. lexicon coverage ----------------------------------------------
    for medium, phrasings in REQUIRED_PHRASINGS.items():
        want = MEDIA_EFFECTS[medium]
        for ph in phrasings:
            fx = infer_required_effects(ph)
            other_media = [e for e in fx
                           if e.startswith("media_") and e != want]
            check(f"lexicon: {ph[:42]!r} -> {want}",
                  want in fx and not other_media, str(fx))

    # ---- 3. routing through the genuine IntentRouter -----------------------
    router = IntentRouter(eng)
    for medium, phrasings in REQUIRED_PHRASINGS.items():
        for ph in phrasings:
            rt = router.route(ph)
            if medium in admitted:
                check(f"route: {ph[:44]!r} -> {medium}",
                      rt.ok and rt.via == "exact_goal"
                      and rt.capability_id == cap_ids[medium],
                      str(rt.as_dict()))
                check(f"route: {medium} effects visible",
                      MEDIA_EFFECTS[medium] in rt.effects, str(rt.effects))
            else:
                # Refused medium: its phrasings were never bound, so the
                # router must NOT resolve them to any capability (fail
                # closed) -- no phantom capability_id.
                check(f"route: refused {medium} {ph[:40]!r} not resolved",
                      not rt.ok and rt.capability_id is None,
                      str(rt.as_dict()))

    # extra bound variants spot-check
    for medium, extra in (("voice", "read this text aloud: hello world"),
                          ("song", "make me a song about the ocean"),
                          ("image", "generate a picture of a sunset over the ocean"),
                          ("video", "animate ocean waves as a short video")):
        rt = router.route(extra)
        if medium in admitted:
            check(f"route: variant {extra[:40]!r} -> {medium}",
                  rt.ok and rt.capability_id == cap_ids[medium],
                  str(rt.as_dict()))
        else:
            check(f"route: variant refused {medium} not resolved",
                  not rt.ok and rt.capability_id is None,
                  str(rt.as_dict()))

    # non-media requests must NOT route to media capabilities
    for other in ("add two numbers together",
                  "write the quarterly report",
                  "what is the weather in boston right now",
                  "bake a chocolate cake at 350 degrees"):
        rt = router.route(other)
        check(f"route: non-media {other[:36]!r} refused",
              not rt.ok and rt.refusal == "unknown_intent"
              and rt.capability_id is None,
              str(rt.as_dict()))

    # malformed input: no regressions in fail-closed behavior
    for bad, code in [("", "empty_intent"), ("   ", "empty_intent"),
                      ("x" * 5000, "oversized_input")]:
        r = router.route(bad)
        check(f"route: {code} refused", not r.ok and r.refusal == code,
              str(r.as_dict()))
    r = router.route(None)
    check("route: non-string refused",
          not r.ok and r.refusal == "invalid_input")

    # Unbound paraphrases with NO inferred effects still refuse as
    # unknown_intent (fail closed). The effect-aware fallback (worker B)
    # fires only when the lexicon infers effects AND an admitted
    # capability declares them; these three infer nothing, so the old
    # boundary stands for them unchanged.
    for para in ("paint a sunset landscape",
                 "sing me a sea shanty",
                 "film the ocean waves"):
        r = router.route(para)
        print(f"   info: paraphrase {para!r} -> ok={r.ok} "
              f"refusal={r.refusal}")
        check(f"route: effectless paraphrase {para[:30]!r} refused",
              not r.ok and r.refusal == "unknown_intent",
              str(r.as_dict()))

    # "narrate ..." infers media_voice, so the fallback routes it ONLY
    # when voice was actually admitted -- never to a phantom capability.
    # (In this environment piper is absent, so voice is unadmitted and
    # this refuses; the admitted branch is exercised where piper exists.)
    r = router.route("narrate the phrase hello world")
    if "voice" in admitted:
        check("route: voice paraphrase -> effect_fallback",
              r.ok and r.via == "effect_fallback"
              and r.capability_id == cap_ids["voice"],
              str(r.as_dict()))
    else:
        check("route: voice paraphrase refused, voice unadmitted",
              not r.ok and r.refusal == "unknown_intent"
              and r.capability_id is None,
              str(r.as_dict()))

    # Effect-aware fallback (worker B, 2026-09-26): unbound phrasings
    # that carry inferred effects route to the admitted capability
    # declaring those effects -- with min_score still 0.55 and
    # structural_score untouched. (The exact-bound variants keep going
    # via exact_goal; these shorter variants are not bound.)
    for para, medium in (("create an image", "image"),
                         ("generate an image", "image"),
                         ("make a video", "video"),
                         ("generate a video", "video")):
        r = router.route(para)
        if medium in admitted:
            check(f"route: fallback {para!r} -> {medium}",
                  r.ok and r.via == "effect_fallback"
                  and r.capability_id == cap_ids[medium],
                  str(r.as_dict()))
        else:
            check(f"route: fallback {para!r} refused, {medium} unadmitted",
                  not r.ok and r.refusal == "unknown_intent"
                  and r.capability_id is None,
                  str(r.as_dict()))

    # ---- 4. dispatch through the real NLToolDispatcher ---------------------
    disp = NLToolDispatcher(eng, router)
    for medium in ("voice", "song", "image", "video"):
        phrasing = REQUIRED_PHRASINGS[medium][0]
        args = dispatch_args(medium, media_out)
        d = disp.dispatch(phrasing, dict(args), producer="test:media_pickup")
        if medium in refused:
            # Nothing admitted for this medium: dispatch must refuse and
            # fabricate no file.
            outp = args.get("path")
            check(f"dispatch: refused {medium} fails closed, no file",
                  not d.ok and not (outp and os.path.exists(outp)),
                  str(d.as_dict()))
            continue
        if media_module_present(medium):
            ok_file = False
            detail = str(d.as_dict())
            if d.ok and isinstance(d.result, dict):
                outp = d.result.get("out_path") or args["path"]
                ok_file = os.path.exists(outp) and os.path.getsize(outp) > 0
                detail = f"result_keys={sorted(d.result.keys())} file={outp}"
            check(f"dispatch: {medium} executes real module + writes file",
                  d.ok and d.result.get("ok", True) and ok_file, detail)
            check(f"dispatch: {medium} record persisted",
                  any(h["capability_id"] == cap_ids[medium]
                      for h in disp.history(cap_ids[medium])),
                  f"history={len(disp.history())}")
        else:
            mod = MEDIA_MODULES[medium][0]
            check(f"dispatch: {medium} stops honestly at missing module",
                  not d.ok and d.refusal == "execution_failed"
                  and f"swarm_engine.media.{mod}" in " ".join(d.reasons),
                  str(d.as_dict()))
            print(f"   info: {medium} execution stops here -- substrate "
                  f"module 'swarm_engine.media.{mod}' not landed yet; "
                  f"routing/admission/validation all passed above")

    # bad args refused before any execution (use an admitted medium: the
    # refused ones never reach argument validation)
    d_bad = disp.dispatch(REQUIRED_PHRASINGS["image"][0],
                          {"prompt": "sunset"})
    check("dispatch: missing required arg refused",
          not d_bad.ok and d_bad.refusal == "bad_arguments",
          str(d_bad.as_dict()))

    # out-of-grant-scope output path: governor denies (no write attempted)
    d_scope = disp.dispatch(
        REQUIRED_PHRASINGS["image"][0],
        {"prompt": "sunset", "width": 64, "height": 64, "seed": 1,
         "path": "/tmp/definitely_not_media_granted/sunset.png"},
        producer="test:media_pickup")
    check("dispatch: out-of-scope path denied by governor",
          not d_scope.ok and d_scope.refusal == "execution_failed",
          str(d_scope.as_dict()))

    # unknown capability id / unknown intent: refused, nothing recorded
    d_van = disp.dispatch_by_id("cap_doesnotexist12345", {},
                                producer="test:media_pickup")
    check("dispatch: unknown capability id refused",
          not d_van.ok and d_van.refusal == "capability_vanished",
          str(d_van.as_dict()))
    n_before = len(disp.history())
    d_unk = disp.dispatch("bake a chocolate cake", {"temp": 350})
    check("dispatch: unknown intent refused + not recorded",
          not d_unk.ok and d_unk.refusal == "unknown_intent"
          and len(disp.history()) == n_before,
          str(d_unk.as_dict()))

    # ---- 4b. spec-driven image renderer (worker B, 2026-09-26) -----------
    # Admitted through the REAL admission path with a live pixel-level
    # smoke predicate (asserts disc-inside red / disc-outside not-red,
    # exact dimensions, byte-determinism). The discrimination proof --
    # a deliberately broken renderer refused at smoke_test, the real
    # one admitted -- lives in the worker's step-4 proof; here we assert
    # the admitted artifact's routing, tie-break, and honest synthesis.
    spec_entry = admit_image_spec_capability(eng, media_out_dir=media_out)
    check("admit: image_spec ok via real admission path",
          spec_entry.get("admitted")
          and spec_entry["verdict"] in (Verdict.ADMITTED, Verdict.REUSED)
          and spec_entry.get("smoke_passed") is True
          and spec_entry.get("phrasings_bound") == len(IMAGE_SPEC_PHRASINGS),
          str({k: spec_entry.get(k) for k in
               ("admitted", "verdict", "smoke", "smoke_passed",
                "phrasings_bound")}))
    spec_id = spec_entry["capability_id"]
    spec_rec = eng.capabilities.get(spec_id)
    check("plan: image_spec declares media effects",
          list(spec_rec.plan.get("effects") or []) == list(IMAGE_SPEC_EFFECTS),
          str(spec_rec.plan.get("effects")))
    for ph in IMAGE_SPEC_PHRASINGS:
        rt = router.route(ph)
        check(f"route: spec phrasing {ph[:40]!r} -> exact_goal",
              rt.ok and rt.via == "exact_goal"
              and rt.capability_id == spec_id,
              str(rt.as_dict()))
    if "image" in admitted:
        # tie-break: a bare image phrasing still reaches the general
        # image capability (fewest declared effects beyond the inferred
        # set), not the spec renderer.
        rt = router.route("generate an image")
        check("route: tie-break keeps bare phrasing on general image",
              rt.ok and rt.via == "effect_fallback"
              and rt.capability_id == cap_ids["image"]
              and rt.capability_id != spec_id,
              str(rt.as_dict()))
    # no-args dispatch synthesizes an honest red-circle spec and renders
    # a real PNG (the test wires the governed dir into its dispatcher,
    # as the HTTP service does at boot).
    disp.media_out_dir = media_out
    d = disp.dispatch("draw a red circle", producer="test:media_pickup")
    png_detail = str(d.as_dict())[:400]
    center_red = False
    if d.ok and isinstance(d.result, dict):
        p = d.result.get("out_path")
        if p and os.path.exists(p) and os.path.getsize(p) > 0:
            from PIL import Image as _PILImage
            px = _PILImage.open(p).convert("RGB").load()
            center_red = px[256, 256] == (255, 0, 0)
            png_detail = (f"file={p} center_red={center_red} "
                          f"bounds={len(d.result.get('bounds', []))}")
    check("dispatch: 'draw a red circle' renders PNG, center pixel red",
          d.ok and center_red, png_detail)
    if d.ok:
        check("dispatch: spec bounds state the depiction limit",
              any("CANNOT" in b for b in d.result.get("bounds", [])),
              str(d.result.get("bounds")))
    # a spec request with no drawable geometry is refused honestly --
    # no fabricated depiction.
    d2 = disp.dispatch("render an image from a spec",
                       producer="test:media_pickup")
    check("dispatch: no-spec request -> underspecified_media",
          not d2.ok and d2.refusal == "underspecified_media",
          str(d2.as_dict()))

    # ---- 5. quarantine stops routing ---------------------------------------
    if "image" in admitted:
        quarantine_everywhere(eng, cap_ids["image"], "media pickup test",
                              caller=eng.oracle)
        rq = router.route(REQUIRED_PHRASINGS["image"][0])
        # Per-capability quarantine: the quarantined capability itself
        # must never route or serve. The effect fallback may still reach
        # the independently-admitted spec renderer (a different
        # capability with its own admission + smoke test) -- or refuse;
        # either way the quarantined capability serves nothing.
        check("route: quarantined image capability never routes",
              rq.capability_id != cap_ids["image"]
              and (not rq.ok or rq.via == "effect_fallback"),
              str(rq.as_dict()))
        dq = disp.dispatch(REQUIRED_PHRASINGS["image"][0],
                           dispatch_args("image", media_out))
        check("dispatch: quarantined image capability serves nothing",
              dq.capability_id != cap_ids["image"],
              str(dq.as_dict()))
    else:
        print("   info: image not admitted here; quarantine section skipped")

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    print(f"scratch: {base}")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
