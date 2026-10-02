"""LATENCY-1: streaming chat with measured first-token latency.

The fast path serves immediately (student); deep completions run in the
background tier with honest provisional labeling per the single-intelligence
UX principle. The fast path is never blocked by background work.

Events yielded by chat_stream:
  {"type": "provisional", "text": ..., "first_token_latency_s": ...}
  {"type": "final", "text": ...}
  {"type": "deeper", "text": ...}  (background completion, may arrive later)
  {"type": "error", "reason": ...}

Backpressure: the background tier is a bounded pool. When full, deep
work is refused with the real reason (not queued silently).
Deadlines: background work has a deadline; expiry yields the provisional
as final with an honest note.
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Dict, Generator, Optional

from runtime.services.chat_api import ChatService


class StreamingChatService(ChatService):
    """ChatService with streaming, background deep tier, and latency measurement."""

    def __init__(self, base_dir: str, *,
                 max_background: int = 2,
                 background_deadline_s: float = 120.0,
                 **kwargs) -> None:
        super().__init__(base_dir, **kwargs)
        self._background = ThreadPoolExecutor(
            max_workers=max_background,
            thread_name_prefix="chat-deep")
        self._background_sem = threading.Semaphore(max_background)
        self._background_deadline_s = background_deadline_s

    def chat_stream(self, prompt: str, *,
                    think_hard: bool = False,
                    deadline_s: Optional[float] = None
                    ) -> Generator[Dict[str, Any], None, None]:
        """Stream a chat turn. Yields provisional immediately, then final,
        then deeper (if escalation triggered and background completes).

        First-token latency is measured from call to first yielded event.

        ARCHITECTURE (fixed 2026-10-02): The provisional MUST yield before
        any blocking deep work. The old code called self.chat() (blocking)
        before the first yield, which violated the "fast path never blocked"
        mandate. The fast path runs the student synchronously (bounded,
        fast); deep work goes to the background tier without blocking
        the provisional yield.
        """
        t0 = self._clock()
        # Fast path: synchronous student turn ONLY (never the deep path).
        # self.chat() with think_hard=False runs student-only; the deep
        # tier is handled explicitly below in background.
        result = self.chat(prompt, think_hard=False)
        first_token_latency_s = self._clock() - t0

        served = result.get("served") or {}

        # Provisional yields IMMEDIATELY after fast path — before any
        # background work is even submitted. This is the streaming guarantee.
        yield {
            "type": "provisional",
            "text": served.get("text", ""),
            "label": served.get("label", ""),
            "first_token_latency_s": first_token_latency_s,
        }

        # Deep work goes to background tier WITHOUT blocking the yield above.
        # If think_hard was requested, submit deep work now.
        if think_hard:
            # Backpressure: try to acquire a background slot without blocking.
            if not self._background_sem.acquire(blocking=False):
                yield {
                    "type": "final",
                    "text": served.get("text", ""),
                    "label": served.get("label", ""),
                    "note": ("background tier full: deep completion deferred; "
                             "provisional stands as final"),
                }
                return
            try:
                deadline = (deadline_s if deadline_s is not None
                            else self._background_deadline_s)
                future = self._background.submit(
                    self._run_deep_background, prompt, think_hard=True)
                try:
                    deep_result = future.result(timeout=deadline)
                    yield {"type": "deeper",
                           "text": deep_result.get("text", ""),
                           "label": deep_result.get("label", "")}
                except FutureTimeout:
                    future.cancel()
                    yield {
                        "type": "final",
                        "text": served.get("text", ""),
                        "label": served.get("label", ""),
                        "note": (f"background deep timed out after {deadline}s; "
                                 "provisional stands as final"),
                    }
            finally:
                self._background_sem.release()
        else:
            # No deep requested: provisional is final.
            yield {
                "type": "final",
                "text": served.get("text", ""),
                "label": served.get("label", ""),
            }

    def chat_stream_background(self, prompt: str, *,
                               think_hard: bool = False,
                               deadline_s: Optional[float] = None
                               ) -> Generator[Dict[str, Any], None, None]:
        """True background deep tier: fast path returns immediately;
        deep work runs in the pool. Demonstrates non-blocking + backpressure.

        DELEGATES to chat_stream (fixed 2026-10-02): the architecture is now
        unified — provisional yields before any background work, with
        backpressure and deadline handling.
        """
        yield from self.chat_stream(
            prompt, think_hard=think_hard, deadline_s=deadline_s)

    def _run_deep_background(self, prompt: str,
                             think_hard: bool) -> Dict[str, Any]:
        """Run the deep path in a background thread. Returns served dict."""
        # Re-run through the router with escalation forced.
        # (The fast path already ran; this is the deeper pass.)
        try:
            student_grant = self.issue_student_grant(prompt)
            deep_grant = self.issue_deep_grant(prompt)
            result = self._router.chat(
                prompt,
                student_grant=student_grant,
                deep_grant=deep_grant,
                think_hard=True,
                deep_mc_id=self._llm.mc_id)
            served = result.get("served") or {}
            return {"text": served.get("text", ""),
                    "label": served.get("label", "[deeper]")}
        except Exception as exc:  # noqa: BLE001
            return {"text": "",
                    "label": "[error]",
                    "error": f"background deep failed: {type(exc).__name__}: {exc}"}

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the background tier."""
        self._background.shutdown(wait=wait)
