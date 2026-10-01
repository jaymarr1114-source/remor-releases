"""Sandboxed plugin execution.

Reuses the proven OS-level sandbox from services (swarm_engine.services.
sandbox.SandboxContext: rlimits via preexec_fn, optional privdrop) and
adds REAL filesystem/network containment on top (PLUGIN-FIX-1):

  * every run gets a FRESH per-run directory (a copy of the verified
    package); the plugin's cwd is that directory and nothing else;
  * the entry hash was verified by the loader immediately before this
    copy was made;
  * the child is placed in fresh mount, network, and (when available)
    user namespaces; the run directory is bind-mounted, the interpreter
    paths are bind-mounted read-only, and pivot_root makes the run
    directory the child's entire filesystem — the host root is detached
    from the child's mount namespace. An absolute-path write by the
    plugin lands inside the per-run jail and dies with it; the host
    filesystem is unreachable, and the child has no network;
  * post-run, generated files are diffed (before/after snapshot);
  * caller-supplied canary paths remain as a tripwire: if a host canary
    ever appears, containment has been breached and the run is refused
    with PLUGIN_POLICY_VIOLATION. With containment enforced this must
    never fire — the battery asserts it doesn't;
  * if the namespaces/pivot_root cannot be established, the run is
    REFUSED with PLUGIN_CONTAINMENT_FAILED — a plugin never runs
    uncontained. No silent fallback to detection-only mode.

Honest residual: no seccomp/syscall filtering (the mount namespace +
pivot_root + no-network + rlimits is the boundary). A user namespace
is added only where the host lets the uid map be written (verified by
probe, not assumed); on hosts where the map write is refused, the
mount+network namespaces still apply and the posture disclosure says
exactly that. With the opt-in privdrop (REMOR_SANDBOX_PRIVDROP=1 as
root) the userns is skipped and the existing setuid applies after the
pivot instead.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from swarm_engine.plugin.errors import (
    PLUGIN_CONTAINMENT_FAILED,
    PLUGIN_EXECUTION_FAILED,
    PLUGIN_POLICY_VIOLATION,
    PLUGIN_TIMEOUT,
    PluginError,
)
from swarm_engine.plugin.loader import LoadedPlugin
from swarm_engine.plugin.protocol import (
    parse_result_envelope,
    task_envelope,
)
from swarm_engine.services.sandbox import SandboxContext

_DEFAULT_TIMEOUT_S = 30.0
_MAX_TIMEOUT_S = 600.0


# Top-level entries created by containment itself (bind-mount targets,
# /dev nodes, tmpfs): never reported as plugin-generated files.
_JAIL_TOPDIRS = {"dev", "tmp", "usr", "lib", "lib64"}


def _snapshot_files(root: str) -> set:
    found = set()
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            try:
                if os.path.isfile(full) and not os.path.islink(full):
                    found.add(os.path.relpath(full, root))
            except OSError:
                continue
    return found


def _copy_tree_nolinks(src: str, dst: str) -> None:
    """Copy regular files only; refuse symlinks (defense in depth — the
    package was already validated, but the run copy is re-checked)."""
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        for dirname in dirnames:
            full = os.path.join(dirpath, dirname)
            if os.path.islink(full):
                raise PluginError(
                    PLUGIN_POLICY_VIOLATION,
                    "run copy refused: symlink in package tree",
                    {"path": os.path.relpath(full, src)})
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            if os.path.islink(full) or not os.path.isfile(full):
                raise PluginError(
                    PLUGIN_POLICY_VIOLATION,
                    "run copy refused: non-regular file in package tree",
                    {"path": os.path.relpath(full, src)})
            shutil.copy2(full, os.path.join(target_dir, filename))


# -- real containment: namespaces + pivot_root (PLUGIN-FIX-1) ----------------
#
# Pure kernel mechanism via libc (unshare/mount/umount2) plus the
# pivot_root syscall — no new binaries, no new substrate. Runs in the
# child (preexec_fn), after rlimits, before the optional privdrop.
_libc = ctypes.CDLL("libc.so.6", use_errno=True)

_CLONE_NEWNS = 0x00020000
_CLONE_NEWNET = 0x40000000
_CLONE_NEWUSER = 0x10000000
_MS_BIND = 4096
_MS_REC = 16384
_MS_PRIVATE = 1 << 18
_MS_RDONLY = 1
_MS_REMOUNT = 32
_MNT_DETACH = 2
_SYS_pivot_root = 155  # x86_64 Linux

_userns_probe: Optional[bool] = None


def _userns_mappable() -> bool:
    """Cached probe: can this host create a user namespace AND write
    the uid map (the part that actually confers the isolation)?
    Forks once; the child tries unshare(CLONE_NEWUSER) + the map
    write and exits 0/1. On hosts where the map write is EPERM
    (e.g. nested containers without subuid ranges) this is False and
    containment runs mount+network namespaces without the userns —
    the posture disclosure says exactly that."""
    global _userns_probe
    if _userns_probe is None:
        pid = os.fork()
        if pid == 0:  # child
            ok = False
            try:
                if _libc.unshare(_CLONE_NEWUSER) == 0:
                    uid, gid = os.getuid(), os.getgid()
                    try:
                        with open("/proc/self/setgroups", "w") as fh:
                            fh.write("deny")
                    except OSError:
                        pass
                    with open("/proc/self/uid_map", "w") as fh:
                        fh.write(f"0 {uid} 1\n")
                    with open("/proc/self/gid_map", "w") as fh:
                        fh.write(f"0 {gid} 1\n")
                    ok = True
            except Exception:
                ok = False
            os._exit(0 if ok else 1)
        _, status = os.waitpid(pid, 0)
        _userns_probe = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
    return _userns_probe


def _ro_binds() -> List[str]:
    """Read-only interpreter paths the jailed child needs. Same
    absolute paths inside the jail."""
    binds = ["/usr", "/lib", "/lib64"]
    covered = list(binds)
    exe_dir = os.path.dirname(os.path.realpath(sys.executable))
    prefix = os.path.realpath(sys.base_prefix)
    for cand in (exe_dir, prefix):
        if (cand and cand != "/" and cand not in covered
                and not any(cand == c or cand.startswith(c + "/")
                            for c in covered)
                and os.path.isdir(cand)):
            binds.append(cand)
            covered.append(cand)
    return [b for b in binds if os.path.isdir(b)]


def _contain_fail(op: str) -> None:
    no = ctypes.get_errno()
    raise OSError(no, f"containment: {op} failed: {os.strerror(no)}")


def _contain_child(run_dir: str, use_userns: bool) -> None:
    """Establish the jail. Raises OSError on ANY failure — the caller
    turns that into PLUGIN_CONTAINMENT_FAILED and the plugin never runs
    uncontained."""
    libc = _libc
    flags = _CLONE_NEWNS | _CLONE_NEWNET
    if use_userns:
        flags |= _CLONE_NEWUSER
    if libc.unshare(flags) != 0:
        _contain_fail("unshare(mount,net%s)" %
                      (",user" if use_userns else ""))
    if use_userns:
        uid, gid = os.getuid(), os.getgid()
        try:
            with open("/proc/self/setgroups", "w") as fh:
                fh.write("deny")
        except OSError:
            pass  # ancient kernels lack the knob; gid_map still writable
        with open("/proc/self/uid_map", "w") as fh:
            fh.write(f"0 {uid} 1\n")
        with open("/proc/self/gid_map", "w") as fh:
            fh.write(f"0 {gid} 1\n")
    # Isolate mount propagation FIRST: nothing we mount may leak back
    # to the host's mount table.
    if libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None) != 0:
        _contain_fail("make-rprivate /")
    run = os.fsencode(run_dir)
    # The run dir must be a mount point for pivot_root.
    if libc.mount(run, run, None, _MS_BIND | _MS_REC, None) != 0:
        _contain_fail("bind run_dir")
    # Read-only interpreter mounts inside the future jail root.
    for src in _ro_binds():
        s = os.fsencode(src)
        dst = run + s  # same absolute path, rooted at the run dir
        os.makedirs(dst, exist_ok=True)
        if libc.mount(s, dst, None, _MS_BIND | _MS_REC, None) != 0:
            _contain_fail(f"bind {src}")
        if libc.mount(None, dst, None,
                       _MS_BIND | _MS_REMOUNT | _MS_RDONLY | _MS_REC,
                       None) != 0:
            _contain_fail(f"remount-ro {src}")
    # Minimal /dev (bind the host nodes; they work across namespaces).
    dev = run + b"/dev"
    os.makedirs(dev, exist_ok=True)
    for node in (b"null", b"zero", b"urandom"):
        target = dev + b"/" + node
        open(target, "wb").close()
        if libc.mount(b"/dev/" + node, target, None, _MS_BIND, None) != 0:
            _contain_fail(f"bind /dev/{node.decode()}")
    # Writable /tmp confined to the jail (tmpfs dies with the namespace).
    tmp = run + b"/tmp"
    os.makedirs(tmp, exist_ok=True)
    if libc.mount(b"tmpfs", tmp, b"tmpfs", 0,
                  b"mode=1777,size=64m") != 0:
        _contain_fail("tmpfs /tmp")
    # Pivot: the run dir becomes the child's entire filesystem; the
    # host root is then detached from this mount namespace.
    old = run + b"/.oldroot"
    os.mkdir(old)
    if libc.syscall(_SYS_pivot_root, run, old) != 0:
        _contain_fail("pivot_root")
    os.chdir(b"/")
    if libc.umount2(b"/.oldroot", _MNT_DETACH) != 0:
        _contain_fail("detach old root")
    os.rmdir(b"/.oldroot")


def _containment_posture(use_userns: bool, binds: List[str]) -> Dict[str, Any]:
    return {
        "enforced": True,
        "mount_namespace": True,
        "network_namespace": True,
        "network": "none (fresh netns; no interfaces)",
        "user_namespace": use_userns,
        "user_namespace_note": (
            "child root mapped to caller uid"
            if use_userns else
            "user namespace not established: the uid_map write was "
            "refused in the pre-run probe on this host (cause not "
            "asserted — see _userns_mappable); mount+network namespaces "
            "still enforced, proven by the p4/p4b escape probes"),
        "root": "pivot_root: per-run dir is the child's entire "
                "filesystem; host root detached from the mount namespace",
        "writable": ["/ (per-run jail, destroyed after the run)",
                     "/tmp (tmpfs 64m, dies with the namespace)"],
        "read_only": binds,
        "escape_semantics": "absolute-path writes land inside the "
                            "per-run jail and die with it; the host "
                            "filesystem is unreachable from the child",
    }


def _sandbox_disclosure(ctx) -> Dict[str, Any]:
    """ctx.describe() with the residual corrected for the plugin path.

    services.sandbox.RESIDUAL is true for the generic execute path (a
    same-host subprocess), but the plugin layer adds fresh
    mount+network namespaces and pivot_root on top — the generic
    residual string ("no mount-namespace, network ... isolation")
    would contradict the containment posture sitting next to it in
    the same detail dict. Correct the copy here; the shared string
    is never edited (it stays true for the execute path).
    """
    d = dict(ctx.describe())
    d["residual"] = (
        "base layer (rlimits, optional privdrop) as described above; "
        "the plugin layer additionally enforces fresh mount + network "
        "namespaces and pivot_root into the per-run dir (host root "
        "detached from the child's mount namespace) — see the "
        "'containment' posture in this same detail dict. No "
        "seccomp/syscall filtering."
    )
    return d


@contextmanager
def _patched_env(overrides: Optional[Dict[str, str]]):
    """Temporarily patch os.environ (restored afterwards). Lets one run
    carry its own REMOR_SANDBOX_* knobs without touching the parent."""
    if not overrides:
        yield
        return
    saved = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def run_plugin(loaded: LoadedPlugin,
               task: Dict[str, Any],
               registry_root: str,
               timeout_s: float = _DEFAULT_TIMEOUT_S,
               canary_paths: Optional[List[str]] = None,
               limits_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Execute one verified plugin run. Raises PluginError on every
    honest refusal; returns the success result dict otherwise."""
    try:
        timeout_s = float(timeout_s)
    except (TypeError, ValueError):
        raise PluginError(PLUGIN_EXECUTION_FAILED,
                          "timeout must be a positive number",
                          {"timeout": timeout_s})
    if not (timeout_s > 0) or timeout_s > _MAX_TIMEOUT_S:
        raise PluginError(PLUGIN_EXECUTION_FAILED,
                          f"timeout must be in (0, {_MAX_TIMEOUT_S:g}]",
                          {"timeout": timeout_s})

    runs_root = os.path.join(os.path.abspath(registry_root), "runs")
    os.makedirs(runs_root, exist_ok=True)
    run_id = uuid.uuid4().hex
    run_dir = os.path.join(runs_root, run_id)
    os.makedirs(run_dir)

    try:
        _copy_tree_nolinks(loaded.package_dir, run_dir)

        before = _snapshot_files(run_dir)
        stdin_bytes = task_envelope(task, timeout_s)

        with _patched_env(limits_env):
            ctx = SandboxContext(run_dir)
            # Containment is mandatory, not best-effort. With the
            # opt-in privdrop active we skip the user namespace (the
            # post-pivot setuid needs real root); otherwise the child
            # is root-in-userns mapped to the caller's uid.
            use_userns = ctx.drop is None and _userns_mappable()
            ro_binds = _ro_binds()
            posture = _containment_posture(use_userns, ro_binds)

            def _between() -> None:
                # rlimits are already applied; containment next; the
                # privdrop (if any) runs after this returns.
                try:
                    _contain_child(run_dir, use_userns)
                except OSError as exc:
                    # Leave the failing op where the parent can read
                    # it: preexec_fn exceptions are re-raised in the
                    # parent as bare SubprocessError, losing the cause.
                    with open(os.path.join(run_dir, ".contain_error"),
                              "w") as fh:
                        fh.write(str(exc))
                    raise

            preexec = ctx.make_preexec(between_hook=_between)
            entry_rel = os.path.relpath(loaded.entry_path,
                                        loaded.package_dir)
            # After pivot_root the entry lives at /<rel> inside the jail.
            jail_entry = "/" + entry_rel
            try:
                proc = subprocess.Popen(
                    [sys.executable, jail_entry],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd="/",
                    preexec_fn=preexec,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                # The plugin NEVER runs uncontained. Distinguish a
                # containment failure (fail closed, typed) from any
                # other pre-exec setup failure (also refused, honestly
                # labeled).
                err_file = os.path.join(run_dir, ".contain_error")
                if os.path.isfile(err_file):
                    with open(err_file) as fh:
                        op = fh.read()
                    raise PluginError(
                        PLUGIN_CONTAINMENT_FAILED,
                        f"plugin {loaded.ref}: containment could not be "
                        f"established; run refused",
                        {"plugin": loaded.ref, "failed_op": op,
                         "containment": posture})
                raise PluginError(
                    PLUGIN_EXECUTION_FAILED,
                    f"plugin {loaded.ref}: sandbox setup failed before "
                    f"exec; run refused",
                    {"plugin": loaded.ref, "os_error": str(exc),
                     "containment": posture})
            try:
                stdout_b, stderr_b = proc.communicate(
                    input=stdin_bytes, timeout=timeout_s)
                returncode = proc.returncode
                timed_out = False
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout_b, stderr_b = proc.communicate()
                raise PluginError(
                    PLUGIN_TIMEOUT,
                    f"plugin {loaded.ref} exceeded its {timeout_s:g}s "
                    f"time budget and was killed",
                    {"plugin": loaded.ref, "timeout_s": timeout_s,
                     "stdout_head": stdout_b.decode(
                         "utf-8", "replace")[:500],
                     "containment": posture})

        after = _snapshot_files(run_dir)
        generated = sorted(
            p for p in (after - before)
            if p.split(os.sep)[0] not in _JAIL_TOPDIRS)
        stdout_text = stdout_b.decode("utf-8", "replace")
        stderr_text = stderr_b.decode("utf-8", "replace")

        # Canary tripwire: with containment enforced, a host canary must
        # NEVER appear. If one does, the jail has been breached: refuse.
        escaped: List[str] = []
        for canary in canary_paths or []:
            if os.path.lexists(canary):
                escaped.append(canary)
        if escaped:
            raise PluginError(
                PLUGIN_POLICY_VIOLATION,
                f"plugin {loaded.ref} breached containment: host "
                f"filesystem was written; the run is refused",
                {"plugin": loaded.ref, "escaped_paths": escaped,
                 "sandbox": _sandbox_disclosure(ctx),
                 "containment": posture})

        if returncode != 0:
            raise PluginError(
                PLUGIN_EXECUTION_FAILED,
                f"plugin {loaded.ref} exited with code {returncode}",
                {"plugin": loaded.ref, "returncode": returncode,
                 "stderr_head": stderr_text[:500],
                 "sandbox": _sandbox_disclosure(ctx),
                 "containment": posture})

        envelope = parse_result_envelope(stdout_text)

        result: Dict[str, Any] = {
            "ok": True,
            "plugin": loaded.ref,
            "version": loaded.manifest["version"],
            "result": envelope.get("result"),
            "report": str(envelope.get("report", "")),
            "plugin_ok": envelope["ok"],
            "generated_files": generated,
            "provenance": {
                "kind": loaded.kind,
                "name": loaded.name,
                "version": loaded.manifest["version"],
                "protocol": loaded.manifest["protocol"],
                "manifest_sha256": loaded.manifest["sha256"],
                "entry_hash_verified_at_load": True,
            },
            "sandbox": _sandbox_disclosure(ctx),
            "containment": posture,
        }
        if not envelope["ok"]:
            # The plugin ran fine but reports its own failure: honest
            # pass-through, never upgraded to success.
            result["ok"] = False
            result["error"] = {
                "code": "PLUGIN_REPORTED_FAILURE",
                "reason": str(envelope.get("error", "plugin reported "
                                                   "failure")),
            }
        return result
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


__all__ = ["run_plugin"]
