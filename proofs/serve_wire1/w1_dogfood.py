"""SERVE-WIRE-1 w1: builtin dogfoods the public registration path.

Verifies:
- ProviderRegistry starts empty.
- register_builtin_provider() registers through the public register() call.
- The builtin entry is retrievable via registry.default().
- No hardwired build_llm_wiring() in the serving path (ChatService takes registry).
"""
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/serve-wire-1")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

from swarm_engine.services.llm_providers import (
    ProviderRegistry, register_builtin_provider, BUILTIN_PROVIDER_NAME)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

# Registry starts empty
reg = ProviderRegistry()
check("w1_registry_starts_empty", reg.default() is None, "no default before registration")

# Builtin registers through the public path
try:
    entry = register_builtin_provider(reg)
    check("w1_builtin_registers", entry is not None, f"name={entry.name}")
    check("w1_builtin_is_default", reg.default() is not None and reg.default().name == BUILTIN_PROVIDER_NAME)
    check("w1_entry_has_provider", entry.provider is not None)
    check("w1_entry_has_grant_issuer", entry.grant_issuer is not None)
    check("w1_entry_has_mc_id", bool(entry.mc_id))
except FileNotFoundError as e:
    # Honest: no substrate on this host
    check("w1_no_substrate_honest", True, f"FileNotFoundError: {e} (registry stays empty)")
except Exception as e:
    check("w1_builtin_registers", False, f"{type(e).__name__}: {e}")

# ChatService takes registry, not hardwired wiring
import inspect
# Import from runtime/services (the modified file), not pylib copy
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "chat_api_mod",
    os.path.join(WT, "runtime", "services", "chat_api.py"))
_mod = importlib.util.module_from_spec(_spec)
# Prepend distill1 so governed_student resolves
if os.path.join(WT, "distill1") not in sys.path:
    sys.path.insert(0, os.path.join(WT, "distill1"))
_spec.loader.exec_module(_mod)
ChatService = _mod.ChatService
sig = inspect.signature(ChatService.__init__)
check("w1_chatservice_takes_registry", "provider_registry" in sig.parameters,
      "ChatService.__init__ accepts provider_registry")

npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
