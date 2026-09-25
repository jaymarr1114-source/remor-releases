"""
swarm_engine/project/commands.py

Sandboxed execution of build/test/tool commands within a project.

This reuses the resource-limit and process-group-kill pattern already proven
in ProcessSandbox — CPU/wall-clock/memory limits, output caps, a scrubbed
environment, group-kill on timeout — applied to arbitrary argv instead of a
single Python candidate. A build or test command is not a sandboxed
candidate under evaluation; it is trusted project code the operator asked to
run. The isolation here is about *containment* rather than *distrust*.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class CommandLimits:
    wall_clock_s: float = 30.0
    cpu_seconds: int = 25
    address_space_mb: int = 1024
    max_output_bytes: int = 500_000

    def as_dict(self) -> Dict[str, Any]:
        return {"wall_clock_s": self.wall_clock_s, "cpu_seconds": self.cpu_seconds,
                "address_space_mb": self.address_space_mb,
                "max_output_bytes": self.max_output_bytes}


@dataclass
class CommandResult:
    ok: bool
    exit_code: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    wall_ms: float = 0.0
    command: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "exit_code": self.exit_code,
                "stdout": self.stdout[-4000:], "stderr": self.stderr[-4000:],
                "timed_out": self.timed_out, "truncated": self.truncated,
                "wall_ms": round(self.wall_ms, 2), "command": self.command}


_DENYLIST = {"rm", "dd", "mkfs", "shutdown", "reboot", "sudo", "su",
            "chmod", "chown", "curl", "wget", "nc", "ssh", "scp"}


class CommandDenied(Exception):
    pass


class CommandRunner:
    """Runs a project's own build/test/tool commands under resource limits."""

    def __init__(self, project_root: str, limits: Optional[CommandLimits] = None):
        self.project_root = os.path.abspath(project_root)
        self.limits = limits or CommandLimits()

    def run(self, argv: List[str], cwd: Optional[str] = None,
           env_overrides: Optional[Dict[str, str]] = None) -> CommandResult:
        if not argv:
            raise CommandDenied("empty command")
        program = os.path.basename(argv[0])
        if program in _DENYLIST:
            raise CommandDenied(f"{program!r} is not permitted as a project command")

        work_dir = os.path.normpath(os.path.join(self.project_root, cwd or "."))
        if not work_dir.startswith(self.project_root):
            raise CommandDenied(f"working directory {cwd!r} is outside the project root")

        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
              "HOME": self.project_root, "LC_ALL": "C"}
        env.update(env_overrides or {})

        preexec = None
        if os.name == "posix":
            def _limit_resources():
                import resource
                try:
                    resource.setrlimit(resource.RLIMIT_CPU,
                                       (self.limits.cpu_seconds, self.limits.cpu_seconds))
                    resource.setrlimit(resource.RLIMIT_AS,
                                       (self.limits.address_space_mb * 1024 * 1024,
                                        self.limits.address_space_mb * 1024 * 1024))
                except (ValueError, OSError):
                    pass
            preexec = _limit_resources

        started = time.time()
        try:
            proc = subprocess.Popen(argv, cwd=work_dir, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True, text=True,
                                    preexec_fn=preexec)
        except FileNotFoundError as exc:
            return CommandResult(False, None, "", str(exc), command=argv)

        try:
            stdout, stderr = proc.communicate(timeout=self.limits.wall_clock_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            self._kill(proc)
            stdout, stderr = "", ""
            timed_out = True

        wall_ms = (time.time() - started) * 1000
        truncated = len(stdout) > self.limits.max_output_bytes
        if truncated:
            stdout = stdout[: self.limits.max_output_bytes]

        return CommandResult(
            ok=(not timed_out and proc.returncode == 0),
            exit_code=proc.returncode if not timed_out else None,
            stdout=stdout, stderr=stderr, timed_out=timed_out,
            truncated=truncated, wall_ms=wall_ms, command=argv)

    @staticmethod
    def _kill(proc) -> None:
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=2)
        except Exception:
            pass
