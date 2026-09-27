"""Capability-based effect sandbox (W4-R1).

James's decision (2026-09-27): the PURE static screen is never the authority
that makes code safe -- the *execution environment* is. This module is that
environment. The mechanism is capability-absence, not pattern matching:

1. Restricted ``__builtins__`` -- the effect capability is simply not granted
   in the execution namespace. There is deliberately NO string blacklist for
   obfuscation patterns (``"op" + "en"`` etc.): ``open`` is absent from the
   namespace, so every spelling of the name fails the same way.
2. Guarded ``__import__`` -- the namespace's ``__import__`` is replaced by
   the guard built by :func:`build_guarded_import`, which is consulted on
   every import statement and permits only the known-pure modules in
   :data:`PURE_MODULES`.
3. ``sys.addaudithook`` observer -- a real PEP 578 audit hook records
   C-layer effect events (``open``, ``socket.__new__``, ``os.system``,
   ``subprocess.Popen``, ``marshal.loads``). Object-model escapes such as
   ``().__class__.__base__.__subclasses__()`` bypass *name* restrictions, but
   any real effect must go through the C layer, which fires audit events --
   so the escape is observed, recorded, and (under a PURE policy) classified
   as a hard trust failure by :func:`summarize_violations`.

Trust rules enforced here (from the W4-R1 decision record):
  * PURE is never granted by static analysis; it is zero granted effect
    capabilities, determined by the execution policy.
  * An observed effect under a PURE policy is a HARD TRUST FAILURE: it blocks
    admission and stays auditable in the recorded event list.
  * Declarations never grant authority: :func:`policy_for_effects` turns a
    declaration into a sandbox *request*; only an explicit grant produces a
    non-pure policy.

Known residual (documented, not hidden): several stdlib modules in
PURE_MODULES expose already-imported modules as attributes because their own
implementation needs them (verified 2026-09-27 on CPython 3.12:
``random._os``, ``statistics.sys``, ``collections._sys``, ``fractions.sys``,
``calendar.sys``, ``typing.sys``, ``dataclasses.sys``, ``enum.sys``,
``json.codecs``). They are used internally, so they cannot be scrubbed without
breaking the modules. A path like ``statistics.sys.modules["os"]`` therefore
reaches an already-imported module without going through the import guard.

Residual repair (2026-09-27, this mission): the subprocess-shim layer now
closes this path with PREVENTION, not just observation:
  * :func:`install_audit_hook` takes the active policy; a policy-denied
    observed event (``os.system``, ``open``, ``subprocess.Popen``,
    ``socket.__new__``, ``marshal.loads``, and the ``os.*`` filesystem
    mutators) is recorded first, then a deliberate ``_EffectDenied`` is
    raised -- the C call is aborted BEFORE the OS action executes.
  * :func:`neuter_os_effects` replaces effectful functions that fire NO
    audit event (the exec/spawn/fork family, process suicide, signals, raw
    fd plumbing, ``_posixsubprocess.fork_exec``) with stubs that raise
    ``_EffectDenied`` on any call.
  * The child installs the import guard GLOBALLY (``builtins.__import__``),
    so module-attribute reaches cannot smuggle in fresh effectful modules;
    the guard itself and the scoped-open stub are exec'd with minimal
    ``__globals__`` so ``fn.__globals__`` exposes no authority; and the
    child drops ``swarm_engine.*`` from ``sys.modules`` so the builder's
    own module globals are unreachable from candidate code.
The fs-diff secondary detector and the TRUSTED full-authority profile are
unchanged.

``sys.addaudithook`` hooks cannot be removed: CPython provides no API to
uninstall an audit hook, so code running under observation cannot disable its
own observer. (It also cannot reach ``sys`` at all: ``sys`` is neither in
SAFE_BUILTINS nor importable under a PURE policy.)

Caveat on audit noise: the import system itself fires ``open`` audit events
when loading a pure module's own source files. Under a PURE policy
:func:`policy_allows` is intentionally fail-closed (False for every event), so
a verification harness adjudicating a record should distinguish
import-machinery noise from genuine effects -- e.g. by baselining the record
over the same imports, or by checking that no event carries a granted
capability. Fail-closed is the safe direction.

TRUSTED profile (2026-09-27, W4-R1 Worker 4): ``trusted`` is the
fixed-procedure execution profile. The namespace carries the FULL real
builtins (no restriction; the real ``__import__`` -- the guarded import is
bypassed). The audit hook is still installed and keeps recording every real
effect (observability), but :func:`summarize_violations` returns [] under
TRUSTED -- the profile never fails.

INVIOLABLE RULE (James, 2026-09-27): TRUSTED may ONLY be passed code bytes
that are FIXED IN THE TREE (reviewed procedure -- technique files, harness
constants, composition drivers). NEVER pass synthesized or admitted
candidate bytes under TRUSTED: candidate-controlled data must never reach
a full-authority namespace. Every call site passing TRUSTED_POLICY must
carry a justification comment stating why the executed bytes are fixed
procedure and not candidate data. ``shim_policy_data`` /
``EffectPolicy.from_dict`` round-trip the profile as data; the profile is
never derived from the executed code itself.
"""

from __future__ import annotations

import builtins as _builtins
import os as _os
import sys as _sys
from dataclasses import dataclass
from typing import Sequence

PURE_PROFILE = "pure"
GRANTED_PROFILE = "granted"
# W4-R1 Worker 4 (2026-09-27): the fixed-procedure profile. See the
# INVIOLABLE RULE in the module docstring -- TRUSTED is only for code
# bytes fixed in the tree, never for synthesized/admitted candidate bytes.
TRUSTED_PROFILE = "trusted"


@dataclass(frozen=True)
class EffectPolicy:
    """What the execution environment grants. Frozen: policies are values."""

    profile: str = PURE_PROFILE
    grants: tuple = ()

    def is_pure(self) -> bool:
        return self.profile == PURE_PROFILE

    def to_dict(self) -> dict:
        return {"profile": self.profile, "grants": list(self.grants)}

    @classmethod
    def from_dict(cls, d: dict) -> "EffectPolicy":
        return cls(
            profile=d.get("profile", PURE_PROFILE),
            grants=tuple(d.get("grants", ())),
        )


PURE_POLICY = EffectPolicy()
TRUSTED_POLICY = EffectPolicy(profile=TRUSTED_PROFILE)
# Narrow explicit grant: dynamic code execution (the real ``exec`` and
# ``compile`` builtins) inside the SAME restricted namespace -- the exec'd
# code gains no new capabilities. For tester/repair procedures that must
# execute generated candidate bytes to judge them (their inner testing uses
# empty-builtins exec, so candidates get nothing). Explicit grant only;
# PURE excludes it.
EXEC_POLICY = EffectPolicy(profile=GRANTED_PROFILE, grants=("code.exec",))

# Modules directly importable under a PURE policy. Conservative: no
# os/sys/subprocess/socket/shutil/pathlib/urllib/http/importlib/ctypes/
# threading/multiprocessing. Nested imports performed by these modules'
# own code use the real import system (their __builtins__ is the genuine
# one), so their transitive stdlib dependencies load normally -- the guard
# below only gates the top-level name the sandboxed code requests.
#
# W4-R1 runner integration (2026-09-27): `ast` added -- it is effect-free
# (source parsing, tree manipulation, literal_eval; no I/O surface) and
# the repair-technique contract (technique_v1/v2) requires executing
# under subprocess isolation. Grants zero effect capabilities; the audit
# hook still observes every real effect.
PURE_MODULES: frozenset = frozenset({
    "ast",
    "math",
    "random",
    "statistics",
    "itertools",
    "functools",
    "collections",
    "string",
    "re",
    "json",
    "decimal",
    "fractions",
    "datetime",
    "calendar",
    "typing",
    "numbers",
    "operator",
    "dataclasses",
    "enum",
    "abc",
})


def _builtin_exception_names() -> frozenset:
    names = set()
    for name in dir(_builtins):
        obj = getattr(_builtins, name, None)
        if isinstance(obj, type) and issubclass(obj, BaseException):
            names.add(name)
    return frozenset(names)


# Builtin names available under a PURE policy. Supports real synthesized
# pure code: arithmetic, containers, classes (needs __build_class__),
# exceptions (full hierarchy), print, comprehensions, generators,
# decorators. Deliberately EXCLUDED: open, input, eval, exec, compile,
# breakpoint, help, exit, quit, globals, locals, vars, memoryview
# (plus delattr/copyright/credits/license, which pure code does not need).
# True/False/None need no entry: they are keywords.
SAFE_BUILTINS: frozenset = frozenset({
    # construction / types
    "bool", "bytearray", "bytes", "complex", "dict", "float", "frozenset",
    "int", "list", "object", "set", "slice", "str", "tuple", "type",
    # numeric / misc
    "abs", "all", "any", "ascii", "bin", "chr", "divmod", "hex", "max",
    "min", "oct", "ord", "pow", "repr", "round", "sorted", "sum",
    # iteration
    "enumerate", "filter", "iter", "len", "map", "next", "range",
    "reversed", "zip", "aiter", "anext",
    # effect-free introspection
    "callable", "dir", "format", "getattr", "hasattr", "hash", "id",
    "isinstance", "issubclass", "setattr",
    # classes
    "classmethod", "property", "staticmethod", "super", "__build_class__",
    # output (captured by the harness; not an environment effect)
    "print",
    # import machinery (replaced by the guard in restricted_builtins)
    "__import__",
    # sentinel constants
    "Ellipsis", "NotImplemented",
} | _builtin_exception_names())


_real_import = _builtins.__import__
_real_open = _builtins.open


# The import guard is exec'd in a minimal namespace on purpose. A plain
# module-level ``def`` would carry this module's ``__globals__`` -- which
# contain ``_real_import`` (full import power), ``_real_open`` (unscoped
# open), ``_os``, ``_builtins`` and the rest of the sandbox builder's
# authority. Candidate code reaches any function's ``__globals__`` via
# ``fn.__globals__``, so a module-level guard function would hand the
# candidate the exact escape the W4-R1 residual repair exists to close
# (observed 2026-09-27: ``__builtins__["__import__"].__globals__`` exposed
# ``_real_import``/``_os``/``_builtins``). Exec'ing the guard with only
# {ImportError, PURE_MODULES, _PREIMPORTED} in its globals removes that
# surface entirely.
_GUARDED_IMPORT_SRC = '''
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = (name or "").split(".")[0]
    if root not in PURE_MODULES:
        if level:
            raise ImportError(
                "effect capability not granted: relative import "
                "%r (level=%r) resolves outside the pure module set"
                % (name, level)
            )
        raise ImportError(
            "effect capability not granted: cannot import %r "
            "under 'pure' policy (root %r not in PURE_MODULES)"
            % (name, root)
        )
    if name in _PREIMPORTED:
        return _PREIMPORTED[name]
    try:
        return _PREIMPORTED[root]
    except KeyError:
        raise ImportError(
            "effect capability not granted: %r is not pre-imported "
            "under this execution policy" % (name,)
        )
'''


def build_guarded_import():
    """Build the sandbox ``__import__`` guard with minimal ``__globals__``.

    Consulted on every import statement inside sandboxed code. Permits
    ``name`` iff its root (``name.split(".")[0]``) is in
    :data:`PURE_MODULES`; anything else raises ImportError with an explicit
    "effect capability not granted" message. Relative imports (``level > 0``)
    resolving to a non-pure root are rejected outright. Permitted modules
    are served from a snapshot of ``sys.modules`` taken here -- never by a
    fresh real import at candidate time, so the candidate cannot observe or
    influence import machinery. No pattern blacklist anywhere: the check is
    pure capability-absence against the allowlist.

    The guard function object is exec'd in a namespace containing only
    ``ImportError``, ``PURE_MODULES`` and the pre-imported module table, so
    ``guard.__globals__`` exposes no authority beyond the allowlist itself.
    Missing-but-pure modules are real-imported ONCE here (builder's
    authority, before any candidate runs), never lazily under the guard.
    """
    preimported = {}
    for modname in list(PURE_MODULES) + ["collections.abc"]:
        mod = _sys.modules.get(modname)
        if mod is None:
            try:
                mod = _real_import(modname)
            except ImportError:
                continue
        preimported[modname] = mod
    ns = {
        "__builtins__": {"ImportError": ImportError},
        "PURE_MODULES": PURE_MODULES,
        "_PREIMPORTED": preimported,
    }
    exec(_GUARDED_IMPORT_SRC, ns)
    return ns["guarded_import"]


def restricted_builtins(policy: EffectPolicy, guard=None) -> dict:
    """Build the ``__builtins__`` dict for a sandboxed namespace.

    Every name in :data:`SAFE_BUILTINS` is resolved from the real builtins,
    with ``__import__`` replaced by the guard built by
    :func:`build_guarded_import` (a fresh minimal-``__globals__`` guard is
    built here when ``guard`` is not supplied). Under a granted policy
    carrying ``fs.write:<scope>``, ``open`` is additionally provided as
    :func:`scoped_open` bound to that scope; under a granted policy
    carrying ``code.exec``, the real ``exec`` and ``compile`` builtins are
    provided (dynamic code execution still cannot reach ungranted
    capabilities -- the exec'd code runs in the same restricted
    namespace); under PURE there is no ``open`` and no ``exec``/``compile``
    at all -- no spelling of the names can reach one.
    """
    if guard is None:
        guard = build_guarded_import()
    table: dict = {}
    for name in SAFE_BUILTINS:
        if name == "__import__":
            table[name] = guard
            continue
        try:
            table[name] = getattr(_builtins, name)
        except AttributeError:
            # Version-dependent builtin (e.g. aiter/anext on older Pythons).
            continue
    if policy.profile == GRANTED_PROFILE:
        for grant in policy.grants:
            if grant.startswith("fs.write:"):
                table["open"] = scoped_open(grant[len("fs.write:"):])
                break
        # code.exec: dynamic code execution WITHOUT new capabilities. The
        # exec'd code inherits this restricted namespace, so it cannot
        # reach open/imports/effects any more than the outer code can.
        # Grants both exec and compile (compile alone cannot execute;
        # techniques use exec(compile(...)) to test generated candidates).
        # Explicit grant only: PURE never includes it.
        if "code.exec" in policy.grants:
            table["exec"] = getattr(_builtins, "exec")
            table["compile"] = getattr(_builtins, "compile")
    return table


def scoped_open(scope_dirs):
    """Return an ``open()`` replacement confined to ``scope_dirs``.

    The target is resolved with :func:`os.path.realpath` and must lie
    within (or be equal to) one of the scope roots; ``..`` escapes fail the
    realpath comparison and raise PermissionError. Integer file descriptors
    are rejected (ambient authority is not a grantable path). In-scope paths
    pass through with the full normal ``open()`` signature (``*args`` /
    ``**kwargs`` forwarded untouched).
    """
    if isinstance(scope_dirs, (str, _os.PathLike)):
        scope_dirs = (str(scope_dirs),)
    scope_real = tuple(_os.path.realpath(d) for d in scope_dirs)

    # Exec'd in a minimal namespace on purpose: a module-level ``def``
    # would carry this module's ``__globals__`` (containing the unscoped
    # ``_real_open``), and candidate code reaches ``open.__globals__`` via
    # the same ``fn.__globals__`` path as the import guard. The stub still
    # needs the unscoped opener internally, but any candidate reaching for
    # it through ``__globals__`` hits the audit hook: the raw ``open``
    # event fires and a policy-denied call is prevented before it executes.
    ns = {
        "__builtins__": {"PermissionError": PermissionError,
                         "TypeError": TypeError, "any": any},
        "_real_open": _real_open,
        "_realpath": _os.path.realpath,
        "_fspath": _os.fspath,
        "_sep": _os.sep,
        "_scope_real": scope_real,
    }
    exec(
        "def _scoped_open(file, *args, **kwargs):\n"
        "    try:\n"
        "        target = _realpath(_fspath(file))\n"
        "    except TypeError:\n"
        "        raise PermissionError(\n"
        "            'effect capability not granted: file descriptors '\n"
        "            'are not grantable paths'\n"
        "        )\n"
        "    if not any(\n"
        "        target == root or target.startswith(root + _sep)\n"
        "        for root in _scope_real\n"
        "    ):\n"
        "        raise PermissionError(\n"
        "            'effect capability not granted: path outside '\n"
        "            'granted scope'\n"
        "        )\n"
        "    return _real_open(file, *args, **kwargs)\n",
        ns,
    )
    _scoped_open = ns["_scoped_open"]
    _scoped_open.__name__ = "open"
    return _scoped_open


def build_namespace(policy: EffectPolicy, guard=None) -> dict:
    """Globals dict for :func:`exec` of capability code under ``policy``.

    Under a TRUSTED profile the namespace carries the FULL real builtins
    (real ``__import__``; the guarded import is bypassed). TRUSTED is
    reserved -- by the inviolable rule in the module docstring -- for code
    bytes fixed in the tree (reviewed procedure), never for
    synthesized/admitted candidate bytes.

    ``guard`` is the import-guard function installed in the namespace's
    ``__builtins__``; when omitted a fresh one is built from the current
    ``sys.modules``. The child shim passes the SAME guard object it
    installs globally as ``builtins.__import__``, so top-level imports and
    function-body imports share one allowlist snapshot.
    """
    if policy.profile == TRUSTED_PROFILE:
        return {
            "__name__": "capability_module",
            "__builtins__": dict(vars(_builtins)),
        }
    return {
        "__name__": "capability_module",
        "__builtins__": restricted_builtins(policy, guard),
    }


# Audit event names observed by install_audit_hook. Every entry below was
# verified empirically on CPython 3.12.3 (2026-09-27): sys.addaudithook fires
# "open" for open() and os.open(), "socket.__new__" for socket.socket(),
# "os.system" for os.system(), "subprocess.Popen" for
# subprocess.Popen._execute_child, "marshal.loads" for marshal.loads(), and
# one "os.<name>" event per filesystem-mutator C call (os.mkdir, os.rmdir,
# os.rename, os.remove, os.symlink, os.link, os.mknod, os.mkfifo,
# os.replace, os.chmod, os.chown, os.lchown, os.truncate, os.utime).
# Deliberately NOT observed: "compile", "exec", "import" -- these fire
# during normal interpreter operation of legitimate pure code (compiling /
# executing the capability itself, loading a pure module), so observing them
# would flag honest computation. Also deliberately NOT observed:
# read-only traversal events ("os.listdir", "os.scandir", "os.walk",
# "os.fwalk", "os.stat") -- listing is not an effect the policy gates, and
# the shim's own post-execution fs snapshot walks the temp dir.
AUDIT_EVENTS: frozenset = frozenset({
    "open",
    "socket.__new__",
    "os.system",
    "subprocess.Popen",
    "marshal.loads",
    "os.mkdir", "os.rmdir", "os.rename", "os.remove", "os.symlink",
    "os.link", "os.mknod", "os.mkfifo", "os.replace", "os.chmod",
    "os.chown", "os.lchown", "os.truncate", "os.utime",
})


class _EffectDenied(RuntimeError):
    """Raised by the audit hook to PREVENT a policy-denied effect.

    Distinct from a swallowed bookkeeping error: this exception is raised
    DELIBERATELY, after the event has been recorded, when the active
    :class:`EffectPolicy` denies the observed high-risk audit event. The
    CPython guarantee that an audit hook's exception propagates to (and
    aborts) the audited call is what turns observation into prevention: the
    OS action never executes.
    """


class _SilentEffectDenied(_EffectDenied):
    """Raised by neutered (non-audited) effect functions.

    Unlike the hook's :class:`_EffectDenied` -- which is raised only after
    the event was recorded -- a neutered stub raises BEFORE any audit event
    can exist. The child shim catches this subclass and appends a synthetic
    ``{"event": "effect.blocked", ...}`` record, so a prevented silent
    effect is still an auditable hard-trust-failure, not just a case error.
    """


def _hook_denies(event: str, args, policy: EffectPolicy | None) -> bool:
    """Total deny-decision for the audit hook: never raises.

    Returns True iff ``event`` is an observed effect event that ``policy``
    does not allow. Any internal error (including an error inside
    :func:`policy_allows`) yields False -- a fail-closed deny decision must
    never be corrupted into an accidental ALLOW by a bookkeeping fault; the
    worst case here is a missed prevention, which the fs-diff and violation
    summarizer still catch after the fact.
    """
    try:
        if policy is None or policy.profile == TRUSTED_PROFILE:
            return False
        if event not in AUDIT_EVENTS:
            return False
        return not policy_allows(
            {"event": event, "args": [str(a)[:200] for a in args]}, policy
        )
    except Exception:
        return False


def install_audit_hook(record: list,
                       policy: EffectPolicy | None = None) -> None:
    """Install a real ``sys.addaudithook`` appending observed effect events.

    Each recorded entry is ``{"event": event, "args": [str(a)[:200] ...]}``.
    Only events in :data:`AUDIT_EVENTS` are kept. CPython guarantee: audit
    hooks cannot be removed -- there is no API to uninstall one, so observed
    code cannot disable its own observer (and it cannot reach ``sys`` to try:
    ``sys`` is neither in SAFE_BUILTINS nor importable under PURE).

    W4-R1 residual repair (2026-09-27): when ``policy`` is given and is not
    TRUSTED, the hook is no longer a passive observer. A policy-denied
    high-risk event is recorded first, then a deliberate
    :class:`_EffectDenied` is raised -- the audited OS call is aborted
    BEFORE it executes, so the effect never lands. Bookkeeping errors are
    still swallowed (an audit hook must stay total); only the explicit
    policy denial raises. With ``policy=None`` (or TRUSTED) the hook keeps
    its original record-only behavior.
    """
    def _hook(event, args):
        if event in AUDIT_EVENTS:
            try:
                record.append(
                    {"event": event, "args": [str(a)[:200] for a in args]}
                )
            except Exception:
                # An audit hook must never raise on bookkeeping faults:
                # keep it total.
                pass
        if _hook_denies(event, args, policy):
            raise _EffectDenied(
                "effect capability not granted: audit event "
                f"{event!r} denied under {policy.profile!r} policy -- "
                "the effect was prevented before it executed"
            )

    _sys.addaudithook(_hook)


# Effectful os/posix functions that fire NO CPython audit event, so the
# audit hook cannot see them at all. Verified silent on CPython 3.12.3
# (2026-09-27): the exec/spawn/fork family, process suicide, signal
# delivery, raw fd plumbing, and the C-level fork_exec accelerator used by
# subprocess internals. Filesystem mutators with their own "os.*" audit
# event (mkdir, rmdir, rename, remove, ...) are NOT neutered -- the hook
# observes and prevents them, which keeps the attempt in the audit record.
# os.system / subprocess.Popen ARE audited and are likewise left in place
# so the hook records the attempt before preventing it.
_NEUTERED_OS_NAMES = frozenset({
    # Process replacement: no audit event fires before the image swaps.
    "execv", "execve", "execl", "execle", "execlp", "execvp",
    "_execvpe",
    # Process spawning.
    "spawnl", "spawnle", "spawnlp", "spawnv", "spawnve", "spawnvp",
    "spawnvpe", "posix_spawn", "posix_spawnp",
    # Fork / signals / suicide.
    "fork", "forkpty", "kill", "killpg", "_exit", "abort",
    # Pipes and raw fd writes: silent, and fd smuggling surface.
    "pipe", "pipe2", "openpty", "write", "writev", "dup", "dup2",
    "fdatasync", "fsync",
    # os.popen forks+execs without an audited entry point.
    "popen",
})

# Module -> extra silent names beyond _NEUTERED_OS_NAMES.
_NEUTERED_EXTRA = {
    "_posixsubprocess": frozenset({"fork_exec"}),
}

# Exec'd in a minimal namespace on purpose (same reasoning as the import
# guard): a module-level stub would carry this module's __globals__. The
# stub needs nothing but the denial exception and its own qualified name.
_DENIED_STUB_SRC = (
    "def _denied(*args, **kwargs):\n"
    "    raise _EffectDenied(\n"
    "        'effect capability not granted: ' + _funcname +\n"
    "        ' is not available under this execution policy'\n"
    "    )\n"
)


def neuter_os_effects() -> list:
    """Replace silent effectful os/posix functions with raising stubs.

    The audit hook can only prevent what it can observe; the functions in
    :data:`_NEUTERED_OS_NAMES` fire no audit event, so they are replaced in
    place on the ``os``, ``posix`` (and ``_posixsubprocess``) module
    objects with stubs that raise :class:`_EffectDenied` on ANY call. The
    stubs are exec'd with minimal ``__globals__`` (``_EffectDenied`` and
    the qualified name only), so reaching ``stub.__globals__`` yields no
    authority. Returns the list of qualified names neutered. Idempotent for
    the names it handles; only modules present in ``sys.modules`` are
    touched. The child shim calls this for every policy except TRUSTED.
    """
    neutered = []
    targets = [("os", _NEUTERED_OS_NAMES), ("posix", _NEUTERED_OS_NAMES)]
    for modname, extra in _NEUTERED_EXTRA.items():
        targets.append((modname, extra))
    for modname, names in targets:
        mod = _sys.modules.get(modname)
        if mod is None:
            continue
        for fname in names:
            if not hasattr(mod, fname):
                continue
            ns = {
                "__builtins__": {},
                # The stub raises the SILENT subclass: the hook never saw
                # this call (no audit event exists), so the shim records a
                # synthetic "effect.blocked" event on catching it.
                "_EffectDenied": _SilentEffectDenied,
                "_funcname": f"{modname}.{fname}",
            }
            exec(_DENIED_STUB_SRC, ns)
            try:
                setattr(mod, fname, ns["_denied"])
            except Exception:
                continue
            neutered.append(f"{modname}.{fname}")
    return neutered


def policy_allows(event: dict, policy: EffectPolicy) -> bool:
    """Does ``policy`` allow an observed audit event?

    PURE profile: False for every event (fail-closed; an observed effect
    under PURE is a hard trust failure). Granted profile: True iff the event
    is an ``open`` whose realpath-resolved target lies within a granted
    ``fs.write:<scope>``. Read opens outside the scope are NOT allowed --
    strict in-scope-only.
    """
    if policy.profile == PURE_PROFILE:
        return False
    if event.get("event") != "open":
        return False
    args = event.get("args") or []
    if not args:
        return False
    try:
        target = _os.path.realpath(args[0])
    except Exception:
        return False
    for grant in policy.grants:
        if not grant.startswith("fs.write:"):
            continue
        root = _os.path.realpath(grant[len("fs.write:"):])
        if target == root or target.startswith(root + _os.sep):
            return True
    return False


def policy_for_effects(declared: Sequence[str], grant_scope: str | None) -> EffectPolicy:
    """Build the sandbox policy from a declaration plus an explicit grant.

    The declaration builds the sandbox REQUEST; the GRANT decides. A
    ``"write_fs"`` declaration grants nothing by itself: only when a
    ``grant_scope`` is explicitly supplied does the policy become granted
    with ``("fs.write:<scope>",)``. Everything else -- including a bare
    declaration with no grant -- yields :data:`PURE_POLICY`.
    """
    if "write_fs" in declared and grant_scope:
        return EffectPolicy(GRANTED_PROFILE, ("fs.write:" + grant_scope,))
    return PURE_POLICY


def shim_policy_data(policy: EffectPolicy) -> dict:
    """JSON-serializable policy payload for the subprocess shim.

    Single source of truth for what the out-of-process executor enforces:
    profile, grants, and the sorted allowlists it must replicate.
    """
    return {
        "profile": policy.profile,
        "grants": list(policy.grants),
        "safe_builtins": sorted(SAFE_BUILTINS),
        "pure_modules": sorted(PURE_MODULES),
        "audit_events": sorted(AUDIT_EVENTS),
    }


def summarize_violations(observed: list, policy: EffectPolicy) -> list:
    """Observed events the policy does not allow: the trust-failure set.

    Under a TRUSTED profile this always returns []: the audit hook still
    records every real effect (observability is preserved), but fixed,
    reviewed procedure bytes can never commit a trust violation -- the
    profile never fails.
    """
    if policy.profile == TRUSTED_PROFILE:
        return []
    return [e for e in observed if not policy_allows(e, policy)]
