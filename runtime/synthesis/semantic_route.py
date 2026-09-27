"""Intent routing + direct answers for the NL semantic layer (Worker B).

This layer sits on top of semantic intent frames (semantic_frames.py) and
handles the intents that can be answered directly WITHOUT running the
engine's acquisition machinery:

* ANSWER_META  -> answered from the LIVE engine inventory (capabilities,
                  primitives, media substrate). Never hard-coded: every
                  value is read at call time, so the answer changes when
                  the inventory changes.
* COMPUTE      -> answered by a SAFE AST-based arithmetic evaluator.
                  No eval(); malformed expressions fail closed.
* AMBIGUOUS / UNKNOWN -> handled=True with an HONEST "I don't understand"
                  message. This is not a run failure; it is the correct
                  fail-closed behaviour required by the frame contract.
* everything else (CREATE_*, ANSWER_FACTUAL, EXECUTE, ROUTE) ->
                  handled=False; downstream machinery owns these.

route_frame never raises on a well-typed IntentFrame. Missing engine
attributes are reported honestly in the answer text, never invented.
"""
from __future__ import annotations

import ast
import operator
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from swarm_engine.synthesis.semantic_frames import Intent, IntentFrame


@dataclass
class SemanticRouteResult:
    """The outcome of routing one IntentFrame through the direct-answer layer."""
    handled: bool                        # True iff this layer fully handles the frame
    answer_text: Optional[str] = None    # the direct answer, when handled
    refusal: Optional[str] = None        # machine-readable refusal code when failing closed
    detail: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_DIRECTLY_HANDLED = (Intent.ANSWER_META, Intent.COMPUTE)
_FAIL_CLOSED = (Intent.AMBIGUOUS, Intent.UNKNOWN)


def route_frame(frame: IntentFrame, engine: Any) -> SemanticRouteResult:
    """Route a semantic intent frame to a direct answer or downstream.

    Returns a SemanticRouteResult; never raises on an IntentFrame input.
    Non-frame input is treated as UNKNOWN (fail closed).
    """
    if not isinstance(frame, IntentFrame):
        return SemanticRouteResult(
            handled=True,
            answer_text=("I don't understand what you're asking for — "
                         "could you rephrase that?"),
            refusal="unknown_intent",
            detail={"reason": "input was not an IntentFrame"},
        )

    intent = frame.intent

    if intent == Intent.ANSWER_META:
        return _answer_meta(frame, engine)
    if intent == Intent.COMPUTE:
        return _answer_compute(frame, engine)
    if intent == Intent.AMBIGUOUS:
        return _fail_closed_ambiguous(frame)
    if intent == Intent.UNKNOWN:
        return _fail_closed_unknown(frame)

    # Everything else (CREATE_FILE/IMAGE/VIDEO/SONG/VOICE, ANSWER_FACTUAL,
    # EXECUTE, ROUTE) belongs to downstream machinery.
    return SemanticRouteResult(
        handled=False,
        answer_text=None,
        refusal=None,
        detail={
            "reason": f"intent {intent.value!r} is owned by downstream machinery",
            "intent": intent.value,
        },
    )


# ---------------------------------------------------------------------------
# ANSWER_META — live inventory answer
# ---------------------------------------------------------------------------

def _live_inventory(engine: Any) -> Dict[str, Any]:
    """Read the real inventory from the live engine at call time.

    Every field is probed defensively: a missing attribute is recorded as
    missing (and said honestly in the answer), never invented.
    """
    inv: Dict[str, Any] = {}

    # -- capability store -------------------------------------------------
    store = getattr(engine, "capabilities", None)
    if store is None:
        inv["capabilities_missing"] = True
    else:
        try:
            records = store.list(status="active")
        except Exception as exc:  # fail honest, not loud
            inv["capabilities_error"] = f"{type(exc).__name__}: {exc}"
        else:
            inv["capability_count"] = len(records)
            inv["capability_names"] = [getattr(r, "name", None) or
                                       getattr(r, "capability_id", "?")
                                       for r in records]

    # -- primitive registry -----------------------------------------------
    registry = getattr(engine, "primitives", None)
    if registry is None:
        inv["primitives_missing"] = True
    else:
        try:
            inv["primitive_count"] = len(registry)
            inv["primitive_families"] = registry.families()
        except Exception as exc:
            inv["primitives_error"] = f"{type(exc).__name__}: {exc}"

    # -- media substrate ---------------------------------------------------
    inv["media"] = _probe_media_substrate()

    return inv


def _probe_media_substrate() -> Dict[str, Any]:
    """Which media generators can actually deliver right now.

    Delegates to the truthful ``swarm_engine.services.media.status()``:
    each medium runs a genuine minimal generation through the real
    substrate path and is reported available ONLY if the substrate
    actually delivered. Merely checking that the generator functions are
    importable (the old behavior) claimed availability for media whose
    substrate was absent -- e.g. ``synthesize`` is callable while piper
    is not installed -- and the ANSWER_META text repeated the lie.

    Results are cached briefly: the values are environmental and change
    rarely, while a full probe costs seconds (model load). Never raises:
    if the probe harness itself fails, falls back to the import check and
    the answer degrades gracefully instead of breaking the meta path.
    """
    now = time.time()
    if (now - _media_probe_cache["at"] < _MEDIA_PROBE_TTL_S
            and _media_probe_cache["found"]):
        return dict(_media_probe_cache["found"])
    try:
        from swarm_engine.services import media as media_svc
        st = media_svc.status()
        found = {
            "image": bool(st.get("generate_image", {}).get("available")),
            "video": bool(st.get("generate_video", {}).get("available")),
            "song": bool(st.get("assemble_song", {}).get("available")),
            "voice": bool(st.get("synthesize_voice", {}).get("available")),
        }
    except Exception:
        found = _import_check_media_substrate()
    _media_probe_cache["at"] = now
    _media_probe_cache["found"] = found
    return dict(found)


# Brief cache for the substrate probe above: environmental truth that is
# expensive to re-derive (seconds per full probe when models must load).
_MEDIA_PROBE_TTL_S = 120.0
_media_probe_cache: Dict[str, Any] = {"at": 0.0, "found": {}}


def _import_check_media_substrate() -> Dict[str, Any]:
    """Fallback: which media generators are importable/callable.

    Necessary but NOT sufficient for delivery -- used only when the real
    probe harness fails. (Kept from the original _probe_media_substrate;
    the song entry point is assemble_song, not generate -- see the Worker
    D note below.)
    """
    found: Dict[str, Any] = {}
    # NOTE (Worker D repair): the song substrate's real entry point is
    # assemble_song, not generate -- probing for "generate" reported song
    # as absent when the verified substrate was present, and the
    # ANSWER_META text understated the inventory. Probe the real names.
    checks = (
        ("image", "swarm_engine.media.image", "generate"),
        ("video", "swarm_engine.media.video", "generate"),
        ("song", "swarm_engine.media.song", "assemble_song"),
        ("voice", "swarm_engine.media.voice", "synthesize"),
    )
    for kind, module_path, func_name in checks:
        try:
            module = __import__(module_path, fromlist=[func_name])
        except Exception:
            found[kind] = False
            continue
        found[kind] = callable(getattr(module, func_name, None))
    return found


def _answer_meta(frame: IntentFrame, engine: Any) -> SemanticRouteResult:
    inv = _live_inventory(engine)
    topic = frame.entities.get("topic")

    parts: list[str] = []

    # capabilities
    if inv.get("capabilities_missing"):
        parts.append("this engine doesn't expose a capability store, so I "
                     "can't enumerate its admitted capabilities")
    elif "capabilities_error" in inv:
        parts.append(f"I couldn't read the capability store ({inv['capabilities_error']})")
    else:
        n = inv["capability_count"]
        if n == 0:
            parts.append("no task capabilities have been admitted to my "
                         "capability store yet")
        else:
            names = inv["capability_names"][:5]
            named = f", including {', '.join(names)}" if names else ""
            parts.append(f"{n} admitted task capabilit{'y' if n == 1 else 'ies'}{named}")

    # primitives
    if inv.get("primitives_missing"):
        parts.append("this engine doesn't expose a primitive registry")
    elif "primitives_error" in inv:
        parts.append(f"I couldn't read the primitive registry ({inv['primitives_error']})")
    else:
        n = inv["primitive_count"]
        families = inv.get("primitive_families") or {}
        fam_list = ", ".join(sorted(families)) if families else "none listed"
        parts.append(f"{n} built-in primitives across families: {fam_list}")

    # media
    media = inv.get("media") or {}
    available = [k for k, ok in media.items() if ok]
    if available:
        parts.append("media generation available for: " + ", ".join(available))
    else:
        parts.append("no media generation substrate is currently available")

    body = "; ".join(parts) + "."
    answer = (f"Here's what I can actually do right now. {body} "
              f"I can also answer factual questions about my own inventory "
              f"and do exact arithmetic.")

    return SemanticRouteResult(
        handled=True,
        answer_text=answer,
        refusal=None,
        detail={"topic": topic, "inventory": inv},
    )


# ---------------------------------------------------------------------------
# COMPUTE — safe AST-based arithmetic
# ---------------------------------------------------------------------------

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class _UnsafeExpression(Exception):
    """Raised when the expression contains anything outside arithmetic."""


def _safe_eval(node: ast.AST) -> Any:
    """Recursively evaluate an AST; only pure arithmetic nodes are allowed."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_BINOPS.get(type(node.op))
        if op is None:
            raise _UnsafeExpression(f"operator {type(node.op).__name__} not allowed")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_UNARYOPS.get(type(node.op))
        if op is None:
            raise _UnsafeExpression(f"unary operator {type(node.op).__name__} not allowed")
        return op(_safe_eval(node.operand))
    if isinstance(node, ast.Constant):
        # bool is a subclass of int — exclude it; only real numbers.
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _UnsafeExpression(f"constant of type {type(node.value).__name__} not allowed")
        return node.value
    # ast.Num / ast.Str etc. on older Pythons collapse to Constant; anything
    # else (Name, Call, Attribute, Subscript, ...) is rejected.
    raise _UnsafeExpression(f"node {type(node).__name__} not allowed")


def _format_number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def evaluate_expression(expression: str) -> Any:
    """Safely evaluate an arithmetic expression string.

    Raises _UnsafeExpression for anything outside pure arithmetic and
    lets through SyntaxError/ValueError from parsing plus arithmetic
    runtime errors (ZeroDivisionError, OverflowError) for the caller to
    classify.
    """
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("empty expression")
    tree = ast.parse(expression, mode="eval")
    return _safe_eval(tree)


def _answer_compute(frame: IntentFrame, engine: Any) -> SemanticRouteResult:
    expression = frame.entities.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        return SemanticRouteResult(
            handled=True,
            answer_text=("I couldn't find an arithmetic expression to compute "
                         "in your request — could you rephrase it with the "
                         "numbers and operators, like \"5*6\"?"),
            refusal="compute_malformed",
            detail={"expression": expression},
        )
    try:
        value = evaluate_expression(expression)
    except (SyntaxError, ValueError, _UnsafeExpression) as exc:
        return SemanticRouteResult(
            handled=True,
            answer_text=(f"I can't compute {expression!r} — it isn't a valid "
                         f"arithmetic expression. {exc}"),
            refusal="compute_malformed",
            detail={"expression": expression, "error": str(exc)},
        )
    except (ZeroDivisionError, OverflowError, ArithmeticError) as exc:
        return SemanticRouteResult(
            handled=True,
            answer_text=(f"I can't compute {expression!r} — {exc}."),
            refusal="compute_error",
            detail={"expression": expression, "error": str(exc)},
        )
    return SemanticRouteResult(
        handled=True,
        answer_text=_format_number(value),
        refusal=None,
        detail={"expression": expression, "value": value},
    )


# ---------------------------------------------------------------------------
# AMBIGUOUS / UNKNOWN — honest fail-closed
# ---------------------------------------------------------------------------

def _fail_closed_ambiguous(frame: IntentFrame) -> SemanticRouteResult:
    alternatives = [a.intent.value for a in (frame.alternatives or [])]
    if alternatives:
        readings = " or ".join(f"'{a}'" for a in alternatives[:3])
        text = (f"I'm not sure what you're asking for — it could be {readings}. "
                f"Could you rephrase so it's clear which one you mean?")
    else:
        text = ("I'm not sure what you're asking for — I see more than one "
                "possible reading. Could you rephrase?")
    return SemanticRouteResult(
        handled=True,
        answer_text=text,
        refusal="ambiguous_intent",
        detail={"alternatives": alternatives},
    )


def _fail_closed_unknown(frame: IntentFrame) -> SemanticRouteResult:
    return SemanticRouteResult(
        handled=True,
        answer_text=("I don't understand what you're asking for — could you "
                     "rephrase that?"),
        refusal="unknown_intent",
        detail={"raw": frame.raw},
    )
