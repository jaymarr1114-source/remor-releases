"""Shared harness for the Track 1 repair-learning loop.

Deploys the isolated org with the external trust anchor (via the
scratch anchor_shim -- the same operational model as the trust-anchor
mission's battery-E shim), provides the engine-side independent example
extractor and the technique judges, and the check() accounting used by
all drivers.
"""
import ast
import json
import os
import re
import sys

# Canonical paths (ported from remor_repair_loop)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CANONICAL = os.path.join(_THIS_DIR, "..", "..")
sys.path.insert(0, os.path.join(_CANONICAL, "pylib"))   # swarm_engine -> runtime
sys.path.insert(0, _THIS_DIR)  # anchor_shim (local)

_WORK_ROOT = os.environ.get("REMOR_TEST_WORK_ROOT", "/tmp/remor_repair_test")
WORK = os.path.join(_WORK_ROOT, "repair_loop", "work")
# TRACK1 for compatibility (points to canonical test dir)
TRACK1 = _THIS_DIR
os.makedirs(WORK, exist_ok=True)

import anchor_shim  # noqa: E402
from swarm_engine.agent_org.store import digest  # noqa: E402
from swarm_engine.acquisition.semantic import Case  # noqa: E402
from swarm_engine.agent_org.subprocess_runner import run_code  # noqa: E402

CHECKS = {"pass": 0, "fail": 0, "log": []}


def check(name, cond):
    CHECKS["log"].append((name, bool(cond)))
    if cond:
        CHECKS["pass"] += 1
        print(f"  ok: {name}", flush=True)
    else:
        CHECKS["fail"] += 1
        print(f"  FAIL: {name}", flush=True)


def failfast():
    if CHECKS["fail"]:
        raise SystemExit(f"{CHECKS['fail']} checks failed")


def deploy_org(workdir):
    """Fresh org + explicit anchor genesis + auto-anchor wrapper."""
    os.makedirs(workdir, exist_ok=True)
    return anchor_shim.deploy_org(workdir)


def attach_org(workdir):
    """Boot an existing anchored org (fresh process)."""
    return anchor_shim.attach_org(workdir)


# --- engine-side INDEPENDENT example extraction (used by judges) ---
_ASSERT_RE = re.compile(
    r"^\s*assert\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*==\s*([^\n#;]+)",
    re.MULTILINE)


def engine_extract_examples(test_text):
    """Independently parse assert f(args)==expected examples, by function.

    This is the ENGINE's implementation, written separately from the
    technique's own extractor: the judge never trusts the technique's
    parsing. The parsed data is bound via the spec digest; the judge
    code is bound via the oracle registry.
    """
    by_func = {}
    for m in _ASSERT_RE.finditer(test_text or ""):
        name = m.group(1)
        args = [ast.literal_eval(t.strip())
                for t in m.group(2).split(",") if t.strip()]
        expected = ast.literal_eval(m.group(3).strip())
        by_func.setdefault(name, []).append((tuple(args), expected))
    return by_func


class TechniqueSpec:
    input_names = ["source_text", "test_text"]
    examples = [({"source_text": "def f(a, b):\n    return a - b\n",
                  "test_text": "assert f(1, 2) == 3\n"}, None)]


def make_technique_predicate(test_text, target):
    """Build the judge predicate for one technique review case.

    The predicate is a pure function of the technique's returned value:
    it parses the returned JSON, INDEPENDENTLY re-extracts the examples
    for `target` from the case's test_text (engine implementation), then
    executes the returned repaired source in a subprocess and requires
    every example to hold. The technique's own claims determine nothing.
    """
    def technique_judge(value):
        try:
            report = json.loads(value)
        except Exception:
            return False
        if not isinstance(report, dict) or "error" in report:
            return False
        repaired = report.get("repaired_source")
        tgt = report.get("target")
        if not repaired or tgt != target:
            return False
        examples = engine_extract_examples(test_text).get(target, [])
        if not examples:
            return False
        call_args = [{"a": a, "b": b} for (a, b), _ in examples]
        rr = run_code(repaired, target, call_args)
        if not rr.ok:
            return False
        for ((a, b), expected), res in zip(examples, rr.value):
            if not res.get("ok") or res.get("value") != expected:
                return False
        return True
    return technique_judge


def technique_case(source_text, test_text, target, label):
    return Case(args={"source_text": source_text, "test_text": test_text},
                predicate=make_technique_predicate(test_text, target),
                label=label)


def instance_cases(examples, label_prefix="ex"):
    """Cases for verifying a repaired single-function artifact instance."""
    return [Case(args={"a": a, "b": b}, expect=expected,
                 label=f"{label_prefix}{i}")
            for i, ((a, b), expected) in enumerate(examples)]


class FuncSpec:
    input_names = ["a", "b"]
    examples = [({"a": 1, "b": 2}, None)]
