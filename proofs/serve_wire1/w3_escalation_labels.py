"""SERVE-WIRE-1 w3: escalation goes through the FRM-governed inlet with labels.

Verifies that when a ChatService is in the services dict, escalation
routes through it (not direct provider calls), and labels survive.
"""
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/serve-wire-1")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)
sys.path.insert(0, os.path.join(WT, "distill1"))

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

from swarm_engine.services import chat_handler

# Fake ChatService that records calls and returns labeled responses.
calls = []
class FakeChatService:
    def chat(self, prompt, think_hard=False):
        calls.append(prompt)
        return {
            "ok": True,
            "served": {
                "text": "[provisional — deeper reasoning running] fake answer",
                "label": "[provisional — deeper reasoning running]",
                "via": "deep",
                "materially_better": False,
                "diff_ratio": None,
            },
            "provenance": "fake:test",
        }

services = {"chat_service": FakeChatService()}
# Use a prompt that triggers escalation (unknown kind).
res = chat_handler.answer("xqzzy not a real thing", services)
check("w3_escalation_called_inlet", len(calls) > 0, f"inlet calls={len(calls)}")
if res:
    check("w3_label_survives", res.get("label") is not None,
          f"label={res.get('label')}")
    check("w3_kind_borrowed", res.get("kind") == "borrowed",
          f"kind={res.get('kind')}")
    check("w3_via_chat_service", res.get("grounded", {}).get("via") == "chat_service")
else:
    check("w3_escalation_called_inlet", False, "no response")

npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
