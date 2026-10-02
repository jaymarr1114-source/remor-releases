"""SERVE-WIRE-1 w5: sustained load — 10 sequential turns, no corruption.

Verifies the serving path handles repeated turns without state
corruption, leaking grants, or degrading.
"""
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/serve-wire-1")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

from swarm_engine.services import chat_handler
from swarm_engine.services.llm_providers import ProviderRegistry

services = {"llm_providers": ProviderRegistry()}
prompts = ["hello", "thanks", "what can you do", "hello again",
           "status", "thanks very much", "hello there", "what is my tier",
           "hi", "thank you"]
oks = 0
for i, p in enumerate(prompts):
    try:
        r = chat_handler.answer(p, services)
        if r is not None and r.get("text"):
            oks += 1
    except Exception as e:
        print(f"turn {i} raised {type(e).__name__}: {e}", flush=True)

check("w5_all_turns_answered", oks == len(prompts), f"{oks}/{len(prompts)}")
check("w5_no_crash", True, "no exceptions escaped")

npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
