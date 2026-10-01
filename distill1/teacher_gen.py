#!/usr/bin/env python3
"""DISTILL-1: governed teacher demonstrations.

Generates teacher responses for the training turns through
GrantedCognitionProvider with a real FrmGrant per call. Every output
carries provenance borrowed:qwen3@<rev>. No third-party chat API anywhere
in this path: the teacher is the local Qwen3-8B GGUF via llama-cli.

Usage: teacher_gen.py  (reads distill1/turns_train.json, writes
distill1/teacher_demos.json). Resumable: skips turn_ids already present.
"""
import json
import os
import sys
import time

TREE = "/home/hatch/workspace/worktrees/distill-1"
sys.path.insert(0, os.path.join(TREE, "pylib"))

from swarm_engine.core.microcontroller.granted_cognition import (
    GrantedCognitionProvider, CognitionProvider, NativeRefusal)
from swarm_engine.core.microcontroller.qwen3_teacher import (
    Qwen3Teacher, QWEN3_GGUF_REVISION)
from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord
from swarm_engine.core.microcontroller.substrate import (
    MicrocontrollerSubstrate)

MODELS_DIR = "/home/hatch/workspace/models/qwen3-8b"
LLAMA_CLI = ("/home/hatch/workspace/tools/llama.cpp-b11284/"
             "llama-b11284/llama-cli")
HERE = os.path.join(TREE, "distill1")


class RefusingNative(CognitionProvider):
    def request_cognition(self, *, mc_id, prompt, context):
        raise NativeRefusal("distill1:conversational_reasoning",
                            "native tier cannot do open conversational "
                            "reasoning; borrow justified")


def make_grant(budget_s=400.0):
    return FrmGrant.issue(
        domain="curiosity", epoch_id=1, epoch_s=300.0,
        budget_s=budget_s, max_concurrent=1,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        lent=False, lending=LendingRecord(0.0, 0),
        enforcement_state_at_issue="RUNNING",
        issued_at=time.time(), note="distill-1-teacher-demo")


def teacher_prompt(turn_text):
    return (
        "You are a helpful AI assistant having a casual chat. Respond "
        "naturally and briefly (1-3 sentences) to this chat turn. If asked "
        "about your capabilities, be honest about what you can and can't "
        "do rather than making things up.\n\n"
        f"Chat turn: {turn_text}\n\nResponse:")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", default=os.path.join(HERE, "turns_train.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "teacher_demos.json"))
    args = ap.parse_args()

    gguf = os.path.join(MODELS_DIR,
                        "qwen3-8b-q4_k_m.d98cdcbd03e17ce4.gguf")
    assert os.path.isfile(gguf), f"teacher weights missing: {gguf}"
    teacher = Qwen3Teacher(gguf_path=gguf, llama_cli=LLAMA_CLI,
                           max_new_tokens=96)

    with open(args.turns) as fh:
        turns = json.load(fh)["turns"]
    out_path = args.out
    demos = []
    if os.path.isfile(out_path):
        with open(out_path) as fh:
            demos = json.load(fh)
    done = {d["turn_id"] for d in demos}
    print(f"resuming: {len(done)}/{len(turns)} already done", flush=True)

    sub = MicrocontrollerSubstrate()
    sub.register_loop("run", budget_s=7200.0)
    sr = sub.spawn("run", purpose="distill-1-teacher", budget_s=7000.0)
    assert sr.ok, f"spawn refused: {sr.refusal}"
    mc_id = sr.mc.mc_id
    prov = GrantedCognitionProvider(
        substrate=sub, native=RefusingNative(), teacher=teacher)

    for t in turns:
        if t["id"] in done:
            continue
        grant = make_grant()
        res = prov.request_cognition(
            mc_id=mc_id, prompt=teacher_prompt(t["text"]),
            context={"frm_grant": grant, "purpose": "distillation-demo",
                     "target_profile": "laptop-cpu"})
        if not res.ok:
            print(f"{t['id']}: BORROW FAILED: {res.error}", flush=True)
            sys.exit(1)
        assert res.provenance == f"borrowed:qwen3@{QWEN3_GGUF_REVISION}", \
            f"bad provenance: {res.provenance}"
        demos.append({
            "turn_id": t["id"],
            "turn_class": t["class"],
            "turn_text": t["text"],
            "teacher_text": res.text,
            "provenance": res.provenance,
            "consumed_s": prov.grant_consumed_s(grant.grant_id),
        })
        with open(out_path, "w") as fh:
            json.dump(demos, fh, indent=1)
        print(f"{t['id']}: ok ({demos[-1]['consumed_s']:.0f}s, "
              f"{len(res.text)} chars)", flush=True)

    print(f"done: {len(demos)} demonstrations -> {out_path}")


if __name__ == "__main__":
    main()
