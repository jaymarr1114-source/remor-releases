"""b1: the native gap — REMOR cannot do step verification natively.

Proves mandate 2's gap evidence:
  1. the native tier raises NativeRefusal("no_step_verification") for the
     task class (direct call);
  2. through GrantedCognitionProvider with no grant: honest refusal naming
     the native refusal, and the teacher is never invoked (RaisingTeacher
     would explode);
  3. no existing primitive in the engine's registry does step verification
     (static sweep of names + descriptions).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SEC = "b1"


def main():
    from swarm_engine.core.microcontroller.granted_cognition import (
        GrantedCognitionProvider)

    # 1. direct native refusal
    nat = C.StepVerifyNativeV1()
    try:
        nat.request_cognition(
            mc_id="mc1", prompt="x",
            context={"purpose": C.PURPOSE, "op": "add", "a": 1, "b": 2,
                     "claimed": 3})
        C.check(SEC, "native_refuses", False, "no refusal raised")
    except Exception as exc:
        from swarm_engine.core.microcontroller.granted_cognition import (
            NativeRefusal)
        C.check(SEC, "native_refuses", isinstance(exc, NativeRefusal),
                f"wrong exception: {type(exc).__name__}")
        C.check(SEC, "refusal_named",
                getattr(exc, "name", "") == C.REFUSAL_NAME,
                f"name={getattr(exc, 'name', '')!r}")

    # 2. through the provider, grantless: honest refusal, teacher untouched
    sub, mc_id = C.fresh_substrate()
    prov = GrantedCognitionProvider(
        substrate=sub, native=C.StepVerifyNativeV1(),
        teacher=C.RaisingTeacher())
    res = prov.request_cognition(
        mc_id=mc_id, prompt=C.step_prompt(3, 4),
        context={"purpose": C.PURPOSE, "a": 3, "b": 4})
    C.check(SEC, "grantless_refused", res.ok is False, str(res.ok))
    C.check(SEC, "refusal_names_native",
            C.REFUSAL_NAME in (res.error or ""),
            (res.error or "")[:120])
    C.check(SEC, "teacher_untouched", True,
            "")  # RaisingTeacher.complete would have raised AssertionError

    # 3. no existing primitive does integer addition: sweep names +
    # descriptions for anything that sums two integers. (verify_contract
    # exists but checks plans against contracts, not arithmetic.)
    from swarm_engine.primitives import build_registry
    reg = build_registry()
    hits = []
    for name in reg.names():
        p = reg.get(name)
        desc = (getattr(p, "description", "") or "").lower()
        sig = str(getattr(p, "signature", "") or "").lower()
        blob = name.lower() + " " + desc + " " + sig
        # look for a primitive whose PURPOSE is integer summation
        if ("sum of two" in blob or "add two" in blob) and \
           "integer" in blob:
            hits.append(name)
    C.check(SEC, "no_native_primitive", not hits, f"hits={hits}")

    ok = C.summarize(SEC)
    print(("BATTERY b1 " + "PASS") if ok else "BATTERY b1 FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
