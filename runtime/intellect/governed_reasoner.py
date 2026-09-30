"""runtime/intellect/governed_reasoner.py

The governed facade for legacy ExternalReasoner sockets (U-5).

James, 2026-09-30: every legacy reasoning entry point through the
governed path with no private FRM bypass.

IntellectualEngine keeps its ExternalReasoner call sites, but the engine
constructor wraps any provided reasoner in GovernedExternalReasoner.
The facade routes the call through the ONE cognition inlet --
substrate.cognize(mc_id, prompt, context) -- so it is grant-gated,
charged, and telemetered exactly like any microcontroller cognition.

Two deliberate, documented judgments:

1. The wrapped ExternalReasoner is RETIRED as a call target: it is
   never called. Its old role -- "the injection point for a real
   reasoning model" -- belongs in the v3 architecture to the governed
   provider's teacher slot (StubTeacher now, Qwen3 via QWEN3-ACQUIRE-1).
   The facade preserves the ExternalReasoner ABC so the engine's call
   sites do not change, and so nothing can attach a raw reasoner that
   the engine would call directly (the private bypass U-5 closes).
   (A RecordingExternalReasoner in tests proves the point: it records
   zero direct calls while the governed path answers.)

2. The native_refusal context is attested by the engine itself: the
   engine consults external reasoning exactly where its own mechanisms
   cannot produce -- the ABC's own docstring calls this "an
   unimplemented capability", the honest boundary. The refusal name is
   "intellect_engine:internal_generation_insufficient".

Fail-closed contract (mirrors NoExternalReasoner): when there is no
substrate, no mc_id, or no FrmGrant in context, every method returns
the empty default and the engine falls back to its internal generation
exactly as it does today. The engine's current call sites pass no
governance context, so attaching a reasoner today changes nothing --
the governed path activates only when the hosting layer supplies
mc_id + FrmGrant in context (executive hosting; SEAM-WIRE-1's seams).
Missing governance never degrades to an unguarded model call.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from swarm_engine.core.microcontroller.substrate import (
    MicrocontrollerSubstrate,
)
from swarm_engine.intellect.reasoner import ExternalReasoner


class GovernedExternalReasoner(ExternalReasoner):
    """ExternalReasoner ABC shape, governed inlet inside.

    Constructed by IntellectualEngine around any provided real
    reasoner; can also be constructed directly with a substrate that
    has a GrantedCognitionProvider installed.
    """

    #: Native-refusal name attested by the engine for external consults.
    NATIVE_REFUSAL = "intellect_engine:internal_generation_insufficient"

    def __init__(self, reasoner: ExternalReasoner, *,
                 substrate: Optional[MicrocontrollerSubstrate] = None
                 ) -> None:
        self._reasoner = reasoner
        self._substrate = substrate

    @property
    def wrapped(self) -> ExternalReasoner:
        """The retired reasoner (never called; interface compatibility)."""
        return self._reasoner

    def _call(self, method: str, context: Optional[Dict[str, Any]],
              prompt: str, empty: Any) -> Any:
        ctx = dict(context or {})
        mc_id = ctx.get("mc_id")
        # Fail closed: no substrate, no mc, or no grant means the engine's
        # internal generation runs exactly as under NoExternalReasoner.
        if self._substrate is None or mc_id is None \
                or ctx.get("frm_grant") is None:
            return empty
        ctx.setdefault("purpose", f"intellect:{method}")
        ctx.setdefault("native_refusal", self.NATIVE_REFUSAL)
        res = self._substrate.cognize(mc_id, prompt, ctx)
        if not res.ok:
            return empty
        return self._parse(method, res.text, empty)

    @staticmethod
    def _parse(method: str, text: str, empty: Any) -> Any:
        """Parse the governed teacher's text into the ABC's return shapes.

        The stub teacher's marked text carries no real results: parsing
        yields the empty default (the engine's internal fallback), never
        a fabricated question, hypothesis, or experiment design. Line
        parsing applies to a real teacher's structured output when one
        exists; design_experiment has no honest text->dict parse, so a
        real teacher must speak a structured protocol (future work) --
        never a guessed dict.
        """
        if text.startswith("[STUB-TEACHER"):
            return empty
        if method in ("propose_questions", "propose_hypotheses"):
            lines = [line.strip() for line in text.splitlines()
                     if line.strip()]
            return lines[:10] or empty
        return empty

    def propose_questions(self, context: Dict[str, Any]) -> List[str]:
        prompt = ("intellect:propose_questions -- external reasoning "
                  "consult via the governed cognition inlet")
        return self._call("propose_questions", context, prompt, [])

    def propose_hypotheses(self, question_text: str,
                           context: Dict[str, Any]) -> List[str]:
        prompt = ("intellect:propose_hypotheses -- external reasoning "
                  f"consult via the governed cognition inlet; "
                  f"question={question_text!r}")
        return self._call("propose_hypotheses", context, prompt, [])

    def design_experiment(self, question_text: str,
                          hypothesis_statements: List[str],
                          context: Dict[str, Any]
                          ) -> Optional[Dict[str, Any]]:
        prompt = ("intellect:design_experiment -- external reasoning "
                  f"consult via the governed cognition inlet; "
                  f"question={question_text!r}")
        return self._call("design_experiment", context, prompt, None)
