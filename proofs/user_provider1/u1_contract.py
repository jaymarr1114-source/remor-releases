"""USER-PROVIDER-1: user provider via the public contract.

- Registers through registry.register() (same path as builtin).
- Serves a turn through the same serving path.
- Credential hygiene: key from env, never in code/logs.
- Fails closed: no key -> FileNotFoundError; no grant -> refusal.
"""
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/user-provider-1")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

from swarm_engine.services.llm_providers import ProviderRegistry
from swarm_engine.services.user_provider import (
    register_user_provider, PROVIDER_NAME, ENV_API_KEY)

# Credential hygiene: no key -> honest FileNotFoundError.
os.environ.pop(ENV_API_KEY, None)
reg = ProviderRegistry()
try:
    register_user_provider(reg)
    check("u1_no_key_honest", False, "should have raised")
except FileNotFoundError as e:
    check("u1_no_key_honest", True, str(e)[:50])

# With key: registers through the public path.
os.environ[ENV_API_KEY] = "test-key-123"
def fake_transport(prompt, api_key):
    assert api_key == "test-key-123", "key passed to transport"
    return f"echo: {prompt}"
entry = register_user_provider(reg, transport=fake_transport)
check("u1_registers", entry.name == PROVIDER_NAME)
check("u1_in_registry", reg.get(PROVIDER_NAME) is not None)

# Serves through the same path: request_cognition with grant.
grant = entry.grant_issuer(1.0)
res = entry.provider.request_cognition(
    mc_id=entry.mc_id, prompt="hello",
    context={"frm_grant": grant})
check("u1_serves", res.ok and res.text == "echo: hello", res.text[:30])
check("u1_provenance", PROVIDER_NAME in res.provenance, res.provenance)

# No grant -> honest refusal (governance contract).
res2 = entry.provider.request_cognition(
    mc_id=entry.mc_id, prompt="hello", context={})
check("u1_no_grant_refused", not res2.ok and "frm_grant" in (res2.error or ""),
      (res2.error or "")[:50])

# Key never in repr/logs.
check("u1_key_not_leaked", "test-key-123" not in repr(entry.provider),
      "key absent from repr")

os.environ.pop(ENV_API_KEY, None)
npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
