#!/usr/bin/env python3
"""AUTO-ROUTE-1: the hidden execution hierarchy — one chat interface,
two FRM-governed brains, silent escalation.

James's standing principle (2026-10-01): "REMOR should optimize for
perceived continuity, not computational uniformity." The user experiences
one REMOR; the architecture handles the rest.

Layers:
  fast brain  = interaction layer. GovernedStudent (DISTILL-2 persistent
                server + FRM-STUDENT-1 governance). Immediate, ~seconds.
  deep brain  = reasoning layer. GrantedCognitionProvider with
                substrate_kind="llm" (LLM-SUBSTRATE-1) + Qwen3Teacher
                (Qwen3-8B). Absorbs expensive work, ~minutes on this host.
  controller  = this router. Decides answer-now / escalate / defer /
                return-stronger. The user never chooses a brain.
  resources   = invisible economics. Both paths need their own FrmGrant;
                both refuse/defer without one; both charge actual seconds.

Escalation policy (defined and defended in the mission report):
  * Explicit: the prompt invokes deep-think ("think hard", "think
    carefully", "deep think", "reason carefully", "think this through"),
    or the caller passes think_hard=True. Always escalates.
  * Heuristic: surface signals only — long prompt (>300 chars),
    deep-reasoning interrogatives (why/how/explain/analyze/compare/prove/
    derive/evaluate), multi-part structure (>=2 '?' or >=3 sentences),
    explicit work-showing requests ("step by step", "show your work").
    Escalates on >=2 signals. This is a handful of transparent rules,
    not a learned classifier; no difficulty classifier exists anywhere
    in the runtime (verified: only vendored NLTK + acquisition gap-node
    heuristic fields, neither of which routes inference).

"Materially better" (operational, not vibes):
  deep_ok AND not deep_degenerate AND (fast_not_ok OR diff_ratio >= 0.70)
  where deep_degenerate = empty text / error / refusal, and diff_ratio
  is the symmetric token difference over the union of normalized tokens.
  Threshold calibration (measured 2026-10-01, not guessed): a pure
  rephrase pair scored 0.625; a genuine student-vs-teacher depth pair
  (21-token student vs 49-token teacher answer on the same economics
  prompt) scored 0.804. 0.70 sits between, biased toward serving the
  deeper result when escalation was judged warranted. The gate prints
  every measured ratio; if production pairs land in the ambiguous band,
  the threshold gets re-anchored on data, not vibes.

Anti-masquerade (load-bearing): when escalation is triggered, the served
fast answer carries the provisional label in its actual text. It never
poses as final while deeper work is warranted.

Concurrency note: on this 2-core host the deep path runs sequentially
after the fast answer is served (the 8B saturates both cores). True
concurrent fast+deep is architecturally supported and classified BOUNDED
by host cores, not by this router.

Ownership: distill1/router.py (AUTO-ROUTE-1). Report, do not land.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from governed_student import GovernedStudent

# GrantedCognitionProvider lives in the canonical runtime tree; the router
# consumes it, never modifies it.
from runtime.core.microcontroller.granted_cognition import (
    GrantedCognitionProvider,
)
from swarm_engine.curiosity.frm.grant import FrmGrant


# ---------------------------------------------------------------------------
# served-output labels (user-visible; the anti-masquerade contract)
# ---------------------------------------------------------------------------

PROVISIONAL_LABEL = "[provisional — deeper reasoning running]"
DEEPER_LABEL = "[deeper result]"
FINAL_LABEL = "[final]"

THINK_HARD_PHRASES = (
    "think hard",
    "think carefully",
    "deep think",
    "reason carefully",
    "think this through",
)

_DEEP_WORDS = (
    "why", "how", "explain", "analyze", "compare", "prove",
    "derive", "evaluate", "justify",
)
_WORK_SHOWING = ("step by step", "show your work", "work through")

_SENT_SPLIT = re.compile(r"[.!?]+")
_WORD_SPLIT = re.compile(r"[a-z0-9']+")


class AutoRouter:
    """One chat interface over the fast and deep FRM-governed paths."""

    def __init__(self, student: GovernedStudent,
                 deep: GrantedCognitionProvider, *,
                 mc_id: str = "auto-route",
                 enforcement_state: Optional[Callable[[], str]] = None,
                 clock=time.monotonic) -> None:
        self._student = student
        self._deep = deep
        self._mc_id = mc_id
        # Liveness check for "grant revoked mid-escalation": consulted
        # after the fast answer, before the deep path runs. A real branch
        # in the execution path, not a test hook in disguise.
        self._enforcement_state = enforcement_state or (lambda: "RUNNING")
        self._clock = clock

    # -- escalation policy -------------------------------------------------

    @staticmethod
    def escalation_signals(prompt: str) -> List[str]:
        """Transparent surface signals. Returned so the gate can show
        exactly why a prompt did or did not escalate."""
        p = (prompt or "")
        low = p.lower()
        signals: List[str] = []
        if any(ph in low for ph in THINK_HARD_PHRASES):
            signals.append("explicit:deep-think-phrase")
        if len(p) > 300:
            signals.append(f"long-prompt:{len(p)}chars")
        hits = sorted({w for w in _DEEP_WORDS
                       if re.search(rf"\b{w}\b", low)})
        if hits:
            signals.append("deep-interrogatives:" + ",".join(hits))
            if len(hits) >= 3:
                # Three distinct deep-reasoning interrogatives is itself
                # a strong depth marker (e.g. "why ... how ... compare").
                signals.append("deep-interrogatives:multiple")
        questions = p.count("?")
        sentences = [s for s in _SENT_SPLIT.split(p) if s.strip()]
        if questions >= 2 or len(sentences) >= 3:
            signals.append(f"multi-part:q{questions}s{len(sentences)}")
        if any(w in low for w in _WORK_SHOWING):
            signals.append("explicit:show-work")
        return signals

    @staticmethod
    def should_escalate(prompt: str,
                        think_hard: bool = False) -> Tuple[bool, List[str]]:
        """(escalate, reasons). Explicit invocation always escalates;
        otherwise >=2 heuristic signals are required."""
        signals = AutoRouter.escalation_signals(prompt)
        if think_hard or "explicit:deep-think-phrase" in signals:
            reasons = ["explicit deep-think invocation"]
            if think_hard:
                reasons.append("caller think_hard=True")
            reasons += [s for s in signals
                        if s != "explicit:deep-think-phrase"]
            return True, reasons
        if len(signals) >= 2:
            return True, [f"heuristic:{s}" for s in signals]
        return False, signals

    # -- materially-better ---------------------------------------------------

    @staticmethod
    def diff_ratio(a: str, b: str) -> float:
        """Symmetric token difference over union. 0.0 = identical,
        1.0 = nothing shared."""
        ta = set(_WORD_SPLIT.findall((a or "").lower()))
        tb = set(_WORD_SPLIT.findall((b or "").lower()))
        if not ta and not tb:
            return 0.0
        return len(ta ^ tb) / len(ta | tb)

    # -- the single chat interface ------------------------------------------

    def chat(self, prompt: str, *, student_grant: Any,
             deep_grant: Any = None,
             think_hard: bool = False,
             deep_mc_id: Optional[str] = None) -> Dict[str, Any]:
        """One turn through the hidden hierarchy. Returns fast /
        escalation / deep / served sections. Never raises on governance
        grounds; every refusal/deferral/failure is a real reason in the
        output, with zero phantom charges."""
        escalate, reasons = self.should_escalate(prompt,
                                                 think_hard=think_hard)

        fast = self._student.turn(prompt, frm_grant=student_grant)
        fast_out: Dict[str, Any] = {
            "ok": fast["ok"],
            "provisional": bool(escalate and fast["ok"]),
        }
        if fast["ok"]:
            fast_out.update({
                "text": fast["text"],
                "provenance": fast["provenance"],
                "charged_s": fast["charged_s"],
                "wall_s": fast["wall_s"],
            })
            # Anti-masquerade: the label is in the served text itself.
            fast_out["served_text"] = (
                f"{PROVISIONAL_LABEL} {fast['text']}" if escalate
                else f"{FINAL_LABEL} {fast['text']}")
        else:
            fast_out.update({
                "error": fast["error"],
                "charged_s": fast.get("charged_s", 0.0),
            })
            fast_out["served_text"] = (
                f"{FINAL_LABEL} I couldn't answer that: {fast['error']}")

        escalation: Dict[str, Any] = {
            "triggered": escalate,
            "reasons": reasons,
            "mode": ("explicit" if (think_hard or any(
                "explicit" in r for r in reasons))
                     else "heuristic") if escalate else "none",
        }

        deep_out: Dict[str, Any] = {"attempted": False}
        served: Dict[str, Any]

        if not escalate:
            served = {"text": fast_out["served_text"],
                      "label": FINAL_LABEL,
                      "via": "fast",
                      "materially_better": False,
                      "diff_ratio": None}
            return {"ok": fast["ok"], "fast": fast_out,
                    "escalation": escalation, "deep": deep_out,
                    "served": served}

        # -- escalation: live enforcement check BEFORE the deep path ------
        live_state = self._enforcement_state()
        if live_state != "RUNNING":
            deep_out.update({
                "ok": False,
                "error": (f"route_refused:escalation_blocked: enforcement "
                          f"state is {live_state!r} (was RUNNING at fast "
                          f"answer); deep path not attempted, zero charge"),
                "charged_s": 0.0,
            })
            served = {"text": fast_out["served_text"],
                      "label": PROVISIONAL_LABEL,
                      "via": "fast",
                      "materially_better": False,
                      "diff_ratio": None,
                      "note": "deeper result blocked: " + deep_out["error"]}
            return {"ok": fast["ok"], "fast": fast_out,
                    "escalation": escalation, "deep": deep_out,
                    "served": served}

        if deep_grant is None:
            deep_out.update({
                "ok": False,
                "error": ("route_refused:no_deep_grant: escalation "
                          "triggered but no deep FrmGrant was provided; "
                          "deep path not attempted, zero charge"),
                "charged_s": 0.0,
            })
            served = {"text": fast_out["served_text"],
                      "label": PROVISIONAL_LABEL,
                      "via": "fast",
                      "materially_better": False,
                      "diff_ratio": None,
                      "note": "deeper result unavailable: " +
                              deep_out["error"]}
            return {"ok": fast["ok"], "fast": fast_out,
                    "escalation": escalation, "deep": deep_out,
                    "served": served}

        # -- deep path under its own grant --------------------------------
        deep_out["attempted"] = True
        mc = deep_mc_id or self._mc_id
        t0 = self._clock()
        try:
            res = self._deep.request_cognition(
                mc_id=mc, prompt=prompt,
                context={"frm_grant": deep_grant,
                         "purpose": "auto-route-deep",
                         "target_profile": "deep-reasoning"})
        except Exception as exc:  # noqa: BLE001 -- fail closed, zero charge
            deep_out.update({
                "ok": False,
                "error": (f"deep_failed:transport: {type(exc).__name__}: "
                          f"{exc}"),
                "charged_s": 0.0,
                "wall_s": self._clock() - t0,
            })
        else:
            deep_out["wall_s"] = self._clock() - t0
            if res.ok:
                deep_out.update({
                    "ok": True,
                    "text": res.text,
                    "provenance": res.provenance,
                    "charged_s": self._deep.grant_consumed_s(
                        deep_grant.grant_id) if isinstance(
                            deep_grant, FrmGrant) else 0.0,
                })
            else:
                # Grant refusal / deferral from the deep inlet: the real
                # reason, zero charge by construction of that inlet.
                deep_out.update({
                    "ok": False,
                    "error": res.error,
                    "charged_s": 0.0,
                })

        # -- materially-better verdict ------------------------------------
        deep_text = deep_out.get("text") or ""
        degenerate = (not deep_out.get("ok")) or (not deep_text.strip())
        diff = (self.diff_ratio(fast_out.get("text", ""), deep_text)
                if not degenerate else None)
        better = (not degenerate and
                  (not fast["ok"] or (diff is not None and diff >= 0.70)))

        if better:
            deep_out["served_text"] = f"{DEEPER_LABEL} {deep_text}"
            served = {"text": deep_out["served_text"],
                      "label": DEEPER_LABEL,
                      "via": "deep",
                      "materially_better": True,
                      "diff_ratio": round(diff, 3) if diff is not None
                      else None}
        else:
            reason = ("deep path degenerate" if degenerate
                      else f"not materially different (diff_ratio="
                           f"{diff:.3f} < 0.70)")
            served = {"text": fast_out["served_text"],
                      "label": (PROVISIONAL_LABEL if fast["ok"]
                                else FINAL_LABEL),
                      "via": "fast",
                      "materially_better": False,
                      "diff_ratio": (round(diff, 3)
                                     if diff is not None else None),
                      "note": f"deeper result withheld: {reason}"}

        return {"ok": fast["ok"] or better, "fast": fast_out,
                "escalation": escalation, "deep": deep_out,
                "served": served}
