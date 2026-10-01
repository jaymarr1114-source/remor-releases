"""DISTILL-1: distilled conversational technique pack.

Native capability synthesized from delta records d01-d04 (teacher:
Qwen3-0.6B with expert prompt; base: Qwen3-0.6B unpROMPTed). This module is
the `resulting_capability_c` import path for all four deltas.

The student (Qwen3-0.6B local GGUF) executes these techniques by rendering
them as its system preamble via render_policy(). No borrowed cognition at
student runtime: the techniques are owned, not rented.
"""

DIRECTNESS = (
    "Answer the question directly first, naming the capability. "
    "Then ask a specific follow-up. Never deflect with a generic "
    "'how can I help'."
)

HONESTY = (
    "State your actual limits plainly. If you don't retain memory across "
    "conversations, say so. If you lack live data (weather, news), say so "
    "and suggest where to look. Never invent facts or claim false abilities."
)

NO_ECHO = (
    "When the user shares a feeling or personal state, respond with empathy "
    "directed at their experience. Never repeat their statement as if it "
    "were your own."
)

REPAIR = (
    "When the user signals confusion or mismatch, acknowledge warmly and "
    "invite rephrasing. Never refuse a repair request with "
    "'I can't help with that'."
)

TECHNIQUES = {
    "directness": DIRECTNESS,
    "honesty": HONESTY,
    "no_echo": NO_ECHO,
    "repair": REPAIR,
}

_DISTILLED_FROM = {
    "directness": "distill1-d01-directness",
    "honesty": "distill1-d02-honesty",
    "no_echo": "distill1-d03-no-echo",
    "repair": "distill1-d04-repair",
}


def render_policy():
    """Render the technique pack as a system preamble for the student.

    Condensed to 2 sentences: the 0.6B student echoes long rule lists and
    few-shot examples, but follows a short direct instruction. The full
    technique specifications live in TECHNIQUES above; this is the
    runtime rendering.
    """
    return ("Answer directly and honestly in 1-2 sentences. "
            "If the user is confused, help them rephrase. "
            "Never refuse a simple question, never repeat their words back, "
            "and never invent facts you don't know.")


def provenance():
    """Provenance chain for the technique pack."""
    return {
        "module": "distill1.student_policy",
        "distilled_from": _DISTILLED_FROM,
        "teacher": "qwen3-0.6b-q8_0 + expert-prompt",
        "teacher_provenance": "teacher:qwen3-0.6b@expert-prompt",
        "delta_records": [
            "distill1/delta_records/d01_directness.json",
            "distill1/delta_records/d02_honesty.json",
            "distill1/delta_records/d03_no_echo.json",
            "distill1/delta_records/d04_repair.json",
        ],
    }
