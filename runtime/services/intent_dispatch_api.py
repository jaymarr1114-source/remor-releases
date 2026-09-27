"""NL intent-dispatch service (contract mission, Tasks 1+2).

Owns a dedicated SwarmEngine (intent.db under the service base dir) on ONE
engine thread (`_EngineThread` -- the same marshal pattern as
`agent_api._SameThreadExecutor`, needed because the oracle registry's
sqlite connection is thread-affine). Every engine-touching call is
marshaled onto that thread. The dispatch audit table (`intent_dispatches`)
is read via short-lived sqlite connections, so history reads need no
marshaling.

HTTP contract (the GUI's Dispatch view call pattern):
  POST /api/intent/dispatch   {text, args?, producer?}
    200  {ok, result, capability_id, dispatch_id, route_via, refusal,
          reasons} -- a refusal is BODY DATA, not an HTTP error (same
          shape as the Track-2B contract the view was built against)
    200  chat/ambiguous turns: the chat handler's answer dict
          ({mode: answer|clarify|acknowledge|refuse, kind, text,
           grounded}) -- never a task slot, never a 429
    400  {ok: false, error}   -- missing/empty text, malformed body
    429  {ok: false, limit: {code}} -- the REAL quota refusal
          (daily_task_cap / queue_cap) from the metering machinery
    500  {ok: false, error}   -- honest execution failure
    504  {ok: false, error, run_id} -- the run did not finish in time;
          poll /api/runs/<id> for the real outcome
  GET /api/dispatches?limit=50
    200  {dispatches: [...]}   -- newest first, from the real audit table

Message-text normalization: `text` is passed through
`message_text.normalize_message_text()` at the very top of
intent_dispatch(), before the discriminator and the metering gate --
leading/trailing dictation-artifact punctuation is stripped (inner
content byte-preserved), so the run's goal, audit rows, and any
refusal/failure echoes never carry the junk.

Metering (Task 2): every accepted TASK dispatch goes through
`metering.guarded_submit()` with `metadata={"kind": "intent_dispatch",
...}`. Before the gate, the Worker-1 discriminator
(`swarm_engine.services.discriminator`) classifies the text: chat-mode
and ambiguous turns NEVER reach the gate (no slot consumed, no
scheduler_runs row) and are answered by the chat handler instead. The slot is consumed at submit time by the REAL scheduler
machinery -- a genuine `scheduler_runs` row, counted by the same
`tasks_today()` query that enforces the 35/day cap. The scheduler worker
executes the NL dispatch AS the run's work (see
`RunScheduler._run_intent_dispatch`) through the real Composer path;
it never launches a full engine run for an intent dispatch.

What counts against quota: an accepted dispatch SUBMISSION (the gate
passed), regardless of the route/execute outcome -- exactly the
`guarded_submit` semantics every other task type uses (the slot is
consumed by the INSERT, before the outcome is known). A gate-refused
dispatch (36th of the day, 26th queued) is never submitted, never
routed, never executed. Whether a trivially-refused intent SHOULD burn
a slot is an open product question -- flagged in the mission report,
not decided here.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from swarm_engine.services import discriminator as _discriminator
from swarm_engine.services.message_text import (
    normalize_message_text as _normalize_message_text)

T = TypeVar("T")

_TERMINAL_RUN = {"completed", "failed", "stopped", "error", "cancelled"}
# NOTE (phase 4c convergence repair): this set MUST equal the canonical
# scheduler's TERMINAL_STATUSES (services/scheduler.py). The A lineage's
# scheduler used "complete"; the canonical status machine finishes runs as
# "completed" and also admits "failed" -- with "complete" in this set the
# _wait_run poll never recognized a completed run (HTTP 504 after 120s).
_WAIT_TIMEOUT_S = 120.0
_WAIT_POLL_S = 0.05


class _EngineThread:
    """Run callables on one dedicated thread, synchronously.

    Same pattern as agent_api._SameThreadExecutor (kept local so this
    service does not depend on another service's private helper): the
    SwarmEngine's sqlite-backed stores are thread-affine, so every
    engine-touching call from any caller thread is funneled here;
    exceptions propagate to the caller.
    """

    def __init__(self, name: str = "remor-intent-engine") -> None:
        self._q: "queue.Queue" = queue.Queue()
        self._t = threading.Thread(target=self._loop, daemon=True, name=name)
        self._t.start()

    def _loop(self) -> None:
        while True:
            fn, ev, box = self._q.get()
            try:
                box.append(("ok", fn()))
            except BaseException as ex:  # noqa: BLE001 - must propagate
                box.append(("err", ex))
            ev.set()

    def run(self, fn: Callable[[], T]) -> T:
        ev = threading.Event()
        box: List[Any] = []
        self._q.put((fn, ev, box))
        ev.wait()
        status, payload = box[0]
        if status == "err":
            raise payload
        return payload


class IntentDispatchService:
    """NL intent dispatch bound to a dedicated engine + the real metering."""

    def __init__(self, base_dir: str, scheduler: Any, metering: Any) -> None:
        os.makedirs(base_dir, exist_ok=True)
        self.base_dir = base_dir
        self.db_path = os.path.join(base_dir, "intent.db")
        self.scheduler = scheduler
        self.metering = metering
        # Full service map, set by http_adapter.build_services AFTER
        # construction (the dict contains this service itself, so it can
        # only be attached after the dict exists). Chat/ambiguous turns
        # are answered via chat_handler.answer(text, services); when this
        # is None the handler gets a minimal map of what this service
        # directly holds.
        self.services: Optional[Dict[str, Any]] = None
        self._thread = _EngineThread()
        self._eng: Any = None
        self._router: Any = None
        self._dispatcher: Any = None

    # -- engine lifecycle (all on the engine thread) ----------------------
    def _ensure(self) -> Tuple[Any, Any, Any]:
        """Boot (engine, router, dispatcher) once, on the engine thread."""
        def _boot():
            if self._eng is None:
                from swarm_engine.core.engine import SwarmEngine
                from swarm_engine.synthesis.intent_router import IntentRouter
                from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher
                eng = SwarmEngine(db_path=self.db_path)
                router = IntentRouter(eng)
                self._eng = eng
                self._router = router
                self._dispatcher = NLToolDispatcher(eng, router)
            return self._eng, self._router, self._dispatcher
        return self._thread.run(_boot)

    # -- machinery access (engine thread) ----------------------------------
    def execute_for_run(self, text: Any, args: Any,
                        producer: Optional[str]) -> Any:
        """Run one NL dispatch; called by the scheduler worker for
        kind='intent_dispatch' runs. Returns the DispatchResult object
        (the scheduler stores it via .as_dict())."""
        _, _, dispatcher = self._ensure()
        return self._thread.run(
            lambda: dispatcher.dispatch(text, args, producer=producer))

    def route(self, text: str) -> Any:
        """Route only (no execution) -- for tests and operators."""
        _, router, _ = self._ensure()
        return self._thread.run(lambda: router.route(text))

    def dispatch_direct(self, text: str, args: Any = None,
                        producer: Optional[str] = None) -> Any:
        """Dispatch without the scheduler -- for causal machinery tests
        (route -> tamper -> dispatch TOCTOU probes)."""
        _, _, dispatcher = self._ensure()
        return self._thread.run(
            lambda: dispatcher.dispatch(text, args, producer=producer))

    def dispatch_with_route(self, route: Any, text: str, args: Any = None,
                            producer: Optional[str] = None) -> Any:
        """Execute a pre-obtained RouteResult through the dispatcher's
        invocation-time re-verification + execution path. Deterministic
        TOCTOU machinery tests only (route -> tamper/quarantine ->
        dispatch); the HTTP path always re-routes via dispatch()."""
        _, _, dispatcher = self._ensure()
        # (merge note: the backend_ff lineage named this method
        # _dispatch_routed(route, text, args, producer); the loopwiring
        # lineage renamed it _dispatch_validated(text, cap_id, args,
        # producer, route_via, route_score) with identical TOCTOU
        # semantics -- invocation-time re-verification, argument
        # validation, Composer execution, audit record. Call the merged
        # name with the route's fields.)
        return self._thread.run(
            lambda: dispatcher._dispatch_validated(
                text, route.capability_id, args, producer,
                route_via=route.via, route_score=route.score))

    def admit_capability(self, goal: str, plan: Dict[str, Any],
                         name: Optional[str] = None) -> Dict[str, Any]:
        """Admit a plan through the engine's real AdmissionController."""
        eng, _, _ = self._ensure()

        def _admit():
            res = eng.admission.admit(goal, plan, name=name)
            return {
                "ok": bool(res.ok),
                "verdict": res.verdict,
                "capability_id": res.capability_id,
                "reasons": list(res.reasons or []),
                "stage": res.stage,
            }
        out = self._thread.run(_admit)
        return out

    def bind_goal(self, goal: str, capability_id: str) -> None:
        eng, _, _ = self._ensure()
        self._thread.run(
            lambda: eng.capabilities.bind_goal(goal, capability_id))

    def issue_grant(self, effect: str, pattern: str,
                    note: str = "") -> str:
        """Issue an effect grant through the engine's own Governor (the
        engine's oracle handle is the authority -- the Governor's
        documented default)."""
        from swarm_engine.primitives.core import Effect
        eng, _, _ = self._ensure()

        def _grant():
            g = eng.governor.grant(Effect(effect), pattern, note=note)
            return g.note
        return self._thread.run(_grant)

    def quarantine_capability(self, capability_id: str,
                              reason: str) -> Dict[str, Any]:
        """Quarantine across the tri-system via the real
        integrity.quarantine_everywhere path."""
        from swarm_engine.synthesis import integrity as _integrity
        eng, _, _ = self._ensure()
        return self._thread.run(
            lambda: _integrity.quarantine_everywhere(
                eng, capability_id, reason=reason))

    def get_capability(self, capability_id: str) -> Optional[Dict[str, Any]]:
        eng, _, _ = self._ensure()

        def _get():
            rec = eng.capabilities.get(capability_id)
            return rec.as_dict() if rec is not None else None
        return self._thread.run(_get)

    # -- audit history (short-lived sqlite reads; any thread) --------------
    def dispatch_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        _, _, dispatcher = self._ensure()
        return dispatcher.history(limit=int(limit))

    # -- HTTP-facing entry point -------------------------------------------
    def _wait_run(self, run_id: str,
                  timeout: float = _WAIT_TIMEOUT_S) -> Optional[Dict[str, Any]]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            rec = self.scheduler.get_run(run_id)
            if rec is None:
                return None
            if rec["status"] in _TERMINAL_RUN:
                return rec
            time.sleep(_WAIT_POLL_S)
        return None

    def intent_dispatch(self, body: Optional[Dict[str, Any]]
                        ) -> Tuple[int, Dict[str, Any]]:
        """POST /api/intent/dispatch handler. Returns (http_code, payload).

        Conversational-minimum gate (Worker 1): the discriminator
        classifies the text BEFORE the metering gate. A chat-mode or
        ambiguous turn NEVER reaches guarded_submit -- no task slot is
        consumed, no scheduler_runs row is created -- and is instead
        answered by the chat handler (Worker 2's
        swarm_engine.services.chat_handler). Ambiguous turns go to the
        chat handler too, which asks a clarifying question. Only task
        turns continue to the metering gate below, unchanged.

        The metering gate then runs through the real guarded-submit path;
        a refused gate yields the REAL typed quota payload (HTTP 429) and
        the dispatch is never routed. An accepted gate submits a real
        scheduler run whose work IS the NL dispatch; this call waits for
        the run and returns the dispatch result in the GUI's shape.
        """
        body = body or {}
        text = body.get("text")
        # Dictation-artifact normalization FIRST: strip leading/trailing
        # junk punctuation before the text becomes a goal, before the
        # discriminator sees it, and before the metering gate. Everything
        # downstream (audit rows, scheduler metadata/goal, refusal and
        # failure echoes, the chat handler's input) sees the clean text.
        # Inner content is byte-preserved; only the edges are stripped.
        text = _normalize_message_text(text)
        if not isinstance(text, str) or not text.strip():
            return 400, {"ok": False,
                         "error": "text is required (non-empty string)"}

        # -- conversational-minimum gate: BEFORE the metering gate ------
        cls = _discriminator.classify(text)
        mode = cls.get("mode")
        if mode in ("chat", "ambiguous"):
            try:
                from swarm_engine.services import chat_handler as _chat
            except ImportError as exc:
                # Loud, not silent: a conversational turn must never be
                # re-routed into the task pipeline just because the chat
                # handler module is absent.
                raise RuntimeError(
                    "swarm_engine.services.chat_handler is missing: this "
                    "turn classified as %r (conversational minimum) and "
                    "must be answered by the chat handler; refusing to "
                    "route it into the task pipeline" % (mode,)) from exc
            services = self.services or {
                "scheduler": self.scheduler,
                "metering": self.metering,
                "intent": self,
            }
            # The handler's contract: ambiguous=True makes it ask a
            # clarifying question (zero store reads, zero actions).
            return 200, _chat.answer(text, services,
                                     ambiguous=(mode == "ambiguous"))

        args = body.get("args")
        producer = body.get("producer") or "http:operator"

        gate = self.metering.guarded_submit(
            text,
            metadata={"kind": "intent_dispatch", "text": text,
                      "args": args, "producer": producer})
        if not gate.get("ok"):
            # Real quota refusal: typed limit payload, HTTP 429. The
            # dispatch was never submitted, never routed, never executed.
            return 429, gate

        run_id = gate["run_id"]
        rec = self._wait_run(run_id)
        if rec is None:
            return 504, {"ok": False,
                         "error": "dispatch run did not finish in time; "
                                  "poll /api/runs/%s for the real outcome"
                                  % run_id,
                         "run_id": run_id}
        if rec["status"] == "completed" and rec.get("outcome"):
            # (phase 4c convergence repair: backend_ff's lineage wrote
            # "complete"; the canonical status machine finishes runs as
            # "completed" -- without this the success branch never fired
            # and every completed task turn returned HTTP 500.)
            # The outcome IS the dispatch result dict
            # ({ok, result, capability_id, dispatch_id, route_via,
            #   refusal, reasons}); a refusal is body data -> HTTP 200.
            return 200, rec["outcome"]
        return 500, {"ok": False,
                     "error": rec.get("error")
                     or f"dispatch run ended as {rec['status']} "
                        "without a result",
                     "run_id": run_id}


def build_intent_dispatch_service(base_dir: str, scheduler: Any,
                                  metering: Any) -> IntentDispatchService:
    """Construct the service and register its executor on the scheduler so
    kind='intent_dispatch' runs execute the real NL dispatch as their
    work."""
    svc = IntentDispatchService(base_dir, scheduler, metering)
    scheduler.register_intent_executor(svc.execute_for_run)
    return svc
