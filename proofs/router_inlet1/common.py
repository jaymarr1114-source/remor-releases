"""Shared harness for the ROUTER-INLET-1 gate: the real ChatService inlet,
real FRM grant issuance (the single shared path), the real persistent
student server, and the real 8B deep path. Nothing is mocked: every turn
is real inference; every grant is a real FrmGrant; every charge depletes
a real substrate budget."""
import os
import sys
import time
from types import SimpleNamespace

TREE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(TREE, "pylib"))
sys.path.insert(0, os.path.join(TREE, "distill1"))
sys.path.insert(0, TREE)

from swarm_engine.curiosity.frm.grant import (  # noqa: E402
    FrmGrant, issue_run_grant)
from swarm_engine.core.microcontroller.substrate import (  # noqa: E402
    MicrocontrollerSubstrate)
from swarm_engine.core.microcontroller.granted_cognition import (  # noqa: E402
    GrantedCognitionProvider)
from swarm_engine.core.microcontroller.qwen3_teacher import (  # noqa: E402
    Qwen3Teacher)
from swarm_engine.services.agent_api import build_llm_wiring  # noqa: E402
from swarm_engine.services.chat_api import ChatService  # noqa: E402

GGUF = os.path.expanduser(
    "~/workspace/models/qwen3-8b/qwen3-8b-q4_k_m.d98cdcbd03e17ce4.gguf")
LLAMA_CLI = os.path.expanduser(
    "~/workspace/tools/llama.cpp-b11284/llama-b11284/llama-cli")


def make_service(base="/tmp/router-inlet1-gate", **kw):
    """A real ChatService with the real deep wiring."""
    return ChatService(base, llm_wiring=build_llm_wiring(), **kw)


def make_small_deep_wiring(budget_s=0.05):
    """A real deep wiring on a nearly-exhausted budget pool."""
    sub = MicrocontrollerSubstrate()
    sub.register_loop("run", budget_s=7200.0)
    spawned = sub.spawn("run", purpose="gate-small-deep",
                        budget_s=budget_s)
    assert spawned.ok, spawned.refusal
    teacher = Qwen3Teacher(gguf_path=GGUF, llama_cli=LLAMA_CLI)
    provider = GrantedCognitionProvider(sub, native=None, teacher=teacher)
    return SimpleNamespace(provider=provider, grant_issuer=None,
                           mc_id=spawned.mc.mc_id, substrate=sub)


def make_corrupt_deep_wiring(corrupt_path):
    """A real deep wiring whose teacher cannot load (transport failure)."""
    sub = MicrocontrollerSubstrate()
    sub.register_loop("run", budget_s=7200.0)
    spawned = sub.spawn("run", purpose="gate-corrupt-deep",
                        budget_s=3600.0)
    assert spawned.ok, spawned.refusal
    teacher = Qwen3Teacher(gguf_path=corrupt_path, llama_cli=LLAMA_CLI)
    provider = GrantedCognitionProvider(sub, native=None, teacher=teacher)
    return SimpleNamespace(provider=provider, grant_issuer=None,
                           mc_id=spawned.mc.mc_id)


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"{status} {name}" + (f": {detail}" if detail else ""),
          flush=True)
    if not cond:
        raise SystemExit(f"battery failed at {name}: {detail}")
