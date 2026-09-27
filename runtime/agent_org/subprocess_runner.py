"""Isolated subprocess code runner for the Tester protocol.

run_code(code, entrypoint, cases, effect_policy=None) executes the code in
a fresh `python3` subprocess (30s timeout, temp dir, JSON over stdio). The
entrypoint is called as entrypoint(**case_args) for each case.

W4-R1 (2026-09-27): the child no longer execs candidate code with full
builtins. The capability sandbox from
``swarm_engine.capability.effect_sandbox`` is the authority:

* ``effect_policy=None`` (default) means PURE: zero granted effect
  capabilities. Every existing 3-arg caller gets PURE automatically.
* The policy travels to the child as JSON data
  (``shim_policy_data``) -- never as code. The child reconstructs it via
  ``EffectPolicy.from_dict`` and enforces it with restricted builtins
  (``open`` simply absent under PURE -- no spelling of the name, however
  obfuscated, can reach one), a guarded ``__import__`` (PURE_MODULES
  allowlist only), and a real ``sys.addaudithook`` observer installed
  BEFORE candidate exec (it cannot be uninstalled).
* Pure modules are pre-imported before the hook so legitimate imports hit
  ``sys.modules`` and never fire import-machinery ``open`` audit events.
* A temp-dir file snapshot before/after catches effects that bypass the
  audit hook (belt and suspenders); under a PURE profile any new/modified
  file is itself a violation.
* An observed effect the policy does not allow is a HARD TRUST FAILURE:
  fatal, explicit, auditable -- never misreported as a behavioural failure.

Returns RunReport(ok, value, error):
- ok=True, value=[{"ok": bool, "value": <JSON>, "error": str}, ...]
- ok=False, value=[], error=<driver-visible reason>

The report object exposes .ok/.value/.error attributes because the real
Tester implementation (swarm_engine.verification.independent) reads those
attributes; it also supports tuple unpacking for convenience.
The report additionally carries .observed_effects (the real audit-hook
record), .effect_policy (the enforced policy as a dict), .effect_violations
and .fs_diff -- while __iter__/__len__/__getitem__ stay the 3-tuple
(ok, value, error) for backward compatibility with Tester and unpackers.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from swarm_engine.capability.effect_sandbox import (
    EffectPolicy,
    PURE_POLICY,
    shim_policy_data,
)

TIMEOUT_S = 30


def _review_subprocess_env() -> Dict[str, str]:
    """Environment for review subprocesses.

    Guarantees the child can ``import swarm_engine``: the dispatch-evidence
    re-execution harness (and any review-procedure code) does
    ``from swarm_engine... import ...``, but a parent's ``sys.path``
    tweaks do not cross the subprocess boundary -- only the environment
    does. The path is derived from this module's own file
    (``swarm_engine`` is a namespace package -- ``__file__`` is None -- so
    the package file cannot be used), guaranteeing the child imports the
    exact tree the parent is running; a driver-set PYTHONPATH is preserved
    after it, not replaced.

    This grants the child no new authority and weakens no check: the
    isolation boundary is the subprocess itself (temp cwd, stdio-only,
    timeout), and the review-procedure code it executes is fixed, never
    agent input. Without this, every dispatch-evidence verdict fails
    closed for an environmental reason ("No module named 'swarm_engine'")
    that the verdict reasons then misreport as a behavioural failure.
    """
    env = dict(os.environ)
    try:
        # This module lives at <container>/swarm_engine/agent_org/; the
        # container dir is what the child needs on PYTHONPATH.
        here = os.path.abspath(__file__)
        container = os.path.dirname(os.path.dirname(
            os.path.dirname(here)))
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (container + os.pathsep + prev
                             if prev else container)
    except Exception:
        pass
    return env


@dataclass
class RunReport:
    ok: bool
    value: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""
    observed_effects: List[Dict[str, Any]] = field(default_factory=list)
    effect_policy: Dict[str, Any] = field(default_factory=dict)
    effect_violations: List[Dict[str, Any]] = field(default_factory=list)
    fs_diff: Dict[str, Any] = field(default_factory=dict)

    def __iter__(self):
        return iter((self.ok, self.value, self.error))

    def __len__(self):
        return 3

    def __getitem__(self, index):
        return (self.ok, self.value, self.error)[index]


_SHIM = textwrap.dedent('''
    import json, os, sys

    # W4-R1 capability sandbox: imported once at shim top level so the
    # module-level helpers below can use it. The policy itself still
    # travels as JSON data in the payload, never as code. If the import
    # fails, _main() fails closed and never runs the candidate.
    try:
        from swarm_engine.capability.effect_sandbox import (
            EffectPolicy, PURE_MODULES, TRUSTED_PROFILE,
            _SilentEffectDenied,
            build_guarded_import, build_namespace,
            install_audit_hook, neuter_os_effects, summarize_violations)
        _SANDBOX_OK = True
        _SANDBOX_ERR = ""
    except ImportError as exc:
        _SANDBOX_OK = False
        _SANDBOX_ERR = str(exc)

    def _snapshot_fs(root):
        snap = {}
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, root)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                snap[rel] = [st.st_size, st.st_mtime_ns]
        return snap

    def _diff_fs(before, after):
        return {
            "added": sorted(k for k in after if k not in before),
            "modified": sorted(k for k in after
                               if k in before and after[k] != before[k]),
            "removed": sorted(k for k in before if k not in after),
        }

    def _summarize(v):
        return "%s(%s)" % (v.get("event"),
                           ", ".join(repr(a)[:80] for a in v.get("args", [])))

    def _fmt_exc(limit=3):
        # Traceback text WITHOUT linecache source-line reads: the stdlib
        # traceback formatter opens the source files, which would fire
        # 'open' audit events from the shim itself (observer noise).
        etype, exc, tb = sys.exc_info()
        frames = []
        while tb is not None and len(frames) < limit:
            frames.append(tb)
            tb = tb.tb_next
        lines = ["Traceback (most recent call last):"]
        for fr in frames:
            code = fr.tb_frame.f_code
            lines.append('  File "%s", line %d, in %s'
                         % (code.co_filename, fr.tb_lineno, code.co_name))
        lines.append("%s: %s" % (etype.__name__, exc))
        return "\\n".join(lines)

    def _collect_violations(observed, policy, fs_before, tmp_root):
        # Audit-hook events the policy does not allow, plus -- under a PURE
        # profile -- any new/modified file in the temp dir (belt and
        # suspenders: catches effects that never fire an audit event).
        violations = list(summarize_violations(observed, policy))
        diff = _diff_fs(fs_before, _snapshot_fs(tmp_root))
        if policy.is_pure():
            for rel in diff["added"]:
                violations.append({"event": "fs.new_file", "args": [rel]})
            for rel in diff["modified"]:
                violations.append({"event": "fs.modified_file", "args": [rel]})
        return violations, diff

    def _fail_trust(observed, policy, fs_before, tmp_root):
        violations, diff = _collect_violations(
            observed, policy, fs_before, tmp_root)
        if not violations:
            return False
        first3 = "; ".join(_summarize(v) for v in violations[:3])
        print(json.dumps({
            "fatal": ("HARD TRUST FAILURE (W4-R1): observed %d effect(s) "
                      "under %s execution profile: %s"
                      % (len(violations), policy.profile.upper(), first3)),
            "observed_effects": observed,
            "effect_violations": violations,
            "effect_policy": policy.to_dict(),
            "fs_diff": diff,
        }))
        return True

    def _record_blocked(observed, exc):
        # A neutered (non-audited) effect function raised: the audit hook
        # never saw this call, so record a synthetic event -- a prevented
        # silent effect is an auditable hard-trust-failure, not just a
        # case error. (The hook's own _EffectDenied needs no synthetic
        # event: the real event was recorded before it raised.)
        if isinstance(exc, _SilentEffectDenied):
            observed.append({"event": "effect.blocked",
                             "args": [str(exc)[:200]]})

    def _main():
        with open(sys.argv[1], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        code_path = payload["code_path"]
        entrypoint = payload["entrypoint"]
        cases = payload["cases"]

        # --- W4-R1: capability sandbox. The policy travels as JSON data,
        # --- never as code. Fail closed if the sandbox cannot be installed.
        if not _SANDBOX_OK:
            print(json.dumps(
                {"fatal": "sandbox unavailable: fail closed (%s)"
                          % _SANDBOX_ERR}))
            return
        policy = EffectPolicy.from_dict(payload.get("effect_policy") or {})

        with open(code_path, "r", encoding="utf-8") as fh:
            code_text = fh.read()
        try:
            compiled = compile(code_text, code_path, "exec")
        except SyntaxError as exc:
            print(json.dumps({"fatal": "syntax: %s" % exc}))
            return

        # Pre-import pure modules BEFORE installing the audit hook, so
        # legitimate imports hit sys.modules and never fire
        # import-machinery open audit events (no false positives).
        # collections.abc is pre-imported too: several pure modules
        # (collections, enum, ...) resolve ABCs through it at call time.
        for _mod in sorted(PURE_MODULES) + ["collections.abc"]:
            try:
                __import__(_mod)
            except Exception:
                pass

        # W4-R1 residual repair (2026-09-27): harden the child execution
        # environment. Skipped ONLY under TRUSTED -- the full-authority
        # fixed-procedure profile, where the code bytes are tree-fixed
        # reviewed procedure by the inviolable rule (effect_sandbox).
        _guard = build_guarded_import()
        if policy.profile != TRUSTED_PROFILE:
            # (1) Install the import guard GLOBALLY on builtins: a
            # module-attribute reach such as statistics.sys cannot smuggle
            # in fresh effectful modules, and imports inside function
            # bodies consult the same allowlist snapshot as top-level
            # imports (previously only the candidate namespace's
            # __import__ was guarded; builtins.__import__ was the real
            # one, reachable via function __globals__).
            import builtins as _builtins_shim
            _builtins_shim.__import__ = _guard
            # (2) Neuter effectful os/posix functions that fire NO audit
            # event (exec/spawn/fork, process suicide, signals, raw fd
            # plumbing, _posixsubprocess.fork_exec): the audit hook cannot
            # prevent what it cannot observe, so these raise on any call.
            neuter_os_effects()
            # (3) Drop the sandbox builder's own modules from sys.modules:
            # the guard and stubs are exec'd with minimal __globals__, but
            # the builder module's globals contain _real_import (full
            # import power), _real_open (unscoped open) and _os -- a
            # statistics.sys.modules["swarm_engine..."] reach would hand
            # them to candidate code. Unreachable once del'd (the guard
            # blocks re-import; the names the shim needs are bound).
            for _m in [m for m in sys.modules
                       if m == "swarm_engine"
                       or m.startswith("swarm_engine.")]:
                del sys.modules[_m]

        # Snapshot the temp dir before candidate execution.
        tmp_root = os.path.dirname(os.path.abspath(code_path))
        fs_before = _snapshot_fs(tmp_root)

        # The observer goes in immediately before candidate exec. The
        # shim's own file reads all happened above. CPython provides no
        # API to uninstall an audit hook. Under every policy except
        # TRUSTED, a policy-denied observed event is recorded and then
        # deliberately raised as _EffectDenied -- the audited C call is
        # aborted BEFORE the OS action executes (prevention, not
        # post-hoc observation).
        observed = []
        install_audit_hook(observed, policy)

        namespace = build_namespace(policy, _guard)
        # W4-R1 Worker 4: build_namespace honors the policy profile. Under
        # TRUSTED the namespace carries the full real builtins (real
        # __import__; the guarded import is bypassed) -- reserved for fixed,
        # reviewed procedure bytes only (inviolable rule, effect_sandbox).
        # The audit hook installed above still records every real effect;
        # summarize_violations returns [] under TRUSTED (records-only).
        namespace["__name__"] = "candidate_module"
        try:
            exec(compiled, namespace)
        except Exception:
            _record_blocked(observed, sys.exc_info()[1])
            if _fail_trust(observed, policy, fs_before, tmp_root):
                return
            print(json.dumps({
                "fatal": "import: %s" % _fmt_exc(3),
                "observed_effects": observed,
                "effect_violations": [],
                "effect_policy": policy.to_dict(),
                "fs_diff": _diff_fs(fs_before, _snapshot_fs(tmp_root)),
            }))
            return
        fn = namespace.get(entrypoint)
        if not callable(fn):
            print(json.dumps({"fatal": "no callable entrypoint %r" % entrypoint}))
            return
        results = []
        for case_args in cases:
            try:
                value = fn(**case_args)
            except Exception as exc:
                _record_blocked(observed, exc)
                results.append({"ok": False, "value": None,
                                "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError) as exc:
                results.append({"ok": False, "value": None,
                                "error": "non-JSON-serializable return: %s" % exc})
                continue
            results.append({"ok": True, "value": value, "error": ""})
        if _fail_trust(observed, policy, fs_before, tmp_root):
            return
        print(json.dumps({
            "results": results,
            "observed_effects": observed,
            "effect_violations": [],
            "effect_policy": policy.to_dict(),
            "fs_diff": _diff_fs(fs_before, _snapshot_fs(tmp_root)),
        }))

    _main()
''')


def _case_args(case: Any) -> Dict[str, Any]:
    if hasattr(case, "args"):
        return dict(case.args)
    if isinstance(case, dict):
        return dict(case)
    raise TypeError(f"case must be a Case or dict, got {type(case).__name__}")


def _as_list(value: Any) -> List[Dict[str, Any]]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def run_code(code: str, entrypoint: str,
             cases: Sequence[Any],
             effect_policy: Optional[EffectPolicy] = None) -> RunReport:
    """Run code under isolation. Matches the Tester protocol.

    effect_policy: an EffectPolicy from
    swarm_engine.capability.effect_sandbox. None (the default) means PURE:
    zero granted effect capabilities -- all existing 3-arg callers get PURE
    automatically. Pass TRUSTED_POLICY explicitly for fixed, reviewed
    procedure bytes only (inviolable rule: never for synthesized/admitted
    candidate bytes -- each such call site must carry a justification
    comment). The policy is serialized into the child payload as JSON
    data; the child reconstructs it and enforces it. An observed effect the
    policy does not allow is a HARD TRUST FAILURE (fatal with an explicit
    auditable reason), never a behavioural failure.
    """
    policy = PURE_POLICY if effect_policy is None else effect_policy
    if not isinstance(policy, EffectPolicy):
        raise TypeError(
            "effect_policy must be an EffectPolicy, got "
            f"{type(policy).__name__}")
    arg_list = [_case_args(c) for c in cases]
    with tempfile.TemporaryDirectory(prefix="ao_run_") as tmp:
        code_path = os.path.join(tmp, "candidate_module.py")
        shim_path = os.path.join(tmp, "shim.py")
        payload_path = os.path.join(tmp, "payload.json")
        with open(code_path, "w", encoding="utf-8") as fh:
            fh.write(code)
        with open(shim_path, "w", encoding="utf-8") as fh:
            fh.write(_SHIM)
        with open(payload_path, "w", encoding="utf-8") as fh:
            json.dump({"code_path": code_path, "entrypoint": entrypoint,
                       "cases": arg_list,
                       "effect_policy": shim_policy_data(policy)}, fh)
        try:
            proc = subprocess.run(
                [sys.executable, shim_path, payload_path],
                capture_output=True, text=True, timeout=TIMEOUT_S,
                cwd=tmp, env=_review_subprocess_env())
        except subprocess.TimeoutExpired:
            return RunReport(False, [], "subprocess timed out "
                             f"after {TIMEOUT_S}s (killed)")
        except Exception as exc:
            return RunReport(False, [],
                             f"could not launch subprocess: {exc}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        return RunReport(False, [],
                         f"subprocess exited {proc.returncode}: {tail}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return RunReport(False, [],
                         f"shim produced non-JSON stdout: {exc}")
    if "fatal" in out:
        # Fatal carries the audit evidence too: a HARD TRUST FAILURE must
        # stay auditable, not just a one-line reason.
        return RunReport(False, [], out["fatal"],
                         observed_effects=_as_list(out.get("observed_effects")),
                         effect_policy=_as_dict(out.get("effect_policy")),
                         effect_violations=_as_list(
                             out.get("effect_violations")),
                         fs_diff=_as_dict(out.get("fs_diff")))
    results = out.get("results")
    if not isinstance(results, list) or len(results) != len(arg_list):
        return RunReport(False, [],
                         "shim returned malformed results")
    return RunReport(True, results, "",
                     observed_effects=_as_list(out.get("observed_effects")),
                     effect_policy=_as_dict(out.get("effect_policy")),
                     effect_violations=_as_list(out.get("effect_violations")),
                     fs_diff=_as_dict(out.get("fs_diff")))
