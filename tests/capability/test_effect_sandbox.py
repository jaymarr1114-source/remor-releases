"""Real-execution tests for runtime/capability/effect_sandbox.py (W4-R1).

Every deny is proven by actually exec'ing code in the sandboxed namespace
in-process -- no mocks, no simulated denials. The centerpiece is the exact
W4-R1 bypass: ``__builtins__["op" + "en"]``, which defeated the old AST-based
PURE screen, must fail here by capability-absence (KeyError), with no string
blacklist anywhere in the mechanism.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from runtime.capability.effect_sandbox import (  # noqa: E402
    AUDIT_EVENTS,
    PURE_MODULES,
    PURE_POLICY,
    PURE_PROFILE,
    SAFE_BUILTINS,
    TRUSTED_POLICY,
    TRUSTED_PROFILE,
    EffectPolicy,
    build_guarded_import,
    build_namespace,
    install_audit_hook,
    policy_allows,
    policy_for_effects,
    restricted_builtins,
    scoped_open,
    shim_policy_data,
    summarize_violations,
)


def _pure_ns():
    return build_namespace(PURE_POLICY)


# ---------------------------------------------------------------------------
# 1-4: the W4-R1 bypass and its siblings, denied by capability-absence
# ---------------------------------------------------------------------------

def test_w4r1_obfuscated_open_raises_keyerror(tmp_path):
    """The exact W4-R1 bypass: __builtins__["op" + "en"] must be absent."""
    target = tmp_path / "pwned.txt"
    ns = _pure_ns()
    with pytest.raises(KeyError):
        exec('__builtins__["op" + "en"]("%s", "w")' % target, ns)
    assert not target.exists(), "no file may be created by the bypass"


def test_bare_open_raises_nameerror():
    ns = _pure_ns()
    with pytest.raises(NameError):
        exec('open("/tmp/should_not_exist_xyz", "w")', ns)


def test_getattr_on_builtins_cannot_reach_open():
    ns = _pure_ns()
    with pytest.raises((KeyError, AttributeError)):
        exec('getattr(__builtins__, "op" + "en")', ns)


def test_eval_absent_so_no_dynamic_import():
    ns = _pure_ns()
    with pytest.raises(NameError):
        exec('eval("__import__(\'os\')")', ns)
    # exec / compile / input are gone too
    for banned in ("exec", "compile", "input", "breakpoint"):
        with pytest.raises(NameError):
            exec(f"{banned}('1')", _pure_ns())


# ---------------------------------------------------------------------------
# 5: the guarded __import__ -- real gate, consulted on every import
# ---------------------------------------------------------------------------

def test_import_os_denied_by_guard():
    ns = _pure_ns()
    with pytest.raises(ImportError) as excinfo:
        exec("import os", ns)
    assert "effect capability not granted" in str(excinfo.value)


def test_import_socket_subprocess_sys_denied():
    for mod in ("sys", "socket", "subprocess", "shutil", "pathlib",
                "ctypes", "importlib", "threading"):
        with pytest.raises(ImportError):
            exec(f"import {mod}", _pure_ns())


def test_pure_imports_keep_working():
    """Legitimate pure imports synthesized code relies on must succeed."""
    ns = _pure_ns()
    exec(
        "import math\n"
        "import random\n"
        "import statistics\n"
        "from math import sqrt\n"
        "from collections import Counter\n"
        "result = (sqrt(16), statistics.mean([1, 2, 3, 4]))\n",
        ns,
    )
    assert ns["result"] == (4.0, 2.5)
    assert ns["Counter"]("aab")["a"] == 2


def test_relative_import_of_nonpure_root_rejected():
    guard = build_guarded_import()
    with pytest.raises(ImportError) as excinfo:
        guard("os", level=1)
    assert "effect capability not granted" in str(excinfo.value)


def test_guard_exposes_no_authority_in_globals():
    # W4-R1 residual repair: the guard is exec'd with minimal __globals__
    # so fn.__globals__ cannot reach _real_import/_os/_builtins.
    guard = build_guarded_import()
    assert guard.__name__ == "guarded_import"
    assert set(guard.__globals__) <= {
        "__builtins__", "PURE_MODULES", "_PREIMPORTED", "guarded_import",
    }
    assert guard.__globals__["__builtins__"] == {"ImportError": ImportError}


def test_guard_serves_pure_modules_from_preimport_snapshot():
    guard = build_guarded_import()
    import math
    assert guard("math") is math
    with pytest.raises(ImportError) as excinfo:
        guard("os")
    assert "effect capability not granted" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6: real pure computation still works
# ---------------------------------------------------------------------------

def test_pure_computation_classes_exceptions_comprehensions(capsys):
    ns = _pure_ns()
    exec(
        "import json, math, statistics\n"
        "\n"
        "def double(f):\n"
        "    def w(*a, **k):\n"
        "        return 2 * f(*a, **k)\n"
        "    return w\n"
        "\n"
        "class Shape:\n"
        "    def __init__(self, sides):\n"
        "        self.sides = sides\n"
        "    def perimeter(self):\n"
        "        return sum(self.sides)\n"
        "\n"
        "class Square(Shape):\n"
        "    def __init__(self, side):\n"
        "        super().__init__([side] * 4)\n"
        "\n"
        "class OddError(ValueError):\n"
        "    pass\n"
        "\n"
        "@double\n"
        "def tripled(x):\n"
        "    return 3 * x\n"
        "\n"
        "squares = {i: i * i for i in range(5)}\n"
        "evens = [i for i in range(10) if i % 2 == 0]\n"
        "gen = (i * i for i in range(4))\n"
        "try:\n"
        "    raise OddError('nope')\n"
        "except OddError as e:\n"
        "    caught = str(e)\n"
        "payload = json.dumps({'s': squares, 'm': statistics.mean(evens)})\n"
        "root = math.sqrt(49)\n"
        "print('hello pure', 42)\n",
        ns,
    )
    assert ns["Square"](3).perimeter() == 12
    assert ns["tripled"](5) == 30
    assert ns["squares"] == {0: 0, 1: 1, 2: 4, 3: 9, 4: 16}
    assert ns["evens"] == [0, 2, 4, 6, 8]
    assert list(ns["gen"]) == [0, 1, 4, 9]
    assert ns["caught"] == "nope"
    assert json.loads(ns["payload"]) == {
        "s": {"0": 0, "1": 1, "2": 4, "3": 9, "4": 16}, "m": 4.0}
    assert ns["root"] == 7.0
    out = capsys.readouterr().out
    assert "hello pure 42" in out


# ---------------------------------------------------------------------------
# 7: scoped_open
# ---------------------------------------------------------------------------

def test_scoped_open_allows_inside_denies_outside(tmp_path):
    scope = tmp_path / "scope"
    scope.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    opener = scoped_open(str(scope))

    inside = scope / "data.txt"
    with opener(str(inside), "w", encoding="utf-8") as f:
        f.write("granted")
    with opener(str(inside), "r") as f:
        assert f.read() == "granted"

    with pytest.raises(PermissionError) as excinfo:
        opener(str(outside / "evil.txt"), "w")
    assert "effect capability not granted" in str(excinfo.value)


def test_scoped_open_blocks_dotdot_escape(tmp_path):
    scope = tmp_path / "scope"
    (scope / "a").mkdir(parents=True)
    opener = scoped_open(str(scope))
    escape = os.path.join(str(scope), "a", "..", "..", "escaped.txt")
    assert os.path.realpath(escape) != str(scope)  # sanity: it escapes
    with pytest.raises(PermissionError):
        opener(escape, "w")
    assert not os.path.exists(os.path.realpath(escape))


def test_scoped_open_rejects_file_descriptors(tmp_path):
    opener = scoped_open(str(tmp_path))
    with pytest.raises(PermissionError):
        opener(1, "w")


def test_scoped_open_accepts_pathlike_and_single_string(tmp_path):
    opener = scoped_open(tmp_path)  # PathLike, not str
    p = tmp_path / "ok.txt"
    with opener(p, "w") as f:
        f.write("x")
    assert p.read_text() == "x"


# ---------------------------------------------------------------------------
# 8: the audit hook is a real sys.addaudithook
# ---------------------------------------------------------------------------

def test_audit_hook_records_real_open(tmp_path):
    record = []
    install_audit_hook(record)
    probe = tmp_path / "probe.txt"
    with open(probe, "w") as f:
        f.write("audit me")
    hits = [e for e in record
            if e["event"] == "open" and str(probe) in (e["args"][0] if e["args"] else "")]
    assert hits, f"expected a recorded open() event, got: {record[-3:]}"
    assert set(hits[0].keys()) == {"event", "args"}
    assert all(isinstance(a, str) and len(a) <= 200 for a in hits[0]["args"])


def test_audit_events_verified():
    assert "open" in AUDIT_EVENTS
    for required in ("socket.__new__", "os.system", "subprocess.Popen"):
        assert required in AUDIT_EVENTS


# ---------------------------------------------------------------------------
# 9: declarations never grant authority
# ---------------------------------------------------------------------------

def test_declaration_without_grant_is_pure():
    assert policy_for_effects(["write_fs"], None) == PURE_POLICY
    assert policy_for_effects(["write_fs"], None).is_pure()


def test_explicit_grant_produces_granted_policy():
    policy = policy_for_effects(["write_fs"], "/tmp/s")
    assert policy == EffectPolicy("granted", ("fs.write:/tmp/s",))
    assert not policy.is_pure()


def test_no_declaration_no_grant():
    assert policy_for_effects([], "/tmp/s") == PURE_POLICY


# ---------------------------------------------------------------------------
# policy plumbing
# ---------------------------------------------------------------------------

def test_effect_policy_roundtrip_and_frozen():
    p = EffectPolicy("granted", ("fs.write:/tmp/s",))
    assert EffectPolicy.from_dict(p.to_dict()) == p
    assert EffectPolicy.from_dict({}) == PURE_POLICY
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.profile = "pure"  # type: ignore[misc]


def test_pure_policy_denies_every_event():
    event = {"event": "open", "args": ["/tmp/s/file.txt"]}
    assert policy_allows(event, PURE_POLICY) is False
    assert summarize_violations([event], PURE_POLICY) == [event]


def test_granted_policy_allows_only_in_scope_open(tmp_path):
    scope = tmp_path / "scope"
    scope.mkdir()
    policy = policy_for_effects(["write_fs"], str(scope))
    inside = {"event": "open", "args": [str(scope / "f.txt")]}
    outside = {"event": "open", "args": [str(tmp_path / "f.txt")]}
    escape = {"event": "open",
              "args": [os.path.join(str(scope), "..", "f.txt")]}
    other = {"event": "os.system", "args": ["echo hi"]}
    assert policy_allows(inside, policy) is True
    assert policy_allows(outside, policy) is False
    assert policy_allows(escape, policy) is False
    assert policy_allows(other, policy) is False
    # read opens outside the scope are NOT allowed: strict in-scope-only
    assert summarize_violations([inside, outside, other], policy) == [outside, other]


def test_granted_namespace_open_end_to_end(tmp_path):
    scope = tmp_path / "scope"
    scope.mkdir()
    policy = policy_for_effects(["write_fs"], str(scope))
    ns = build_namespace(policy)
    target = scope / "cap.txt"
    exec('open("%s", "w").write("via policy")' % target, ns)
    assert target.read_text() == "via policy"
    with pytest.raises(PermissionError):
        exec('open("%s", "w")' % (tmp_path / "nope.txt"), build_namespace(policy))


def test_shim_policy_data_is_json_single_source_of_truth():
    data = shim_policy_data(PURE_POLICY)
    assert data["profile"] == "pure"
    assert data["grants"] == []
    assert data["safe_builtins"] == sorted(data["safe_builtins"])
    assert data["pure_modules"] == sorted(data["pure_modules"])
    assert data["audit_events"] == sorted(data["audit_events"])
    assert set(data.keys()) == {"profile", "grants", "safe_builtins",
                                "pure_modules", "audit_events"}
    json.dumps(data)  # must be JSON-serializable
    assert set(data["pure_modules"]) == set(PURE_MODULES)
    assert set(data["audit_events"]) == set(AUDIT_EVENTS)


def test_restricted_builtins_exclusions_and_inclusions():
    table = restricted_builtins(PURE_POLICY)
    for banned in ("open", "input", "eval", "exec", "compile", "breakpoint",
                   "help", "exit", "quit", "globals", "locals", "vars",
                   "memoryview"):
        assert banned not in table, banned
    assert "__build_class__" in table  # class statements need it
    # the guard is installed (a minimal-__globals__ guarded_import, not the
    # real __import__)
    assert table["__import__"].__name__ == "guarded_import"
    import builtins as _b2
    assert table["__import__"] is not _b2.__import__
    for exc in ("Exception", "ValueError", "KeyError", "RuntimeError",
                "TypeError", "StopIteration", "ArithmeticError"):
        assert exc in table, exc
    # every SAFE_BUILTINS name that exists on this interpreter resolves
    import builtins as _b
    for name in SAFE_BUILTINS:
        if name == "__import__":
            continue
        if hasattr(_b, name):
            assert name in table, name


def test_pure_modules_minimum_set():
    for mod in ("math", "random", "statistics", "itertools", "functools",
                "collections", "string", "re", "json", "decimal",
                "fractions", "datetime", "calendar", "typing", "numbers",
                "operator", "dataclasses", "enum", "abc"):
        assert mod in PURE_MODULES, mod
    for mod in ("os", "sys", "subprocess", "socket", "shutil", "pathlib",
                "urllib", "http", "importlib", "ctypes", "threading",
                "multiprocessing"):
        assert mod not in PURE_MODULES, mod


def test_build_namespace_shape():
    ns = build_namespace(PURE_POLICY)
    assert ns["__name__"] == "capability_module"
    assert isinstance(ns["__builtins__"], dict)


# ---------------------------------------------------------------------------
# 10: the TRUSTED profile -- fixed-procedure bytes only (W4-R1 Worker 4)
# ---------------------------------------------------------------------------

def test_trusted_policy_roundtrips():
    assert TRUSTED_POLICY.profile == TRUSTED_PROFILE
    assert TRUSTED_POLICY.grants == ()
    assert not TRUSTED_POLICY.is_pure()
    assert EffectPolicy.from_dict(TRUSTED_POLICY.to_dict()) == TRUSTED_POLICY
    data = shim_policy_data(TRUSTED_POLICY)
    assert data["profile"] == TRUSTED_PROFILE
    assert data["grants"] == []
    json.dumps(data)  # must be JSON-serializable
    # the shim reconstructs the same profile from the payload data
    assert EffectPolicy.from_dict(data).profile == TRUSTED_PROFILE


def test_trusted_namespace_carries_full_real_builtins():
    import builtins as _b
    ns = build_namespace(TRUSTED_POLICY)
    table = ns["__builtins__"]
    assert isinstance(table, dict)
    # the real effect surface is present: open, exec, eval, import
    assert table["open"] is _b.open
    assert table["exec"] is _b.exec
    assert table["eval"] is _b.eval
    # the guarded import is bypassed: real __import__
    assert table["__import__"] is _b.__import__


def test_trusted_exec_can_import_os_and_open_genuinely(tmp_path):
    """TRUSTED in-process: import os + open() genuinely work."""
    target = tmp_path / "trusted.txt"
    ns = build_namespace(TRUSTED_POLICY)
    exec(
        "import os\n"
        'open(%r, "w").write("trusted-ok")\n'
        "have_exec = callable(exec)\n" % str(target),
        ns,
    )
    assert target.read_text() == "trusted-ok"
    assert ns["have_exec"] is True


def test_pure_denies_the_same_trusted_code(tmp_path):
    """Contrast: the exact code TRUSTED allows is denied under PURE."""
    target = tmp_path / "pure.txt"
    ns = build_namespace(PURE_POLICY)
    with pytest.raises(ImportError):
        exec("import os\n", ns)
    with pytest.raises(NameError):
        exec('open(%r, "w")\n' % str(target), ns)
    with pytest.raises(NameError):
        exec("exec('1')\n", ns)
    assert not target.exists()


def test_trusted_violations_always_empty_even_when_effects_observed():
    events = [
        {"event": "open", "args": ["/tmp/x"]},
        {"event": "os.system", "args": ["touch /tmp/x"]},
    ]
    assert summarize_violations(events, TRUSTED_POLICY) == []
    # and the same events are violations under PURE (contrast)
    assert summarize_violations(events, PURE_POLICY) == events


def test_trusted_does_not_weaken_pure_or_granted_paths():
    # PURE is still fail-closed and GRANTED still scope-bound: the TRUSTED
    # branch in build_namespace/summarize_violations touches neither.
    table = restricted_builtins(PURE_POLICY)
    assert "open" not in table and "exec" not in table
    event = {"event": "open", "args": ["/tmp/s/file.txt"]}
    assert summarize_violations([event], PURE_POLICY) == [event]
    assert policy_allows(event, PURE_POLICY) is False
