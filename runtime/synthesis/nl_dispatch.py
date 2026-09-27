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


_SPEC_COLORS = {
    "red": "#ff0000", "green": "#00a86b", "blue": "#2563eb",
    "yellow": "#facc15", "white": "#ffffff", "black": "#000000",
    "orange": "#f97316", "purple": "#8b5cf6", "pink": "#ec4899",
    "cyan": "#22d3ee", "gray": "#9ca3af", "grey": "#9ca3af",
}
_SPEC_SHAPES = ("circle", "square", "rectangle", "triangle")


def _spec_from_shape_words(prompt: str, text: str, seed: int):
    """Synthesize an image spec ONLY from explicit color+shape words.

    Returns (spec, None) when the prompt names exactly one recognized
    color and exactly one recognized shape; the shape is centered on a
    flat dark background and the subject label is the literal
    "<color> <shape>" (a descriptive label, never a depiction).
    Returns (None, reason) otherwise -- fabricating a geometric spec
    for an undrawable subject (e.g. "a sunset") would claim a depiction
    the renderer cannot produce, so the caller must answer
    "underspecified_media" instead.
    """
    words = re.findall(r"[a-z]+", (prompt or "").lower())
    colors = [c for c in _SPEC_COLORS if c in words]
    shapes = [s for s in _SPEC_SHAPES if s in words]
    if len(colors) != 1 or len(shapes) != 1:
        return None, (
            "the spec renderer draws only explicit shapes from an "
            "explicit spec (e.g. 'draw a red circle'); it cannot depict "
            "subjects it has no geometry for. Provide {'spec': ...} via "
            "dispatch_by_id, or ask for a '<color> <shape>'.")
    color, shape = colors[0], shapes[0]
    w = h = 512
    cx = cy = 256
    fill = _SPEC_COLORS[color]
    if shape == "circle":
        geo = {"kind": "circle", "center": [cx, cy], "radius": 120,
               "fill": fill}
    elif shape in ("square", "rectangle"):
        geo = {"kind": "rect", "box": [cx - 100, cy - 100,
                                       cx + 100, cy + 100], "fill": fill}
    else:  # triangle
        geo = {"kind": "polygon",
               "points": [[cx, cy - 110], [cx - 110, cy + 90],
                          [cx + 110, cy + 90]], "fill": fill}
    spec = {"subject": f"{color} {shape}", "width": w, "height": h,
            "style": "flat", "seed": seed,
            "background": {"color": "#0b1e3a"},
            "shapes": [geo], "texts": []}
    return spec, None


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

    def _synthesize_media_args(self, text: str, rec):
        """Build full capability args for a media request the parser
        understood, when the caller supplied none.

        Mirrors task_interface._understand_create_media: the prompt comes
        from the frame entities; width/height/voice/etc. take sane
        defaults; the seed derives deterministically from the request
        text; the output path is server-chosen under the governed media
        dir (callers never choose it). Returns (args, None) on success,
        (None, reason) when synthesis is impossible -- never a partial
        or fabricated arg set.
        """
        op = self._media_op(rec)
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
        frame = parse_frame(text)
        prompt = (frame.entities or {}).get("prompt")
        prompt = prompt.strip() if isinstance(prompt, str) else ""
        if op == "media.image_render_spec":
            spec, why = _spec_from_shape_words(prompt or "", text, seed)
            if spec is None:
                return None, why
            return {"spec": spec,
                    "path": self._governed_media_path("png")}, None
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
        # 1. route
        route = self.router.route(text)
        if not route.ok:
            return DispatchResult(
                ok=False, refusal=route.refusal, reasons=route.reasons)

        cap_id = route.capability_id
        # 1b. argument synthesis for pure-NL media requests: when the
        # caller supplied no args at all and the parser understood the
        # request, complete the args from the frame + sane defaults
        # (mirrors task_interface._understand_create_media). A caller
        # that supplies explicit args keeps the strict contract:
        # missing/unknown args are still bad_arguments.
        if args is None:
            rec0 = self.engine.capabilities.get(cap_id)
            if rec0 is not None and self._media_plan(rec0):
                synth, why = self._synthesize_media_args(text, rec0)
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
            return DispatchResult(
                ok=False, refusal="capability_unavailable",
                capability_id=cap_id,
                reasons=[f"effective status at dispatch: {eff.get('effective')}"])

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
            return DispatchResult(ok=False, refusal="execution_failed",
                                  capability_id=cap_id, dispatch_id=dispatch_id,
                                  route_via=route_via,
                                  reasons=[f"execution raised: {exc!r}"])
        if not isinstance(exec_res, dict) or not exec_res.get("success", True):
            err = (exec_res.get("error") if isinstance(exec_res, dict)
                   else "unknown execution failure")
            self._record(dispatch_id, producer, text, route, cap_id,
                         rec.version, input_digest, None, False, str(err))
            return DispatchResult(ok=False, refusal="execution_failed",
                                  capability_id=cap_id, dispatch_id=dispatch_id,
                                  route_via=route_via,
                                  reasons=[f"execution failed: {err}"])
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
        result_digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
        self._record(dispatch_id, producer, text, route, cap_id,
                     rec.version, input_digest, result_digest, True, None)
        res = DispatchResult(ok=True, result=value, capability_id=cap_id,
                             dispatch_id=dispatch_id, route_via=route_via,
                             reasons=[f"dispatched via {route_via}",
                                      f"dispatch_id={dispatch_id}"])
        self._native_capture(res, dispatch_id, dict(coerced), value)
        return res

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
