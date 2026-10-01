#!/usr/bin/env python3
"""LLM-SERVE-PATH-1 b7: end-to-end serving path (one real inference).

Exercises the full chain: chat_handler.answer() with a services dict
carrying the provider registry -> template path can't answer ->
escalates to the default provider -> governed borrow -> answer labeled
with provenance.

Also verifies the no-provider path still refuses honestly (no registry
= the original template "I don't know").
"""
import sys

WT = "/home/hatch/workspace/worktrees/llm-serve-path-1"
sys.path.insert(0, WT)
sys.path.insert(0, WT + "/pylib")

from runtime.services import chat_handler
from runtime.services.llm_providers import (
    ProviderRegistry, register_builtin_provider)

def main() -> int:
    passed = 0
    total = 0

    # 1. no registry -> honest template fallback (existing behavior)
    total += 1
    res = chat_handler.answer("flibberty gibbet wobble", {})
    ok = (res["kind"] == "unknown" and "no language substrate" in res["text"])
    print(f"[b7] no-provider honest fallback: {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 2. registry with builtin -> escalation with real inference
    total += 1
    registry = ProviderRegistry()
    try:
        register_builtin_provider(registry)
    except FileNotFoundError as e:
        print(f"[b7] SKIP: no substrate ({e})")
        print(f"[b7] PASS={passed} FAIL={total-passed} SKIP=1")
        return 0 if passed == total - 1 else 1
    services = {"llm_providers": registry}
    res = chat_handler.answer(
        "What is 7+7? Reply with just the number.", services)
    ok = (res["kind"] == "borrowed"
          and "14" in res["text"]
          and res["grounded"]["provenance"].startswith("borrowed:qwen3@")
          and res["grounded"]["provider"] == "remor-builtin-qwen3")
    print(f"[b7] escalation kind={res['kind']} text={res['text'][:30]!r} "
          f"prov={res['grounded'].get('provenance', '')[:20] if res['grounded'] else ''} "
          f"-> {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 3. grounded template kinds do NOT escalate (native tier wins)
    total += 1
    res = chat_handler.answer("what is my tier", services)
    ok = res["kind"] != "borrowed"
    print(f"[b7] grounded kind stays native (kind={res['kind']}): "
          f"{'PASS' if ok else 'FAIL'}")
    passed += ok

    print(f"[b7] PASS={passed} FAIL={total-passed}")
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
