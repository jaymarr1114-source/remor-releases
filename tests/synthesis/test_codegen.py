"""Tests for the CREATE_FILE codegen machinery (Worker C).

Frame in -> real file out. The headline case uses the REAL planner
(engine.planner.propose("random number generator", allow_effects=True))
and asserts the full causal chain:

  purpose -> plan (ops_used) -> emitted source containing those ops' real
  implementations -> file bytes on disk -> subprocess execution output.

Randomness is proved, not asserted: the generated file is executed twice
and the outputs must differ (a canned file would repeat itself).

Refusal cases assert fail-closed behavior: no plan / unrenderable plan /
bad frame / unsupported language never produce a file.
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "pylib"
    ),
)

from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.synthesis.codegen import (  # noqa: E402
    CodegenError,
    render_plan_to_source,
    sanitize_filename,
    synthesize_file,
)
from swarm_engine.synthesis.semantic_frames import Intent, IntentFrame  # noqa: E402


@pytest.fixture(scope="module")
def engine():
    workdir = tempfile.mkdtemp(prefix="codegen_test_eng_")
    eng = SwarmEngine(db_path=os.path.join(workdir, "eng.db"))
    yield eng


def _frame(purpose, **overrides):
    entities = {"artifact": "file", "language": "python", "purpose": purpose}
    entities.update(overrides.pop("entities", {}))
    return IntentFrame(
        raw=overrides.pop("raw", "Create a python file"),
        intent=overrides.pop("intent", Intent.CREATE_FILE),
        entities=entities,
        mood="imperative",
        confidence=0.9,
        trace=["test-constructed"],
    )


def _run_file(path, timeout=30):
    return subprocess.run(
        [sys.executable, path],
        capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# headline: purpose -> plan -> source -> file -> random numbers
# ---------------------------------------------------------------------------

def test_planner_genuinely_synthesizes_rng_plan(engine):
    """The REAL planner (not a stub) composes a plan for the purpose."""
    proposals = engine.planner.propose("random number generator",
                                       allow_effects=True)
    assert proposals, "planner composed nothing -- synthesis_failed path"
    top = proposals[0]
    assert top.ops_used == ["random_float"], top.ops_used
    assert top.strategy == "backward_search"
    # the op is the engine's real randomness primitive (an effect, hence
    # allow_effects=True was required)
    prim = engine.primitives.get("random_float")
    assert prim is not None and not prim.pure


def test_rng_end_to_end(engine):
    out_dir = tempfile.mkdtemp(prefix="codegen_test_out_")
    res = synthesize_file(
        _frame("random number generator", entities={"filename_hint": "rng.py"}),
        engine, out_dir)

    assert res["ok"] is True, res
    assert res["refusal"] is None
    assert res["language"] == "python"
    assert res["purpose"] == "random number generator"
    assert res["ops_used"] == ["random_float"]
    path = res["path"]
    assert path and os.path.isfile(path)
    assert path.endswith("rng.py")
    assert res["bytes"] > 0

    # the emitted source contains the op's REAL implementation, lifted from
    # the registered primitive -- not a template, not a guess
    src = open(path, encoding="utf-8").read()
    assert "_random.random()" in src
    assert "import random" in src
    assert "def run(" in src
    assert '__main__' in src
    # the plan is embedded for auditability
    assert "random_float" in src

    # run() itself is the plan's dataflow, callable in-process
    ns = {}
    exec(compile(src, path, "exec"), ns)
    assert isinstance(ns["run"](), float)

    # the file executes standalone and prints numbers...
    outs = []
    for _ in range(2):
        proc = _run_file(path)
        assert proc.returncode == 0, proc.stderr
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        assert lines, "generated file printed nothing"
        numbers = [float(ln) for ln in lines]  # raises if not numeric
        assert all(0.0 <= n < 1.0 for n in numbers), numbers
        outs.append(proc.stdout)
    # ...and the outputs DIFFER across runs: real randomness, not canned
    assert outs[0] != outs[1], "identical output across runs -- not random"


def test_causal_chain_source_matches_plan(engine):
    """render_plan_to_source works on plan structure for another purpose."""
    proposal = engine.planner.propose("average of the values",
                                      allow_effects=True)[0]
    assert proposal.ops_used == ["computation.mean"]
    src, meta = render_plan_to_source(proposal.plan, engine.primitives,
                                      purpose="average of the values")
    # the mean primitive's real implementation + its closure helper were lifted
    assert "statistics.fmean" in src
    assert "import statistics" in src
    assert "_nonempty" in src
    assert meta["ops"] == ["computation.mean"]
    # and the rendered dataflow computes the right answer in-process
    ns = {}
    exec(compile(src, "<codegen-test>", "exec"), ns)
    assert ns["run"](values=[1.0, 2.0, 3.0, 4.0]) == 2.5


def test_lambda_plan_renders(engine):
    """A plan with a nested $lambda (filter_aggregate) renders and runs."""
    proposal = engine.planner.propose("sum of values above 5",
                                      allow_effects=True)[0]
    assert "data.filter" in proposal.ops_used
    src, _meta = render_plan_to_source(proposal.plan, engine.primitives,
                                       purpose="sum of values above 5")
    ns = {}
    exec(compile(src, "<codegen-test>", "exec"), ns)
    assert ns["run"](values=[3, 7, 9]) == 16


# ---------------------------------------------------------------------------
# fail-closed refusals
# ---------------------------------------------------------------------------

def test_refusal_when_plan_needs_engine_context(engine):
    """The planner composes a plan, but its primitive needs the engine's
    ExecContext -- no standalone file can provide it, so we refuse rather
    than emit a fake."""
    proposals = engine.planner.propose("recall a number", allow_effects=True)
    assert proposals and proposals[0].ops_used == ["recall"]
    assert engine.primitives.get("recall").needs_ctx

    out_dir = tempfile.mkdtemp(prefix="codegen_test_ref_")
    res = synthesize_file(_frame("recall a number"), engine, out_dir)

    assert res["ok"] is False
    assert res["refusal"] == "render_failed"
    assert "execution context" in res["detail"]
    assert res["path"] is None
    assert os.listdir(out_dir) == [], "refusal must not leave a file behind"


def test_refusal_unsupported_language(engine):
    out_dir = tempfile.mkdtemp(prefix="codegen_test_lang_")
    res = synthesize_file(
        _frame("random number generator", entities={"language": "cobol"}),
        engine, out_dir)
    assert res["ok"] is False
    assert res["refusal"] == "unsupported_language"
    assert os.listdir(out_dir) == []


def test_refusal_non_actionable_frame(engine):
    out_dir = tempfile.mkdtemp(prefix="codegen_test_unk_")
    bad = _frame("random number generator", intent=Intent.UNKNOWN)
    res = synthesize_file(bad, engine, out_dir)
    assert res["ok"] is False
    assert res["refusal"] == "frame_not_actionable"


def test_refusal_wrong_intent(engine):
    out_dir = tempfile.mkdtemp(prefix="codegen_test_int_")
    bad = _frame("random number generator", intent=Intent.COMPUTE)
    res = synthesize_file(bad, engine, out_dir)
    assert res["ok"] is False
    assert res["refusal"] == "wrong_intent"


def test_refusal_missing_purpose(engine):
    out_dir = tempfile.mkdtemp(prefix="codegen_test_purp_")
    bad = _frame("", entities={"purpose": ""})
    res = synthesize_file(bad, engine, out_dir)
    assert res["ok"] is False
    assert res["refusal"] == "missing_purpose"


def test_refusal_when_demo_cannot_supply_params(engine):
    """A plan whose steps need caller params cannot be demoed or verified
    standalone -- refused, not shipped unverified."""
    out_dir = tempfile.mkdtemp(prefix="codegen_test_param_")
    res = synthesize_file(_frame("sum of the unique values"), engine, out_dir)
    assert res["ok"] is False
    assert res["refusal"] == "verification_failed"


def test_render_refuses_unknown_op(engine):
    with pytest.raises(CodegenError):
        render_plan_to_source(
            {"name": "x", "params": {}, "steps": [
                {"id": "s1", "op": "no.such.primitive", "args": {}}],
             "output": {"$step": "s1"}},
            engine.primitives)


# ---------------------------------------------------------------------------
# governed writing: filename hygiene + no traversal
# ---------------------------------------------------------------------------

def test_filename_sanitization():
    assert sanitize_filename("rng.py", "whatever") == "rng.py"
    assert sanitize_filename("../../evil.py", "whatever") == "evil.py"
    assert sanitize_filename("/abs/path.py", "whatever") == "path.py"
    assert sanitize_filename("my rng!!", "whatever") == "my_rng.py"
    assert sanitize_filename("", "random number generator") == \
        "random_number_generator.py"
    assert sanitize_filename(None, "") == "generated.py"


def test_traversal_hint_cannot_escape_out_dir(engine):
    out_dir = tempfile.mkdtemp(prefix="codegen_test_trav_")
    res = synthesize_file(
        _frame("random number generator",
               entities={"filename_hint": "../../evil.py"}),
        engine, out_dir)
    assert res["ok"] is True, res
    real_out = os.path.realpath(out_dir)
    assert os.path.realpath(res["path"]).startswith(real_out + os.sep)
    assert os.path.basename(res["path"]) == "evil.py"
    assert not os.path.exists(os.path.join(os.path.dirname(real_out),
                                           "evil.py"))


def test_result_contract_shape(engine):
    """The result dict carries exactly the contracted keys."""
    out_dir = tempfile.mkdtemp(prefix="codegen_test_shape_")
    res = synthesize_file(_frame("random number generator"), engine, out_dir)
    for key in ("ok", "path", "bytes", "language", "purpose", "refusal"):
        assert key in res, f"missing contracted key {key!r}"
    # bytes is the real on-disk size
    assert res["bytes"] == os.path.getsize(res["path"])
    # execution evidence is attached for the reviewer
    assert res["execution"]["exit_code"] == 0
    assert res["execution"]["numbers"] >= 1
