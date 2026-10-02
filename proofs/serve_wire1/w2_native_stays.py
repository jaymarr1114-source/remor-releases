"""SERVE-WIRE-1 w2: native tier stays native.

Verifies:
- Without a provider, native template answers (no crash, honest fallback).
- The serving path does not REQUIRE a provider (works with empty registry).
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

# Empty registry: native answers, no crash.
services = {"llm_providers": ProviderRegistry()}
res = chat_handler.answer("hello", services)
check("w2_native_answers_no_provider", res is not None,
      f"mode={res.get('mode') if res else None}")
check("w2_not_borrowed", res.get("kind") != "borrowed" if res else False,
      f"kind={res.get('kind') if res else None}")

# No registry at all: still answers.
res2 = chat_handler.answer("hello", {})
check("w2_no_registry_ok", res2 is not None,
      f"mode={res2.get('mode') if res2 else None}")

npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
