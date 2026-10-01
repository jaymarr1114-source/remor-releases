"""BORROW-NATIVE-1 shared battery infrastructure.

Task class: single-step arithmetic verification ("step_verify").
  input : {"op": "add"|"sub"|"mul", "a": int, "b": int, "claimed": int}
  output: bool — True iff op(a,b) == claimed.

The technique being borrowed-to-native: "recompute the claimed step
independently and compare" — the verification discipline, not arithmetic.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

TREE = "/home/hatch/workspace/worktrees/borrow-native-1"
sys.path.insert(0, os.path.join(TREE, "pylib"))
sys.path.insert(0, TREE)

MODELS_DIR = "/home/hatch/workspace/models/qwen3-8b"
LLAMA_CLI = ("/home/hatch/workspace/tools/llama.cpp-b11284/"
             "llama-b11284/llama-cli")
QWEN3_REV = "7c41481f57cb95916b40956ab2f0b139b296d974"

PROOF_DIR = os.path.join(TREE, "proofs", "borrow_native1")
DEMOS_PATH = os.path.join(PROOF_DIR, "demos.json")
DELTA_PATH = os.path.join(PROOF_DIR, "delta_record.json")
DISTILL_PATH = os.path.join(PROOF_DIR, "distill_result.json")
ENGINE_DB = os.path.join(PROOF_DIR, "borrow_native1_engine.db")

PASS: list = []
FAIL: list = []


def check(section, name, cond, detail=""):
    if cond:
        PASS.append(f"{section}:{name}")
    else:
        FAIL.append(f"{section}:{name} -- {detail}")


def ground_truth(a, b):
    """The actual sum — what the teacher must recompute."""
    return a + b


def make_grant(budget_s, note="borrow-native-1", enforcement="RUNNING"):
    from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord
    return FrmGrant.issue(
        domain="curiosity", epoch_id=1, epoch_s=3600.0,
        budget_s=budget_s, max_concurrent=1,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        lent=False, lending=LendingRecord(0.0, 0),
        enforcement_state_at_issue=enforcement,
        issued_at=time.time(), note=note)


def gguf_path():
    hits = sorted(glob.glob(os.path.join(MODELS_DIR, "*.gguf")))
    assert hits, "no .gguf under %s" % MODELS_DIR
    return hits[0]


_TEACHER = None


def real_teacher():
    """The real Qwen3-8B teacher (singleton per process)."""
    global _TEACHER
    if _TEACHER is None:
        from swarm_engine.core.microcontroller.qwen3_teacher import Qwen3Teacher
        _TEACHER = Qwen3Teacher(
            gguf_path=gguf_path(), llama_cli=LLAMA_CLI,
            threads=2, context_size=256, max_new_tokens=64)
    return _TEACHER


def fresh_substrate(budget_s=4000.0):
    from swarm_engine.core.microcontroller.substrate import (
        MicrocontrollerSubstrate)
    sub = MicrocontrollerSubstrate()
    sub.register_loop("run", budget_s=budget_s)
    sr = sub.spawn("run", purpose="borrow-native-1", budget_s=budget_s)
    assert sr.ok, f"spawn refused: {sr.refusal}"
    return sub, sr.mc.mc_id


# ---------------------------------------------------------------- native --

from swarm_engine.core.microcontroller.substrate import (
    CognitionProvider, CognitionResult)
from swarm_engine.core.microcontroller.granted_cognition import NativeRefusal

PURPOSE = "compute_sum"
REFUSAL_NAME = "no_sum_computation"


class StepVerifyNativeV1(CognitionProvider):
    """Pre-distillation native tier: REMOR owns no addition machinery,
    so the task class is refused by name. This refusal IS the
    capability gap (Y-Z)."""

    def request_cognition(self, *, mc_id, prompt, context):
        if (context or {}).get("purpose") == PURPOSE:
            raise NativeRefusal(
                REFUSAL_NAME,
                "native tier has no addition machinery; "
                "integer summation is not an owned technique")
        raise NativeRefusal("unknown_purpose",
                            f"no native handler for {(context or {}).get('purpose')!r}")


class RaisingTeacher:
    """Teacher slot that explodes if the borrow path ever runs: proves
    the independence battery never touches the teacher."""

    model_id = "raising-stub"
    revision = "must-never-be-called"

    def estimate_cost_s(self, prompt):
        raise AssertionError("teacher.estimate_cost_s called: borrow attempted")

    def complete(self, prompt, context):
        raise AssertionError("teacher.complete called: borrow attempted")


def step_prompt(a, b):
    return (
        "Compute the sum of the two integers below. First show your work, "
        "then give the result.\n"
        f"Compute: {a} + {b}\n"
        "Reply in exactly this format:\n"
        "recompute: <your work, e.g. 3 + 4 = 7>\n"
        "result: <the sum as an integer>")


def parse_result(text):
    """Strict parse of the teacher's result line. Returns int or None.

    Searches from the END backwards: the teacher echoes the prompt (which
    contains the literal placeholder '<the sum as an integer>'), so the
    first 'result:' line is the placeholder, not the answer.
    """
    for line in reversed((text or "").splitlines()):
        s = line.strip().lower()
        if s.startswith("result:"):
            v = s.split(":", 1)[1].strip()
            # skip the echoed placeholder line
            if "<" in v:
                continue
            try:
                return int(v)
            except ValueError:
                return None
    return None


def summarize(section):
    print(f"[{section}] PASS={len([p for p in PASS if p.startswith(section+':')])} "
          f"FAIL={len([f for f in FAIL if f.startswith(section+':')])}")
    for f in FAIL:
        if f.startswith(section + ":"):
            print(f"  FAIL {f}")
    return not any(f.startswith(section + ":") for f in FAIL)
