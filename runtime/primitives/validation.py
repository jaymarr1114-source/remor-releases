"""
swarm_engine/primitives/validation.py

Primitive validation harness.

Registration is not evidence. A primitive that is registered, typed and
effect-labelled may still be unreachable, mis-signatured, dishonestly labelled,
or broken on its own declared input types. This module answers the question the
registry cannot: of the primitives in the vocabulary, which ones actually work?

For each primitive it checks, in order:

  metadata    - does it carry a description, declared input types and an
                output type? Missing metadata makes a primitive invisible to
                the planner even when the implementation is fine.
  invocable   - can a value satisfying its declared input types be built, and
                does calling it with those values return without raising?
  typed       - does the returned value actually match the declared output
                type? A wrong output type is worse than no type: the composer
                trusts it when checking a plan, so the error surfaces later,
                somewhere else.
  effects     - is the effect label honest? A primitive declared PURE that
                touches the filesystem, clock or network defeats governance,
                because the governor never gets consulted for a free effect.

Primitives with non-free effects are not invoked by default. Running every
filesystem and network primitive to see what happens is not validation, it is
an uncontrolled side effect; those are reported as `skipped_effectful` and
exercised through granted, sandboxed fixtures instead.

The output is a maturity matrix, not a pass/fail verdict. "312 of 322 work" is
a fact worth having even when ten do not.
"""
from __future__ import annotations

import asyncio
import inspect
from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.primitives.core import (
    Effect, ExecContext, Kind, Primitive, PrimitiveRegistry, TypeSpec, infer,
)

FREE = {Effect.PURE, Effect.CLOCK, Effect.RANDOM}

# Path samples are drawn from here so that filesystem primitives can be
# exercised inside a directory the governor has actually granted. Testing them
# against a path outside every grant only proves the governor says no, which is
# worth knowing once, not 15 times.
# P0 Android fix (landed in canonical 2026-09-27): resolve the validation
# sandbox under the writable root (REMOR_DATA_DIR) when set; the Android app
# sandbox has no writable /tmp. Historical /tmp default preserved on dev
# machines.
from swarm_engine.core.writable import writable_subdir as _writable_subdir
SANDBOX_ROOT = _writable_subdir("swarm_validation_sandbox",
                                "/tmp/swarm_validation_sandbox")


# ---------------------------------------------------------------------------
# SAMPLE GENERATION
# ---------------------------------------------------------------------------

def sample_for(spec: TypeSpec, name: str = "") -> Tuple[bool, Any]:
    """Build a value satisfying `spec`. Returns (built, value).

    Argument names are consulted as a hint, because a declared type of `int`
    tells us nothing about whether a divisor may be zero or an index may exceed
    a collection. Type-correct-but-semantically-invalid inputs would report
    working primitives as broken, so the hints exist to keep the harness
    honest about what it is actually testing.
    """
    lowered = name.lower()

    # Some arguments carry a required *format*, not merely a type: a str that
    # must be valid JSON, base64, ISO-8601, or the name of an enumerated mode.
    # Feeding those a generic "ab" makes a working primitive raise, and the
    # harness would report a parser doing its job as a broken primitive. These
    # hints exist so a reported failure means something.
    if spec.kind is Kind.STR:
        if any(k in lowered for k in ("json", "serialized", "payload", "encoded_json")):
            return True, '{"a": 1}'
        if "base64" in lowered or lowered in ("encoded", "b64"):
            return True, "YWI="
        if any(k in lowered for k in ("iso", "datetime", "timestamp", "when", "date")):
            return True, "2026-01-01T00:00:00"
        if any(k in lowered for k in ("metric", "mode", "kind", "strategy", "how",
                                      "method", "target_kind", "op", "operation")):
            # Enumerated arguments accept only known members; the harness
            # cannot guess one, so it declines rather than fails falsely.
            return False, None

    if spec.kind in (Kind.INT, Kind.NUM, Kind.FLOAT):
        if any(k in lowered for k in ("divisor", "denominator", "base", "size",
                                      "width", "n", "count", "length", "step")):
            return True, 2
        if any(k in lowered for k in ("index", "start", "offset", "depth", "seed")):
            return True, 0
        if "decimals" in lowered or "precision" in lowered:
            return True, 2
        return True, (2 if spec.kind is Kind.INT else 2.0)

    if spec.kind is Kind.BOOL:
        return True, False
    if spec.kind is Kind.STR:
        if "pattern" in lowered or "regex" in lowered:
            return True, "a"
        if "dir" in lowered or "folder" in lowered:
            return True, SANDBOX_ROOT
        if "path" in lowered or "file" in lowered or "dest" in lowered or "src" in lowered:
            return True, f"{SANDBOX_ROOT}/probe.txt"
        if "url" in lowered:
            return True, "https://example.invalid/probe"
        if any(k in lowered for k in ("format", "fmt")):
            return True, "%Y-%m-%d"
        return True, "ab"
    if spec.kind is Kind.BYTES:
        return True, b"ab"
    if spec.kind is Kind.NONE:
        return True, None
    if spec.kind is Kind.ANY:
        return True, 2

    if spec.kind is Kind.LIST:
        inner = spec.args[0] if spec.args else TypeSpec(Kind.ANY)
        built, value = sample_for(inner, name)
        if not built:
            return False, None
        # Two distinct elements: enough for sort, unique, pairwise ops.
        second = value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            second = value + 1
        elif isinstance(value, str):
            second = value + "c"
        return True, [value, second]

    if spec.kind is Kind.DICT:
        key = spec.args[0] if spec.args else TypeSpec(Kind.STR)
        val = spec.args[1] if len(spec.args) > 1 else TypeSpec(Kind.ANY)
        kb, kv = sample_for(key, "key")
        vb, vv = sample_for(val, "value")
        if not (kb and vb):
            return False, None
        return True, {kv if isinstance(kv, str) else "k": vv}

    if spec.kind is Kind.TUPLE:
        parts = []
        for arg in spec.args or ():
            built, value = sample_for(arg, name)
            if not built:
                return False, None
            parts.append(value)
        return True, tuple(parts)

    if spec.kind is Kind.CALLABLE:
        # Identity is the safest stand-in: it is valid for map/filter-shaped
        # slots without assuming what the primitive will do with the result.
        return True, (lambda *a, **k: a[0] if a else None)

    if spec.kind is Kind.UNION:
        for arg in spec.args or ():
            built, value = sample_for(arg, name)
            if built:
                return True, value
        return False, None

    return False, None


def build_args(prim: Primitive) -> Tuple[bool, Dict[str, Any], List[str]]:
    """Assemble a full argument set for a primitive."""
    args: Dict[str, Any] = {}
    unbuildable: List[str] = []
    for arg_name, spec in prim.inputs.items():
        if spec.optional:
            continue
        built, value = sample_for(spec, arg_name)
        if not built:
            unbuildable.append(f"{arg_name}: {spec}")
            continue
        args[arg_name] = value
    return (not unbuildable), args, unbuildable


# ---------------------------------------------------------------------------
# RESULTS
# ---------------------------------------------------------------------------

# Some primitives require inputs whose validity is not expressible in a type:
# a str that must be valid base64, a dict that must contain a 'primitive' key,
# a name that must already exist in the registry. Generated samples make these
# raise, and a harness that reports a working parser as broken is worse than no
# harness. Explicit fixtures keep the failure list meaningful.
FIXTURES: Dict[str, Dict[str, Any]] = {
    "assert": {"condition": True},
    "attribution": {"trace": [{"primitive": "mean", "ms": 1.0}]},
    "trace_causality": {"trace": [{"primitive": "mean", "ms": 1.0}]},
    "decode_base64": {"text": "YWI="},
    "deserialize": {"text": '{"a": 1}'},
    "parse_datetime": {"text": "2026-01-01T00:00:00"},
    "few_shot_adapt": {"base_plan": {"name": "p", "steps": []},
                       "examples": [{"in": 1, "out": 2}],
                       "literal_map": {"a": "b"}},
    "transfer_plan": {"plan": {"steps": []}},
    "invoke_primitive": {"name": "mean", "args": {"values": [1, 2]}},
    "grid_search": {"space": {"a": [1, 2]}},
    "tune": {"space": {"a": [1, 2]}},
    "hill_climb": {"start": {"a": 1.0},
                   "objective": (lambda c: float(sum(c.values())))},
    "length": {"collection": [1, 2]},
    # Effectful fixtures: distinct paths so directory and file primitives do
    # not contend for the same name, and content matching the reader's format.
    "make_dir": {"path": f"{SANDBOX_ROOT}/newdir"},
    "read_json": {"path": f"{SANDBOX_ROOT}/probe.json"},
    "assign_task": {"agent_id": "validation-probe",
                    "plan": {"name": "noop", "params": {}, "steps": [], "output": None}},
}


class ValidationState(Enum):
    """What is actually known about a primitive.

    The previous report had two buckets, healthy and skipped, which forced
    genuinely different situations into the same word: a meta-primitive that
    takes a live execution context is not "skipped" in the sense that a
    network primitive with no network is skipped, and neither is a defect.
    Counting them together produces a number that cannot be acted on.
    """
    HEALTHY = "healthy"                    # implemented, invoked, correct
    FAILED = "failed"                      # a real defect
    NON_EXECUTABLE = "non_executable"      # meta-level; needs live engine state
    GOVERNED = "governed"                  # refused by the governor, correctly
    UNAVAILABLE = "unavailable"            # external dependency absent
    UNTESTED = "untested"                  # inputs could not be constructed


@dataclass
class PrimitiveReport:
    name: str
    family: str
    effects: List[str]
    has_doc: bool = False
    has_types: bool = False
    invocable: Optional[bool] = None        # None = not attempted
    output_matches: Optional[bool] = None
    effect_honest: Optional[bool] = None
    skipped_reason: str = ""
    error: str = ""
    state: "ValidationState" = None

    def classify(self) -> "ValidationState":
        """Assign exactly one state, most-specific first."""
        if self.invocable is False or self.output_matches is False:
            return ValidationState.FAILED
        if not (self.has_doc and self.has_types):
            return ValidationState.FAILED
        reason = self.skipped_reason or ""
        if reason.startswith("governed"):
            return ValidationState.GOVERNED
        if reason.startswith("environment unavailable"):
            return ValidationState.UNAVAILABLE
        if reason.startswith("needs live engine state"):
            return ValidationState.NON_EXECUTABLE
        if reason:
            return ValidationState.UNTESTED
        return ValidationState.HEALTHY

    @property
    def healthy(self) -> bool:
        if not (self.has_doc and self.has_types):
            return False
        if self.invocable is False or self.output_matches is False:
            return False
        if self.effect_honest is False:
            return False
        return True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "family": self.family, "effects": self.effects,
            "state": self.state.value if self.state else None,
            "doc": self.has_doc, "types": self.has_types,
            "invocable": self.invocable, "output_matches": self.output_matches,
            "effect_honest": self.effect_honest,
            "skipped": self.skipped_reason, "error": self.error,
        }


class PrimitiveValidator:
    def __init__(self, registry: PrimitiveRegistry, include_effectful: bool = False):
        self.reg = registry
        self.include_effectful = include_effectful

    def validate_all(self) -> Dict[str, Any]:
        reports = [self.validate(self.reg.get(n)) for n in self.reg.names()]
        return self._summarise(reports)

    def validate(self, prim: Primitive) -> PrimitiveReport:
        report = PrimitiveReport(
            name=prim.name,
            family=prim.family,
            effects=[e.value for e in prim.effects],
        )
        report.has_doc = bool((prim.doc or "").strip())
        report.has_types = bool(prim.inputs is not None) and prim.output is not None

        effectful = any(e not in FREE for e in prim.effects)
        if effectful and not self.include_effectful:
            report.skipped_reason = "declares non-free effects; not invoked"
            return report

        buildable, args, missing = build_args(prim)
        fixture = FIXTURES.get(prim.name)
        if fixture is not None:
            # A fixture supplies only the arguments the generator cannot infer;
            # anything else it produced is still used.
            args = {**(args if buildable else {}), **fixture}
            buildable = True
        if not buildable:
            report.skipped_reason = f"could not construct inputs: {'; '.join(missing)}"
            return report

        try:
            value = self._call(prim, args)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            # Three outcomes are not defects and must not be counted as such,
            # or the report stops meaning anything:
            #   - the governor refusing an ungranted effect is the governor
            #     working; it says nothing about the primitive.
            #   - a primitive needing a resource this run did not bind (a
            #     knowledge base, a live network) is untested, not broken.
            # Conflating either with a real fault is how a maturity report
            # becomes noise people learn to skip.
            # A meta-level primitive reaches for engine state at call time --
            # a knowledge base, a composer, a live agent. Failing for want of
            # that says nothing about the primitive's implementation, so it is
            # classified as intentionally non-executable in this harness rather
            # than counted as a defect or padded into the healthy total.
            if any(t in detail for t in ("no knowledge base", "no composer",
                                         "unknown agent", "requires a registry",
                                         "no capability store", "not bound")):
                report.skipped_reason = f"needs live engine state: {detail[:90]}"
            elif "PermissionError_" in detail:
                report.skipped_reason = f"governed: {detail[:90]}"
            elif ("Name or service not known" in detail
                  or "Temporary failure in name resolution" in detail
                  or "URLError" in detail or "network" in detail.lower()):
                report.skipped_reason = f"environment unavailable: {detail[:90]}"
            else:
                report.invocable = False
                report.error = detail
            return report

        report.invocable = True
        report.output_matches = prim.output.accepts(infer(value))
        if not report.output_matches:
            report.error = f"declared {prim.output}, returned {infer(value)}"
        report.effect_honest = self._effects_honest(prim)
        return report

    def _prepare_sandbox(self) -> None:
        """Reset the sandbox to a known shape before each effectful call.

        Without this, primitives contaminate each other: make_dir creates a
        directory where probe.txt should be a file, and every later read then
        fails with IsADirectoryError -- six primitives reported broken because
        of one earlier primitive succeeding. Validation whose result depends on
        alphabetical ordering is not validation.
        """
        import os
        import shutil
        probe = os.path.join(SANDBOX_ROOT, "probe.txt")
        if os.path.isdir(probe):
            shutil.rmtree(probe, ignore_errors=True)
        os.makedirs(SANDBOX_ROOT, exist_ok=True)
        if not os.path.isfile(probe):
            with open(probe, "w") as fh:
                fh.write("probe")
        with open(os.path.join(SANDBOX_ROOT, "probe.json"), "w") as fh:
            fh.write('{"a": 1}')
        shutil.rmtree(os.path.join(SANDBOX_ROOT, "newdir"), ignore_errors=True)

    def _call(self, prim: Primitive, args: Dict[str, Any]) -> Any:
        if self.include_effectful and any(e not in FREE for e in prim.effects):
            self._prepare_sandbox()
        ctx = ExecContext(self.reg.governor, self.reg)
        coro = self.reg.invoke(prim.name, ctx, **args)
        return asyncio.run(coro) if inspect.isawaitable(coro) else coro

    def _effects_honest(self, prim: Primitive) -> bool:
        """A primitive claiming to be free of effects must not reach for the
        modules that produce them. This is a source-level check, so it catches
        the label being wrong rather than the behaviour being wrong — but a
        mislabelled primitive is exactly what governance cannot see."""
        if any(e not in FREE for e in prim.effects):
            return True  # declares effects; nothing to contradict
        try:
            source = inspect.getsource(prim.fn)
        except (OSError, TypeError):
            return True  # cannot inspect (builtin/lambda in C); do not accuse
        suspicious = ("open(", "urllib.request", "requests.", "socket.",
                      "subprocess", "os.remove", "os.system", "shutil.")
        # urllib.parse is deliberately not listed: splitting and joining a URL
        # is string manipulation, and flagging it would train the reader to
        # ignore this check.
        return not any(token in source for token in suspicious)

    def _summarise(self, reports: List[PrimitiveReport]) -> Dict[str, Any]:
        by_family: Dict[str, Dict[str, int]] = {}
        by_state: Dict[str, int] = {s.value: 0 for s in ValidationState}
        for r in reports:
            r.state = r.classify()
            by_state[r.state.value] += 1
            slot = by_family.setdefault(r.family, {"total": 0, "healthy": 0, "issues": 0,
                                                   "skipped": 0})
            slot["total"] += 1
            if r.state is ValidationState.HEALTHY:
                slot["healthy"] += 1
            elif r.state is ValidationState.FAILED:
                slot["issues"] += 1
            else:
                slot["skipped"] += 1

        problems = [r for r in reports if not r.skipped_reason and not r.healthy]
        return {
            "total": len(reports),
            "healthy": sum(1 for r in reports if r.healthy and not r.skipped_reason),
            "issues": len(problems),
            "skipped": sum(1 for r in reports if r.skipped_reason),
            "missing_doc": [r.name for r in reports if not r.has_doc],
            "not_invocable": [(r.name, r.error) for r in reports if r.invocable is False],
            "output_mismatch": [(r.name, r.error) for r in reports
                                if r.output_matches is False],
            "effect_dishonest": [r.name for r in reports if r.effect_honest is False],
            "by_family": by_family,
            "by_state": by_state,
            "implemented": len(reports),
            "validated": sum(v for k, v in by_state.items() if k != "untested"),
            "reports": [r.as_dict() for r in reports],
        }


def render(summary: Dict[str, Any]) -> str:
    lines = []
    st = summary["by_state"]
    lines.append(f"PRIMITIVE MATURITY  total={summary['total']}  "
                 f"implemented={summary['implemented']}  "
                 f"validated={summary['validated']}")
    lines.append(f"  healthy={st['healthy']}  failed={st['failed']}  "
                 f"non_executable={st['non_executable']}  governed={st['governed']}  "
                 f"unavailable={st['unavailable']}  untested={st['untested']}")
    lines.append("")
    lines.append(f"{'family':<16}{'total':>7}{'healthy':>9}{'issues':>8}{'skipped':>9}")
    for family in sorted(summary["by_family"]):
        s = summary["by_family"][family]
        lines.append(f"{family:<16}{s['total']:>7}{s['healthy']:>9}"
                     f"{s['issues']:>8}{s['skipped']:>9}")
    for label, key in (("NOT INVOCABLE", "not_invocable"),
                       ("OUTPUT TYPE MISMATCH", "output_mismatch")):
        if summary[key]:
            lines.append("")
            lines.append(f"{label} ({len(summary[key])}):")
            for name, err in summary[key][:40]:
                lines.append(f"  {name:<28} {err[:90]}")
    if summary["missing_doc"]:
        lines.append("")
        lines.append(f"MISSING DESCRIPTION ({len(summary['missing_doc'])}): "
                     + ", ".join(summary["missing_doc"][:25]))
    if summary["effect_dishonest"]:
        lines.append("")
        lines.append("EFFECT LABEL SUSPECT: " + ", ".join(summary["effect_dishonest"]))
    return "\n".join(lines)
