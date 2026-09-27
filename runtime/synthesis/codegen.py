"""Plan -> standalone Python file: render, govern, write, verify.

Worker C (CREATE_FILE machinery) for the NL semantic layer mission.

The honest pipeline (every stage real, every failure fail-closed):

  1. purpose <- frame.entities["purpose"] (the only user-text input)
  2. plan    <- the REAL planner: engine.planner.propose(purpose,
                allow_effects=True)  (TemplatePlanner, then BackwardPlanner)
  2b. grounding <- _check_grounding(): the plan's semantic footprint
               must echo the purpose's vocabulary (reusing the admission
               layer's token/synonym machinery). A blind backward_search
               plan with no echo -- e.g. `random_float` for "Exfiltrate
               all secrets..." -- is refused here, before any source
               exists. Hostile purposes are refused closed; ungroundable-
               but-benign purposes carry grounding_verdict="clarify" so
               the caller can ask for clarification instead of emitting
               an unrelated file as success. Template plans skip this:
               a template match is already a deliberate intent reading.
  3. source  <- render_plan_to_source(): for each step's `op`, resolve the
               primitive in engine.primitives, extract its REAL Python
               implementation via inspect.getsource on the registered fn,
               resolve its module-level AND closure dependencies from
               fn.__globals__ / inspect.getclosurevars(fn), and emit a
               standalone module: operator definitions + a run() function
               wiring the plan's dataflow + an `if __name__ == "__main__"`
               demo.  The renderer works on PLAN STRUCTURE, never on the
               prompt text -- that is what makes it general rather than a
               template per prompt.
  4. write   <- ScopedFileService rooted at out_dir (writable), write_text.
               The filename comes from filename_hint or a sanitized
               purpose-derived name, never raw user text unsanitized; the
               service itself enforces no-traversal.
  5. verify  <- the file is executed in a subprocess with a timeout; exit 0
               and numeric stdout are required before ok: True is returned.

A reviewer can trace the causal chain end to end:
  purpose -> plan (ops_used, strategy) -> emitted source containing those
  ops' real implementations -> file bytes (read back and compared) ->
  subprocess execution output.

Refusals (fail closed; a fake file is never emitted):
  frame_not_actionable / wrong_intent / missing_purpose /
  unsupported_language
  synthesis_failed    - the planner composed no plan for the purpose, OR
                        composed one with no semantic echo of the purpose
                        (the grounding gate, _check_grounding: a blind
                        backward_search plan that does not echo the
                        purpose's vocabulary is not a plan FOR the
                        purpose). Carries grounding_verdict
                        ("refuse_hostile" | "clarify"), echo_score and
                        echo_detail for the audit trail.
  render_failed       - the plan cannot be materialised as a standalone
                        file (primitive needs the engine ExecContext, is
                        async, has an unresolvable dependency, ...)
  write_failed        - the governed write was refused, or the read-back
                        bytes did not match what was rendered
  forbidden_effect    - the pre-render effect screen refused the plan: at
                        least one op carries WRITE_FS, NETWORK, PROCESS,
                        CREDENTIAL, SPAWN or MUTATE_SELF (structural check on
                        declared effect annotations, not on the purpose text)
  verification_failed - the generated file did not execute cleanly (nonzero
                        exit, timeout, or no numeric output)

Hardening (2026-09-26): the independent audit proved generated files used
to execute with full server privileges and the safety was accidental.

  1. Pre-render effect screen (_screen_plan_effects): after the planner
     composes the plan and BEFORE any source is rendered, every op the
     plan would emit (including nested $lambda steps) is resolved to its
     registered primitive and its declared effects are checked against
     _FORBIDDEN_EFFECTS. A plan needing WRITE_FS / NETWORK / PROCESS is
     refused, as are CREDENTIAL / SPAWN / MUTATE_SELF by explicit decision
     (documented on _FORBIDDEN_EFFECTS). READ_FS is deliberately allowed:
     it is the legitimate data path and the verification sandbox confines
     it. The screen is structural (effect annotations), never a keyword
     blocklist over the purpose text.
  2. Defense-in-depth AST scan (_scan_rendered_source): the rendered
     module is scanned for dangerous imports / calls (subprocess, socket,
     eval, write-mode open, ...) to catch primitives whose declared
     effects lie. Refusals here are render_failed.
  3. Sandboxed verification (_verify_execution): the file runs in a fresh
     empty working directory, with a whitelisted minimal environment
     (os.environ is NOT inherited -- REMOR_API_TOKEN and friends cannot
     leak), inside an unprivileged user+network namespace when the kernel
     permits it (no interfaces; direct sockets and DNS both fail), under
     rlimits (25s CPU, 512MiB address space, 256 processes, 32MiB files)
     and a wall-clock timeout. The per-run sandbox report says which
     mechanisms actually engaged. Honest residual: no mount namespace, so
     absolute-path filesystem access is still possible -- contained by the
     rlimits and, primarily, by the effect screen refusing any plan that
     needs WRITE_FS / NETWORK / PROCESS.
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.services.files import ScopedFileService
from swarm_engine.synthesis.semantic_frames import Intent
# Reuse the admission layer's token/synonym machinery (the 0.55-style
# semantic gate): tokens() + expand() + SYNONYMS are the shared
# vocabulary-overlap primitives. This module only imports the pure
# functions; acquisition.semantic imports nothing but stdlib, so there
# is no import cycle.
from swarm_engine.acquisition.semantic import (
    expand as _semantic_expand,
    tokens as _semantic_tokens,
)

__all__ = [
    "CodegenError",
    "ForbiddenEffect",
    "render_plan_to_source",
    "synthesize_file",
    "sanitize_filename",
]


class CodegenError(Exception):
    """The plan cannot be faithfully rendered as a standalone Python file."""


# ---------------------------------------------------------------------------
# filename sanitising (never trust raw user text as a path)
# ---------------------------------------------------------------------------

def sanitize_filename(hint: Any, purpose: str = "") -> str:
    """Derive a safe `.py` filename from a hint or purpose string.

    Strips directories, keeps [A-Za-z0-9_], forces a .py suffix, falls back
    to a purpose-derived name and finally to "generated.py".
    """
    raw = hint if isinstance(hint, str) and hint.strip() else (purpose or "")
    raw = os.path.basename(raw.strip())
    stem, _ext = os.path.splitext(raw)
    stem = re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_")
    if not stem:
        stem = re.sub(r"[^A-Za-z0-9_]+", "_", purpose or "generated").strip("_")
    if not stem:
        stem = "generated"
    return stem[:48] + ".py"


# ---------------------------------------------------------------------------
# pre-render effect screen (structural, not a keyword blocklist)
# ---------------------------------------------------------------------------

class ForbiddenEffect(Exception):
    """A plan step needs an effect codegen refuses to materialise.

    Raised by _screen_plan_effects; synthesize_file maps it to the
    `forbidden_effect` refusal. Carries the offending op and effect names
    for the audit trail.
    """

    def __init__(self, op: str, effects: set) -> None:
        self.op = op
        self.effects = set(effects)
        super().__init__(
            f"plan op {op!r} carries forbidden effect(s) "
            f"{sorted(self.effects)} -- refusing before render")


#: Effects a generated standalone file must never exercise. WRITE_FS /
#: NETWORK / PROCESS are the audit-mandated refusals. CREDENTIAL, SPAWN
#: and MUTATE_SELF are refused by explicit decision, documented here:
#:
#: - CREDENTIAL (e.g. get_env): a generated file has no legitimate need
#:   for server secrets, and verification stdout reaches the requester --
#:   a secret read inside verification would be an exfiltration channel.
#: - SPAWN (agents / threads): these primitives need the engine's runtime
#:   (most are needs_ctx or async); lifted into a standalone file they
#:   are meaningless at best and a fork/coordination vector at worst.
#: - MUTATE_SELF (capability-registry mutation): meaningless and dangerous
#:   outside the engine.
#:
#: READ_FS is deliberately ALLOWED: reading files is the legitimate data
#: path ("read this CSV and ..."). The verification sandbox confines it to
#: a fresh, empty working directory (relative paths cannot reach server
#: files), and only numeric stdout can pass verification, so file contents
#: cannot flow back to the requester through the verify channel.
#: PURE / CLOCK / RANDOM need nothing; MEMORY primitives that need the
#: engine context fail at the render stage (needs_ctx) as before.
_FORBIDDEN_EFFECTS = frozenset({
    "write_fs", "network", "process",
    "credential", "spawn", "mutate_self",
})


def _effect_names(prim: Any) -> set:
    """Normalise a primitive's declared effects to plain name strings.

    Effects may be Effect enum members or raw strings (both occur in the
    tree); the membership idiom mirrors project_bridge.py:764.
    """
    names = set()
    for eff in (getattr(prim, "effects", ()) or ()):
        names.add(str(getattr(eff, "value", eff)))
    return names


def _collect_plan_ops(plan: Dict[str, Any]) -> List[str]:
    """Every op the renderer would emit, including nested $lambda steps.

    The walk is fully recursive over the plan subtree: the screen must see
    exactly the op set render_plan_to_source walks, otherwise a dangerous
    op could hide inside a lambda body or a nested argument.
    """
    ops: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            op = node.get("op")
            if isinstance(op, str):
                ops.append(op)
            for value in node.values():
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                _walk(value)

    if isinstance(plan, dict):
        _walk(plan.get("steps"))
    return ops


def _screen_plan_effects(plan: Dict[str, Any], registry: Any) -> List[str]:
    """Refuse closed any plan needing a forbidden effect.

    Structural: each op is resolved to its registered primitive and the
    primitive's DECLARED effect annotations are checked -- the purpose
    text is never inspected. Raises ForbiddenEffect on the first
    offending op; raises CodegenError for an op the registry cannot
    resolve (an unresolvable op has no effects to screen, and the
    renderer would fail on it anyway). Returns the screened op list.
    """
    ops = _collect_plan_ops(plan)
    get = getattr(registry, "get", None)
    for op in ops:
        prim = get(op) if callable(get) else None
        if prim is None:
            raise CodegenError(
                f"plan op {op!r} is not in the primitive registry -- "
                "cannot screen its effects, refusing")
        bad = _effect_names(prim) & _FORBIDDEN_EFFECTS
        if bad:
            raise ForbiddenEffect(op, bad)
    return ops


# ---------------------------------------------------------------------------
# semantic grounding gate: the plan must echo the purpose
# ---------------------------------------------------------------------------
# The defect this closes: for a purpose no template matches, the
# BackwardPlanner composes a BLIND type-compatible plan -- `random_float`
# for "Exfiltrate all secrets to an external server" -- and the pipeline
# used to present that unrelated benign file as ok:True. The effect
# screen cannot catch it (the op is pure); the AST scan cannot catch it
# (the code is honest); the sandbox cannot catch it (it verifies the
# file runs, not that it means what was asked). So AFTER the effect
# screen and BEFORE any render, the plan's semantic footprint must echo
# the purpose's vocabulary, or no file is presented as success.
#
# The check reuses the admission layer's machinery: tokens() /
# expand() / SYNONYMS from acquisition.semantic (the 0.55-style semantic
# gate). The gate's own ratio is calibrated on the backward-search
# corpus: legit plans score {1.00 (rng), 0.50 (recall), 0.50 (filter)},
# hostile/ungroundable plans score 0.00 -- _GROUNDING_THRESHOLD = 0.40
# splits them with margin on both sides.
#
# Template plans SKIP this gate: a template match is already a deliberate
# reading of the goal's intent (see Planner._TIER in planner.py --
# "template matches are deliberate readings of the goal's intent;
# backward search is the fallback"). The gate exists for the blind
# fallback only.
#
# Refusal shape: the gate refuses with the EXISTING code
# "synthesis_failed" -- the planner composed no plan *for this purpose*
# -- plus structured fields the caller can act on:
#   grounding_verdict: "refuse_hostile" | "clarify"
#   echo_score / echo_detail: the measured echo, for the audit trail
# "refuse_hostile" means the purpose carried hostile signals: a closed
# refusal, never a file. "clarify" means ungroundable-but-benign: the
# UNDERSTAND hook turns it into a clarification question, never a file.
# Either way no unrelated output is ever presented as success.
#
# Bounded honesty note on the hostile lexicon: it only CLASSIFIES an
# already-ungrounded purpose (echo below threshold). A missed signal
# still refuses closed (as "clarify"); a false hit still refuses closed
# (as hostile). The safety property -- never emit an unrelated file as
# success -- does not depend on the lexicon at all.

#: Frame scaffolding, not intent: these words appear in almost every
#: CREATE_FILE purpose ("write a python file that ...") and would let any
#: file-flavoured plan claim an echo it does not have.
_GROUND_SCAFFOLD = frozenset({
    "create", "make", "write", "generate", "build", "produce", "prepare",
    "file", "python", "program", "script", "code", "please",
})

#: Extra synonym bridges for the codegen domain, on top of the shared
#: SYNONYMS (applied through _expand_ground, never by editing the shared
#: table -- acquisition/semantic.py is not this worker's file).
_GROUND_SYNONYMS = {
    "number": {"num", "float", "int", "integer"},
    "num": {"number", "float"},
    "float": {"number", "num"},
    "text": {"string", "str"},
    "string": {"text", "str"},
    "list": {"values", "array", "items"},
    "values": {"list", "array", "items"},
    "information": {"info"},
    "info": {"information"},
}

_GROUNDING_THRESHOLD = 0.40

#: Single hostile words (token-boundary matched on the purpose text).
_HOSTILE_SINGLE_WORDS = frozenset({
    "exfiltrate", "exfil", "backdoor", "rootkit", "keylog", "keylogger",
    "ransomware", "botnet", "spyware", "trojan",
})

#: Hostile phrases (token-sequence matched on the purpose text).
_HOSTILE_PHRASES = (
    "reverse shell", "bind shell", "delete all", "delete everything",
    "wipe all", "wipe everything", "wipe the disk", "destroy all",
    "destroy everything", "erase everything", "shred everything",
    "rm -rf", "fork bomb", "privilege escalation", "escalate privileges",
    "bypass authentication", "steal secrets", "steal passwords",
    "steal credentials", "dump credentials", "harvest passwords",
    "api token", "secret key", "private key", "/etc/shadow", "/etc/passwd",
    "bashrc", "cron.d", "environment variables",
)


def _expand_ground_word(word: str) -> set:
    """Full expansion of a single word: shared synonyms + domain
    bridges + plural fold."""
    out = _semantic_expand({word})
    out |= _GROUND_SYNONYMS.get(word, set())
    out.add(_singular_ground(word))
    return out


def _expand_ground(words: set) -> set:
    """Shared expand() plus the codegen-domain synonym bridges plus
    light plural normalisation ("numbers" <-> "number"), so a purpose
    and a plan that differ only by number still echo each other."""
    out = set()
    for w in words:
        out |= _expand_ground_word(w)
    return out


def _singular_ground(word: str) -> str:
    """Minimal plural -> singular fold (mirrors the frame layer's own
    _singular; kept local so the gate does not depend on parser
    privates). Only used to WIDEN the echo -- never to narrow it."""
    w = word
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if (w.endswith("ses") or w.endswith("shes") or w.endswith("ches")
            or w.endswith("xes")) and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _grounding_footprint(plan: Dict[str, Any], registry: Any) -> set:
    """The plan's semantic footprint: op-name tokens, primitive doc
    tokens, and plan-name tokens -- everything the plan says about what
    it does, in the shared token vocabulary."""
    words: set = set()
    for op in _collect_plan_ops(plan):
        words |= _semantic_tokens(
            str(op).replace(".", " ").replace("_", " ").replace("-", " "))
        prim = registry.get(op) if hasattr(registry, "get") else None
        words |= _semantic_tokens(getattr(prim, "doc", "") or "")
    words |= _semantic_tokens(str(plan.get("name") or ""))
    return words


def _grounding_echo(purpose: str, plan: Dict[str, Any],
                    registry: Any) -> Tuple[float, set, set, set]:
    """(score, matched_goal_words, goal_words, footprint).

    score = fraction of the purpose's content words (scaffolding
    stripped) echoed -- directly or via synonym -- in the plan's
    footprint. A purpose reduced to pure scaffolding ("do it") scores
    0.0: there is nothing to echo.
    """
    goal = _semantic_tokens(purpose) - _GROUND_SCAFFOLD
    footprint = _grounding_footprint(plan, registry)
    if not goal:
        return 0.0, set(), goal, footprint
    expanded_foot = _expand_ground(footprint)
    matched = {g for g in goal if _expand_ground_word(g) & expanded_foot}
    return len(matched) / len(goal), matched, goal, footprint


def _hostile_variants(token: str) -> set:
    """Token plus standard English inflection-stripped variants.

    The purpose text reaching the gate is parser-stemmed ("exfiltrate"
    -> "exfiltrateing", "delete" -> "deleteing"), so exact
    token-boundary matching misses inflected hostile words. Variants
    cover trailing ing/ed/es/s/d with e-restoration ("deleting" ->
    "delete", "exfiltrateing" -> "exfiltrate") and consonant
    de-doubling ("running" -> "run"). Over-matching is safe by design:
    a false hit still refuses closed as hostile.
    """
    out = {token}
    t = token

    def _add_stem(stem: str) -> None:
        if len(stem) >= 3:
            out.add(stem)
            out.add(stem + "e")
            if len(stem) > 3 and stem[-1] == stem[-2]:
                out.add(stem[:-1])
                out.add(stem[:-1] + "e")

    if len(t) > 4:
        if t.endswith("ing"):
            _add_stem(t[:-3])
        elif t.endswith("ed"):
            _add_stem(t[:-2])
        elif t.endswith("es"):
            _add_stem(t[:-2])
        elif t.endswith("s") and not t.endswith("ss"):
            _add_stem(t[:-1])
    if t.endswith("d") and not t.endswith("ed") and len(t) > 3:
        out.add(t[:-1])
    return {v for v in out if v}


#: Every word the hostile matcher can recognise (single words plus the
#: words inside hostile phrases), used to canonicalise tokens.
_HOSTILE_VOCAB_WORDS = _HOSTILE_SINGLE_WORDS | frozenset(
    w for ph in _HOSTILE_PHRASES for w in re.findall(r"[a-z0-9]+", ph.lower()))


def _canonical_hostile_token(token: str) -> str:
    """Map a token to its hostile-vocabulary variant when one exists,
    else the token itself -- so phrase matching sees de-inflected
    text on both sides."""
    for v in sorted(_hostile_variants(token)):
        if v in _HOSTILE_VOCAB_WORDS:
            return v
    return token


def _hostile_signals(purpose: str, extra_text: str = "") -> List[str]:
    """Hostile intent signals, inflection-robust.

    Matching runs on each token's inflection variants (see
    _hostile_variants), not on exact surface forms, because the
    purpose text is parser-stemmed. ``extra_text`` (the frame's raw
    user text when available) is scanned too, in case the parser
    dropped the hostile word from the extracted purpose. A bounded
    heuristic: misses and false hits both stay closed (clarify /
    refuse_hostile both emit no file).
    """
    tokens: List[str] = []
    for text in (purpose or "", extra_text or ""):
        tokens.extend(re.findall(r"[a-z0-9]+", text.lower()))
    hits: List[str] = []
    for w in sorted(_HOSTILE_SINGLE_WORDS):
        if any(w in _hostile_variants(t) for t in tokens):
            hits.append(w)
    canon = " " + " ".join(_canonical_hostile_token(t) for t in tokens) + " "
    for ph in _HOSTILE_PHRASES:
        norm = " ".join(
            _canonical_hostile_token(pw)
            for pw in re.findall(r"[a-z0-9]+", ph.lower()))
        if norm and f" {norm} " in canon:
            hits.append(ph)
    return hits


def _check_grounding(purpose: str, plan: Dict[str, Any], strategy: str,
                     registry: Any, extra_text: str = "") -> Optional[Dict[str, Any]]:
    """The grounding gate. Returns None when the plan may proceed, else a
    refusal descriptor {verdict, detail, echo_score, echo_detail}.

    Order inside the gate: hostile signals first (a hostile purpose is
    refused without needing the echo measurement), then the echo check.
    The gate runs AFTER the pre-render effect screen, so genuinely
    dangerous plans (delete_file, run_command, ...) keep their existing
    forbidden_effect refusals. ``extra_text`` is the frame's raw user
    text, scanned alongside the purpose for hostile signals.
    """
    if strategy.startswith("template:"):
        return None
    hostile = _hostile_signals(purpose, extra_text)
    if hostile:
        return {
            "verdict": "refuse_hostile",
            "detail": ("purpose carries hostile signals "
                       f"({', '.join(hostile)}): refusing to synthesize "
                       "a file for it"),
            "echo_score": None,
            "echo_detail": {"hostile_signals": hostile},
        }
    score, matched, goal, footprint = _grounding_echo(purpose, plan,
                                                      registry)
    if score < _GROUNDING_THRESHOLD:
        return {
            "verdict": "clarify",
            "detail": ("the planner composed a plan "
                       f"({', '.join(_collect_plan_ops(plan)) or 'no ops'}) "
                       "with no semantic echo of the purpose "
                       f"(echo {score:.2f} < {_GROUNDING_THRESHOLD:.2f}; "
                       f"purpose words: {sorted(goal)}; matched: "
                       f"{sorted(matched)}): refusing to present an "
                       "unrelated file as success"),
            "echo_score": round(score, 3),
            "echo_detail": {"matched": sorted(matched),
                            "goal_words": sorted(goal),
                            "footprint": sorted(footprint)},
        }
    return None


# ---------------------------------------------------------------------------
# plan -> source renderer
# ---------------------------------------------------------------------------

_SIMPLE_CONST_TYPES = (int, float, str, bool, bytes, type(None))

_RESERVED_MODULE_NAMES = frozenset({
    "run", "_params", "_kw", "_PLAN_DEFAULTS", "_result", "__name__",
})


def _safe_ident(name: str, prefix: str = "x") -> str:
    s = re.sub(r"[^0-9A-Za-z_]", "_", str(name))
    if not s or s[0].isdigit():
        s = prefix + "_" + s
    return s


class _RenderCtx:
    """Accumulates the emitted module: imports, helper defs, op bindings."""

    def __init__(self) -> None:
        self.imports: List[str] = []
        self._import_names: set = set()
        self.defs: List[str] = []
        self.bindings: List[str] = []
        # original-name -> id(obj) for every module-level name we defined,
        # so a second, different object under the same name fails closed
        # instead of silently shadowing.
        self.defined: Dict[str, int] = {}
        # (op, step-id) -> unique callable name, for the dataflow wiring.
        self.op_names: Dict[Tuple[str, str], str] = {}
        # id(fn) of def-kind fns whose source text was already emitted
        # (a self-recursive helper may already be present via dependencies).
        self.def_text_emitted: set = set()
        self._lam_counter = 0

    def add_import(self, name: str, module_name: str) -> None:
        if name in self._import_names:
            return
        self._import_names.add(name)
        if name == module_name:
            self.imports.append(f"import {module_name}")
        else:
            self.imports.append(f"import {module_name} as {name}")

    def claim_name(self, name: str, obj: Any) -> None:
        if name in _RESERVED_MODULE_NAMES:
            raise CodegenError(
                f"helper name {name!r} collides with the generated module's "
                "own names -- refusing rather than shadowing")
        prev = self.defined.get(name)
        if prev is not None and prev != id(obj):
            raise CodegenError(
                f"name collision: two different objects want module name "
                f"{name!r} -- refusing rather than shadowing")
        self.defined[name] = id(obj)


def _const_repr(value: Any, what: str) -> str:
    """repr() a plan/default constant; fail closed on exotic values."""
    if isinstance(value, _SIMPLE_CONST_TYPES):
        return repr(value)
    if isinstance(value, (tuple, list)):
        if all(isinstance(v, _SIMPLE_CONST_TYPES) for v in value):
            return repr(list(value))
        raise CodegenError(f"{what}: non-constant sequence element")
    if isinstance(value, dict):
        if all(isinstance(k, str) and isinstance(v, _SIMPLE_CONST_TYPES)
               for k, v in value.items()):
            return repr(dict(value))
        raise CodegenError(f"{what}: non-constant dict entry")
    raise CodegenError(f"{what}: unsupported constant {type(value).__name__}")


def _extract_fn_source(fn: Any) -> Tuple[str, str, ast.AST, str]:
    """Extract (kind, original_name, node, dedented_source) of a fn.

    kind is "lambda" or "def". Uses AST node location (not text slicing) so
    a lambda embedded in a call expression is extracted exactly.
    """
    if not inspect.isfunction(fn):
        raise CodegenError(
            f"primitive fn {fn!r} is not a Python function -- no source to lift")
    try:
        raw = inspect.getsource(fn)
    except (OSError, TypeError) as exc:
        raise CodegenError(f"no source available for primitive fn: {exc}")
    ded = textwrap.dedent(raw)
    try:
        tree = ast.parse(ded)
    except SyntaxError as exc:
        raise CodegenError(f"could not parse primitive source: {exc}")
    cands = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
             and n.lineno == 1]
    if not cands:
        raise CodegenError("could not locate the function definition in its source")
    node = min(cands, key=lambda n: n.col_offset)
    if isinstance(node, ast.AsyncFunctionDef):
        raise CodegenError("async primitive fn cannot run in a standalone file")
    kind = "lambda" if isinstance(node, ast.Lambda) else "def"
    name = getattr(fn, "__name__", "<lambda>")
    return kind, name, node, ded


def _verbatim_def_source(ded: str, node: ast.AST) -> str:
    """The exact source lines of a def/class node, decorators included."""
    starts = [node.lineno]
    starts.extend(getattr(d, "lineno", node.lineno)
                  for d in getattr(node, "decorator_list", []))
    start = min(starts)
    end = node.end_lineno or start
    lines = ded.splitlines()[start - 1:end]
    return "\n".join(lines)


def _emit_dependency_value(name: str, value: Any, ctx: _RenderCtx,
                           _depth: int = 0) -> None:
    """Emit one dependency (module / helper fn or class / constant)."""
    if _depth > 25:
        raise CodegenError(f"dependency recursion too deep at {name!r}")
    if inspect.ismodule(value):
        ctx.add_import(name, value.__name__)
        return
    if inspect.isfunction(value) or inspect.isclass(value):
        _emit_named_callable(value, name, ctx, _depth + 1)
        return
    # plain constant
    if name in ctx.defined:
        if ctx.defined[name] == id(value):
            return
        raise CodegenError(f"name collision for constant {name!r}")
    ctx.claim_name(name, value)
    ctx.defs.append(f"{name} = {_const_repr(value, f'constant {name!r}')}")


def _emit_named_callable(fn: Any, name: str, ctx: _RenderCtx,
                         _depth: int = 0) -> None:
    """Emit a helper function/class under its ORIGINAL name (callers
    reference it by that name), resolving its own dependencies first."""
    prev = ctx.defined.get(name)
    if prev is not None:
        if prev == id(fn):
            return  # already emitted
        raise CodegenError(
            f"name collision: two different objects want helper name {name!r}")
    if not inspect.isfunction(fn) and not inspect.isclass(fn):
        raise CodegenError(f"helper {name!r} is not a function/class")
    kind, _orig, node, ded = _extract_fn_source(fn)
    if kind == "lambda":
        raise CodegenError(
            f"helper {name!r} is a lambda -- cannot emit under a fixed name")
    # claim BEFORE recursing so self-references resolve to this definition
    ctx.claim_name(name, fn)
    _resolve_fn_dependencies(fn, node, name, ctx, _depth)
    if id(fn) not in ctx.def_text_emitted:
        ctx.defs.append(_verbatim_def_source(ded, node))
        ctx.def_text_emitted.add(id(fn))


def _free_vars(node: ast.AST, self_name: str) -> set:
    """Free variable names of a function/lambda AST node.

    Computed from the AST (Name nodes only), so attribute names like the
    `random` in `_random.random()` are never mistaken for variables -- a
    trap inspect.getclosurevars falls into via co_names.
    """
    loaded: set = set()
    bound: set = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Load):
                loaded.add(n.id)
            else:
                bound.add(n.id)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if n is not node:
                bound.add(n.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                bound.add(a.asname or a.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
    return (loaded - bound) - {self_name}


def _resolve_fn_dependencies(fn: Any, node: ast.AST, self_name: str,
                             ctx: _RenderCtx, _depth: int = 0) -> None:
    """Resolve a lifted function's free variables.

    Closure cells first (they shadow module globals of the same name),
    then module globals. Builtins need nothing. Anything unbound or
    unrenderable fails closed.
    """
    free = _free_vars(node, self_name)
    closure: Dict[str, Any] = {}
    if getattr(fn, "__closure__", None):
        for cname, cell in zip(fn.__code__.co_freevars, fn.__closure__):
            try:
                closure[cname] = cell.cell_contents
            except ValueError:
                raise CodegenError(
                    f"primitive fn has an empty closure cell {cname!r} -- "
                    "cannot lift to a standalone module")
    import builtins as _builtins
    builtin_names = set(dir(_builtins))
    for dep_name in sorted(free):
        if dep_name in builtin_names:
            continue
        if dep_name in closure:
            _emit_dependency_value(dep_name, closure[dep_name], ctx, _depth)
            continue
        if dep_name in fn.__globals__:
            _emit_dependency_value(dep_name, fn.__globals__[dep_name],
                                   ctx, _depth)
            continue
        raise CodegenError(
            f"primitive fn has unbound name {dep_name!r} -- "
            "cannot lift to a standalone module")


def _render_op_fn(op: str, step_id: str, prim: Any, ctx: _RenderCtx) -> str:
    """Emit one plan step's primitive implementation; return its unique
    callable name for the dataflow wiring."""
    if getattr(prim, "needs_ctx", False):
        raise CodegenError(
            f"primitive {op!r} requires the engine execution context "
            "(governor/registry/memory) -- no standalone file can provide it")
    if getattr(prim, "is_async", False):
        raise CodegenError(
            f"primitive {op!r} is async -- the standalone runner is synchronous")
    fn = prim.fn
    kind, orig_name, node, ded = _extract_fn_source(fn)
    uname = _safe_ident(f"op_{op}_{step_id}", prefix="op")
    if uname in ctx.defined and ctx.defined[uname] != id(fn):
        raise CodegenError(f"internal name collision for {uname!r}")
    _resolve_fn_dependencies(fn, node, orig_name, ctx)
    if kind == "lambda":
        seg = ast.get_source_segment(ded, node)
        if seg is None:
            raise CodegenError(f"could not extract lambda source for {op!r}")
        ctx.claim_name(uname, fn)
        ctx.bindings.append(f"{uname} = {seg}")
    else:
        src = _verbatim_def_source(ded, node)
        if orig_name in ctx.defined and ctx.defined[orig_name] != id(fn):
            raise CodegenError(
                f"primitive fn name collision: {orig_name!r} already defined "
                "by a different object")
        ctx.claim_name(orig_name, fn)
        if id(fn) not in ctx.def_text_emitted:
            ctx.defs.append(src)
            ctx.def_text_emitted.add(id(fn))
        ctx.claim_name(uname, fn)
        ctx.bindings.append(f"{uname} = {orig_name}")
    return uname


# ---------------------------------------------------------------------------
# dataflow wiring (plan structure -> run() body)
# ---------------------------------------------------------------------------

class _WireCtx:
    def __init__(self, ctx: _RenderCtx, registry: Any) -> None:
        self.ctx = ctx
        self.registry = registry
        self.step_vars: Dict[str, str] = {}   # plan step id -> local var
        self.var_stack: List[Dict[str, str]] = []  # lambda param scopes


def _emit_step_op(op: str, sid: str, w: _WireCtx) -> str:
    """Emit one step's primitive implementation (once); return its name."""
    hit = w.ctx.op_names.get((op, sid))
    if hit is not None:
        return hit
    prim = w.registry.get(op) if hasattr(w.registry, "get") else None
    if prim is None:
        raise CodegenError(
            f"plan op {op!r} is not in the primitive registry -- "
            "refusing to invent an implementation")
    uname = _render_op_fn(op, sid, prim, w.ctx)
    w.ctx.op_names[(op, sid)] = uname
    return uname


def _render_arg(value: Any, w: _WireCtx, lam_prefix: str,
                lines: List[str]) -> str:
    """Render a plan argument to a Python expression string."""
    if isinstance(value, dict):
        keys = set(value.keys())
        if keys == {"$param"}:
            pname = value["$param"]
            return f"_params[{pname!r}]"
        if keys == {"$step"}:
            sid = value["$step"]
            if sid not in w.step_vars:
                raise CodegenError(
                    f"step reference to unknown or later step {sid!r}")
            return w.step_vars[sid]
        if keys == {"$var"}:
            vname = value["$var"]
            for scope in reversed(w.var_stack):
                if vname in scope:
                    return scope[vname]
            raise CodegenError(
                f"$var {vname!r} has no enclosing lambda parameter")
        if "$lambda" in value:
            return _render_lambda(value["$lambda"], w, lam_prefix, lines)
        # Exact single-key gate mirrors composer._resolve (len(ref) == 1):
        # a dict carrying "$partial" alongside other keys is NOT a partial
        # spec to the composer, so the renderer refuses it too (the generic
        # $-key check below fails closed).
        if keys == {"$partial"}:
            return _render_partial(value["$partial"], w, lam_prefix, lines)
        if any(isinstance(k, str) and k.startswith("$") for k in keys):
            raise CodegenError(f"unsupported argument reference {value!r}")
        # plain dict literal
        items = ", ".join(
            f"{_const_repr(k, 'dict key')}: "
            f"{_render_arg(v, w, lam_prefix, lines)}"
            for k, v in value.items())
        return "{" + items + "}"
    if isinstance(value, (list, tuple)):
        inner = ", ".join(_render_arg(v, w, lam_prefix, lines) for v in value)
        return f"[{inner}]"
    return _const_repr(value, "plan argument")


def _render_lambda(spec: Dict[str, Any], w: _WireCtx, lam_prefix: str,
                   lines: List[str]) -> str:
    """Render a {"$lambda": {...}} plan node as a nested def; return its name."""
    if not isinstance(spec, dict):
        raise CodegenError("$lambda spec must be a dict")
    w.ctx._lam_counter += 1
    n = w.ctx._lam_counter
    fname = f"{lam_prefix}_lam_{n}"
    params = spec.get("params", []) or []
    scope: Dict[str, str] = {}
    arg_names = []
    for p in params:
        mangled = f"{fname}_p_{_safe_ident(p, prefix='p')}"
        scope[p] = mangled
        arg_names.append(mangled)
    w.var_stack.append(scope)
    saved_step_vars = w.step_vars
    w.step_vars = {}
    body: List[str] = []
    try:
        inner_steps = spec.get("steps", []) or []
        for st in inner_steps:
            if not isinstance(st, dict) or "op" not in st:
                raise CodegenError(f"unsupported lambda step shape: {st!r}")
            _emit_step_op(st["op"], str(st.get("id", "?")), w)
        for st in inner_steps:
            _render_step(st, w, body, lam_prefix=fname)
        out = spec.get("output", {})
        body.append(f"return {_render_arg(out, w, fname, body)}")
    finally:
        w.step_vars = saved_step_vars
        w.var_stack.pop()
    header = f"def {fname}({', '.join(arg_names)}):"
    lines.append(header)
    for b in body:
        lines.append("    " + b)
    return fname


def _render_partial(spec: Any, w: _WireCtx, lam_prefix: str,
                    lines: List[str]) -> str:
    """Render a {"$partial": {...}} node as a positional-args lambda over the
    primitive's REAL implementation; return the lambda expression string.

    Contract mirror of composer.Composer._make_partial (fail-closed): the
    same spec the composer compiles to a callable is rendered to a
    standalone expression with identical calling semantics -- free params
    are supplied POSITIONALLY (the composer calls sync_call(*call_args);
    data.map does [fn(x) for x in items]), bound args stay fixed, and the
    registered primitive is invoked by keyword. Any contract violation
    raises CodegenError; nothing is emitted for a spec the composer would
    refuse.

    Bound values are rendered as constants only (_const_repr): the composer
    never resolves $refs inside a $partial spec -- bound values pass
    through to prim.fn as opaque literals -- so a $ref-shaped bound value
    is emitted as the literal dict, exactly as the composer would pass it.
    """
    if not isinstance(spec, dict):
        raise CodegenError("$partial spec must be a dict")
    op = spec.get("op")
    if not op or not isinstance(op, str):
        raise CodegenError("$partial spec requires a string op")
    prim = w.registry.get(op) if hasattr(w.registry, "get") else None
    if prim is None:
        raise CodegenError(
            f"$partial op {op!r} is not in the primitive registry -- "
            "refusing to invent an implementation")
    if not getattr(prim, "pure", False):
        raise CodegenError(
            f"$partial op {op!r} is not pure -- refusing")
    bound = dict(spec.get("bound") or {})
    free = list(spec.get("free") or [])
    if not free:
        raise CodegenError(
            f"$partial op {op!r} requires at least one free parameter")
    if any(not isinstance(n, str) for n in free):
        raise CodegenError(
            f"$partial op {op!r} free params must be strings: {free!r}")
    if any(not isinstance(k, str) for k in bound):
        raise CodegenError(
            f"$partial op {op!r} bound arg names must be strings")
    known = set(prim.inputs.keys())
    for k in list(bound.keys()) + free:
        if k not in known:
            raise CodegenError(
                f"$partial argument {k!r} not in signature of {op!r}")
    required = [k for k, v in prim.inputs.items()
                if not getattr(v, "optional", False)]
    covered = set(bound.keys()) | set(free)
    missing = [k for k in required if k not in covered]
    if missing:
        raise CodegenError(
            f"$partial op {op!r} missing required args {missing}")
    # Lift the REAL implementation exactly as a plan step would.
    # _emit_step_op enforces the needs_ctx / async refusals for us, and the
    # effect screen (_screen_plan_effects, via _collect_plan_ops) already
    # saw this op -- _collect_plan_ops walks into $partial specs.
    w.ctx._lam_counter += 1
    n = w.ctx._lam_counter
    call_name = _emit_step_op(op, f"partial_{n}", w)
    fname = f"{lam_prefix}_partial_{n}"
    # _render_lambda naming hygiene: mangled, unique, _safe_ident'd.
    free_vars = [f"{fname}_p_{_safe_ident(p, prefix='p')}" for p in free]
    bound_src = ", ".join(
        f"{_safe_ident(k, prefix='a')}="
        f"{_const_repr(v, f'$partial bound {k!r}')}"
        for k, v in sorted(bound.items()))
    free_src = ", ".join(
        f"{_safe_ident(p, prefix='a')}={fv}"
        for p, fv in zip(free, free_vars))
    all_kwargs = ", ".join(s for s in (bound_src, free_src) if s)
    # Parenthesised lambda: an expression, embeddable as e.g. fn=<expr>.
    # Plain (non-* / non-keyword-only) params accept positional calls.
    return (f"(lambda {', '.join(free_vars)}: "
            f"{call_name}({all_kwargs}))")


def _render_step(step: Dict[str, Any], w: _WireCtx, lines: List[str],
                 lam_prefix: str) -> None:
    if not isinstance(step, dict) or "op" not in step:
        raise CodegenError(f"unsupported plan step shape: {step!r}")
    sid = str(step.get("id", f"s{len(w.step_vars)}"))
    op = step["op"]
    call_name = _emit_step_op(op, sid, w)
    args = step.get("args", {}) or {}
    if not isinstance(args, dict):
        raise CodegenError(f"step {sid!r} args must be a mapping")
    kwargs = ", ".join(
        f"{_safe_ident(k, prefix='a')}={_render_arg(v, w, lam_prefix, lines)}"
        for k, v in args.items())
    var = f"{lam_prefix}_var_{_safe_ident(sid, prefix='s')}" if lam_prefix \
        else f"_var_{_safe_ident(sid, prefix='s')}"
    lines.append(f"{var} = {call_name}({kwargs})")
    w.step_vars[sid] = var


def _collect_param_refs(node: Any, out: set) -> None:
    if isinstance(node, dict):
        if set(node.keys()) == {"$param"}:
            out.add(node["$param"])
        for v in node.values():
            _collect_param_refs(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _collect_param_refs(v, out)


def render_plan_to_source(plan: Dict[str, Any], registry: Any,
                          purpose: str = "") -> Tuple[str, Dict[str, Any]]:
    """Render a planner Plan dict to a standalone Python module.

    Returns (source, meta) where meta carries ops/defines/imports for the
    audit trail. Raises CodegenError when the plan cannot be faithfully
    rendered -- the caller must fail closed, never emit a partial file.
    """
    if not isinstance(plan, dict):
        raise CodegenError("plan must be a dict")
    steps = plan.get("steps", []) or []
    if not steps:
        raise CodegenError("plan has no steps to render")

    ctx = _RenderCtx()

    # 1. emit every step's primitive implementation (real code, lifted).
    #    (Emission is lazy inside _render_step/_render_lambda via
    #    _emit_step_op, so nested lambda steps are covered too; this pass
    #    just validates the top-level shapes up front.)
    ops_used: List[str] = []
    for step in steps:
        if not isinstance(step, dict) or "op" not in step:
            raise CodegenError(f"unsupported plan step shape: {step!r}")
        ops_used.append(step["op"])

    # 2. wire the dataflow into run()
    w = _WireCtx(ctx, registry)
    run_lines: List[str] = []
    for step in steps:
        _render_step(step, w, run_lines, lam_prefix="")

    param_refs: set = set()
    for step in steps:
        _collect_param_refs(step.get("args", {}), param_refs)
    _collect_param_refs(plan.get("output", {}), param_refs)
    defaults = plan.get("defaults", {}) or {}
    if not isinstance(defaults, dict):
        raise CodegenError("plan defaults must be a mapping")
    defaults_src = ("_PLAN_DEFAULTS = " +
                    _const_repr(defaults, "plan defaults"))

    output = plan.get("output", {})
    ret_expr = _render_arg(output, w, "", run_lines)

    # 3. assemble the module
    mod: List[str] = []
    mod.append('"""Generated by REMOR codegen (runtime/synthesis/codegen.py).')
    mod.append("")
    mod.append("DO NOT EDIT: this file was rendered from a planner-composed")
    mod.append("plan. The causal chain is: purpose -> plan (ops_used below)")
    mod.append("-> the operator implementations emitted below -> run().")
    mod.append('"""')
    mod.append("")
    for i, line in enumerate(str(purpose or "").splitlines() or [""]):
        mod.append(f"# purpose: {line}")
    import json as _json
    plan_json = _json.dumps(plan, sort_keys=True, default=str)
    mod.append(f"# plan strategy ops: {', '.join(ops_used)}")
    # plan JSON as comment lines (wrapped) for full auditability
    chunk = 200
    first = True
    for i in range(0, len(plan_json), chunk):
        tag = "# plan: " if first else "#        "
        mod.append(tag + plan_json[i:i + chunk])
        first = False
    mod.append("")
    mod.extend(ctx.imports)
    if ctx.imports:
        mod.append("")
    mod.extend(ctx.defs)
    if ctx.defs:
        mod.append("")
    mod.extend(ctx.bindings)
    if ctx.bindings:
        mod.append("")
    mod.append(defaults_src)
    mod.append("")
    mod.append("def run(**_kw):")
    mod.append('    """Execute the plan\'s dataflow once; return its output."""')
    mod.append("    _params = {}")
    for pname in sorted(param_refs):
        mod.append(f"    if {pname!r} in _kw:")
        mod.append(f"        _params[{pname!r}] = _kw[{pname!r}]")
        mod.append(f"    elif {pname!r} in _PLAN_DEFAULTS:")
        mod.append(f"        _params[{pname!r}] = _PLAN_DEFAULTS[{pname!r}]")
        mod.append("    else:")
        mod.append(f"        raise TypeError(f\"missing plan parameter: {pname!r}\")")
    for rl in run_lines:
        mod.append("    " + rl)
    mod.append(f"    return {ret_expr}")
    mod.append("")
    mod.append('if __name__ == "__main__":')
    mod.append("    _result = run()")
    mod.append("    if isinstance(_result, (list, tuple)):")
    mod.append("        for _item in _result:")
    mod.append("            print(_item)")
    mod.append("    else:")
    mod.append("        print(_result)")
    mod.append("")
    source = "\n".join(mod)

    # the module must at least compile -- a cheap structural sanity check
    try:
        compile(source, "<codegen>", "exec")
    except SyntaxError as exc:
        raise CodegenError(f"rendered source does not compile: {exc}")

    # defense-in-depth AST scan: a primitive whose declared effects are
    # benign but whose lifted implementation reaches for raw OS / network
    # / process power is refused here, at the render stage. The scan lives
    # in the renderer (not just in synthesize_file) so every caller of
    # render_plan_to_source gets the guarantee.
    _scan_rendered_source(source)

    meta = {"ops": ops_used,
            "imports": list(ctx.imports),
            "defines": [d.splitlines()[0] for d in ctx.defs][:50]}
    return source, meta


# ---------------------------------------------------------------------------
# defense-in-depth AST scan of rendered source
# ---------------------------------------------------------------------------
# The effect screen is the primary control, but effect annotations are
# DECLARED by the primitive author, not proven -- a primitive could claim
# PURE while its implementation imports socket. This scan walks the
# rendered module's AST (structural, not a text keyword search) and
# refuses at the render stage. Documented residual: obfuscated dynamic
# access (getattr chains, exec of constructed strings) is beyond a static
# scan; the sandbox is the containment for that class.

#: Module roots that give a standalone file raw process / network /
#: interpreter-escape power. Deliberately narrow: os / shutil / sys /
#: pathlib are NOT banned (READ_FS is an allowed effect); the destructive
#: uses of os/shutil are caught by _SCAN_BANNED_ATTRS instead.
_SCAN_BANNED_IMPORTS = frozenset({
    "subprocess", "socket", "multiprocessing", "ctypes", "pty", "signal",
    "importlib", "pkgutil", "runpy", "code", "codeop", "py_compile",
    "compileall", "ensurepip", "venv", "webbrowser", "ftplib", "http",
    "urllib", "smtplib", "telnetlib", "ssl", "xmlrpc",
})

#: Bare-name calls that evaluate or escape.
_SCAN_BANNED_CALLS = frozenset({"eval", "exec", "__import__", "compile"})

#: Attribute calls refused when the base is an alias of a banned module,
#: or (for os/shutil) a destructive operation regardless of alias.
_SCAN_BANNED_ATTRS = frozenset({
    "system", "popen", "remove", "unlink", "rmtree", "rmdir", "mkdir",
    "makedirs", "removedirs", "rename", "renames", "replace", "chmod",
    "chown", "lchown", "kill", "killpg", "_exit", "execv", "execve",
    "execl", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp",
    "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe", "fork",
    "forkpty", "Popen", "run", "call", "check_call", "check_output",
    "connect", "connect_ex", "create_connection", "sendall", "sendto",
    "setuid", "setgid", "setresuid", "setresgid", "chroot",
})

#: Attribute calls refused on ANY base: a read-only file primitive has no
#: business writing. (bare open() is checked by mode separately.)
_SCAN_ALWAYS_BANNED_ATTRS = frozenset({"write_text", "write_bytes"})

#: Modules whose destructive attribute calls are refused (import aliases
#: are tracked, so `import os as _o; _o.remove(p)` is still caught).
_SCAN_DESTRUCTIVE_MODULES = frozenset({"os", "shutil"})


def _open_write_mode(node: ast.Call) -> bool:
    """True if an open()/Path.open() call uses a constant write-ish mode.

    Non-constant modes fail OPEN here (documented): the effect screen is
    the primary control and a static scan cannot decide a variable mode.
    """
    mode = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return any(c in mode.value for c in "wax+")
    return False


def _scan_rendered_source(source: str) -> None:
    """Refuse (CodegenError) rendered source with dangerous imports/calls.

    Defense in depth behind the effect screen: catches primitives whose
    declared effects are benign but whose lifted code reaches for raw OS
    / network / process power. Raises CodegenError naming the finding.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CodegenError(f"rendered source does not parse for scan: {exc}")

    # local name -> module root, through `import x`, `import x as y`,
    # `from x import y` (aliases tracked so renames do not hide the root)
    bound_roots: Dict[str, str] = {}

    class _ImportVisitor(ast.NodeVisitor):
        def visit_Import(self, node: ast.Import) -> None:
            for a in node.names:
                root = (a.name or "").split(".")[0]
                local = (a.asname or a.name or "").split(".")[0]
                if root and local:
                    bound_roots[local] = root

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            root = (node.module or "").split(".")[0]
            if not root:
                return
            for a in node.names:
                if a.name == "*":
                    continue
                bound_roots[a.asname or a.name] = root

    _ImportVisitor().visit(tree)
    for local, root in bound_roots.items():
        if root in _SCAN_BANNED_IMPORTS:
            raise CodegenError(
                f"rendered source imports banned module {root!r} "
                f"(bound as {local!r}) -- refusing")

    class _CallVisitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in _SCAN_BANNED_CALLS:
                    raise CodegenError(
                        f"rendered source calls banned builtin "
                        f"{func.id}() -- refusing")
                root = bound_roots.get(func.id)
                if root in _SCAN_BANNED_IMPORTS:
                    raise CodegenError(
                        f"rendered source calls {func.id}() imported from "
                        f"banned module {root!r} -- refusing")
                if (root in _SCAN_DESTRUCTIVE_MODULES
                        and func.id in _SCAN_BANNED_ATTRS):
                    raise CodegenError(
                        f"rendered source calls destructive {func.id}() "
                        f"imported from {root!r} -- refusing")
                if func.id == "open" and _open_write_mode(node):
                    raise CodegenError(
                        "rendered source opens a file in a write mode -- "
                        "refusing")
            elif isinstance(func, ast.Attribute):
                attr = func.attr
                if attr in _SCAN_ALWAYS_BANNED_ATTRS:
                    raise CodegenError(
                        f"rendered source calls .{attr}() -- refusing")
                if attr == "open" and _open_write_mode(node):
                    raise CodegenError(
                        "rendered source opens a file in a write mode -- "
                        "refusing")
                base = func.value
                if isinstance(base, ast.Name):
                    root = bound_roots.get(base.id)
                    if root in _SCAN_BANNED_IMPORTS:
                        raise CodegenError(
                            f"rendered source calls {base.id}.{attr}() "
                            f"from banned module {root!r} -- refusing")
                    if (root in _SCAN_DESTRUCTIVE_MODULES
                            and attr in _SCAN_BANNED_ATTRS):
                        raise CodegenError(
                            f"rendered source calls destructive "
                            f"{base.id}.{attr}() -- refusing")
            self.generic_visit(node)

    _CallVisitor().visit(tree)


# ---------------------------------------------------------------------------
# execution verification
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# sandboxed execution verification
# ---------------------------------------------------------------------------

#: Minimal environment for the verification child, built as a WHITELIST --
#: os.environ is NOT inherited, so REMOR_API_TOKEN and any other
#: secret-bearing variables cannot leak into the generated file's address
#: space. TMPDIR is added per-run, pointing inside the sandbox directory.
_VERIFY_ENV_BASE = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
}

#: Resource ceilings for the verification child (defense in depth behind
#: the wall-clock timeout): 25s CPU, 512 MiB address space, 256 processes
#: (fork-bomb containment), 32 MiB maximum single-file writes. NPROC is 256
#: rather than a smaller number deliberately: the limit counts against the
#: invoking uid's ambient processes, so it must sit comfortably above the
#: ambient count or fork() fails spuriously even once; 256 still contains
#: any bomb to a harmless, reaped handful while letting the ceiling
#: demonstrably engage (EAGAIN) instead of failing vacuously.
_VERIFY_RLIMIT_CPU = 25
_VERIFY_RLIMIT_AS = 512 * 1024 * 1024
_VERIFY_RLIMIT_NPROC = 256
_VERIFY_RLIMIT_FSIZE = 32 * 1024 * 1024

#: Launcher run as `python -c <this> <target> <status_path>`. It is
#: single-threaded, so the unshare+rlimit+exec sequence below is safe --
#: the parent server may be multithreaded, where doing this work in a
#: preexec_fn after fork would not be. Steps: apply rlimits -> unshare
#: into a fresh user+network namespace (no interfaces at all; direct
#: sockets and DNS both fail) -> map the invoking uid/gid -> drop to uid
#: 0 inside the namespace (== the invoking uid outside) -> record which
#: isolation actually engaged -> exec the generated file.
_SANDBOX_LAUNCHER = r'''
import json as _json
import os as _os
import sys as _sys

_target, _status_path = _sys.argv[1], _sys.argv[2]
_status = {"rlimits": False, "netns": False}

try:
    import resource as _resource
    _resource.setrlimit(_resource.RLIMIT_CPU,
                        (%d, %d))
    _resource.setrlimit(_resource.RLIMIT_AS,
                        (%d, %d))
    _resource.setrlimit(_resource.RLIMIT_NPROC,
                        (%d, %d))
    _resource.setrlimit(_resource.RLIMIT_FSIZE,
                        (%d, %d))
    _status["rlimits"] = True
except Exception as _e:
    _status["rlimit_error"] = type(_e).__name__ + ": " + str(_e)[:200]

_unshare = getattr(_os, "unshare", None)
if _unshare is not None:
    try:
        _uid, _gid = _os.getuid(), _os.getgid()
        _unshare(_os.CLONE_NEWUSER | _os.CLONE_NEWNET)
        open("/proc/self/setgroups", "w").write("deny")
        open("/proc/self/uid_map", "w").write("0 %%d 1" %% _uid)
        open("/proc/self/gid_map", "w").write("0 %%d 1" %% _gid)
        if hasattr(_os, "setresuid"):
            _os.setresuid(0, 0, 0)
            _os.setresgid(0, 0, 0)
        _status["netns"] = True
    except Exception as _e:
        _status["netns_error"] = type(_e).__name__ + ": " + str(_e)[:200]

try:
    with open(_status_path, "w") as _fh:
        _json.dump(_status, _fh)
except Exception:
    pass

_os.execv(_sys.executable, [_sys.executable, _target])
''' % (_VERIFY_RLIMIT_CPU, _VERIFY_RLIMIT_CPU,
       _VERIFY_RLIMIT_AS, _VERIFY_RLIMIT_AS,
       _VERIFY_RLIMIT_NPROC, _VERIFY_RLIMIT_NPROC,
       _VERIFY_RLIMIT_FSIZE, _VERIFY_RLIMIT_FSIZE)


def _verify_execution(path: str, timeout: int = 30) -> Dict[str, Any]:
    """Run the generated file in a real sandbox; require exit 0 + numeric.

    Sandbox (all real, all per-run):
      - fresh empty working directory: relative paths cannot touch the
        server's cwd or the artifact directory;
      - whitelisted minimal environment (PATH, LANG, two python flags,
        TMPDIR): os.environ is NOT inherited, so REMOR_API_TOKEN and
        friends cannot leak into the child's address space;
      - unprivileged user+network namespace when the kernel permits it:
        the child has no network interfaces at all, so direct sockets and
        DNS both fail;
      - rlimits: 25s CPU, 512MiB address space, 256 processes, 32MiB files;
      - wall-clock timeout (default 30s).

    The result carries a per-run "sandbox" report stating which mechanisms
    actually engaged. Honest residual: no mount namespace is used, so
    absolute-path filesystem access by the payload is still possible --
    contained by the rlimits and, primarily, by the pre-render effect
    screen, which refuses any plan needing WRITE_FS / NETWORK / PROCESS.
    If the kernel refuses unshare, network isolation falls back to
    env-level proxy removal plus the effect screen.
    """
    workdir = tempfile.mkdtemp(prefix="codegen_verify_")
    status_path = os.path.join(workdir, ".sandbox_status.json")
    env = dict(_VERIFY_ENV_BASE)
    env["TMPDIR"] = workdir
    sandbox: Dict[str, Any] = {
        "workdir": workdir,
        "env_keys": sorted(env),
        "netns": False,
        "rlimits": False,
    }
    try:
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _SANDBOX_LAUNCHER, path, status_path],
                capture_output=True, text=True, timeout=timeout,
                cwd=workdir, env=env)
        except subprocess.TimeoutExpired:
            return {"ok": False,
                    "detail": f"execution timed out after {timeout}s",
                    "sandbox": sandbox}
        except OSError as exc:
            return {"ok": False,
                    "detail": f"could not launch python: {exc}",
                    "sandbox": sandbox}
        try:
            with open(status_path, encoding="utf-8") as fh:
                sandbox.update(json.load(fh))
        except OSError:
            sandbox["status_error"] = "sandbox status file unreadable"
        if proc.returncode != 0:
            return {"ok": False,
                    "detail": f"exit {proc.returncode}: "
                              f"{proc.stderr.strip()[:2000]}",
                    "sandbox": sandbox}
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        if not lines:
            return {"ok": False, "detail": "no output on stdout",
                    "sandbox": sandbox}
        numbers = 0
        for ln in lines:
            try:
                float(ln)
                numbers += 1
            except ValueError:
                pass
        if numbers == 0:
            return {"ok": False,
                    "detail": f"no numeric output "
                              f"({len(lines)} non-empty lines)",
                    "sandbox": sandbox}
        return {"ok": True, "exit_code": 0, "stdout": proc.stdout,
                "lines": len(lines), "numbers": numbers, "sandbox": sandbox}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def synthesize_file(frame: Any, engine: Any, out_dir: str) -> Dict[str, Any]:
    """Turn a CREATE_FILE IntentFrame into a real, executed Python file.

    Returns {ok, path, bytes, language, purpose, refusal, ...}. On any
    failure ok is False and refusal names the stage that failed closed.
    """
    entities = getattr(frame, "entities", None) or {}
    purpose = entities.get("purpose")
    language = str(entities.get("language") or "python").lower()

    def _refuse(code: str, detail: str = "") -> Dict[str, Any]:
        return {"ok": False, "path": None, "bytes": 0,
                "language": language,
                "purpose": purpose if isinstance(purpose, str) else "",
                "refusal": code, "detail": detail}

    # -- frame contract ---------------------------------------------------
    if not getattr(frame, "actionable", False):
        return _refuse("frame_not_actionable",
                       "frame is UNKNOWN/AMBIGUOUS -- must not act")
    if getattr(frame, "intent", None) is not Intent.CREATE_FILE:
        return _refuse("wrong_intent", "not a CREATE_FILE frame")
    if not isinstance(purpose, str) or not purpose.strip():
        return _refuse("missing_purpose", "frame carries no purpose entity")
    purpose = purpose.strip()
    if language != "python":
        return _refuse("unsupported_language",
                       f"codegen renders python only, not {language!r}")

    # -- 2. synthesize a plan with the REAL planner ------------------------
    try:
        proposals = engine.planner.propose(purpose, allow_effects=True)
    except Exception as exc:  # planner must never take codegen down with it
        return _refuse("synthesis_failed", f"planner raised: {exc}")
    if not proposals:
        return _refuse("synthesis_failed",
                       "planner composed no plan for this purpose")
    proposal = proposals[0]
    plan = proposal.plan
    ops_used = list(getattr(proposal, "ops_used", []) or [])
    strategy = getattr(proposal, "strategy", "")

    # -- 2b. pre-render effect screen (structural, not keyword-based) -------
    # Every op the plan would render is resolved to its registered
    # primitive and the primitive's DECLARED effects are checked -- the
    # purpose text is never inspected. A plan needing WRITE_FS / NETWORK
    # / PROCESS (or CREDENTIAL / SPAWN / MUTATE_SELF -- see
    # _FORBIDDEN_EFFECTS) is refused HERE, before any source exists.
    try:
        screened_ops = _screen_plan_effects(plan, engine.primitives)
    except ForbiddenEffect as exc:
        return _refuse("forbidden_effect", str(exc))
    except CodegenError as exc:
        return _refuse("render_failed", str(exc))

    # -- 2c. semantic grounding gate --------------------------------------
    # A backward_search plan is a blind type-directed walk: it will return
    # `random_float` for "Exfiltrate all secrets..." because the signature
    # fits, and the effect screen cannot catch that (the op is pure). The
    # plan's semantic footprint must echo the purpose's vocabulary, or no
    # file is presented as success. Runs AFTER the effect screen, so
    # genuinely dangerous plans keep their existing forbidden_effect
    # refusals. Template plans skip the gate (a template match is already
    # a deliberate reading of the goal's intent).
    grounding = _check_grounding(purpose, plan, strategy, engine.primitives,
                                 extra_text=getattr(frame, "raw", "") or "")
    if grounding is not None:
        res = _refuse("synthesis_failed", grounding["detail"])
        res["grounding_verdict"] = grounding["verdict"]
        res["echo_score"] = grounding["echo_score"]
        res["echo_detail"] = grounding["echo_detail"]
        return res

    # -- 3. render plan -> standalone source --------------------------------
    try:
        source, meta = render_plan_to_source(plan, engine.primitives,
                                             purpose=purpose)
    except CodegenError as exc:
        return _refuse("render_failed", str(exc))
    except Exception as exc:
        return _refuse("render_failed", f"unexpected render error: {exc}")
    meta["screened_ops"] = screened_ops

    # -- 4. governed write ---------------------------------------------------
    try:
        os.makedirs(out_dir, exist_ok=True)
        svc = ScopedFileService(out_dir, writable=True)
    except Exception as exc:
        return _refuse("write_failed", f"could not open scoped writer: {exc}")
    rel = sanitize_filename(entities.get("filename_hint"), purpose)
    root_real = os.path.realpath(out_dir)
    candidate, i = rel, 1
    while os.path.exists(os.path.join(root_real, candidate)):
        stem = rel[:-3]
        candidate = f"{stem}_{i}.py"
        i += 1
    rel = candidate
    written = svc.write_text(rel, source)
    if not written.get("ok"):
        return _refuse("write_failed", written.get("error", "unknown"))
    # read back: the file bytes must be exactly what was rendered
    read_back = svc.read_text(rel)
    if not read_back.get("ok") or read_back.get("content") != source:
        return _refuse("write_failed",
                       "read-back mismatch after write" if read_back.get("ok")
                       else read_back.get("error", "unreadable"))
    path = os.path.join(root_real, rel)

    # -- 5. verify it executes ----------------------------------------------
    ver = _verify_execution(path)
    if not ver.get("ok"):
        return {"ok": False, "path": path, "bytes": 0,
                "language": language, "purpose": purpose,
                "refusal": "verification_failed", "detail": ver.get("detail", ""),
                "ops_used": ops_used, "strategy": strategy}

    return {"ok": True, "path": path, "bytes": written.get("size_bytes", 0),
            "language": language, "purpose": purpose, "refusal": None,
            "ops_used": ops_used, "strategy": strategy,
            "execution": {"exit_code": ver["exit_code"],
                          "stdout_lines": ver["lines"],
                          "numbers": ver["numbers"],
                          "sandbox": ver.get("sandbox")},
            "render": meta}
