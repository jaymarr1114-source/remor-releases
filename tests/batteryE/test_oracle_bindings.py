"""Adversarial + functional tests for the 9 bound oracle families.

Runs against the isolated worktree (pylib symlink). Not a deliverable;
scratch verification for the trust-anchor workstream.
"""
import asyncio
import os
import sys
import tempfile

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))

from swarm_engine.governance.oracle_binding import OracleBindingError

PASS = []
FAIL = []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  ok: {name}")
    except Exception as exc:
        FAIL.append((name, exc))
        print(f"  FAIL: {name}: {type(exc).__name__}: {exc}")


def expect_raise(name, fn, substr=""):
    try:
        fn()
    except OracleBindingError as exc:
        assert substr in str(exc), f"message missing {substr!r}: {exc}"
        PASS.append(name)
        print(f"  ok: {name}")
        return
    except Exception as exc:
        FAIL.append((name, exc))
        print(f"  FAIL: {name}: wrong exc {type(exc).__name__}: {exc}")
        return
    FAIL.append((name, AssertionError("no exception raised")))
    print(f"  FAIL: {name}: no exception raised")


def make_engine():
    from swarm_engine.core.engine import SwarmEngine
    db = tempfile.mktemp(suffix=".db")
    eng = SwarmEngine(db_path=db)
    return eng, db


def make_producer(eng, ptype="agent"):
    cred = eng.oracle_registry.register_producer(ptype, source="test")
    return cred.producer_id, cred.token


# ---------------------------------------------------------------- O18
print("== O18 artifact-validator registry ==")
eng, db = make_engine()
try:
    import swarm_engine.capability.artifact_validation as av

    class MyValidator(av.TextArtifactValidator):
        domain = "testdom"
        def validate(self, content):
            return av.ValidationVerdict(valid=True, checks_passed=["ok"])

    class EvilValidator(av.TextArtifactValidator):
        domain = "testdom"
        def validate(self, content):
            return av.ValidationVerdict(valid=True, checks_passed=["evil"])

    my_v = MyValidator()

    # unregistered install refused in bound mode
    expect_raise("o18 no-creds refused",
                 lambda: av.register_validator(my_v),
                 "authenticated producer required")
    pid, tok = make_producer(eng)
    expect_raise("o18 wrong-token refused",
                 lambda: av.register_validator(my_v, producer_id=pid,
                                               token="bad"),
                 "authentication failed")
    # valid registration works
    av.register_validator(my_v, producer_id=pid, token=tok)
    check("o18 valid registration",
          lambda: (_ for _ in ()).throw(AssertionError("no binding"))
          if "testdom" not in av._REGISTRY_BINDINGS else None)

    # use-site verification passes for the registered validator
    def _use():
        v = av.get_validator("testdom")
        assert v is my_v
        b = av.verify_validator_binding("testdom", v)
        assert isinstance(b, dict), "verify failed"
    check("o18 use-site verify passes", _use)

    # swap the validator post-registration -> use-site refuses
    av._REGISTRY["testdom"] = EvilValidator()
    expect_raise("o18 swapped validator refused",
                 lambda: av.verify_validator_binding(
                     "testdom", av.get_validator("testdom")),
                 "refusing")
    # restore
    av._REGISTRY["testdom"] = my_v
finally:
    os.unlink(db)

# ---------------------------------------------------------------- O9
print("== O9 synthesis oracle ==")
eng, db = make_engine()
try:
    def oracle_fn(args):
        return 1

    # direct attribute assignment (unregistered) is refused by the binder
    eng.synthesis_oracle = oracle_fn
    orch = eng.acquisition_orchestrator
    expect_raise("o9 unregistered oracle refused",
                 lambda: orch._bind_synthesis_oracle(oracle_fn),
                 "unregistered")
    # no oracle -> None (legacy)
    eng.synthesis_oracle = None
    check("o9 no oracle -> None",
          lambda: (_ for _ in ()).throw(
              AssertionError("unreachable")) if orch._bind_synthesis_oracle(None) is not None else None)
    # registered path
    pid, tok = make_producer(eng)
    eng.bind_synthesis_oracle(oracle_fn, pid, tok)
    from swarm_engine.governance.binding_helpers import BoundCallable
    bound = orch._bind_synthesis_oracle(oracle_fn)
    check("o9 registered -> BoundCallable",
          lambda: (_ for _ in ()).throw(AssertionError("not bound"))
          if not isinstance(bound, BoundCallable) else None)
    # wrong token refused at bind time
    expect_raise("o9 wrong token refused",
                 lambda: eng.bind_synthesis_oracle(oracle_fn, pid, "bad"),
                 "authentication failed")
    # swap fn after registration -> refused
    def oracle_fn2(args):
        return 2
    eng.synthesis_oracle = oracle_fn2
    expect_raise("o9 swapped fn refused",
                 lambda: orch._bind_synthesis_oracle(oracle_fn2),
                 "unregistered")
finally:
    os.unlink(db)

# ---------------------------------------------------------------- O12
print("== O12 acquisition predicate ==")
eng, db = make_engine()
try:
    from swarm_engine.acquisition.pipeline import (
        AcquisitionPipeline, AcquisitionTest)

    def pred(v):
        return v == 42

    pipe = eng.acquisition  # bound pipeline from the engine
    # unregistered predicate refused at the gate
    t = AcquisitionTest(args={"x": 1}, predicate=pred)
    expect_raise("o12 unregistered predicate refused",
                 lambda: pipe._judge_test(t, 42, type("C", (), {"code": "c"})(), 0),
                 "unregistered predicate refused")
    # bind then judge
    pid, tok = make_producer(eng)
    oid, ver = pipe.bind_predicate("is_42", pred, pid, tok)
    t2 = AcquisitionTest(args={"x": 1}, predicate=pred,
                         oracle_id=oid, oracle_version=ver)
    cand = type("C", (), {"code": "def f(x): return x"})()
    res = pipe._judge_test(t2, 42, cand, 0)
    check("o12 bound predicate judges", lambda: (_ for _ in ()).throw(
        AssertionError(f"bad {res}")) if res != (True, "predicate") else None)
    res2 = pipe._judge_test(t2, 43, cand, 1)
    check("o12 bound predicate rejects", lambda: (_ for _ in ()).throw(
        AssertionError(f"bad {res2}")) if res2[0] else None)
    # tampered predicate (same test, swapped fn) refused
    def pred_evil(v):
        return True
    t3 = AcquisitionTest(args={"x": 1}, predicate=pred_evil,
                         oracle_id=oid, oracle_version=ver)
    expect_raise("o12 swapped predicate refused",
                 lambda: pipe._judge_test(t3, 43, cand, 2),
                 "refusing")
    # wrong token at bind time
    expect_raise("o12 bind wrong token",
                 lambda: pipe.bind_predicate("x", pred, pid, "bad"),
                 "authentication failed")

    # unbound pipeline: legacy judge preserved exactly
    pipe2 = AcquisitionPipeline(sources=[], provenance=None)
    t4 = AcquisitionTest(args={"x": 1}, predicate=lambda v: v == 7)
    check("o12 unbound legacy judge",
          lambda: (_ for _ in ()).throw(AssertionError("bad"))
          if t4.judge(7) != (True, "predicate") else None)
    # unbound _judge_test with predicate == legacy
    r = pipe2._judge_test(t4, 7, cand, 0)
    check("o12 unbound gate == legacy",
          lambda: (_ for _ in ()).throw(AssertionError("bad"))
          if r != (True, "predicate") else None)
finally:
    os.unlink(db)

# ---------------------------------------------------------------- O19
print("== O19 example provenance ==")
eng, db = make_engine()
try:
    from swarm_engine.governance.examples_provenance import (
        record_examples_batch, cite_batch)
    examples = [({"x": 1}, 2), ({"x": 2}, 4)]
    b1 = record_examples_batch(eng.oracle_registry, eng.oracle, "g",
                               examples, "test")
    b2 = record_examples_batch(eng.oracle_registry, eng.oracle, "g",
                               examples, "test")
    check("o19 idempotent batch id",
          lambda: (_ for _ in ()).throw(AssertionError(f"{b1} != {b2}"))
          if b1 != b2 else None)
    check("o19 batch id shape",
          lambda: (_ for _ in ()).throw(AssertionError(b1))
          if not (isinstance(b1, str) and b1.startswith("exb_")) else None)
    check("o19 cite", lambda: (_ for _ in ()).throw(AssertionError("bad"))
          if "exb_" not in cite_batch(b1) else None)
    check("o19 cite none empty",
          lambda: (_ for _ in ()).throw(AssertionError("bad"))
          if cite_batch(None) != "" else None)
    # different examples -> different id
    b3 = record_examples_batch(eng.oracle_registry, eng.oracle, "g",
                               [({"x": 9}, 9)], "test")
    check("o19 distinct examples distinct id",
          lambda: (_ for _ in ()).throw(AssertionError("same"))
          if b3 == b1 else None)
    # verify the stored row exists in the registry
    from swarm_engine.governance.examples_provenance import batch_oracle_row
    row = batch_oracle_row(eng.oracle_registry, eng.oracle.producer_id, b1)
    check("o19 batch stored as oracle",
          lambda: (_ for _ in ()).throw(AssertionError("no row"))
          if row is None else None)
    check("o19 batch row pins examples",
          lambda: (_ for _ in ()).throw(AssertionError("bad row"))
          if row.get("producer_id") != eng.oracle.producer_id else None)
finally:
    os.unlink(db)

# ---------------------------------------------------------------- O20
print("== O20 planner templates ==")
eng, db = make_engine()
try:
    from swarm_engine.synthesis.planner import (
        TemplatePlanner, Template, parse_goal)

    tp = eng.planner.templates
    # anonymous add refused in bound mode
    def buildX(goal, reg):
        return None
    expect_raise("o20 anonymous add refused",
                 lambda: tp.add(Template("evil", ["evil"], [], buildX)),
                 "authenticated producer")
    pid, tok = make_producer(eng)
    expect_raise("o20 add wrong token",
                 lambda: tp.add(Template("evil", ["evil"], [], buildX),
                                producer_id=pid, token="bad"),
                 "authentication failed")
    # duplicate builtin name refused
    expect_raise("o20 duplicate name refused",
                 lambda: tp.add(Template("aggregate", ["z"], [], buildX),
                                producer_id=pid, token=tok,
                                _engine_builtin=True),
                 "already registered")
    # plan() works and verifies (use a real goal)
    goal = parse_goal("compute the average of the list")
    prop = tp.plan(goal)
    check("o20 plan works bound",
          lambda: (_ for _ in ()).throw(AssertionError("no plan"))
          if prop is None else None)
    # tamper a builtin build -> plan refuses
    agg = next(t for t in tp.templates if t.name == "aggregate")
    orig_build = agg.build
    agg.build = lambda goal, reg: {"name": "pwned", "ops": []}
    expect_raise("o20 tampered template refused",
                 lambda: tp.plan(goal),
                 "refusing")
    agg.build = orig_build
    # unbound planner: legacy add + plan
    tp2 = TemplatePlanner(eng.primitives)
    tp2.add(Template("x", ["zzz"], [], buildX))
    check("o20 unbound legacy add", lambda: len(tp2.templates))
finally:
    os.unlink(db)

# ---------------------------------------------------------------- O16
print("== O16 delegate provenance ==")
eng, db = make_engine()
try:
    from swarm_engine.acquisition.strategies import DelegatingSource
    from swarm_engine.acquisition.pipeline import CapabilityRequirement

    def my_delegate(spec):
        return ["def f(x): return x"]

    # no creds in bound mode -> refused
    expect_raise("o16 no-creds refused",
                 lambda: DelegatingSource(
                     my_delegate, oracle_registry=eng.oracle_registry,
                     engine_oracle=eng.oracle),
                 "authenticated producer")
    pid, tok = make_producer(eng)
    expect_raise("o16 wrong token",
                 lambda: DelegatingSource(
                     my_delegate, producer_id=pid, token="bad",
                     oracle_registry=eng.oracle_registry,
                     engine_oracle=eng.oracle),
                 "authentication failed")
    src = DelegatingSource(my_delegate, producer_id=pid, token=tok,
                           oracle_registry=eng.oracle_registry,
                           engine_oracle=eng.oracle)
    req = CapabilityRequirement(name="d", description="d")
    cands = src.search(req)
    check("o16 bound search works",
          lambda: (_ for _ in ()).throw(AssertionError("no cands"))
          if not cands else None)
    # swap delegate post-registration -> refused
    src.delegate = lambda spec: ["evil"]
    expect_raise("o16 swapped delegate refused",
                 lambda: src.search(req), "refusing")
    # unbound: legacy
    src2 = DelegatingSource(my_delegate)
    c2 = src2.search(req)
    check("o16 unbound legacy",
          lambda: (_ for _ in ()).throw(AssertionError("bad"))
          if len(c2) != 1 else None)
finally:
    os.unlink(db)

# ---------------------------------------------------------------- O15
print("== O15 spec provider ==")
eng, db = make_engine()
try:
    from swarm_engine.acquisition.strategies import (
        SynthesizingSource, CapabilitySpec)
    from swarm_engine.acquisition.pipeline import CapabilityRequirement

    def my_provider(req):
        return CapabilitySpec.from_requirement(req)

    expect_raise("o15 no-creds refused",
                 lambda: SynthesizingSource(
                     my_provider, oracle_registry=eng.oracle_registry,
                     engine_oracle=eng.oracle),
                 "authenticated producer")
    pid, tok = make_producer(eng)
    src = SynthesizingSource(my_provider, producer_id=pid, token=tok,
                             oracle_registry=eng.oracle_registry,
                             engine_oracle=eng.oracle)
    req = CapabilityRequirement(name="s", description="s")
    spec = src._bound_provider()[0](req)
    check("o15 bound provider works", lambda: spec.name)
    # default path (no provider) still works bound
    src2 = SynthesizingSource(oracle_registry=eng.oracle_registry,
                              engine_oracle=eng.oracle)
    check("o15 default path unbound-by-design",
          lambda: src2._bound_provider()[0])
    # unbound legacy
    src3 = SynthesizingSource(my_provider)
    check("o15 unbound legacy",
          lambda: (_ for _ in ()).throw(AssertionError("bad"))
          if src3._bound_provider()[0] is not my_provider else None)
finally:
    os.unlink(db)

# ---------------------------------------------------------------- O14
print("== O14 goal sampler ==")
eng, db = make_engine()
try:
    from swarm_engine.improvement.pipeline import AcquisitionPolicyValidator

    def my_sampler(sig):
        return [("g1", [({"x": 1}, 1)])]

    expect_raise("o14 no-creds refused",
                 lambda: AcquisitionPolicyValidator(
                     eng, goal_sampler=my_sampler),
                 "authenticated producer")
    pid, tok = make_producer(eng)
    v = AcquisitionPolicyValidator(eng, goal_sampler=my_sampler,
                                   producer_id=pid, token=tok)
    goals, batch = v._sample_goals("sig")
    check("o14 bound sample works + batch id",
          lambda: (_ for _ in ()).throw(AssertionError((goals, batch)))
          if not (goals and batch and batch.startswith("sampled_")) else None)
    # tamper sampler -> refused
    v.goal_sampler = lambda sig: []
    expect_raise("o14 swapped sampler refused",
                 lambda: v._sample_goals("sig"), "refusing")
    # engine default pinned (from boot)
    v2 = eng.acquisition_improvement_pipeline.validators[
        "acquisition.strategy_policy"]
    check("o14 engine default pinned",
          lambda: (_ for _ in ()).throw(AssertionError("not bound"))
          if v2._sampler_binding is None else None)
finally:
    os.unlink(db)

# ---------------------------------------------------------------- O13
print("== O13 regression runner ==")
eng, db = make_engine()
try:
    from swarm_engine.improvement.loop import SelfImprovementEngine

    def my_runner():
        return True, {"suites": 3}

    expect_raise("o13 no-creds refused",
                 lambda: SelfImprovementEngine(eng, regression_runner=my_runner),
                 "authenticated producer")
    pid, tok = make_producer(eng)
    sie = SelfImprovementEngine(eng, regression_runner=my_runner,
                                producer_id=pid, token=tok)
    r, b = sie._bound_runner()
    check("o13 bound runner resolves",
          lambda: (_ for _ in ()).throw(AssertionError("bad"))
          if r is not my_runner or b is None else None)
    # direct assignment of unregistered runner -> refused at gate
    sie.regression_runner = lambda: (True, {})
    expect_raise("o13 unregistered assignment refused",
                 lambda: sie._bound_runner(), "unregistered")
    # swap after registration -> refused
    sie2 = SelfImprovementEngine(eng, regression_runner=my_runner,
                                 producer_id=pid, token=tok)
    sie2.regression_runner = lambda: (True, {})
    expect_raise("o13 swapped runner refused",
                 lambda: sie2._bound_runner(), "unregistered")
    # legacy engine default: no runner, gate skipped, marked
    sie3 = eng.self_improvement
    check("o13 engine default no runner",
          lambda: (_ for _ in ()).throw(AssertionError("has runner"))
          if sie3.regression_runner is not None else None)
finally:
    os.unlink(db)

# ------------------------------------------------- BoundCallable fail-closed
print("== BoundCallable fail-closed recording ==")
eng, db = make_engine()
try:
    from swarm_engine.governance.binding_helpers import BoundCallable

    def fn(x):
        return x * 2

    pid, tok = make_producer(eng)
    oid, ver = eng.oracle_registry.register_oracle(
        pid, tok, "probe_fn", fn, source="test")
    bound = BoundCallable(eng.oracle_registry, eng.oracle, fn, oid, ver,
                          pid, what="probe")
    check("bound call works", lambda: (_ for _ in ()).throw(
        AssertionError("bad")) if bound(21) != 42 else None)

    # break recording: poison the handle
    class BrokenHandle:
        def evaluate(self, *a, **k):
            raise RuntimeError("db gone")
    broken = BoundCallable(eng.oracle_registry, BrokenHandle(), fn, oid, ver,
                           pid, what="probe")
    expect_raise("recording failure raises",
                 lambda: broken(1), "not chained")
finally:
    os.unlink(db)

print()
print(f"PASSED {len(PASS)}, FAILED {len(FAIL)}")
if FAIL:
    sys.exit(1)
