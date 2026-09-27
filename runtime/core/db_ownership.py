"""D11 — single-owner DB architecture for SwarmEngine.

The defect: two ``SwarmEngine`` instances booted on the same database file
(one per thread, or one per process) form a split-brain — separate in-memory
chain heads and caches with interleaved chain-row appends. sqlite serializes
page writes but cannot prevent the logical divergence. The GUI rebase proved
the pattern occurs in practice (``get_engine()`` boots engine #1 on
``RUNTIME_DB``; the scheduler's ``_engine_lazy()`` boots engine #2 on the
same file on the worker thread).

This module provides the fail-closed mechanism:

1. **Process-wide single-owner registry.** ``SwarmEngine.__init__`` claims
   ``os.path.abspath(db_path)`` at construction, *before* any sqlite work.
   A second construction on an already-owned path raises
   :class:`DuplicateEngineError` — fail-closed at construction. The claim is
   held by weak reference to the engine: ``engine.close()`` releases it
   deterministically; garbage collection of a dead engine releases it
   automatically (so ``del eng; gc.collect()`` restart-simulation patterns
   keep working).

2. **Cross-process owner lock.** On claim, the engine takes
   ``fcntl.flock(LOCK_EX | LOCK_NB)`` on ``<db>.owner.lock``. A second
   process attempting the same file gets :class:`DuplicateEngineError`
   naming the lockfile. The lock fd is closed (releasing the lock) on
   ``close()``, on engine GC, and implicitly on process exit.

3. **Explicit thread-affinity.** :class:`ThreadBoundConnection` wraps a
   thread-bound sqlite connection; any use from a thread other than the
   constructing thread raises :class:`ThreadAffinityError` instead of a raw
   ``sqlite3.ProgrammingError``. The engine records its constructing thread
   ident as ``engine._owner_thread``.

Escape hatch (tests ONLY, provably so): ``SwarmEngine(...,
_test_allow_shared=True)`` skips the claim ONLY when
:func:`_hatch_honored` holds — i.e. the explicit test-mode env var
``REMOR_TEST_MODE=1`` is set AND the database path is under the system
temp dir. Otherwise the flag is silently ignored and the claim proceeds:
fail closed. Production code passing the flag without the env var, or on
a real (non-temp) path, gets no bypass. The shipped code path cannot
construct the two-engines-one-DB state, period.
"""

import fcntl
import os
import tempfile
import threading
import weakref


class DuplicateEngineError(Exception):
    """Raised when a second engine is constructed on a database file that
    already has a live owner — in this process or another one.

    Fail-closed: the second engine never boots, so no split-brain can form.
    """


class ThreadAffinityError(Exception):
    """Raised when a thread-bound engine resource (e.g. the oracle
    registry's sqlite connection) is touched from a thread other than the
    one that constructed it."""


_REGISTRY_LOCK = threading.Lock()
# normalized db path -> dict(ref=weakref(engine), fd=lock fd or None,
#                            owner=desc str, released=bool, ticket=ticket)
_OWNERS = {}


def _normalize(db_path):
    """Registry key for a db path. ``file:`` URIs have no filesystem path
    to abspath, so they key on the raw string (in-process registry only)."""
    if isinstance(db_path, str) and db_path.startswith("file:"):
        return db_path
    return os.path.abspath(db_path)


def _lock_path_for(key):
    return key + ".owner.lock"


def _make_finalizer(key):
    def _finalize(ref):
        # Runs on GC of the engine: release the claim + owner lock if this
        # entry is still ours. Never resurrects the engine.
        with _REGISTRY_LOCK:
            entry = _OWNERS.get(key)
            if (entry is not None and entry["ref"] is ref
                    and not entry["released"]):
                entry["released"] = True
                _OWNERS.pop(key, None)
                fd = entry["fd"]
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
    return _finalize


def _take_flock(key):
    """Take LOCK_EX|LOCK_NB on the owner lockfile. Returns the fd (kept
    open for the claim's lifetime), or None for ``file:`` URIs."""
    if key.startswith("file:"):
        return None
    lock_path = _lock_path_for(key)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise DuplicateEngineError(
            "cannot create owner lockfile %r for db %r: %s"
            % (lock_path, key, exc))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise DuplicateEngineError(
            "refusing second engine on %r: owner lock held by another "
            "process (lockfile %r)" % (key, lock_path))
    except OSError:
        os.close(fd)
        raise
    return fd


def _hatch_honored(db_path):
    """Provably-test-only gate for the ``_test_allow_shared`` bypass.

    Honored ONLY when BOTH hold:
      (a) the explicit test-mode env var ``REMOR_TEST_MODE=1`` is set, and
      (b) the database path is under the system temp dir.

    Otherwise the flag is ignored (the claim proceeds normally). This is
    what makes the bypass provably test-only rather than a production
    escape hatch: without the env var it is dead code, and even with the
    env var it cannot touch a real (non-temp) database.
    """
    if os.environ.get("REMOR_TEST_MODE") != "1":
        return False
    key = _normalize(db_path)
    if key.startswith("file:"):
        return False
    tmp = os.path.abspath(tempfile.gettempdir())
    return key == tmp or key.startswith(tmp + os.sep)


class _DbOwnershipTicket:
    """Handle for a held ownership claim. ``release()`` is idempotent."""

    def __init__(self, key, fd):
        self._key = key
        self._fd = fd
        self._released = False

    def release(self):
        with _REGISTRY_LOCK:
            if self._released:
                return
            self._released = True
            entry = _OWNERS.get(self._key)
            if entry is not None and entry.get("ticket") is self:
                entry["released"] = True
                _OWNERS.pop(self._key, None)
            fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    @property
    def db_key(self):
        return self._key


def claim_db_ownership(db_path, owner_obj, owner_desc="SwarmEngine"):
    """Claim single ownership of ``db_path`` for ``owner_obj``.

    Raises :class:`DuplicateEngineError` if a live engine already owns the
    path in this process, or if another process holds the owner lock.
    The claim auto-releases when ``owner_obj`` is garbage collected; call
    ``ticket.release()`` (or ``engine.close()``) for deterministic release.
    """
    key = _normalize(db_path)
    with _REGISTRY_LOCK:
        entry = _OWNERS.get(key)
        if entry is not None and not entry["released"] \
                and entry["ref"]() is not None:
            raise DuplicateEngineError(
                "refusing second engine on %r: already owned in this "
                "process by %s" % (key, entry["owner"]))
        # Stale entry (dead owner whose finalizer has not run, or an
        # explicitly released claim): drop it before re-claiming.
        if entry is not None:
            _OWNERS.pop(key, None)
            fd = entry["fd"]
            if fd is not None and not entry["released"]:
                try:
                    os.close(fd)
                except OSError:
                    pass
        ref = weakref.ref(owner_obj, _make_finalizer(key))
        fd = _take_flock(key)
        ticket = _DbOwnershipTicket(key, fd)
        _OWNERS[key] = {"ref": ref, "fd": fd, "owner": owner_desc,
                        "released": False, "ticket": ticket}
        return ticket


def release_db_ownership(db_path):
    """Explicitly release this process's claim on ``db_path`` (no-op if
    unclaimed). Prefer ``engine.close()`` for engine-owned claims."""
    key = _normalize(db_path)
    with _REGISTRY_LOCK:
        entry = _OWNERS.pop(key, None)
        if entry is None:
            return
        entry["released"] = True
        ticket = entry.get("ticket")
        if ticket is not None:
            ticket._released = True
        fd = entry["fd"]
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def db_owner(db_path):
    """Return the owner description for ``db_path``, or None if unclaimed."""
    key = _normalize(db_path)
    with _REGISTRY_LOCK:
        entry = _OWNERS.get(key)
        if entry is None or entry["released"] or entry["ref"]() is None:
            return None
        return entry["owner"]


class ThreadBoundConnection:
    """Wraps a thread-bound sqlite3 connection.

    Every attribute access (including ``cursor()``, ``execute()``,
    ``commit()``, ``close()``, and attribute writes like ``row_factory``)
    first verifies the calling thread is the constructing thread; otherwise
    raises :class:`ThreadAffinityError` instead of sqlite3's raw
    ``ProgrammingError`.
    """

    def __init__(self, conn, owner_thread=None, owner_desc="connection"):
        object.__setattr__(self, "_wrapped", conn)
        object.__setattr__(
            self, "_owner_thread",
            owner_thread if owner_thread is not None
            else threading.get_ident())
        object.__setattr__(self, "_owner_desc", owner_desc)

    def _check_thread(self):
        if threading.get_ident() != self._owner_thread:
            raise ThreadAffinityError(
                "%s is thread-affine: constructed on thread %s, touched "
                "from thread %s" % (self._owner_desc, self._owner_thread,
                                    threading.get_ident()))

    def __getattr__(self, name):
        self._check_thread()
        return getattr(self._wrapped, name)

    def __setattr__(self, name, value):
        if name in ("_wrapped", "_owner_thread", "_owner_desc"):
            object.__setattr__(self, name, value)
        else:
            self._check_thread()
            setattr(self._wrapped, name, value)

    def __enter__(self):
        self._check_thread()
        return self._wrapped.__enter__()

    def __exit__(self, *args):
        self._check_thread()
        return self._wrapped.__exit__(*args)
