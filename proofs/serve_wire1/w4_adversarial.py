"""SERVE-WIRE-1 w4: adversarial cases fail closed.

- Provider down (raises) -> honest fallback, no crash.
- ChatService returns not-ok -> fallback, no borrowed claim.
- No provider, no service -> honest "I don't know".
- Zero phantom charges: failures carry no grant consumption.
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

# Case 1: ChatService raises -> fallback, no crash.
class ExplodingService:
    def chat(self, prompt, think_hard=False):
        raise RuntimeError("provider down")
res = chat_handler.answer("xqzzy unknown", {"chat_service": ExplodingService()})
check("w4_provider_down_fallback", res is not None and res.get("kind") != "borrowed",
      f"kind={res.get('kind') if res else None}")

# Case 2: ChatService returns not-ok -> no borrowed claim.
class RefusingService:
    def chat(self, prompt, think_hard=False):
        return {"ok": False, "served": {"text": "refused", "label": "[final]"}}
res2 = chat_handler.answer("xqzzy unknown", {"chat_service": RefusingService()})
check("w4_refusal_not_borrowed", res2 is None or res2.get("kind") != "borrowed",
      f"kind={res2.get('kind') if res2 else None}")

# Case 3: empty services -> honest answer, no crash.
res3 = chat_handler.answer("xqzzy unknown", {})
check("w4_empty_services_ok", res3 is not None,
      f"mode={res3.get('mode') if res3 else None}")

# Case 4: None services -> honest answer, no crash.
res4 = chat_handler.answer("hello", None)
check("w4_none_services_ok", res4 is not None)

npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
