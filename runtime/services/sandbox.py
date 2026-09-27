"""OS-level sandbox hardening for the artifact execute path (S13).

What is real here (verified by tests/backend/test_sandbox.py, which
actually exceed the limits):
  * rlimit confinement applied in the child via preexec_fn --
    RLIMIT_CPU, RLIMIT_AS, RLIMIT_FSIZE, RLIMIT_NPROC -- read from the
    environment (REMOR_SANDBOX_*) with generous defaults so normal
    execution is unaffected. A memory hog dies with MemoryError, an
    infinite loop is SIGKILLed at the CPU cap, a file-bomb is SIGXFSZed.
  * privilege drop (OPT-IN via REMOR_SANDBOX_PRIVDROP=1): when this
    process runs as root AND an unprivileged account exists (``nobody``)
    AND the operator opted in, the child setgid/setuid's to it AFTER the
    rlimits are applied. The sandbox dir is chown'd to the drop user so
    the pinned semantics hold: child cwd == sandbox dir, generated
    files land in the sandbox dir. The drop is opt-in (not default)
    because it requires the sandbox dir's ancestor chain to be
    traversable by the drop user -- a property the server cannot
    guarantee for arbitrary operator-chosen directories; when the
    chain is not traversable the run falls back to same-user + rlimits
    and says so in the disclosure.

Residuals (stated, not hidden):
  * No container, mount-namespace, network, or syscall filtering: this
    is a same-host subprocess sandbox, hardened, not a container.
  * UID isolation applies ONLY with REMOR_SANDBOX_PRIVDROP=1, running as
    root, and an existing drop user; otherwise the child is same-user +
    rlimits. The opt-in is deliberate: the drop needs a traversable
    ancestor chain, which the server cannot guarantee by default.
  * RLIMIT_NPROC counts every process of the child's real uid (with the
    drop, that is the drop user's own processes only).
  * The runner script is root-written then made readable (0644) so the
    dropped child can read it; a pre-existing attacker already running
    as the drop user is outside this threat model.
"""
from __future__ import annotations

import os
import pwd
from typing import Callable, Dict, List, Optional, Tuple

try:
    import resource as _resource
except ImportError:  # non-POSIX platform: rlimits unavailable
    _resource = None

# -- environment knobs ---------------------------------------------------
ENV_CPU = "REMOR_SANDBOX_CPU_SECONDS"
ENV_AS_MB = "REMOR_SANDBOX_AS_MB"
ENV_FSIZE_MB = "REMOR_SANDBOX_FSIZE_MB"
ENV_NPROC = "REMOR_SANDBOX_NPROC"
# Opt-in privilege drop: the operator explicitly accepts the sandbox-dir
# ownership change and the traversability requirement.
ENV_PRIVDROP = "REMOR_SANDBOX_PRIVDROP"

# Generous defaults: normal execution (quick scripts, small outputs)
# must be unaffected; the tests prove breaches die and normal runs live.
_DEF_CPU_SECONDS = 120
_DEF_AS_MB = 1024
_DEF_FSIZE_MB = 256
_DEF_NPROC = 64

# Unprivileged accounts considered for the setuid drop, in order.
_PRIVDROP_CANDIDATES = ("nobody",)

RESIDUAL = (
    "No container, mount-namespace, network, or syscall isolation: the "
    "sandbox is a same-host subprocess. UID isolation applies only with "
    "REMOR_SANDBOX_PRIVDROP=1, running as root, and an existing drop "
    "user (otherwise same-user + rlimits). RLIMIT_NPROC counts every "
    "process of the child's real uid."
)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value > 0 else default


class SandboxLimits:
    """rlimit values applied to every sandboxed child process."""

    def __init__(self, cpu_seconds: int = _DEF_CPU_SECONDS,
                 as_mb: int = _DEF_AS_MB,
                 fsize_mb: int = _DEF_FSIZE_MB,
                 nproc: int = _DEF_NPROC) -> None:
        self.cpu_seconds = cpu_seconds
        self.as_mb = as_mb
        self.fsize_mb = fsize_mb
        self.nproc = nproc

    def as_dict(self) -> Dict[str, int]:
        return {
            "cpu_seconds": self.cpu_seconds,
            "address_space_mb": self.as_mb,
            "max_file_mb": self.fsize_mb,
            "max_processes": self.nproc,
        }


def limits_from_env() -> SandboxLimits:
    """Read the sandbox limits from the environment (with defaults)."""
    return SandboxLimits(
        cpu_seconds=_int_env(ENV_CPU, _DEF_CPU_SECONDS),
        as_mb=_int_env(ENV_AS_MB, _DEF_AS_MB),
        fsize_mb=_int_env(ENV_FSIZE_MB, _DEF_FSIZE_MB),
        nproc=_int_env(ENV_NPROC, _DEF_NPROC),
    )


def find_drop_user() -> Optional[Tuple[str, int, int]]:
    """(name, uid, gid) of an unprivileged account usable for setuid.

    Only when the operator opted in (REMOR_SANDBOX_PRIVDROP=1) AND this
    process runs as root (setuid is impossible otherwise). Returns None
    when no drop is available -- the caller then runs the child as the
    same user (rlimits still apply).
    """
    if os.environ.get(ENV_PRIVDROP) != "1":
        return None
    try:
        if os.geteuid() != 0:
            return None
    except AttributeError:
        return None  # no geteuid: not a POSIX server
    for name in _PRIVDROP_CANDIDATES:
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            continue
        if entry.pw_uid != 0:
            return (entry.pw_name, entry.pw_uid, entry.pw_gid)
    return None


def _traversable_by(path: str, uid: int, gid: int) -> Optional[str]:
    """Check the drop user can traverse every ancestor of path.

    Returns None when traversable, else the first blocking directory.
    The leaf itself is checked by the caller (it gets chown'd).
    """
    cur = os.path.dirname(os.path.abspath(path))
    while True:
        try:
            st = os.stat(cur)
        except OSError:
            return cur
        if st.st_uid == uid:
            ok = bool(st.st_mode & 0o100)
        elif st.st_gid == gid:
            ok = bool(st.st_mode & 0o010)
        else:
            ok = bool(st.st_mode & 0o001)
        if not ok:
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


class SandboxContext:
    """Per-run sandbox configuration for one sandbox directory.

    Construction applies the persistent part (chown of the sandbox dir
    to the drop user when a drop is active); make_preexec() builds the
    child-side function (rlimits, then setgid/setuid). describe() is the
    honest disclosure surfaced in execute results.
    """

    def __init__(self, sandbox_dir: str) -> None:
        self.sandbox_dir = os.path.abspath(sandbox_dir)
        self.limits = limits_from_env()
        self.drop: Optional[Tuple[str, int, int]] = find_drop_user()
        self.mode = "same-user"
        self.note = ""
        self._activate()

    def _activate(self) -> None:
        if self.drop is None:
            self.note = ("no privilege drop: same-user mode "
                         "(opt in with REMOR_SANDBOX_PRIVDROP=1 when "
                         "running as root)")
            return
        name, uid, gid = self.drop
        try:
            # The drop user must reach the sandbox dir: every ancestor
            # needs a traverse bit for it. This cannot be fixed by the
            # server (it must not rechmod operator directories), so a
            # blocked chain falls back to same-user and says so.
            blocked = _traversable_by(self.sandbox_dir, uid, gid)
            if blocked is not None:
                self.drop = None
                self.mode = "same-user"
                self.note = (f"privilege drop requested but {blocked} "
                             f"is not traversable by {name}; same-user "
                             f"fallback (rlimits still apply)")
                return
            st = os.stat(self.sandbox_dir)
            if st.st_uid != uid or st.st_gid != gid:
                os.chown(self.sandbox_dir, uid, gid)
            self.mode = f"privdrop:{name}({uid}:{gid})"
            self.note = (f"sandbox dir owned by {name}; child runs as "
                         f"uid {uid}")
        except OSError as exc:
            # e.g. a filesystem where chown is not permitted: fail back
            # to same-user + rlimits rather than failing the run.
            self.drop = None
            self.mode = "same-user"
            self.note = (f"privilege drop unavailable "
                         f"(chown failed: {exc}); same-user fallback")

    def script_readable_by_child(self, path: str) -> None:
        """Make the root-written runner script readable by the dropped
        child. No-op in same-user mode (0600 root is already readable)."""
        if self.drop is not None:
            os.chmod(path, 0o644)

    def make_preexec(self) -> Callable[[], None]:
        """Build the preexec_fn: rlimits first, then the uid drop."""
        limits = self.limits
        drop = self.drop

        def _preexec() -> None:
            if _resource is not None:
                _resource.setrlimit(
                    _resource.RLIMIT_CPU,
                    (limits.cpu_seconds, limits.cpu_seconds))
                as_bytes = limits.as_mb * 1024 * 1024
                _resource.setrlimit(
                    _resource.RLIMIT_AS, (as_bytes, as_bytes))
                fs_bytes = limits.fsize_mb * 1024 * 1024
                _resource.setrlimit(
                    _resource.RLIMIT_FSIZE, (fs_bytes, fs_bytes))
                _resource.setrlimit(
                    _resource.RLIMIT_NPROC,
                    (limits.nproc, limits.nproc))
            if drop is not None:
                _, uid, gid = drop
                try:
                    os.setgroups([])
                except OSError:
                    pass  # best effort; setgid/setuid still apply
                os.setgid(gid)
                os.setuid(uid)

        return _preexec

    def describe(self) -> Dict[str, object]:
        """Honest disclosure of the sandbox posture for this run."""
        return {
            "mode": self.mode,
            "drop_user": self.drop[0] if self.drop else None,
            "limits": self.limits.as_dict(),
            "rlimits_applied": _resource is not None,
            "note": self.note,
            "residual": RESIDUAL,
        }


__all__: List[str] = [
    "ENV_CPU", "ENV_AS_MB", "ENV_FSIZE_MB", "ENV_NPROC", "ENV_PRIVDROP",
    "RESIDUAL", "SandboxLimits", "SandboxContext",
    "limits_from_env", "find_drop_user",
]
