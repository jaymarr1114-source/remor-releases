"""t1: native gap — both purposes refused by name pre-distillation (no inference).

Calls the native tier directly: the refusal IS the capability gap (Y-Z).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tg_common as C

SEC = "t1"


def main():
    native = C.TraceGuidedNativeV1()
    n_refused = 0
    for purpose in C.PURPOSES:
        tech = C.TECHNIQUES[purpose]
        for i, task in enumerate(tech["tasks"][:6]):
            try:
                native.request_cognition(
                    mc_id="test-mc", prompt=tech["prompt"](task),
                    context={"purpose": purpose})
            except C.NativeRefusal as exc:
                C.check(SEC, f"{purpose}/{i}_refused_by_name",
                        exc.name == C.REFUSAL_NAME, exc.name)
                n_refused += 1
                continue
            C.check(SEC, f"{purpose}/{i}_refused", False,
                    "native tier answered: no gap")
    # unknown purposes refuse too (differently named: not the gap)
    try:
        native.request_cognition(
            mc_id="test-mc", prompt="x", context={"purpose": "nope"})
        C.check(SEC, "unknown_refused", False, "answered unknown purpose")
    except C.NativeRefusal as exc:
        C.check(SEC, "unknown_refused", exc.name == "unknown_purpose",
                exc.name)
    C.check(SEC, "all_refused", n_refused == 12, f"{n_refused}/12")
    print("T1_DONE")


if __name__ == "__main__":
    main()
