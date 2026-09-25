"""
swarm_engine/acquisition/isolation.py

Process-level isolation for candidate code.

The in-process Sandbox (restricted builtins, import allowlist) stays: it is a
cheap first filter and it catches careless code. But it cannot contain code
that is actively hostile, because a restricted __builtins__ is a convention
inside the same interpreter, not a boundary. `while True: pass` hangs the
engine. A memory bomb takes the engine down with it.

This module adds the boundary underneath: the candidate runs in a separate
process with OS-enforced limits, and the parent survives whatever happens to
it. Limits are applied with setrlimit in the child before the candidate is
imported, so a candidate cannot raise its own ceiling.

What is enforced:
  CPU seconds      RLIMIT_CPU     - kills spin loops
  address space    RLIMIT_AS      - kills memory bombs
  file size        RLIMIT_FSIZE   - kills disk filling
  open files       RLIMIT_NOFILE  - limits fd exhaustion
  subprocesses     RLIMIT_NPROC   - blocks fork bombs where supported
  wall clock       parent-side timeout, then SIGKILL
  output size      parent-side cap on what is read back
  environment      scrubbed to a minimal allowlist
  working dir      a private temp directory, removed afterwards

Honest limits of this design are stated in `IsolationReport.caveats` rather
than left for the reader to discover: RLIMIT_AS is not available on every
platform, and there is no network namespace here, so network denial still
relies on the import allowlist and the governor rather than the kernel.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from swarm_engine.representation.json_codec import (
    canonical_json_dumps,
    from_tagged,
)
import textwrap
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class IsolationLimits:
    cpu_seconds: int = 2
    wall_clock_s: float = 5.0
    address_space_mb: int = 256
    file_size_mb: int = 8
    open_files: int = 64
    max_processes: int = 0          # 0 disables forking where supported
    max_output_bytes: int = 256_000

    def as_dict(self) -> Dict[str, Any]:
        return {
            "cpu_seconds": self.cpu_seconds, "wall_clock_s": self.wall_clock_s,
            "address_space_mb": self.address_space_mb,
            "file_size_mb": self.file_size_mb, "open_files": self.open_files,
            "max_processes": self.max_processes,
            "max_output_bytes": self.max_output_bytes,
        }


@dataclass
class IsolationReport:
    ok: bool
    value: Any = None
    error: str = ""
    exit_status: Optional[int] = None
    signal: Optional[int] = None
    timed_out: bool = False
    wall_ms: float = 0.0
    stdout_bytes: int = 0
    truncated: bool = False
    limits: Dict[str, Any] = field(default_factory=dict)
    caveats: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok, "error": self.error[:400],
            "exit_status": self.exit_status, "signal": self.signal,
            "timed_out": self.timed_out, "wall_ms": round(self.wall_ms, 2),
            "stdout_bytes": self.stdout_bytes, "truncated": self.truncated,
            "limits": self.limits, "caveats": self.caveats,
        }


# The child runner. Kept as source text rather than a module so the sandbox has
# no import path back into the engine: a candidate cannot reach swarm_engine
# even by name, because swarm_engine is not on the child's sys.path.
_CHILD_RUNNER = r'''
import json, os, resource, sys, signal, ast as _ast

def _apply_limits(limits):
    applied, skipped = [], []
    def _set(name, soft, hard=None):
        try:
            resource.setrlimit(getattr(resource, name), (soft, hard if hard is not None else soft))
            applied.append(name)
        except (ValueError, OSError, AttributeError) as exc:
            skipped.append("%s (%s)" % (name, exc.__class__.__name__))
    _set("RLIMIT_CPU", int(limits["cpu_seconds"]))
    _set("RLIMIT_FSIZE", int(limits["file_size_mb"]) * 1024 * 1024)
    _set("RLIMIT_NOFILE", int(limits["open_files"]))
    if int(limits["address_space_mb"]) > 0:
        _set("RLIMIT_AS", int(limits["address_space_mb"]) * 1024 * 1024)
    if int(limits["max_processes"]) >= 0:
        _set("RLIMIT_NPROC", int(limits["max_processes"]))
    _set("RLIMIT_CORE", 0)
    return applied, skipped

SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "bytes": bytes,
    "dict": dict, "divmod": divmod, "enumerate": enumerate, "filter": filter,
    "float": float, "format": format, "int": int, "isinstance": isinstance,
    "len": len, "list": list, "map": map, "max": max, "min": min, "next": next,
    "pow": pow, "range": range, "repr": repr, "reversed": reversed,
    "round": round, "set": set, "sorted": sorted, "str": str, "sum": sum,
    "tuple": tuple, "zip": zip, "iter": iter, "hash": hash, "chr": chr, "ord": ord,
    "ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
    "ArithmeticError": ArithmeticError, "Exception": Exception,
    "StopIteration": StopIteration, "AttributeError": AttributeError,
    "True": True, "False": False, "None": None,
}
ALLOWED_MODULES = {"math", "json", "re", "statistics", "datetime", "itertools",
                   "functools", "collections", "decimal", "fractions", "string",
                   "random", "bisect", "heapq", "textwrap", "unicodedata"}

def guarded_import(name, *a, **k):
    root = name.split(".")[0]
    if root not in ALLOWED_MODULES:
        raise ImportError("sandbox forbids importing %r" % name)
    return __import__(name, *a, **k)

def _tag_encode(x):
    """Tagged encoding matching swarm_engine.representation.json_codec.

    Embedded here because the child runs with -I -S and cannot import the
    engine package. Round-trips set/frozenset/bytes/bytearray/tuple/complex
    so the parent can compare candidate outputs exactly instead of via repr.
    """
    if isinstance(x, bool) or x is None or isinstance(x, (str, int, float)):
        return x
    if isinstance(x, set):
        return {"$set": [_tag_encode(v) for v in _sorted_tag(x)]}
    if isinstance(x, frozenset):
        return {"$frozenset": [_tag_encode(v) for v in _sorted_tag(x)]}
    if isinstance(x, bytes):
        return {"$bytes": x.hex()}
    if isinstance(x, bytearray):
        return {"$bytearray": bytes(x).hex()}
    if isinstance(x, tuple):
        return {"$tuple": [_tag_encode(v) for v in x]}
    if isinstance(x, complex):
        return {"$complex": [x.real, x.imag]}
    if isinstance(x, list):
        return [_tag_encode(v) for v in x]
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if isinstance(k, str):
                out[k] = _tag_encode(v)
            else:
                out["$key:%s:%r" % (type(k).__name__, k)] = _tag_encode(v)
        return out
    return {"$repr": repr(x)}

def _sorted_tag(xs):
    try:
        return sorted(xs)
    except TypeError:
        return sorted(xs, key=lambda v: (type(v).__name__, repr(v)))

def _decode_key(k):
    # Inverse of the $key:{type}:{repr} tagging in _tag_encode.
    if isinstance(k, str) and k.startswith("$key:"):
        rest = k[len("$key:"):]
        typename, _, rep = rest.partition(":")
        if typename in ("int", "float", "bool", "NoneType", "tuple",
                        "complex"):
            try:
                v = _ast.literal_eval(rep)
            except Exception:
                return k
            if v is None and typename == "NoneType":
                return None
            if type(v).__name__ == typename:
                return v
    return k

def _tag_decode(x):
    if isinstance(x, list):
        return [_tag_decode(v) for v in x]
    if isinstance(x, dict):
        keys = set(x.keys())
        if keys == {"$set"}:
            return set(_tag_decode(v) for v in x["$set"])
        if keys == {"$frozenset"}:
            return frozenset(_tag_decode(v) for v in x["$frozenset"])
        if keys == {"$bytes"}:
            return bytes.fromhex(x["$bytes"])
        if keys == {"$bytearray"}:
            return bytearray.fromhex(x["$bytearray"])
        if keys == {"$tuple"}:
            return tuple(_tag_decode(v) for v in x["$tuple"])
        if keys == {"$complex"}:
            r, i = x["$complex"]
            return complex(r, i)
        return {_decode_key(k): _tag_decode(v) for k, v in x.items()}
    return x

def main():
    payload = _tag_decode(json.loads(sys.stdin.read()))
    applied, skipped = _apply_limits(payload["limits"])

    def _run_one(code, entrypoint):
        # Each candidate executes in a FRESH namespace: one candidate's
        # globals, imports or accidental state cannot leak into another's.
        # (A candidate that hangs still consumes the batch's wall clock;
        # the parent detects the batch timeout and re-runs that batch's
        # members individually, so one hanging candidate cannot poison its
        # batch-mates' results.)
        ns = {"__builtins__": dict(SAFE_BUILTINS)}
        ns["__builtins__"]["__import__"] = guarded_import
        try:
            exec(compile(code, "<candidate>", "exec"), ns)
        except BaseException as exc:
            return {"ok": False, "phase": "load",
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        fn = ns.get(entrypoint)
        if not callable(fn):
            return {"ok": False, "phase": "load",
                    "error": "entrypoint %r is not callable" % entrypoint}
        results = []
        for call in payload["calls"]:
            try:
                value = fn(**call)
                # Tagged round-trip: the parent decodes back to the real
                # object so exact comparisons work for set/tuple/bytes etc.
                results.append({"ok": True, "value": _tag_encode(value)})
            except BaseException as exc:
                results.append({"ok": False,
                                "error": "%s: %s" % (type(exc).__name__, exc)})
        return {"ok": True, "results": results}

    if "items" in payload:
        # Batched screening: many candidates, one process spawn. The parent
        # still gets per-candidate results; isolation from the parent is
        # unchanged (separate process, same rlimits, scrubbed environment).
        batch = [_run_one(item["code"], item["entrypoint"])
                 for item in payload["items"]]
        print(json.dumps({"ok": True, "batch": batch,
                          "limits_applied": applied, "limits_skipped": skipped}))
        return

    outcome = _run_one(payload["code"], payload["entrypoint"])
    outcome.update({"limits_applied": applied, "limits_skipped": skipped})
    print(json.dumps(outcome))

main()
'''


class ProcessSandbox:
    """Runs candidate code in a separate, resource-limited process."""

    def __init__(self, limits: Optional[IsolationLimits] = None):
        self.limits = limits or IsolationLimits()

    @staticmethod
    def available() -> bool:
        """Process isolation needs setrlimit. Callers check rather than assume,
        because silently degrading to in-process execution would mean the
        engine reports isolation it does not have."""
        try:
            import resource  # noqa: F401
            return os.name == "posix"
        except ImportError:
            return False

    def run(self, code: str, entrypoint: str,
            calls: List[Dict[str, Any]]) -> IsolationReport:
        report = IsolationReport(ok=False, limits=self.limits.as_dict())
        if not self.available():
            report.error = "process isolation unavailable on this platform"
            report.caveats.append("falls back to in-process sandbox only")
            return report

        workdir = tempfile.mkdtemp(prefix="swarm_sbx_")
        runner_path = os.path.join(workdir, "_runner.py")
        with open(runner_path, "w") as fh:
            fh.write(textwrap.dedent(_CHILD_RUNNER))

        payload = canonical_json_dumps({
            "code": code, "entrypoint": entrypoint, "calls": calls,
            "limits": self.limits.as_dict(),
        })

        # A scrubbed environment: no inherited credentials, no PYTHONPATH back
        # into the engine, and an empty sys.path entry for the workdir only.
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": workdir,
               "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}

        started = time.time()
        proc = None
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-S", runner_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=workdir, env=env,
                start_new_session=True, text=True)
            try:
                stdout, stderr = proc.communicate(payload,
                                                  timeout=self.limits.wall_clock_s)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                report.timed_out = True
                report.error = (f"exceeded wall clock limit "
                                f"({self.limits.wall_clock_s}s); process killed")
                report.wall_ms = (time.time() - started) * 1000
                return report

            report.wall_ms = (time.time() - started) * 1000
            report.exit_status = proc.returncode
            if proc.returncode is not None and proc.returncode < 0:
                report.signal = -proc.returncode
                report.error = (f"terminated by signal {report.signal} "
                                f"(a resource limit was enforced)")
                return report

            report.stdout_bytes = len(stdout or "")
            if report.stdout_bytes > self.limits.max_output_bytes:
                report.truncated = True
                report.error = "output exceeded the size limit"
                return report

            if not stdout.strip():
                report.error = (f"child produced no result "
                                f"(exit {proc.returncode}): {(stderr or '')[:200]}")
                return report

            try:
                parsed = json.loads(stdout.strip().splitlines()[-1])
            except json.JSONDecodeError:
                report.error = f"unparseable child output: {stdout[:200]}"
                return report

            if parsed.get("limits_skipped"):
                report.caveats.append(
                    "limits not enforced on this platform: "
                    + ", ".join(parsed["limits_skipped"]))
            if not parsed.get("ok"):
                report.error = parsed.get("error", "candidate failed to load")
                return report

            report.ok = True
            report.value = [
                dict(r, value=from_tagged(r.get("value")))
                if isinstance(r, dict) and "value" in r else r
                for r in (parsed.get("results", []) or [])
            ]
            return report
        except Exception as exc:
            report.error = f"{type(exc).__name__}: {exc}"
            return report
        finally:
            if proc is not None and proc.poll() is None:
                self._kill(proc)
            shutil.rmtree(workdir, ignore_errors=True)

    def run_batch(self, items: List[Tuple[str, str]],
                  calls: List[Dict[str, Any]]) -> Optional[List["IsolationReport"]]:
        """Run several candidates against the same calls in ONE child process.

        Each candidate still executes in a fresh namespace inside the child,
        and the child is still a separate, resource-limited, scrubbed-
        environment process -- isolation from the parent is unchanged. What
        changes is only the per-candidate cost: one process spawn validates a
        whole batch instead of one spawn per candidate.

        `items` is a list of (code, entrypoint). Returns one IsolationReport
        per item, in order, or None if the batch exceeded the wall clock
        (the caller should then fall back to individual `run` calls for that
        batch, so one hanging candidate cannot poison its batch-mates'
        results).
        """
        reports = [IsolationReport(ok=False, limits=self.limits.as_dict())
                   for _ in items]
        if not items:
            return reports
        if not self.available():
            for report in reports:
                report.error = "process isolation unavailable on this platform"
                report.caveats.append("falls back to in-process sandbox only")
            return reports

        workdir = tempfile.mkdtemp(prefix="swarm_sbx_")
        runner_path = os.path.join(workdir, "_runner.py")
        with open(runner_path, "w") as fh:
            fh.write(textwrap.dedent(_CHILD_RUNNER))

        payload = canonical_json_dumps({
            "items": [{"code": code, "entrypoint": entrypoint}
                      for code, entrypoint in items],
            "calls": calls,
            "limits": self.limits.as_dict(),
        })

        # A scrubbed environment: no inherited credentials, no PYTHONPATH back
        # into the engine, and an empty sys.path entry for the workdir only.
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": workdir,
               "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}

        started = time.time()
        proc = None
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-S", runner_path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, cwd=workdir, env=env,
                start_new_session=True, text=True)
            try:
                stdout, stderr = proc.communicate(
                    payload, timeout=self.limits.wall_clock_s)
            except subprocess.TimeoutExpired:
                self._kill(proc)
                return None

            # Any batch-level failure -- child killed (e.g. one member spun
            # into the CPU limit and took the process down), no output, or
            # unparsable output -- falls back to individual runs, exactly as
            # a wall-clock timeout does. A batch-mate must never be failed
            # for another candidate's behaviour.
            if proc.returncode != 0 or not stdout.strip():
                return None

            try:
                parsed = json.loads(stdout.strip().splitlines()[-1])
            except json.JSONDecodeError:
                return None

            if (not parsed.get("ok") or "batch" not in parsed
                    or len(parsed.get("batch") or []) != len(items)):
                return None

            item_results = parsed.get("batch") or []
            if parsed.get("limits_skipped"):
                caveat = ("limits not enforced on this platform: "
                          + ", ".join(parsed["limits_skipped"]))
            else:
                caveat = ""
            for report, item in zip(reports, item_results):
                report.wall_ms = (time.time() - started) * 1000
                report.exit_status = proc.returncode
                if caveat:
                    report.caveats.append(caveat)
                if not item.get("ok"):
                    report.error = item.get("error", "candidate failed to load")
                    continue
                report.ok = True
                report.value = [
                    dict(r, value=from_tagged(r.get("value")))
                    if isinstance(r, dict) and "value" in r else r
                    for r in (item.get("results", []) or [])
                ]
            return reports
        except Exception as exc:
            for report in reports:
                if not report.error:
                    report.error = f"{type(exc).__name__}: {exc}"
            return reports
        finally:
            if proc is not None and proc.poll() is None:
                self._kill(proc)
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _kill(proc) -> None:
        """Kill the whole process group: a candidate that spawned children
        should not outlive the parent's decision to stop it."""
        import signal as _signal
        try:
            os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
