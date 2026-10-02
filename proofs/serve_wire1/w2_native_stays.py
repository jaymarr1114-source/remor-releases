"""SERVE-WIRE-1 w2: native tier stays native.

Verifies that requests the template path can answer do NOT invoke the
provider — zero provider calls for native-capable prompts.
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
from swarm_engine.services.llm_providers import ProviderRegistry, ProviderEntry

# Track actual provider invocations (not escalation attempts).
provider_calls = []
class FakeProvider:
    def request_cognition(self, **kw):
        provider_calls.append(kw.get("prompt"))
        class R: ok = True; text = "fake"; provenance = "fake"; native_refusal = None
        return R()

# Register a fake provider so escalation has something to call.
reg = ProviderRegistry()
reg.register(ProviderEntry(
    name="fake", provider=FakeProvider(),
    grant_issuer=lambda s: type("G", (), {"grant_id": "g1"})(),
    mc_id="mc1"))
services = {"llm_providers": reg}

# Native-capable prompt: template answers, provider NOT invoked.
res = chat_handler.answer("hello", services)
check("w2_native_answers", res is not None, f"mode={res.get('mode') if res else None}")
check("w2_no_provider_called", len(provider_calls) == 0,
      f"provider invocations={len(provider_calls)}")
npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
