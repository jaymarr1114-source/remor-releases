"""Governed natural-language tool dispatch (§4).

dispatch(text, args, producer):
  1. Route the text through IntentRouter -> exactly one capability_id
     (or a refusal; unknown/ambiguous/unavailable intents never dispatch).
  2. Validate the arguments: must be a JSON object of JSON scalar values
     (str/int/float/bool/None) or plain lists/dicts thereof; no callables,
     no objects, no oversized payloads. Then check names and kinds against
     the capability's declared plan params (composer._spec + coerce).
  3. Re-verify at invocation time: the record still exists, its stored
     plan still fingerprints to its id (tamper check), and
     effective_status is still "active" across the tri-system. A
     capability quarantined between routing and dispatch is refused.
  4. Execute through the real Composer path (execute_sync) -- never a
     caller-supplied callable. There is no code path by which the
     request text or args become executable code.
  5. Persist a dispatch record: request text, route, capability id +
     version, input digest, result digest, producer, outcome. These are
     operational audit rows (plain table, not part of the tamper-evident
     trust chain -- documented bound).

Refusals are fail-closed and machine-readable (DispatchResult.refusal).

Organizational learning (item 2, 2026-09-25): the dispatcher accepts an
optional learning hook (``learning=``) plus an agent binding
(``bind_agent``). On a SUCCESSFUL dispatch, when both are present, the
dispatcher natively calls ``learning.capture_evidence(...)`` so dispatch
evidence capture is a product behavior, not driver orchestration.

Attribution is fail-closed: the evidence is attributed to the BOUND
agent_id (an engine-issued identity), never to the free-form ``producer``
string. With no agent bound, no capture happens (the dispatch still
succeeds). The native call runs as the engine (the learning hook's
documented engine caller): ``capture_dispatch_evidence`` re-verifies the
agent/assignment binding itself, so the caller identity can never forge
attribution.

A capture exception never fails the already-successful dispatch: it is
recorded as ``DispatchResult.learning_error`` (an operational field,
like the intent_dispatches row) and documented here. Rationale: the
dispatch executed and was recorded; refusing it after the fact would
rewrite history, and dropping the error silently would hide a learning
outage. The error is visible on the result for the operator to act on.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.synthesis.capability_store import plan_fingerprint
from swarm_engine.synthesis.composer import _spec
from swarm_engine.primitives.core import coerce
from swarm_engine.synthesis.integrity import effective_status
from swarm_engine.synthesis.intent_router import IntentRouter
from swarm_engine.synthesis.semantic_frames import parse_frame
from swarm_engine.media import literal as _literal_media

MAX_ARGS_BYTES = 64 * 1024
MAX_ARG_DEPTH = 6
_DISPATCH_DDL = """
CREATE TABLE IF NOT EXISTS intent_dispatches (
  dispatch_id       TEXT PRIMARY KEY,
  ts                REAL NOT NULL,
  producer          TEXT,
  request_text      TEXT NOT NULL,
  route_via         TEXT,
  route_score       REAL,
  capability_id     TEXT NOT NULL,
  capability_version INTEGER,
  input_digest      TEXT,
  result_digest     TEXT,
  ok                INTEGER NOT NULL,
  error             TEXT
)
"""


# Literal-image product vocabulary: single location is
# swarm_engine.media.literal (ACQ-MEDIA-1 relocation). Aliases kept so
# existing references keep working.
_SPEC_COLORS = _literal_media.LITERAL_COLORS
_SPEC_SHAPES = _literal_media.LITERAL_SHAPES


# -- cross-capability composition (M4) --------------------------------------
# A composition request is an empty-args NL request that decomposes into
# >=2 fragments, each routing to a media capability. Decomposition is
# deliberately conservative: fragments are split on conjunctions, each
# fragment is routed through the SAME IntentRouter (routing rules are
# unchanged), and only fragments that route to a media capability (one
# with a governed arg synthesizer) become legs. Anything else is either
# connective tissue (ignored) or an honest problem (named in the
# refusal, never silently dropped).
_COMPOSE_SPLIT_RE = re.compile(
    r"\s+(?:and then|then|and|plus)\s+|;\s*", re.IGNORECASE)

# A fragment counts as a leg-shaped request only when it opens with an
# imperative work verb ("generate an image of a sunset", "create a short
# video of ocean waves"). This keeps adjective-joins ("a dog with brown
# fur and white spots") on the single-dispatch path: "white spots" is
# not an imperative request, so the whole text is not composition-shaped
# and falls through unchanged.
_COMPOSE_VERBS = frozenset(
    "make makes making made "
    "create creates creating created "
    "generate generates generating generated "
    "draw draws drawing drew drawn "
    "paint paints painting painted "
    "synthesize synthesizes synthesizing synthesized "
    "produce produces producing produced "
    "compose composes composing composed "
    "render renders rendering rendered "
    "animate animates animating animated".split())

_COMPOSE_VERB_RE = re.compile(r"^([a-z]+)\b")


# General imperative work verbs for non-media composition legs (Q9).
# The media verbs above cover depiction requests; a composition leg can
# also be a computation or analysis request ("double the values 3, 5, 7",
# "count the words in '...'"). These verbs only mark a fragment as
# leg-shaped -- the IntentRouter must still resolve the fragment to a
# real admitted capability, and Q9's per-leg arg synthesis must still
# fill every declared param, or the leg fails honestly. Adjective-joins
# stay protected: their fragments open with articles/adjectives, never
# with these verbs.
_COMPOSE_VERBS_GENERAL = frozenset(
    "double doubles doubled doubling "
    "halve halves halved halving "
    "triple triples tripled tripling "
    "sum sums summed summing "
    "total totals totaled totaling "
    "count counts counted counting "
    "compute computes computed computing "
    "calculate calculates calculated calculating "
    "average averages averaged averaging "
    "sort sorts sorted sorting".split())


def _is_imperative(fragment: str) -> bool:
    m = _COMPOSE_VERB_RE.match(fragment.strip().lower())
    return bool(m) and (m.group(1) in _COMPOSE_VERBS
                        or m.group(1) in _COMPOSE_VERBS_GENERAL)


# Words marking a fragment as media-leg-shaped. A fragment that fails to
# route AND carries none of these is connective tissue ("combine and mix
# and match") and is ignored; one that carries them but fails to route
# is a real problem (unknown intent, quarantined capability) and fails
# the composition honestly.
_MEDIA_LEG_WORDS = (
    "song", "songs", "image", "images", "picture", "pictures", "photo",
    "photos", "video", "videos", "voice", "speech", "voiceover", "draw",
    "paint", "movie", "movies", "sing", "music", "render",
)


def _spec_from_entities(entities: Dict[str, Any], seed: int):
    """Synthesize an image spec from the frame's literal entities.

    ACQ-MEDIA-1 replacement for the old ``_spec_from_shape_words``: the
    color/shape/size/filename now arrive as frame entities parsed via the
    acquired NLU substrate (semantic_frames), not from a hand-rolled
    regex word scan of the prompt surface. Geometry, refusal contract,
    and legacy defaults live in ``swarm_engine.media.literal``; this is
    the thin call-site adapter.
    """
    return _literal_media.build_literal_spec(entities or {}, seed)


@dataclass
class _RouteInfo:
    """Minimal route descriptor for _record (routed or direct dispatch)."""
    via: Optional[str] = None
    score: Optional[float] = None


@dataclass
class DispatchResult:
    ok: bool
    result: Any = None
    capability_id: Optional[str] = None
    dispatch_id: Optional[str] = None
    route_via: Optional[str] = None
    refusal: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    # Operational field: a native learning-capture failure never fails the
    # dispatch; it is recorded here (None when capture was not attempted or
    # succeeded). See the module docstring for the rationale.
    learning_error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "result": self.result,
            "capability_id": self.capability_id,
            "dispatch_id": self.dispatch_id,
            "route_via": self.route_via,
            "refusal": self.refusal,
            "reasons": self.reasons,
            "learning_error": self.learning_error,
        }


def _q12_refusal_dict(value: Any) -> bool:
    """Whether a media result value is a refusal carried as data:
    {"ok": False, "error": ...}. The composer passes these through as a
    successful value, which would swallow the failure."""
    return (isinstance(value, dict) and value.get("ok") is False
            and isinstance(value.get("error"), str))


class NLToolDispatcher:
    """NL -> capability dispatcher. Bound to one engine."""

    def __init__(self, engine, router: Optional[IntentRouter] = None,
                 learning: Any = None):
        self.engine = engine
        self.router = router or IntentRouter(engine)
        # Organizational-learning hook (item 2): an object exposing
        # capture_evidence(dispatch_id, agent_id, assignment_id, args,
        # result_value, caller) and an engine_caller identity for the
        # native path -- e.g. DispatchLearningService. None disables
        # native capture (driver-orchestrated capture still works).
        self.learning = learning
        # Agent context for native capture: (agent_id, assignment_id).
        # Bound explicitly by the operator/driver; never derived from the
        # free-form producer string (fail-closed attribution).
        self._learning_agent: Optional[tuple] = None
        # Device-path acceptance inlet (ACC-P6-1): lazy-built
        # DeviceAcceptanceAdapter; every successful dispatch enters the
        # Acceptance Controller through it. None until first success.
        self._device_acceptance = None
        # Server-chosen governed output dir for synthesized media args.
        # Set by the HTTP adapter's media wiring (the dir the WRITE_FS
        # grant covers); None means arg synthesis cannot choose a path
        # and media requests without caller args are refused honestly.
        self.media_out_dir: Optional[str] = None
        con = sqlite3.connect(engine.db_path)
        try:
            con.execute(_DISPATCH_DDL)
            con.commit()
        finally:
            con.close()

    def bind_agent(self, agent_id: str, assignment_id: str) -> None:
        """Bind the agent context native capture attributes evidence to."""
        if not agent_id or not assignment_id:
            raise ValueError(
                "bind_agent requires a non-empty agent_id and assignment_id")
        self._learning_agent = (agent_id, assignment_id)

    def unbind_agent(self) -> None:
        """Clear the agent context: dispatches no longer capture natively."""
        self._learning_agent = None

    # -- media argument synthesis -------------------------------------------
    @staticmethod
    def _media_plan(rec) -> bool:
        """Whether the routed capability is a media capability (by its
        admitted plan's declared lexicon effects)."""
        try:
            declared = set((rec.plan or {}).get("effects") or [])
        except Exception:
            return False
        return any(str(e).startswith("media_") for e in declared)

    @staticmethod
    def _media_op(rec) -> Optional[str]:
        try:
            steps = (rec.plan or {}).get("steps") or []
            if steps and isinstance(steps[0], dict):
                return steps[0].get("op")
        except Exception:
            pass
        return None

    def _governed_media_path(self, ext: str) -> str:
        """Server-chosen uuid output path under the governed media dir."""
        if self.media_out_dir is None:
            raise RuntimeError("media_out_dir not configured")
        name = f"{uuid.uuid4().hex[:12]}_image.{ext}"
        return os.path.join(os.path.abspath(self.media_out_dir), name)

    def _synthesize_media_args(self, text: str, rec, frame=None):
        """Build full capability args for a media request the parser
        understood, when the caller supplied none.

        Mirrors task_interface._understand_create_media: the prompt comes
        from the frame entities; width/height/voice/etc. take sane
        defaults; the seed derives deterministically from the request
        text; the output path is server-chosen under the governed media
        dir (callers never choose it -- except a sanitized filename_hint
        basename for literal renders, still confined to the governed
        dir). Returns (args, None) on success, (None, reason) when
        synthesis is impossible -- never a partial or fabricated arg set.

        ``frame`` may be a pre-parsed IntentFrame (dispatch() parses once
        for routing); when None it is parsed here.
        """
        op = self._media_op(rec)
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
        if frame is None:
            frame = parse_frame(text)
        entities = frame.entities or {}
        prompt = entities.get("prompt")
        prompt = prompt.strip() if isinstance(prompt, str) else ""
        if op == "media.image_render_spec":
            # Literal rendering (ACQ-MEDIA-1): the spec comes from the
            # frame's literal entities (color/shape/size via the acquired
            # NLU substrate); the filename from a sanitized filename_hint
            # basename confined to the governed media dir.
            if self.media_out_dir is None:
                return None, ("server has no governed media output dir "
                              "configured; cannot choose an output path")
            spec, why = _spec_from_entities(entities, seed)
            if spec is None:
                return None, why
            fname = _literal_media.sanitize_filename(
                entities.get("filename_hint"))
            if fname is not None:
                path = os.path.join(
                    os.path.abspath(self.media_out_dir), fname)
            else:
                path = self._governed_media_path("png")
            return {"spec": spec, "path": path}, None
        if self.media_out_dir is None:
            return None, ("server has no governed media output dir "
                          "configured; cannot choose an output path")
        if not prompt:
            return None, ("no media prompt understood from the request -- "
                          "describe what to depict (e.g. 'a sunset over "
                          "the ocean')")
        name = f"{uuid.uuid4().hex[:12]}"
        out = os.path.join(os.path.abspath(self.media_out_dir), name)

        def _path(stem: str, ext: str) -> str:
            return f"{out}_{stem}.{ext}"

        if op == "media.image_generate":
            return {"prompt": prompt, "path": _path("image", "png"),
                    "width": 512, "height": 512, "seed": seed}, None
        if op == "media.video_generate":
            return {"prompt": prompt, "path": _path("video", "mp4"),
                    "duration_s": 4.0, "fps": 24,
                    "width": 640, "height": 360, "seed": seed}, None
        if op == "media.song_assemble":
            return {"lyrics": prompt, "spec": {"style": "ballad"},
                    "path": _path("song", "wav"),
                    "work_dir": f"{out}_song_work"}, None
        if op == "media.voice_synthesize":
            return {"text": prompt, "voice": "default",
                    "path": _path("voice", "wav")}, None
        return None, f"no arg synthesizer for plan op {op!r}"

    # -- non-media leg argument synthesis (Q9) ------------------------------
    # A non-media composition leg's arguments are synthesized from the
    # capability's DECLARED contract (plan params) plus the composition
    # context: the leg's own fragment text, then earlier legs' outputs.
    # Every value has a provenance -- a number, quoted string, or
    # true/false word literally present in the fragment, or a
    # type-matching value a prior leg really produced. Anything else
    # (file paths, URLs, credentials, external resources) is refused
    # with a named reason: the synthesizer never hallucinates arguments.
    _Q9_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
    _Q9_QUOTED_RE = re.compile(r"'([^']+)'|\"([^\"]+)\"")

    @staticmethod
    def _q9_kind(kind: str) -> str:
        # Mirror composer._spec's kind normalization: "list[num]" -> "list".
        return str(kind).split("[")[0].strip().lower()

    @classmethod
    def _q9_numbers_in(cls, text: str):
        out = []
        for tok in cls._Q9_NUMBER_RE.findall(text or ""):
            out.append(float(tok) if "." in tok else int(tok))
        return out

    @classmethod
    def _q9_scan_prior(cls, prior_outputs, want):
        """Find a type-matching value in earlier legs' outputs.

        want is one of "int", "float", "num", "str", "bool", "listnum",
        "list", "dict". For "listnum" only a non-empty list of
        int/float (never bool) counts -- a media leg's "bounds" list of
        strings must not become somebody's numeric input.
        """
        def _match(v):
            if want == "int":
                return isinstance(v, int) and not isinstance(v, bool)
            if want == "float":
                return isinstance(v, (int, float)) and not isinstance(v, bool)
            if want == "num":
                return isinstance(v, (int, float)) and not isinstance(v, bool)
            if want == "str":
                return isinstance(v, str)
            if want == "bool":
                return isinstance(v, bool)
            if want == "listnum":
                return (isinstance(v, list) and len(v) > 0
                        and all(isinstance(x, (int, float))
                                and not isinstance(x, bool) for x in v))
            if want == "list":
                return isinstance(v, list)
            if want == "dict":
                return isinstance(v, dict)
            return False

        for out in prior_outputs or []:
            if _match(out):
                return out
            if isinstance(out, dict):
                for v in out.values():
                    if _match(v):
                        return v
        return None

    def _synthesize_leg_arg(self, pname: str, kind: str, frag: str,
                            prior_outputs):
        """Synthesize one declared param. Returns (value, None) or
        (None, reason). Explicit fragment-text evidence wins over
        prior-leg context: the user's literal words outrank chaining."""
        k = self._q9_kind(kind)
        if k in ("int", "integer"):
            nums = [n for n in self._q9_numbers_in(frag)
                    if isinstance(n, int)]
            if nums:
                return nums[0], None
            hit = self._q9_scan_prior(prior_outputs, "int")
            if hit is not None:
                return hit, None
            return None, ("no integer in the fragment and no prior leg "
                          "produced an integer")
        if k in ("float", "number", "num", "double"):
            nums = self._q9_numbers_in(frag)
            if nums:
                v = nums[0]
                return (float(v) if k == "float" else v), None
            hit = self._q9_scan_prior(prior_outputs, "num")
            if hit is not None:
                return hit, None
            return None, ("no number in the fragment and no prior leg "
                          "produced a number")
        if k in ("str", "string", "text"):
            m = self._Q9_QUOTED_RE.search(frag or "")
            if m:
                return (m.group(1) if m.group(1) is not None
                        else m.group(2)), None
            hit = self._q9_scan_prior(prior_outputs, "str")
            if hit is not None:
                return hit, None
            return None, ("no quoted string in the fragment and no prior "
                          "leg produced a string")
        if k in ("bool", "boolean"):
            low = (frag or "").lower()
            if re.search(r"\btrue\b", low):
                return True, None
            if re.search(r"\bfalse\b", low):
                return False, None
            hit = self._q9_scan_prior(prior_outputs, "bool")
            if hit is not None:
                return hit, None
            return None, ("no true/false word in the fragment and no prior "
                          "leg produced a boolean")
        if k == "list":
            nums = self._q9_numbers_in(frag)
            if nums:
                return nums, None
            hit = self._q9_scan_prior(prior_outputs, "listnum")
            if hit is not None:
                return list(hit), None
            return None, ("no list of numbers in the fragment and no prior "
                          "leg produced a list of numbers")
        if k == "dict":
            hit = self._q9_scan_prior(prior_outputs, "dict")
            if hit is not None:
                return hit, None
            return None, "no prior leg produced a dict to fill this argument"
        return None, (f"kind {kind!r} is not synthesizable from request "
                      "text or leg outputs")

    def _synthesize_leg_args(self, frag: str, rec, prior_outputs):
        """Build full args for a non-media composition leg.

        Returns (args, None) on success, (None, reason) when any
        declared param cannot be filled -- never a partial or
        fabricated arg set.
        """
        params = (rec.plan or {}).get("params") or {}
        # Ambiguity guard: with two or more scalar numeric parameters
        # and fewer distinct numbers in the fragment than parameters,
        # reusing the first number for every parameter would fabricate
        # associations. Fail closed instead.
        scalar_num = [p for p, k in params.items()
                      if self._q9_kind(k) in ("int", "float", "num")]
        if len(scalar_num) > 1:
            have = self._q9_numbers_in(frag)
            if len(have) < len(scalar_num):
                return (None, "ambiguous: %d numeric parameters (%s) but "
                        "only %d number(s) in the fragment: %r"
                        % (len(scalar_num), ", ".join(scalar_num),
                           len(have), frag))
        args = {}
        for pname, kind in params.items():
            val, why = self._synthesize_leg_arg(pname, kind, frag,
                                                prior_outputs)
            if why is not None:
                return None, (f"argument {pname!r} (declared {kind}): "
                              f"{why}")
            args[pname] = val
        return args, None

    # -- cross-capability composition (M4) ----------------------------------
    _MEDIUM_WORDS = {
        "image": ("image", "images", "picture", "pictures", "photo", "photos",
                  "draw", "paint", "render"),
        "video": ("video", "videos", "movie", "movies"),
        "song": ("song", "songs", "sing", "music"),
        "voice": ("voice", "speech", "voiceover"),
    }

    def _inactive_media_note(self, frag: str) -> str:
        """Name non-effectively-active media capabilities matching the
        fragment's medium.

        Used when a leg-shaped fragment fails to route: the router's
        refusal alone (e.g. unknown_intent) would hide a quarantine, so
        the reason names the quarantined capability for that medium
        explicitly. Only the frozen effective_status is read here.
        """
        lowered = frag.lower()
        media = [m for m, words in self._MEDIUM_WORDS.items()
                 if any(w in lowered for w in words)]
        if not media:
            return ""
        notes = []
        seen = set()
        for status in ("quarantined", "deprecated", "superseded"):
            try:
                recs = self.engine.capabilities.list(status=status)
            except Exception:
                continue
            for rec in recs:
                cid = getattr(rec, "capability_id", None)
                name = str(getattr(rec, "name", "") or "")
                if not cid or cid in seen or not self._media_plan(rec):
                    continue
                if not any(m in name.lower() for m in media):
                    continue
                seen.add(cid)
                try:
                    state = effective_status(
                        self.engine, cid).get("effective")
                except Exception:
                    state = "unknown"
                notes.append(f"media capability {name[:32]} "
                             f"({cid[:12]}...) is {state}")
        return "; ".join(notes)

    def _detect_composition(self, text: str):
        """Decompose a possible multi-leg request.

        Returns None when this is not a composition request (the caller
        falls through to single dispatch), ("unroutable", problems) when
        a leg-shaped fragment cannot be routed -- problems is a list of
        (fragment, reason) -- or ("ok", legs) with >= 2
        (fragment, route) legs, each routed to a media capability or to
        a non-media capability whose arguments Q9 can synthesize.
        """
        if not isinstance(text, str):
            return None
        fragments = [f.strip() for f in _COMPOSE_SPLIT_RE.split(text.strip())
                     if f and f.strip()]
        if len(fragments) < 2:
            return None
        # Every fragment must be a complete imperative request ("generate
        # an image of a sunset", "create a short video of ocean waves").
        # Adjective-joins ("a dog with brown fur and white spots") are
        # not composition-shaped: the non-imperative fragment means the
        # whole text falls through to single dispatch unchanged.
        if not all(_is_imperative(f) for f in fragments):
            return None
        legs = []
        problems = []
        for frag in fragments:
            route = self.router.route(frag)
            if not route.ok:
                if any(w in frag.lower() for w in _MEDIA_LEG_WORDS):
                    why = route.refusal or "unroutable"
                    note = self._inactive_media_note(frag)
                    if note:
                        why = f"{why}; {note}"
                    problems.append((frag, why))
                continue
            rec = self.engine.capabilities.get(route.capability_id)
            if rec is None:
                problems.append(
                    (frag, "routed capability vanished before argument "
                           "synthesis"))
            elif self._media_plan(rec):
                legs.append((frag, route))
            else:
                # Non-media leg (Q9): viable only when its declared args
                # can be synthesized. Static check against the fragment
                # now; when an earlier leg already exists, defer to
                # execution -- the earlier leg's output may fill what
                # the fragment cannot (chaining). Execution still fails
                # honestly if the context does not deliver.
                synth, why = self._synthesize_leg_args(frag, rec, [])
                if synth is not None or len(legs) > 0:
                    legs.append((frag, route))
                else:
                    problems.append(
                        (frag, "routes to a non-media capability whose "
                               "arguments cannot be synthesized from the "
                               f"request: {why}"))
        if problems:
            return ("unroutable", problems)
        if len(legs) < 2:
            return None
        return ("ok", legs)

    def _dispatch_composition(self, text: str, legs, producer: Optional[str]
                              ) -> DispatchResult:
        """Execute each leg through the governed single-dispatch core and
        compose the results into one artifact.

        Legs are media or non-media. Media legs get args from
        _synthesize_media_args; non-media legs from Q9's
        _synthesize_leg_args (declared contract + fragment text +
        earlier legs' outputs). A leg whose args cannot be synthesized
        fails the composition honestly, naming the leg -- never with
        hallucinated arguments.
        Every leg honors the same gates as a routed dispatch: the record
        must still exist, its plan must fingerprint to its id, its
        effective status must be active (a leg quarantined between
        routing and execution is refused -- composition never routes
        around quarantine), args are validated against the declared
        params, execution goes through the real Composer path, and each
        leg persists its own dispatch record (route_via
        "composition:leg<i>"). A leg failure fails the composition
        honestly, naming the leg -- there are no partial-fake results.
        """
        leg_traces = []
        for i, (frag, route) in enumerate(legs):
            cap_id = route.capability_id
            label = (f"leg {i} ({frag[:60]!r} -> "
                     f"{route.capability_name or cap_id[:12]}...)")
            rec = self.engine.capabilities.get(cap_id)
            if rec is None:
                # Vanished between detection and execution: let the
                # governed core name it (capability_vanished).
                leg_res = self._dispatch_validated(
                    frag, cap_id, {}, producer,
                    route_via=f"composition:leg{i}", route_score=route.score)
                return DispatchResult(
                    ok=False, refusal="composition_leg_failed",
                    route_via="composition",
                    reasons=[f"{label}: {leg_res.refusal}: "
                             f"{'; '.join(leg_res.reasons)}"])
            if self._media_plan(rec):
                synth, why = self._synthesize_media_args(frag, rec)
                synth_label = "media args"
            else:
                # Non-media leg (Q9): synthesize from the declared
                # contract plus earlier legs' outputs (chaining).
                prior = [lt["result"] for lt in leg_traces]
                synth, why = self._synthesize_leg_args(frag, rec, prior)
                synth_label = "leg args"
            if synth is None:
                return DispatchResult(
                    ok=False, refusal="composition_leg_failed",
                    route_via="composition",
                    reasons=[f"{label}: {synth_label} could not be "
                             f"synthesized: {why}"])
            leg_res = self._dispatch_validated(
                frag, cap_id, synth, producer,
                route_via=f"composition:leg{i}", route_score=route.score)
            if not leg_res.ok:
                return DispatchResult(
                    ok=False, refusal="composition_leg_failed",
                    route_via="composition",
                    reasons=[f"{label}: {leg_res.refusal}: "
                             f"{'; '.join(leg_res.reasons)}"])
            leg_traces.append({
                "leg": i,
                "fragment": frag,
                "capability_id": cap_id,
                "capability_name": route.capability_name,
                "dispatch_id": leg_res.dispatch_id,
                "result": leg_res.result,
            })
        artifacts = []
        for lt in leg_traces:
            res = lt["result"]
            if isinstance(res, dict):
                p = res.get("out_path") or res.get("path")
                if isinstance(p, str):
                    artifacts.append({"leg": lt["leg"], "path": p})
        composed = {
            "composition": True,
            "request": text,
            "legs": leg_traces,
            "artifacts": artifacts,
        }
        return DispatchResult(
            ok=True, result=composed, route_via="composition",
            reasons=[f"composition of {len(leg_traces)} legs"] +
                    [f"leg{lt['leg']} dispatch_id={lt['dispatch_id']}"
                     for lt in leg_traces])

    # -- argument validation ------------------------------------------------
    def _check_json_value(self, v: Any, depth: int, path: str) -> Optional[str]:
        """Return an error string, or None if the value is acceptable."""
        if depth > MAX_ARG_DEPTH:
            return f"{path}: nesting exceeds depth {MAX_ARG_DEPTH}"
        if v is None or isinstance(v, (bool, int, float, str)):
            if isinstance(v, str) and len(v) > 8192:
                return f"{path}: string arg exceeds 8192 chars"
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                return f"{path}: non-finite float refused"
            return None
        if isinstance(v, list):
            if len(v) > 1000:
                return f"{path}: list arg exceeds 1000 items"
            for i, item in enumerate(v):
                err = self._check_json_value(item, depth + 1, f"{path}[{i}]")
                if err:
                    return err
            return None
        if isinstance(v, dict):
            if len(v) > 256:
                return f"{path}: dict arg exceeds 256 keys"
            for k, item in v.items():
                if not isinstance(k, str):
                    return f"{path}: non-string key refused"
                if k.startswith("__"):
                    return f"{path}: dunder key {k!r} refused"
                err = self._check_json_value(item, depth + 1, f"{path}.{k}")
                if err:
                    return err
            return None
        return f"{path}: value of type {type(v).__name__} refused (JSON scalars only)"

    def _validate_args(self, args: Any, plan: Dict[str, Any]):
        """Returns (ok, coerced_or_error)."""
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return False, "args must be a JSON object"
        try:
            blob = json.dumps(args)
        except (TypeError, ValueError):
            return False, "args are not JSON-serializable"
        if len(blob.encode("utf-8")) > MAX_ARGS_BYTES:
            return False, f"args exceed {MAX_ARGS_BYTES} bytes"
        for k, v in args.items():
            if not isinstance(k, str):
                return False, "arg names must be strings"
            if k.startswith("__"):
                return False, f"arg name {k!r} refused"
            err = self._check_json_value(v, 0, k)
            if err:
                return False, err
        params = plan.get("params") or {}
        unknown = [k for k in args if k not in params]
        if unknown:
            return False, f"unknown args for this capability: {unknown}"
        missing = [k for k in params if k not in args]
        if missing:
            return False, f"missing required args: {missing}"
        coerced = {}
        for k, v in args.items():
            spec = _spec(params[k])
            ok_c, cv = coerce(v, spec)
            if not ok_c:
                return False, (f"arg {k!r} value {v!r} does not coerce to "
                               f"declared kind {params[k]!r}")
            coerced[k] = cv
        return True, coerced

    # -- dispatch -----------------------------------------------------------
    def _record(self, dispatch_id, producer, text, route, cap_id, version,
                input_digest, result_digest, ok, error):
        con = sqlite3.connect(self.engine.db_path)
        try:
            con.execute(
                "INSERT INTO intent_dispatches "
                "(dispatch_id, ts, producer, request_text, route_via, "
                " route_score, capability_id, capability_version, "
                " input_digest, result_digest, ok, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (dispatch_id, time.time(), producer, text,
                 route.via if route else None,
                 route.score if route else None,
                 cap_id, version, input_digest, result_digest,
                 1 if ok else 0, error))
            con.commit()
        finally:
            con.close()

    def dispatch(self, text: str, args: Optional[Dict[str, Any]] = None,
                 producer: Optional[str] = None) -> DispatchResult:
        # M4 composition: an empty-args NL request that decomposes into
        # multiple routable legs executes as ONE composed dispatch; legs
        # are media capabilities or non-media capabilities whose args
        # Q9 can synthesize (declared contract + fragment + leg context).
        # each leg runs the governed single-dispatch core (existence,
        # plan fingerprint, effective_status, arg validation, real
        # Composer execution, persisted record). Explicit args keep the
        # single-capability strict contract -- composition never engages
        # when the caller supplied an arg set.
        if not args:
            comp = self._detect_composition(text)
            if comp is not None:
                kind, payload = comp
                if kind == "ok":
                    return self._dispatch_composition(text, payload,
                                                      producer)
                reasons = []
                for frag, why in payload:
                    reasons.append(f"leg {frag[:60]!r}: {why}")
                return DispatchResult(
                    ok=False, refusal="composition_leg_unroutable",
                    route_via="composition", reasons=reasons)
        # 1. route. The frame is parsed once here: the router consults it
        # for the literal-entity cue (ACQ-MEDIA-1), and argument synthesis
        # reuses it below. A parse failure degrades to frame=None --
        # routing then behaves exactly as before.
        try:
            frame = parse_frame(text)
        except Exception:
            frame = None
        route = self.router.route(text, frame=frame)
        if not route.ok:
            result = DispatchResult(
                ok=False, refusal=route.refusal, reasons=route.reasons)
            # V10-GAP-GENESIS: a routing failure is a potential composition
            # gap. Attempt genesis (never breaks dispatch; the result is
            # returned unchanged).
            return self._wire_genesis_gap(result, text, args)

        cap_id = route.capability_id
        # 1b. argument synthesis for pure-NL media requests: when the
        # caller supplied no args at all and the parser understood the
        # request, complete the args from the frame + sane defaults
        # (mirrors task_interface._understand_create_media). An empty
        # args dict means the same as absent: the GUI dispatch view
        # always sends "args": {}, so {} must not be mistaken for
        # caller-supplied args. A caller that supplies a NON-EMPTY arg
        # set keeps the strict contract: missing/unknown args are still
        # bad_arguments.
        if not args:
            rec0 = self.engine.capabilities.get(cap_id)
            if rec0 is not None and self._media_plan(rec0):
                synth, why = self._synthesize_media_args(text, rec0,
                                                         frame=frame)
                if synth is None:
                    return DispatchResult(
                        ok=False, refusal="underspecified_media",
                        capability_id=cap_id, route_via=route.via,
                        reasons=[why or "media args could not be synthesized",
                                 f"routed via {route.via} to {cap_id[:12]}..."])
                args = synth
        return self._dispatch_validated(
            text, cap_id, args, producer, route_via=route.via,
            route_score=route.score)

    def dispatch_by_id(self, capability_id: str,
                       args: Optional[Dict[str, Any]] = None,
                       producer: Optional[str] = None,
                       request_text: str = "") -> DispatchResult:
        """Governed direct dispatch by capability identity (no NL routing).

        Used when the caller already knows WHICH capability to run -- e.g.
        an agent acting on admitted organizational knowledge that names the
        capability. This is NOT a bypass: every invocation-time check the
        routed path performs (existence, plan fingerprint, effective
        status, argument validation, real Composer execution, persisted
        record) runs identically. Only the NL routing step is skipped, and
        the record marks route_via="direct_by_id" so the provenance is
        explicit. Refusals are identical to dispatch()'s.
        """
        if not isinstance(capability_id, str) or not capability_id:
            return DispatchResult(ok=False, refusal="invalid_input",
                                  reasons=["capability_id must be a string"])
        return self._dispatch_validated(
            request_text, capability_id, args, producer,
            route_via="direct_by_id", route_score=0.0)

    def _dispatch_validated(self, text: str, cap_id: str,
                            args: Optional[Dict[str, Any]],
                            producer: Optional[str],
                            route_via: Optional[str],
                            route_score: Optional[float]) -> DispatchResult:
        """Shared governed core: re-verify, validate args, execute, record."""
        rec = self.engine.capabilities.get(cap_id)
        if rec is None:
            return DispatchResult(ok=False, refusal="capability_vanished",
                                  reasons=["capability no longer in store"])

        # invocation-time re-verification (TOCTOU between selection and run)
        try:
            fp = plan_fingerprint(rec.plan)
        except Exception as exc:
            return DispatchResult(ok=False, refusal="plan_unreadable",
                                  capability_id=cap_id,
                                  reasons=[f"stored plan unreadable: {exc!r}"])
        if fp != cap_id:
            return DispatchResult(
                ok=False, refusal="capability_tampered", capability_id=cap_id,
                reasons=["stored plan no longer fingerprints to its id"])
        eff = effective_status(self.engine, cap_id)
        if eff.get("effective") != "active" or not eff.get("consistent"):
            return self._wire_failure_gap(DispatchResult(
                ok=False, refusal="capability_unavailable",
                capability_id=cap_id,
                reasons=[f"effective status at dispatch: {eff.get('effective')}"]))

        # argument validation against the declared params
        ok_a, coerced_or_err = self._validate_args(args, rec.plan)
        if not ok_a:
            return DispatchResult(ok=False, refusal="bad_arguments",
                                  capability_id=cap_id,
                                  reasons=[coerced_or_err])
        return self._execute_and_record(text, rec, coerced_or_err, producer,
                                        route_via, route_score)

    def _execute_and_record(self, text: str, rec: Any,
                            coerced: Dict[str, Any],
                            producer: Optional[str],
                            route_via: Optional[str],
                            route_score: Optional[float]) -> DispatchResult:
        """Execute through the real Composer path and persist the record."""
        cap_id = rec.capability_id
        # 4. execute through the real Composer path
        dispatch_id = "dsp_" + uuid.uuid4().hex[:16]
        input_digest = hashlib.sha256(
            json.dumps(coerced, sort_keys=True).encode()).hexdigest()
        route = _RouteInfo(via=route_via, score=route_score)
        try:
            exec_res = self.engine.composer.execute_sync(
                dict(rec.plan), dict(coerced))
        except Exception as exc:
            self._record(dispatch_id, producer, text, route, cap_id,
                         rec.version, input_digest, None, False,
                         f"execution raised: {exc!r}")
            limitation = self._record_dispatch_limitation(
                rec, coerced, exc)
            reasons = [f"execution raised: {exc!r}"]
            if limitation is not None:
                reasons.append("limitation recorded: " + limitation[:160])
            return self._wire_failure_gap(DispatchResult(
                ok=False, refusal="execution_failed",
                capability_id=cap_id, dispatch_id=dispatch_id,
                route_via=route_via,
                reasons=reasons))
        if not isinstance(exec_res, dict) or not exec_res.get("success", True):
            err = (exec_res.get("error") if isinstance(exec_res, dict)
                   else "unknown execution failure")
            self._record(dispatch_id, producer, text, route, cap_id,
                         rec.version, input_digest, None, False, str(err))
            limitation = self._record_dispatch_limitation(
                rec, coerced, err)
            reasons = [f"execution failed: {err}"]
            if limitation is not None:
                reasons.append("limitation recorded: " + limitation[:160])
            return self._wire_failure_gap(DispatchResult(
                ok=False, refusal="execution_failed",
                capability_id=cap_id, dispatch_id=dispatch_id,
                route_via=route_via,
                reasons=reasons))
        value = exec_res.get("value", exec_res.get("result", exec_res))
        # Honest bounds stay attached to every media answer: the substrate
        # result dict does not carry them (services.media adds them only
        # on its own front), so the dispatch path attaches the admitted
        # plan's declared-effect bounds here, from services/media.py.
        try:
            from swarm_engine.media.wiring import media_bounds_for_effects
            declared = (rec.plan or {}).get("effects") or []
            bounds = media_bounds_for_effects(declared)
        except Exception:
            bounds = []
        if bounds and isinstance(value, dict):
            value = dict(value)
            value.setdefault("bounds", list(bounds))
        if self._media_plan(rec) and _q12_refusal_dict(value):
            # The substrate refused as data ({"ok": False, "error": ...}),
            # which the composer passes through as a successful value --
            # a swallowed failure. Report it honestly as a failure and
            # run the limitation recorder.
            err = value.get("error")
            self._record(dispatch_id, producer, text, route, cap_id,
                         rec.version, input_digest, None, False, str(err))
            limitation = self._record_dispatch_limitation(
                rec, coerced, err)
            reasons = [f"execution failed: {err}"]
            if limitation is not None:
                reasons.append("limitation recorded: " + limitation[:160])
            return self._wire_failure_gap(DispatchResult(
                ok=False, refusal="execution_failed",
                capability_id=cap_id, dispatch_id=dispatch_id,
                route_via=route_via,
                reasons=reasons))
        result_digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
        self._record(dispatch_id, producer, text, route, cap_id,
                     rec.version, input_digest, result_digest, True, None)
        res = DispatchResult(ok=True, result=value, capability_id=cap_id,
                             dispatch_id=dispatch_id, route_via=route_via,
                             reasons=[f"dispatched via {route_via}",
                                      f"dispatch_id={dispatch_id}"])
        self._native_capture(res, dispatch_id, dict(coerced), value)
        # ACC-P6-1 acceptance inlet: the successful device dispatch
        # enters the Acceptance Controller here. Guarded: acceptance
        # never breaks the dispatch; the outcome is announced on the
        # result's reasons.
        res = self._wire_device_acceptance(
            res, dispatch_id, text, rec, dict(coerced), value, route_via)
        return res

    # -- dispatch-path gap wiring (V9-WIRE) --------------------------------
    # The dispatch failure path registers limitation / missing-dependency
    # gaps itself, in the unified registry, with no operator in the
    # middle. Q12's production limitation recorder (below) is the model:
    # the system acts on its own observation.
    #
    # The hook fires on the failure edges that carry a capability_id and a
    # real execution outcome: _execute_and_record's three failure branches
    # (raised exception, success=False, refusal-as-data) and
    # _dispatch_validated's capability_unavailable refusal (where a
    # missing-dependency quarantine surfaces). Early refusals without an
    # execution attempt (unknown_intent, bad_arguments, underspecified
    # media, composition_leg_unroutable) carry no gap evidence and are
    # deliberately unwired. Composition legs go through the same governed
    # core (_dispatch_validated -> _execute_and_record), so a failing leg
    # wires its gap exactly like a single dispatch.
    #
    # The wiring never breaks the dispatch: all wiring failures are
    # contained in dispatch_gaps.maybe_register_dispatch_gap (which never
    # raises) and double-contained here. A registered gap is announced on
    # the result's reasons so the turn stays observable.

    # -- device-path acceptance inlet (ACC-P6-1) ---------------------------
    # The dispatch path IS the device path: every successful dispatch --
    # routed, direct-by-id, composed leg, scheduler-driven -- funnels
    # through _execute_and_record's success edge. Presenting here puts
    # every device completion through the Acceptance Controller's
    # present_completion (four standing panels -> driver's auth gate ->
    # CANDIDATE; completed != accepted). Double-contained like the gap
    # wiring: any acceptance failure degrades to an announced note on
    # the result's reasons; the DispatchResult is always returned and
    # the dispatch itself never breaks.

    def _device_acceptance_adapter(self):
        adapter = self._device_acceptance
        if adapter is None:
            from swarm_engine.core.acceptance_device import (
                DeviceAcceptanceAdapter)
            adapter = DeviceAcceptanceAdapter.from_engine(self.engine)
            self._device_acceptance = adapter
        return adapter

    def _wire_device_acceptance(self, res: DispatchResult,
                                dispatch_id: str, text: str, rec: Any,
                                coerced: Dict[str, Any], value: Any,
                                route_via: Optional[str]) -> DispatchResult:
        try:
            adapter = self._device_acceptance_adapter()
            outcome = adapter.present_device_dispatch(
                dispatch_id, text, rec, coerced, value, route_via)
        except Exception as exc:
            res.reasons.append(
                "acceptance inlet unavailable: %s %s; the dispatch "
                "stands but is NOT accepted (no acceptance record)"
                % (type(exc).__name__, str(exc)[:160]))
            return res
        status = outcome.get("status")
        if status == "presented":
            res.reasons.append(
                "acceptance: presented as candidate "
                "(run %s, state %s); completed != accepted"
                % (outcome.get("run_id"), outcome.get("state")))
        elif status == "refused":
            res.reasons.append(
                "acceptance: REFUSED by the %s panel: %s; no acceptance "
                "record exists; the result is NOT accepted"
                % (outcome.get("panel"), outcome.get("reason")))
        else:
            res.reasons.append(
                "acceptance: inlet returned %r; the result is NOT accepted"
                % (status,))
        return res

    def _wire_failure_gap(self, result: DispatchResult) -> DispatchResult:
        try:
            from swarm_engine.acquisition.dispatch_gaps import (
                maybe_register_dispatch_gap)
            wire = maybe_register_dispatch_gap(self.engine, result)
        except Exception:
            return result
        if wire.get("registered") and not wire.get("duplicate_of_open"):
            result.reasons.append(
                "gap registered: %s (route %s, outcome %s)" % (
                    wire.get("gap_id"), wire.get("route"),
                    wire.get("outcome")))
        return result

    # -- gap genesis wiring (V10-GAP-GENESIS) --------------------------------
    # A routing failure (unknown_intent: no capability matched) is not a
    # limitation or a missing dependency -- the existing dispatch_gaps
    # wiring does not cover it. This hook attempts gap GENESIS: the failed
    # request becomes a composition gap with VERIFIED examples synthesized
    # by decomposition (gap_genesis), which the existing gap route
    # dispatcher then drives through the composition inlet. Double-
    # contained like _wire_failure_gap; the dispatch result is returned
    # unchanged.

    def _wire_genesis_gap(self, result: DispatchResult, text: str,
                          args: Optional[Dict[str, Any]]) -> DispatchResult:
        try:
            from swarm_engine.synthesis.gap_genesis import (
                maybe_genesis_composition_gap)
            genesis = maybe_genesis_composition_gap(
                self.engine, text, args, result)
        except Exception:
            return result
        if genesis.get("genesis"):
            result.reasons.append(
                "gap genesis: %s (%s verified examples; use "
                "registry.dispatch to process)" % (
                    genesis.get("gap_id"),
                    genesis.get("examples")))
        return result

    # -- production limitation recorder (Q12) -------------------------------
    # When a governed dispatch fails on a REAL scale/format bound the
    # media substrate declares, the failure is not left as a bare
    # execution_failed (or, worse, a swallowed ok=True carrying a refusal
    # dict): the system records the limitation as a structured quarantine
    # reason (Q10's format_limitation_reason) and quarantines through the
    # governed path (quarantine_everywhere with caller=engine.oracle --
    # the system acting on its own observation, the same authorization
    # M5's proof used). A missing dep that isn't a gap is a gap the
    # system is ignoring; the same holds for a limitation the system
    # observed but didn't record.
    #
    # The classifier fires ONLY on the machinery's genuine refusal
    # signatures, with the attempted value checked against the REAL bound
    # constants imported from the media modules -- never hardcoded
    # numbers. Missing substrate, transient errors, bad args, and every
    # other failure keep the existing behavior. A quarantine failure
    # never breaks the dispatch refusal itself.

    def _record_dispatch_limitation(self, rec: Any,
                                    coerced: Dict[str, Any],
                                    failure: Any) -> Optional[str]:
        """Record a structured limitation reason for an observed dispatch
        failure and quarantine through the governed path.

        Returns the reason string, or None when the failure is not a
        declared-bound limitation (existing behavior kept).
        """
        try:
            effects = set(str(e) for e in ((rec.plan or {}).get("effects")
                                           or []))
        except Exception:
            return None
        failure_text = str(failure)
        probe = self._limitation_probe(effects, coerced or {}, failure_text)
        if probe is None:
            return None
        task, scale_or_format, preventing, reproduce = probe
        try:
            from swarm_engine.synthesis.integrity import (
                format_limitation_reason, quarantine_everywhere)
            reason = format_limitation_reason(
                task, scale_or_format,
                failure=failure_text[:500],
                preventing=preventing,
                reproduce=reproduce)
        except Exception:
            return None
        try:
            quarantine_everywhere(
                self.engine, rec.capability_id, reason,
                caller=getattr(self.engine, "oracle", None))
        except Exception as exc:
            # The dispatch refusal stands; the quarantine failure is
            # reported in the result, never raised.
            return reason + " [quarantine refused: %r]" % (exc,)
        return reason

    def _limitation_probe(self, effects: set, coerced: Dict[str, Any],
                          failure_text: str):
        """Classify an observed dispatch failure as a scale/format
        limitation.

        Returns (task, scale_or_format, preventing, reproduce) or None.
        The attempted scale is checked against the REAL bound constants
        imported from the media modules; the failure signature must be
        the machinery's genuine refusal text. Format failures: no
        production dispatch path currently decodes input bytes, so the
        machinery emits no format failure here -- the classifier
        recognizes nothing rather than inventing a mode.
        """
        # media_image_spec: _validate_spec raises ValueError, e.g.
        #   "spec.width: expected int in [1,2048], got 10000"
        # (surfaced by the composer as success=False).
        if "media_image_spec" in effects:
            spec = coerced.get("spec")
            if isinstance(spec, dict):
                w, h = spec.get("width"), spec.get("height")
                if (isinstance(w, int) and isinstance(h, int)
                        and "expected int in [" in failure_text):
                    from swarm_engine.media.image_spec import _MAX_SIDE
                    if w > _MAX_SIDE or h > _MAX_SIDE:
                        red = dict(spec)
                        red["width"] = 512
                        red["height"] = 512
                        return (
                            "render_image_spec", "%dx%dpx" % (w, h),
                            ("spec renderer declares a %dpx per-side bound "
                             "(swarm_engine.media.image_spec._MAX_SIDE)"
                             % _MAX_SIDE),
                            {"callable": ("swarm_engine.media.image_spec:"
                                          "render_spec"),
                             "args": {"spec": spec,
                                      "out_path": "probe_out.png"},
                             "reduced_args": {"spec": red,
                                              "out_path": "probe_out.png"}})
        # media_image: generate() returns {"ok": False, "error":
        #   "refused: dimensions out of bounds [1, 2048] (got 10000x10000)"}
        if "media_image" in effects:
            w, h = coerced.get("width"), coerced.get("height")
            if (isinstance(w, int) and isinstance(h, int)
                    and not isinstance(w, bool) and not isinstance(h, bool)
                    and "dimensions out of bounds" in failure_text):
                from swarm_engine.media.image import MAX_DIM
                if w > MAX_DIM or h > MAX_DIM:
                    args = {"prompt": coerced.get("prompt", ""),
                            "out_path": "probe_out.png",
                            "width": w, "height": h,
                            "seed": coerced.get("seed")}
                    red = dict(args)
                    red["width"] = 512
                    red["height"] = 512
                    return (
                        "render_image", "%dx%dpx" % (w, h),
                        ("image generator declares a %dpx per-side bound "
                         "(swarm_engine.media.image.MAX_DIM)" % MAX_DIM),
                        {"callable": ("swarm_engine.synthesis."
                                      "limitation_probe_adapters:"
                                      "probe_image_generate"),
                         "args": args, "reduced_args": red})
        # media_video: generate() returns {"ok": False, "error": ...}
        # from _validate, e.g. "width must be an integer in [16, 1280]".
        if "media_video" in effects:
            from swarm_engine.media import video as _vm
            w, h = coerced.get("width"), coerced.get("height")
            dur, fps = coerced.get("duration_s"), coerced.get("fps")
            axis = None
            if (isinstance(w, int) and not isinstance(w, bool)
                    and w > _vm.MAX_WIDTH
                    and "must be an integer in [" in failure_text):
                axis = ("width", w, _vm.MAX_WIDTH, "px")
            elif (isinstance(h, int) and not isinstance(h, bool)
                    and h > _vm.MAX_HEIGHT
                    and "must be an integer in [" in failure_text):
                axis = ("height", h, _vm.MAX_HEIGHT, "px")
            elif (isinstance(dur, (int, float)) and not isinstance(dur, bool)
                    and dur > _vm.MAX_DURATION_S
                    and "duration_s must be in" in failure_text):
                axis = ("duration_s", dur, _vm.MAX_DURATION_S, "s")
            elif (isinstance(fps, int) and not isinstance(fps, bool)
                    and fps > _vm.MAX_FPS
                    and "fps must be an integer in" in failure_text):
                axis = ("fps", fps, _vm.MAX_FPS, "fps")
            if axis is not None:
                name, val, bound, unit = axis
                args = {"prompt": coerced.get("prompt", ""),
                        "out_path": "probe_out.mp4",
                        "duration_s": dur, "fps": fps,
                        "width": w, "height": h,
                        "seed": coerced.get("seed")}
                red = dict(args)
                red[name] = {"width": 640, "height": 360,
                             "duration_s": 4.0, "fps": 24}[name]
                return (
                    "render_video", "%s=%s%s" % (name, val, unit),
                    ("video generator declares %s <= %s%s "
                     "(swarm_engine.media.video)" % (name, bound, unit)),
                    {"callable": ("swarm_engine.synthesis."
                                  "limitation_probe_adapters:"
                                  "probe_video_generate"),
                     "args": args, "reduced_args": red})
        return None

    def _native_capture(self, res: DispatchResult, dispatch_id: str,
                        coerced: Dict[str, Any], value: Any) -> None:
        """Item 2: native organizational-learning capture on success.

        Runs only when a learning hook AND an agent context are both
        bound; otherwise the dispatch simply has no learning side effect.
        The call runs as the learning hook's engine caller (the dispatcher
        never holds agent credentials); attribution comes from the bound
        agent_id, and capture_dispatch_evidence re-verifies the
        agent/assignment binding itself. Any exception is recorded as
        res.learning_error -- it never fails the successful dispatch.
        """
        if self.learning is None or self._learning_agent is None:
            return
        agent_id, assignment_id = self._learning_agent
        try:
            caller = self.learning.engine_caller
        except Exception as exc:
            res.learning_error = (
                f"native capture skipped: learning hook has no engine "
                f"caller ({exc!r})")
            return
        try:
            self.learning.capture_evidence(
                dispatch_id=dispatch_id, agent_id=agent_id,
                assignment_id=assignment_id, args=coerced,
                result_value=value, caller=caller)
        except Exception as exc:
            res.learning_error = (
                f"{type(exc).__name__}: {exc}")[:400]

    def history(self, capability_id: Optional[str] = None,
                limit: int = 50) -> List[Dict[str, Any]]:
        con = sqlite3.connect(self.engine.db_path)
        try:
            con.row_factory = sqlite3.Row
            if capability_id:
                rows = con.execute(
                    "SELECT * FROM intent_dispatches WHERE capability_id=? "
                    "ORDER BY ts DESC LIMIT ?", (capability_id, limit)).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM intent_dispatches ORDER BY ts DESC LIMIT ?",
                    (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
