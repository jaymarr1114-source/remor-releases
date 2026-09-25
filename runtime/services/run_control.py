"""
swarm_engine/services/run_control.py

Cooperative run preemption for the REMOR runtime: pause / resume / stop.

Design (frozen contract — every service in this layer builds on it):
  * Preemption is COOPERATIVE. Long-running run code calls
    ``swarm_engine.services.run_control.checkpoint(where)`` at natural
    cooperation points (stage boundaries, loop iterations). There is no
    thread-kill and no asyncio task cancellation: killing a thread that
    holds sqlite transactions would corrupt engine state, and cancelling
    an asyncio task mid-admission could leave a half-written capability.
  * A ``RunControl`` is a plain thread-safe object. The run's worker
    thread sets it as the current control (contextvar); any other thread
    (e.g. an HTTP handler) calls request_stop()/request_pause()/resume()
    on the same object.
  * ``checkpoint()`` with no control installed is a no-op, so all existing
    call paths behave exactly as before when no preemption is in play.
  * stop: the next checkpoint raises ``RunStopped``. Callers convert it
    into an honest "stopped" outcome (trace intact up to the checkpoint).
    Stop overrides pause.
  * pause: the next checkpoint blocks (in short sleeps, still watching
    for stop) until ``resume()``. Pause granularity is therefore "at the
    next checkpoint", never mid-primitive.

Honest bounds (do not overclaim):
  * A stop/pause requested while the run sits inside a non-cooperating
    stretch (e.g. a subprocess sandbox already launched, a tight C loop
    in numpy) takes effect at the NEXT checkpoint, not instantly.
  * ``checkpoint()`` blocks the calling thread while paused. Run workers
    run on a dedicated thread/loop, so this is safe there; never call it
    on a thread that must stay responsive (e.g. an HTTP server thread).
"""
from __future__ import annotations

import contextvars
import threading
import time
from typing import Optional


class RunStopped(Exception):
    """Raised at a cooperation checkpoint after stop was requested."""


class RunControl:
    """Thread-safe pause/resume/stop handle for one run."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._pause = threading.Event()

    # -- requested from other threads ------------------------------------
    def request_stop(self) -> None:
        """Ask the run to stop at its next checkpoint. Overrides pause."""
        self._stop.set()

    def request_pause(self) -> None:
        """Ask the run to pause at its next checkpoint. No-op once stopped."""
        if not self._stop.is_set():
            self._pause.set()

    def resume(self) -> None:
        """Clear a pending/active pause. Never clears a stop."""
        self._pause.clear()

    # -- observed from any thread -----------------------------------------
    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return self._pause.is_set() and not self._stop.is_set()

    # -- called from run code at cooperation points -----------------------
    def checkpoint(self, where: str = "") -> None:
        """Block while paused; raise RunStopped if stop was requested."""
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.02)
        if self._stop.is_set():
            raise RunStopped(
                f"run stopped at checkpoint {where!r}" if where else "run stopped"
            )


_current: contextvars.ContextVar = contextvars.ContextVar(
    "remor_run_control", default=None
)


def set_current(control: Optional[RunControl]) -> None:
    """Install the control for the current run (call on the worker thread)."""
    _current.set(control)


def current() -> Optional[RunControl]:
    """The control installed for this run, or None."""
    return _current.get()


def checkpoint(where: str = "") -> None:
    """Cooperation point. No-op when no control is installed."""
    control = _current.get()
    if control is not None:
        control.checkpoint(where)
