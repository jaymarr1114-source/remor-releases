"""W4-R1: capability-sandbox integration tests for subprocess_runner.

Every test runs REAL subprocesses: the candidate code genuinely executes,
genuinely attempts the OS effect, and is genuinely denied (Layer 1:
capability absence) or genuinely observed (Layer 2: sys.addaudithook).
Nothing is mocked.
"""
import os as _os
import sys as _sys
import tempfile as _tempfile

_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                  "..", "..", "pylib"))

from swarm_engine.agent_org.subprocess_runner import run_code, RunReport
from swarm_engine.capability.effect_sandbox import (
    EffectPolicy, PURE_POLICY, TRUSTED_POLICY, TRUSTED_PROFILE)

PASS = []


def check(name, cond, detail=""):
    assert cond, f"FAILED: {name} {detail}"
    PASS.append(name)


_TMP = _tempfile.gettempdir()


def _target(name):
    return _os.path.join(_TMP, f"w4r1_t_{name}_{_os.getpid()}")


def _clean(*paths):
    for p in paths:
        try:
            _os.remove(p)
        except OSError:
            pass


# -- 1. PURE default: the exact W4-R1 bypass spelling -------------------------
t1 = _target("bypass")
_clean(t1)
code = ('__builtins__["op" + "en"](%r, "w").write("pwned")\n'
        'def capability():\n'
        '    return 1\n' % t1)
r = run_code(code, "capability", [{}])
check("w4r1.bypass-run-fails", r.ok is False, r.error[-120:])
check("w4r1.bypass-error-names-denial", "open" in r.error, r.error[-120:])
check("w4r1.bypass-file-absent", not _os.path.exists(t1))

# Same bypass inside the entrypoint: denied at call time (case-level).
t1b = _target("bypass_fn")
_clean(t1b)
code = ('def capability():\n'
        '    __builtins__["op" + "en"](%r, "w").write("pwned")\n'
        '    return 1\n' % t1b)
r = run_code(code, "capability", [{}])
check("w4r1.bypass-fn-case-fails",
      r.ok is True and r.value and r.value[0]["ok"] is False,
      str(r.value))
check("w4r1.bypass-fn-error-names-denial", "open" in r.value[0]["error"],
      r.value[0]["error"])
check("w4r1.bypass-fn-file-absent", not _os.path.exists(t1b))

# -- 2. PURE default: plain open() -------------------------------------------
t2 = _target("plain")
_clean(t2)
code = ('open(%r, "w").write("x")\n'
        'def capability():\n'
        '    return 1\n' % t2)
r = run_code(code, "capability", [{}])
check("w4r1.plain-open-fails", r.ok is False, r.error[-120:])
check("w4r1.plain-open-error-names-denial", "open" in r.error,
      r.error[-120:])
check("w4r1.plain-open-file-absent", not _os.path.exists(t2))

# -- 3. PURE default: import guard -------------------------------------------
r = run_code('import os\ndef capability():\n    return 1\n',
             "capability", [{}])
check("w4r1.import-os-denied", r.ok is False, r.error[-160:])
check("w4r1.import-os-guard-message",
      "effect capability not granted" in r.error, r.error[-160:])

r = run_code('import math, json, statistics\n'
             'def capability(x):\n'
             '    return math.floor(x) + len(json.dumps('
             '{"m": statistics.mean([x])}))\n',
             "capability", [{"x": 4.7}])
check("w4r1.pure-imports-succeed", r.ok is True, r.error)
check("w4r1.pure-imports-value",
      r.value and r.value[0]["ok"] and r.value[0]["value"] == 14,
      str(r.value))
check("w4r1.pure-imports-no-audit-noise", r.observed_effects == [],
      str(r.observed_effects))

# -- 4. Granted policy --------------------------------------------------------
scope = _tempfile.mkdtemp(prefix="w4r1_grant_")
outside = _tempfile.mkdtemp(prefix="w4r1_outside_")
in_target = _os.path.join(scope, "in_scope.txt")
out_target = _os.path.join(outside, "out_scope.txt")
policy = EffectPolicy("granted", ("fs.write:" + scope,))

code = ('def capability():\n'
        '    f = open(%r, "w")\n'
        '    f.write("granted-ok")\n'
        '    f.close()\n'
        '    return "wrote"\n' % in_target)
r = run_code(code, "capability", [{}], effect_policy=policy)
check("w4r1.granted-in-scope-succeeds", r.ok is True, r.error)
check("w4r1.granted-in-scope-value",
      r.value and r.value[0]["ok"] and r.value[0]["value"] == "wrote",
      str(r.value))
check("w4r1.granted-in-scope-file-exists", _os.path.exists(in_target))
with open(in_target) as fh:
    check("w4r1.granted-in-scope-content", fh.read() == "granted-ok")
check("w4r1.granted-in-scope-audit-event",
      any(e.get("event") == "open" and e.get("args")
          and in_target in e["args"][0]
          for e in r.observed_effects),
      str(r.observed_effects))
check("w4r1.granted-in-scope-no-violations", r.effect_violations == [],
      str(r.effect_violations))

code = ('open(%r, "w").write("x")\n'
        'def capability():\n'
        '    return 1\n' % out_target)
r = run_code(code, "capability", [{}], effect_policy=policy)
check("w4r1.granted-outside-scope-fails", r.ok is False, r.error[-160:])
check("w4r1.granted-outside-scope-message",
      "outside granted scope" in r.error, r.error[-160:])
check("w4r1.granted-outside-scope-file-absent",
      not _os.path.exists(out_target))

# -- 5. observed_effects is a real list ---------------------------------------
r = run_code("def capability(x):\n    return x * x\n",
             "capability", [{"x": 6}])
check("w4r1.observed-is-list", isinstance(r.observed_effects, list))
check("w4r1.policy-echoed",
      isinstance(r.effect_policy, dict)
      and r.effect_policy.get("profile") == "pure", str(r.effect_policy))
check("w4r1.shim-keys-present",
      isinstance(r.effect_violations, list)
      and isinstance(r.fs_diff, dict)
      and set(r.fs_diff) == {"added", "modified", "removed"},
      str(r.fs_diff))

code = ('__builtins__["op" + "en"](%r, "w")\n'
        'def capability():\n'
        '    return 1\n' % _target("bypass_obs"))
r = run_code(code, "capability", [{}])
check("w4r1.bypass-observed-empty", r.observed_effects == [],
      str(r.observed_effects))

# -- 6. Effectless pure computation -------------------------------------------
r = run_code("def capability(x):\n    return x * x\n",
             "capability", [{"x": 7}])
check("w4r1.pure-computation-ok", r.ok is True, r.error)
check("w4r1.pure-computation-value",
      r.value and r.value[0]["ok"] and r.value[0]["value"] == 49,
      str(r.value))

# -- Layer 2: a REAL attempted effect under PURE is prevented BEFORE it ----
# -- executes, and is a hard trust failure ----------------------------------
# Residual repair (2026-09-27): the module-attribute escape
# (statistics.sys.modules["os"]) no longer lands its effect. The audit hook
# records the os.system attempt and raises _EffectDenied, aborting the C
# call before the OS action executes -- prevention, not post-hoc
# observation. The marker must be ABSENT.
marker = _target("pure_obs")
_clean(marker)
code = ('import statistics\n'
        'def capability():\n'
        '    statistics.sys.modules["os"].system("touch %s")\n'
        '    return 1\n' % marker)
r = run_code(code, "capability", [{}])
check("w4r1.observed-effect-fails-hard", r.ok is False, r.error[:200])
check("w4r1.observed-effect-named",
      r.error.startswith("HARD TRUST FAILURE (W4-R1)"), r.error[:200])
check("w4r1.observed-effect-prevented", not _os.path.exists(marker),
      "the touch must be prevented before the OS action executes")
check("w4r1.observed-effect-recorded",
      any(e.get("event") == "os.system" for e in r.observed_effects),
      str(r.observed_effects))
check("w4r1.observed-effect-violation",
      any(e.get("event") == "os.system" for e in r.effect_violations),
      str(r.effect_violations))
_clean(marker)

# -- Residual-repair regression: equivalent module-attribute paths ----------
# At least two more paths reaching os without the import guard must also be
# prevented before the OS action executes (mission objective 2026-09-27).
for _esc_name, _esc_expr in (
    ("random._os", 'random._os.system("touch %s")'),
    ("collections._sys", 'collections._sys.modules["os"].system("touch %s")'),
    ("fractions.sys", 'fractions.sys.modules["os"].system("touch %s")'),
):
    _m = _target("esc_" + _esc_name.replace(".", "_"))
    _clean(_m)
    _mod = _esc_name.split(".")[0]
    _code = ('import %s\n'
             'def capability():\n'
             '    %s\n'
             '    return 1\n' % (_mod, _esc_expr % _m))
    _r = run_code(_code, "capability", [{}])
    check("w4r1.escape-prevented-%s" % _esc_name, not _os.path.exists(_m),
          "marker created via %s: %s" % (_esc_name, _r.error[:150]))
    check("w4r1.escape-fails-hard-%s" % _esc_name,
          _r.ok is False and "HARD TRUST FAILURE" in _r.error,
          _r.error[:150])
    check("w4r1.escape-recorded-%s" % _esc_name,
          any(e.get("event") == "os.system" for e in _r.observed_effects),
          str(_r.observed_effects))
    _clean(_m)

# -- Residual-repair regression: silent (non-audited) process replacement --
# os.execv fires NO audit event; neuter_os_effects() replaces it with a
# raising stub, so the process image is never swapped.
_m2 = _target("esc_execv")
_code2 = ('import statistics\n'
          'def capability():\n'
          '    statistics.sys.modules["os"].execv("/bin/true", ["/bin/true"])\n'
          '    return 1\n')
_r2 = run_code(_code2, "capability", [{}])
check("w4r1.execv-prevented",
      _r2.ok is False and "HARD TRUST FAILURE" in _r2.error,
      _r2.error[:200])
check("w4r1.execv-blocked-recorded",
      any(e.get("event") == "effect.blocked" for e in _r2.observed_effects),
      str(_r2.observed_effects))

# -- Residual-repair regression: function-__globals__ import escape --------
# The import guard is exec'd with minimal __globals__ and installed
# globally, so __builtins__["__import__"].__globals__ no longer exposes
# _real_import/_os/_builtins.
_m3 = _target("esc_fnglob")
_clean(_m3)
_code3 = ('__builtins__["__import__"].__globals__.get('
          '"_real_import", lambda *a, **k: 1/0)("os").system("touch %s")\n'
          'def capability():\n'
          '    return 1\n' % _m3)
_r3 = run_code(_code3, "capability", [{}])
check("w4r1.fnglobals-no-marker", not _os.path.exists(_m3),
      "marker created via __globals__ escape: %s" % _r3.error[:150])
check("w4r1.fnglobals-fails", _r3.ok is False, _r3.error[:150])
_clean(_m3)

# -- Backward compatibility: the 3-tuple interface ---------------------------
r = run_code("def capability():\n    return 1\n", "capability", [{}])
ok, value, error = r
check("w4r1.report-unpacks-3", (ok, value, error) == (r.ok, r.value, r.error))
check("w4r1.report-len-3", len(r) == 3)
check("w4r1.report-getitem", r[0] is r.ok and r[2] == r.error)
check("w4r1.default-policy-is-pure",
      run_code("def capability():\n    return 1\n",
               "capability", [{}]).effect_policy.get("profile") == "pure")
check("w4r1.explicit-pure-policy",
      run_code("def capability():\n    return 1\n", "capability", [{}],
               effect_policy=PURE_POLICY).ok is True)

# -- 7. TRUSTED profile: fixed-procedure bytes get full authority ----------
# The W4-R1 repair-loop fallout: reviewed in-tree procedure (technique
# files) legitimately needs exec/import/open. TRUSTED grants the full
# real builtins + real __import__; the audit hook still records (the
# profile never fails). Candidate bytes must NEVER run here.
t7 = _target("trusted")
_clean(t7)
code = ('import os\n'
        'open(%r, "w").write("trusted-ok")\n'
        'def capability():\n'
        '    return "ran-trusted"\n' % t7)
r = run_code(code, "capability", [{}], effect_policy=TRUSTED_POLICY)
check("w4r1.trusted-import-os-succeeds", r.ok is True, r.error)
check("w4r1.trusted-value",
      r.value and r.value[0]["ok"] and r.value[0]["value"] == "ran-trusted",
      str(r.value))
check("w4r1.trusted-open-genuinely-wrote", _os.path.exists(t7),
      "the open() must genuinely execute for this to be an honest test")
with open(t7) as fh:
    check("w4r1.trusted-open-content", fh.read() == "trusted-ok")
check("w4r1.trusted-profile-echoed",
      r.effect_policy.get("profile") == TRUSTED_PROFILE,
      str(r.effect_policy))
check("w4r1.trusted-never-fails",
      r.effect_violations == [], str(r.effect_violations))
check("w4r1.trusted-still-observed",
      any(e.get("event") == "open" for e in r.observed_effects),
      str(r.observed_effects[-2:]))

# Contrast: the same code under PURE (default) is denied.
_clean(t7)
r = run_code(code, "capability", [{}])
check("w4r1.trusted-code-fails-under-pure", r.ok is False, r.error[-120:])
check("w4r1.trusted-code-file-absent-under-pure", not _os.path.exists(t7))
check("w4r1.trusted-code-pure-profile",
      r.effect_policy.get("profile") == "pure", str(r.effect_policy))

# TRUSTED grants the obfuscated spelling too: full authority, by design.
t7b = _target("trusted_obf")
_clean(t7b)
code = ('__builtins__["op" + "en"](%r, "w").write("obf")\n'
        'def capability():\n'
        '    return "obf-ok"\n' % t7b)
r = run_code(code, "capability", [{}], effect_policy=TRUSTED_POLICY)
check("w4r1.trusted-full-authority-by-design",
      r.ok is True and r.effect_violations == [], r.error[-120:])
check("w4r1.trusted-obf-wrote", _os.path.exists(t7b))
check("w4r1.trusted-inner-exec-available",
      run_code('def capability():\n'
               '    ns = {}\n'
               '    exec("x = 40 + 2", {"__builtins__": {}}, ns)\n'
               '    return ns["x"]\n',
               "capability", [{}],
               effect_policy=TRUSTED_POLICY).value[0]["value"] == 42,
      "the repair techniques need the exec builtin for their own "
      "empty-builtins inner sandbox")

# -- cleanup ------------------------------------------------------------------
_clean(t1, t1b, t2)
import shutil as _shutil
_shutil.rmtree(scope, ignore_errors=True)
_shutil.rmtree(outside, ignore_errors=True)

print(f"ALL {len(PASS)} W4-R1 RUNNER CHECKS PASSED")
