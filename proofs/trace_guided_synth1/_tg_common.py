"""Shared battery infrastructure for TRACE-GUIDED-SYNTH-1 (no inference here).

Mirrors BORROW-NATIVE-1's common.py patterns: real Qwen3-8B teacher, FrmGrant
borrowing, named native refusals, checkpointed demos.
"""
import sys
import os
import glob
import json
import time

PROOF_DIR = "/home/hatch/workspace/worktrees/warm-grower-loop/proofs/trace_guided_synth1"
WT = "/home/hatch/workspace/worktrees/warm-grower-loop"
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

QWEN3_REV = "7c41481f57cb95916b40956ab2f0b139b296d974"
MODELS_DIR = "/home/hatch/workspace/models/qwen3-8b"
LLAMA_CLI = ("/home/hatch/workspace/tools/llama.cpp-b11284/"
             "llama-b11284/llama-cli")

PURPOSES = ("step_verify", "shout_verify")
REFUSAL_NAME = "no_task_machinery"


class CheckFailed(Exception):
    pass


def check(sec, name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"[{sec}] {status:4} {name} {str(detail)[:120]}", flush=True)
    if not cond:
        raise CheckFailed(f"{sec}/{name}: {detail}")


# ---------------------------------------------------------------- native --

from swarm_engine.core.microcontroller.substrate import (
    CognitionProvider, CognitionResult)
from swarm_engine.core.microcontroller.granted_cognition import NativeRefusal


class TraceGuidedNativeV1(CognitionProvider):
    """Pre-distillation native tier: REMOR owns no machinery for either
    purpose, so both task classes are refused by name. The refusal IS the
    capability gap."""

    def request_cognition(self, *, mc_id, prompt, context):
        purpose = (context or {}).get("purpose")
        if purpose in PURPOSES:
            raise NativeRefusal(
                REFUSAL_NAME,
                f"native tier has no {purpose} machinery; "
                "the task class is not an owned technique")
        raise NativeRefusal("unknown_purpose",
                            f"no native handler for {purpose!r}")


class RaisingTeacher:
    """Teacher slot that explodes if the borrow path ever runs."""

    model_id = "raising-stub"
    revision = "must-never-be-called"

    def estimate_cost_s(self, prompt):
        raise AssertionError("teacher.estimate_cost_s called: borrow attempted")

    def complete(self, prompt, context):
        raise AssertionError("teacher.complete called: borrow attempted")


# ---------------------------------------------------------------- teacher -

def gguf_path():
    hits = sorted(glob.glob(os.path.join(MODELS_DIR, "*.gguf")))
    assert hits, f"no .gguf under {MODELS_DIR}"
    return hits[0]


_TEACHER = None


def real_teacher():
    """The real Qwen3-8B teacher (singleton per process)."""
    global _TEACHER
    if _TEACHER is None:
        from swarm_engine.core.microcontroller.qwen3_teacher import Qwen3Teacher
        _TEACHER = Qwen3Teacher(
            gguf_path=gguf_path(), llama_cli=LLAMA_CLI,
            threads=2, context_size=512, max_new_tokens=96)
    return _TEACHER


def make_grant(budget_s, note, enforcement="RUNNING"):
    from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord
    return FrmGrant.issue(
        domain="curiosity", epoch_id=1, epoch_s=3600.0,
        budget_s=budget_s, max_concurrent=1,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        lent=False, lending=LendingRecord(0.0, 0),
        enforcement_state_at_issue=enforcement,
        issued_at=time.time(), note=note)


def fresh_substrate(budget_s=4000.0):
    from swarm_engine.core.microcontroller.substrate import (
        MicrocontrollerSubstrate)
    sub = MicrocontrollerSubstrate()
    sub.register_loop("run", budget_s=budget_s)
    sr = sub.spawn("run", purpose="trace-guided-synth-1", budget_s=budget_s)
    assert sr.ok, f"spawn refused: {sr.refusal}"
    return sub, sr.mc.mc_id


# ---------------------------------------------------------------- prompts --

def step_prompt(a, b, claimed):
    return (
        "Verify the claimed sum. First recompute the sum yourself, "
        "then compare.\n"
        f"The claim to check is that {a} + {b} = {claimed}.\n"
        "Reply in exactly this format:\n"
        "recompute: <your work, e.g. 3 + 4 = 7>\n"
        "compare: <your comparison, e.g. 7 == 8>\n"
        "result: <true or false>")


def shout_prompt(text, claimed):
    return (
        "Verify the claimed ALL-CAPS version of the text. First convert "
        "the text to uppercase yourself, then compare.\n"
        f'The text is "{text}" and the claimed ALL-CAPS version is '
        f'"{claimed}".\n'
        "Reply in exactly this format:\n"
        "shout: <the text in uppercase>\n"
        "compare: <your comparison, e.g. HELLO WORLD == HELLO WORLD>\n"
        "result: <true or false>")


def ground_truth_step(a, b, claimed):
    return (a + b) == claimed


def ground_truth_shout(text, claimed):
    return text.upper() == claimed


# ---------------------------------------------------------------- tasks ---

# 16 tasks per technique; the battery selects the first 12 verified demos
# with a consistent trace shape. Last 4 tasks use novel magnitudes/texts so
# the held-out split (last third) tests generalization.
STEP_TASKS = [
    (2, 4, 6), (2, 4, 7), (3, 4, 7), (3, 4, 8),
    (10, 15, 25), (10, 15, 24), (10, 14, 24), (10, 14, 25),
    (7, 3, 10), (7, 3, 11), (12, 8, 20), (12, 8, 19),
    (50, 25, 75), (50, 25, 74), (9, 9, 18), (9, 9, 19),
]

SHOUT_TASKS = [
    ("hello world", "HELLO WORLD"),
    ("hello world", "HELLO WOLRD"),
    ("remor is growing", "REMOR IS GROWING"),
    ("remor is growing", "REMOR IS GROWIN"),
    ("trace guided synthesis", "TRACE GUIDED SYNTHESIS"),
    ("trace guided synthesis", "TRACE GUIDED SYNTHESYS"),
    ("seven plus eight", "SEVEN PLUS EIGHT"),
    ("seven plus eight", "SEVEN PLUS EIGHTS"),
    ("distillation wall", "DISTILLATION WALL"),
    ("distillation wall", "DISTILLATION WALE"),
    ("the quick brown fox", "THE QUICK BROWN FOX"),
    ("the quick brown fox", "THE QUICK BROWN FOXX"),
    ("pack my box with five dozen", "PACK MY BOX WITH FIVE DOZEN"),
    ("pack my box with five dozen", "PACK MY BOX WITH FIVE DOZEM"),
    ("synthesis generalizes", "SYNTHESIS GENERALIZES"),
    ("synthesis generalizes", "SYNTHESIS GENERALIZEZ"),
]

TECHNIQUES = {
    "step_verify": {
        "purpose": "step_verify",
        "tasks": STEP_TASKS,
        "prompt": lambda t: step_prompt(*t),
        "input": lambda t: {"a": t[0], "b": t[1], "claimed": t[2]},
        "ground_truth": lambda t: ground_truth_step(*t),
        "objective": "verify whether the claimed sum is correct",
        "technique": "recompute-and-compare",
        "gap": "cannot verify sums",
    },
    "shout_verify": {
        "purpose": "shout_verify",
        "tasks": SHOUT_TASKS,
        "prompt": lambda t: shout_prompt(*t),
        "input": lambda t: {"text": t[0], "claimed": t[1]},
        "ground_truth": lambda t: ground_truth_shout(*t),
        "objective": "verify whether the claimed ALL-CAPS text is correct",
        "technique": "uppercase-and-compare",
        "gap": "cannot verify uppercase claims",
    },
}


def engine_db(technique):
    return os.path.join(PROOF_DIR, f"tg_{technique}_engine.db")


def attempts_path(technique):
    return os.path.join(PROOF_DIR, f"tg_{technique}_attempts.json")


def selected_path(technique):
    return os.path.join(PROOF_DIR, f"tg_{technique}_demos.json")


def distill_result_path(technique):
    return os.path.join(PROOF_DIR, f"tg_{technique}_distill.json")
