"""runtime/synthesis/trace_guided.py

Trace-guided synthesis: distill multi-step programs from teacher
demonstration traces (TRACE-GUIDED-SYNTH-1).

The distillation wall: the exact-fit search cannot find multi-step programs.
Probes showed 12k candidates never producing equals(add(a,b),claimed), with the
winners being spurious correlational patterns (``1 < (claimed % 4)``,
``claimed % 2``, memorized if_else trees). The teacher's demonstration,
however, contains its intermediate work as labeled lines
("recompute: 3 + 4 = 7"). This module decomposes the task into one subproblem
per labeled line -- each solvable by the existing single-step-capable search
(a single exact-fit step is the one shape the search provably handles) --
then composes the per-step programs in trace order.

The mechanism is generic: labeled-line parsing, intermediate-value extraction
(the value after the last '=' in a work line, else the whole line content),
per-step single-step synthesis, plan splicing. No task names, task shapes,
or task-specific constants appear here. The DistillationLoop's Route C drives
this module and puts the composed program through the same trust path as
fresh synthesis (render -> held-out in fresh processes -> negative controls
-> ReviewBoard re-verify of the exact bytes -> frozen promotion).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple


# Labels that mark the final-answer line rather than a work line. The parse
# takes the LAST such line (prompt-echo-safe: search from the end).
RESULT_LABELS = ("result", "answer", "output")


class TraceParseError(ValueError):
    """A demonstration trace could not be parsed into labeled work lines."""


class TraceShapeError(ValueError):
    """Parsed traces are inconsistent or cannot be composed."""


@dataclass
class ParsedTrace:
    work: List[Tuple[str, str]]      # (label, content) in trace order; last dup wins
    result_text: Optional[str]      # raw content of the result line, if any


def parse_labeled_trace(text: str) -> ParsedTrace:
    """Parse a teacher demonstration into labeled work lines + result line.

    Generic labeled-line parsing: each ``label: content`` line is a work
    step; lines whose content carries ``<...>`` placeholders are prompt
    echoes, not teacher work, and are skipped. The last ``result:``-family
    line is the final answer.
    """
    work: List[Tuple[str, str]] = []
    result_text: Optional[str] = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        label, _, content = line.partition(":")
        label = label.strip().lower()
        content = content.strip()
        if not label or not content:
            continue
        if "<" in content and ">" in content:
            continue  # prompt-echo placeholder, not the teacher's work
        if label in RESULT_LABELS:
            result_text = content  # last one wins: search from the end
            continue
        # Dedupe: the last occurrence of a label wins, ordered by last use.
        work = [(l, c) for l, c in work if l != label]
        work.append((label, content))
    return ParsedTrace(work=work, result_text=result_text)


def scalar_value(text: str) -> Any:
    """Parse a scalar: int, then float, else the stripped string."""
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ("'", '"'):
        t = t[1:-1].strip()
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


_BOOL_WORDS = {"true": True, "false": False, "yes": True, "no": False,
               "1": True, "0": False}


def result_value(text: str) -> Any:
    """Parse a result line: boolean words first, else a scalar."""
    t = (text or "").strip().lower()
    if t in _BOOL_WORDS:
        return _BOOL_WORDS[t]
    return scalar_value(text)


_LEADING_NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)")


def _leading_number(t: str) -> Optional[Any]:
    m = _LEADING_NUM_RE.match(t.strip())
    if not m:
        return None
    s = m.group(0)
    return int(s) if "." not in s else float(s)


def intermediate_value(content: str) -> Any:
    """Extract a work line's intermediate: the value after the last
    assignment-style separator ('=', '->', '→', ':'), else the whole line
    content. After a separator, a leading number wins over trailing prose
    ("7 (seven)" -> 7); without a separator the whole content is the value
    ("HELLO WORLD"). Generic -- no task knowledge."""
    c = content.strip()
    had_sep = False
    for sep in ("=", "->", "\u2192", ":"):
        if sep in c:
            c = c.rsplit(sep, 1)[1]
            had_sep = True
            break
    c = c.strip()
    if had_sep:
        n = _leading_number(c)
        if n is not None:
            return n
    return scalar_value(c)


def sanitize_label(label: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", label.strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or fallback


@dataclass
class SubProblem:
    input_names: List[str]
    examples: List[Tuple[Dict[str, Any], Any]]
    kind: str        # "intermediate" | "final"
    label: str       # sanitized work-line label ("final" for the last)


def form_subproblems(
    build: Sequence[Tuple[Dict[str, Any], Any]],
    parsed_traces: Sequence[ParsedTrace],
) -> List[SubProblem]:
    """Decompose one multi-step task into per-labeled-line subproblems.

    Work lines w_0..w_{k-1}: the first k-1 yield intermediates (named by
    their sanitized labels); the last work line is derivation context for
    the final answer. Subproblem i maps (original inputs + earlier
    intermediates) -> intermediate_i; the final subproblem maps (original
    inputs + all intermediates) -> the parsed result. Each subproblem is a
    single-step-shaped task for the existing exact-fit search.
    """
    if not build or not parsed_traces or len(build) != len(parsed_traces):
        raise TraceShapeError(
            f"need one parsed trace per build example "
            f"(got {len(parsed_traces)} traces for {len(build)} examples)")
    k = len(parsed_traces[0].work)
    if k == 0:
        raise TraceShapeError("no work lines parsed from demonstration traces")
    for j, pt in enumerate(parsed_traces):
        if len(pt.work) != k:
            raise TraceShapeError(
                f"trace {j} has {len(pt.work)} work lines, expected {k}")
        if pt.result_text is None:
            raise TraceShapeError(f"trace {j} has no result line")
    orig_names = list(build[0][0].keys())
    labels: List[str] = []
    for i, (lab, _) in enumerate(parsed_traces[0].work):
        s = sanitize_label(lab, f"w{i}")
        base, n = s, 2
        while s in labels:
            s = f"{base}_{n}"
            n += 1
        labels.append(s)
    inter_labels = labels[:-1]  # last work line is context, not an intermediate
    inter_vals = []
    for pt in parsed_traces:
        inter_vals.append([intermediate_value(c) for _, c in pt.work[:-1]])
    results = [result_value(pt.result_text) for pt in parsed_traces]

    subproblems: List[SubProblem] = []
    for i in range(k - 1):
        in_names = list(orig_names) + inter_labels[:i]
        examples = []
        for j, (inputs, _expected) in enumerate(build):
            d = dict(inputs)
            for m in range(i):
                d[inter_labels[m]] = inter_vals[j][m]
            examples.append((d, inter_vals[j][i]))
        subproblems.append(SubProblem(input_names=in_names, examples=examples,
                                      kind="intermediate", label=inter_labels[i]))
    # Final subproblem: original inputs + all intermediates -> parsed result.
    in_names = list(orig_names) + list(inter_labels)
    examples = []
    for j, (inputs, _expected) in enumerate(build):
        d = dict(inputs)
        for m, lab in enumerate(inter_labels):
            d[lab] = inter_vals[j][m]
        examples.append((d, results[j]))
    subproblems.append(SubProblem(input_names=in_names, examples=examples,
                                  kind="final", label="final"))
    return subproblems


def _remap_ref(ref: Any, step_idx: int, orig_names: Sequence[str],
               inter_labels: Sequence[str], out_refs: Sequence[Any],
               prefix: str) -> Any:
    """Remap one $param/$step reference into the composed plan's namespace."""
    if isinstance(ref, dict) and "$param" in ref:
        pname = ref["$param"]
        if pname in orig_names:
            return {"$param": pname}
        if pname in inter_labels[:step_idx]:
            return out_refs[inter_labels.index(pname)]
        raise TraceShapeError(
            f"subplan {step_idx} references unknown input {pname!r}")
    if isinstance(ref, dict) and "$step" in ref:
        return {"$step": f"{prefix}{ref['$step']}"}
    return ref


def compose_plan(sub_plans: Sequence[Dict[str, Any]],
                 orig_names: Sequence[str],
                 inter_labels: Sequence[str],
                 plan_name: str) -> Dict[str, Any]:
    """Splice per-subproblem plans into one composed plan, in trace order.

    Each subplan's steps are namespaced t{i}_* and their $param references
    to earlier intermediates are rewired to the producing $step. Handles
    single-step and multi-step subplans uniformly.
    """
    if not sub_plans:
        raise TraceShapeError("no subplans to compose")
    steps: List[Dict[str, Any]] = []
    out_refs: List[Any] = []
    for i, plan in enumerate(sub_plans):
        prefix = f"t{i}_"
        psteps = plan.get("steps") or []
        if not psteps:
            raise TraceShapeError(f"subplan {i} has no steps")
        for s in psteps:
            if "op" not in s:
                raise TraceShapeError(f"subplan {i} step missing op: {s!r}")
            new_args = {
                an: _remap_ref(av, i, orig_names, inter_labels, out_refs, prefix)
                for an, av in (s.get("args") or {}).items()
            }
            steps.append({"id": f"{prefix}{s.get('id', 's')}",
                          "op": s["op"], "args": new_args})
        out_refs.append(_remap_ref(plan.get("output"), i, orig_names,
                                   inter_labels, out_refs, prefix))
    return {
        "name": plan_name,
        "params": {n: "any" for n in orig_names},
        "steps": steps,
        "output": out_refs[-1],
    }
