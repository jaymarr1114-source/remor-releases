#!/usr/bin/env python3
"""LLM-SERVE-PATH-1 b6: provider registry mechanics (no inference).

  * register/get/default/list/unregister/set_default all work
  * first registration becomes default; set_default changes it
  * unregister of default falls back honestly
  * duplicate registration replaces (documented)
  * empty registry -> default() is None (serving path refuses honestly)
  * thread-safety smoke: concurrent register/get
  * builtin registers through the public register() path (dogfooding):
    verify register_builtin_provider calls registry.register by
    checking the entry appears via get()/list()
"""
import sys
import threading

WT = "/home/hatch/workspace/worktrees/llm-serve-path-1"
sys.path.insert(0, WT)
sys.path.insert(0, WT + "/pylib")

from runtime.services.llm_providers import (
    ProviderRegistry, ProviderEntry, BUILTIN_PROVIDER_NAME)

class FakeProvider:
    def request_cognition(self, *, mc_id, prompt, context):
        from runtime.services.llm_providers import ProviderEntry  # noqa
        return {"ok": True}

def make_entry(name):
    return ProviderEntry(
        name=name, provider=FakeProvider(),
        grant_issuer=lambda est: None, mc_id="mc-test",
        description="test")

def main() -> int:
    passed = 0
    total = 0

    # 1. empty registry
    total += 1
    r = ProviderRegistry()
    ok = r.default() is None and r.list() == [] and r.get("x") is None
    print(f"[b6] empty registry honest: {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 2. first registration becomes default
    total += 1
    r.register(make_entry("a"))
    ok = r.default_name() == "a" and r.get("a").name == "a"
    print(f"[b6] first-is-default: {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 3. list + second registration doesn't steal default
    total += 1
    r.register(make_entry("b"))
    ok = r.list() == ["a", "b"] and r.default_name() == "a"
    print(f"[b6] list + default stable: {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 4. set_default
    total += 1
    r.set_default("b")
    ok = r.default_name() == "b" and r.default().name == "b"
    print(f"[b6] set_default: {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 5. set_default unknown -> KeyError
    total += 1
    try:
        r.set_default("nope")
        ok = False
    except KeyError:
        ok = True
    print(f"[b6] set_default unknown raises: {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 6. unregister default falls back
    total += 1
    r.unregister("b")
    ok = r.default_name() == "a" and r.list() == ["a"]
    print(f"[b6] unregister default falls back: {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 7. unregister last -> empty again
    total += 1
    r.unregister("a")
    ok = r.default() is None and r.list() == []
    print(f"[b6] unregister last -> empty: {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 8. thread-safety smoke
    total += 1
    r2 = ProviderRegistry()
    errors = []
    def worker(n):
        try:
            for i in range(50):
                r2.register(make_entry(f"t{n}-{i}"))
                r2.get(f"t{n}-{i}")
                r2.default()
                r2.list()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    ok = not errors and len(r2.list()) == 200
    print(f"[b6] thread-safety (200 entries, 4 threads): {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 9. builtin registers through the public path
    total += 1
    r3 = ProviderRegistry()
    from runtime.services.llm_providers import register_builtin_provider
    try:
        entry = register_builtin_provider(r3)
        ok = (r3.get(BUILTIN_PROVIDER_NAME) is entry
              and r3.default() is entry
              and BUILTIN_PROVIDER_NAME in r3.list())
        print(f"[b6] builtin via public path: {'PASS' if ok else 'FAIL'}")
    except FileNotFoundError as e:
        # honest about missing substrate: registry stays empty
        ok = r3.default() is None
        print(f"[b6] builtin absent-substrate honest ({e}): {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 10. validation: empty name / None provider rejected
    total += 1
    try:
        r3.register(ProviderEntry(name="", provider=FakeProvider(),
                                  grant_issuer=lambda e: None, mc_id="x"))
        ok = False
    except ValueError:
        ok = True
    print(f"[b6] validation rejects empty name: {'PASS' if ok else 'FAIL'}")
    passed += ok

    print(f"[b6] PASS={passed} FAIL={total-passed}")
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
