"""b5: adversarial — the cycle fails closed.

  1. unverified_technique: a delta whose evidence supports NO technique
     (random outputs) must NOT distill (success=False, reason named).
  2. grantless_borrow: with the REAL teacher configured, no grant ->
     refusal BEFORE any inference (fast, no llama-cli).
  3. insufficient_grant: budget far below estimate -> deferred, zero charge,
     no inference.
  4. bad_grant: a non-FrmGrant object -> refusal naming the confusion.
"""
import sys
import os
import json
import random
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SEC = "b5"


def main():
    from swarm_engine.core.microcontroller.granted_cognition import (
        GrantedCognitionProvider)

    # 1. unverified technique must not distill
    from swarm_engine.acquisition.delta import DeltaRecord
    rng = random.Random(1234)
    ev = []
    for i in range(6):
        a, b = 10 + i, 20 + i
        # random outputs: no technique demonstrated
        ev.append({"input": {"a": a, "b": b},
                   "output": rng.randint(0, 100)})
    bad_delta = DeltaRecord(
        objective="compute the sum of two integers",
        external_actions="random coin flips, no technique demonstrated",
        prior_capability="none",
        capability_gap="no gap: the 'demonstrations' are noise",
        technique="none",
        evidence=ev,
        source="borrow-native-1/b5-adversarial",
    ).validate()
    os.environ["REMOR_TEST_MODE"] = "1"
    from runtime.core.engine import SwarmEngine
    eng = SwarmEngine(db_path=C.ENGINE_DB, _test_allow_shared=True)
    from swarm_engine.acquisition.distill import DistillationLoop
    loop = DistillationLoop(eng, epistemic=None)
    t0 = time.time()
    res = loop.distill(bad_delta)
    dt = time.time() - t0
    C.check(SEC, "unverified_not_distilled", res.success is False,
            f"unexpected success in {dt:.1f}s")
    C.check(SEC, "failure_named", bool(res.reason), "")
    print(f"  unverified delta refused in {dt:.1f}s: {res.reason[:100]}")

    # 2. grantless borrow with the REAL teacher: fast refusal, no inference
    sub, mc_id = C.fresh_substrate()
    prov = GrantedCognitionProvider(
        substrate=sub, native=C.StepVerifyNativeV1(),
        teacher=C.real_teacher())
    t0 = time.time()
    res = prov.request_cognition(
        mc_id=mc_id, prompt=C.step_prompt(1, 2),
        context={"purpose": C.PURPOSE, "a": 1, "b": 2})
    dt = time.time() - t0
    C.check(SEC, "grantless_refused", res.ok is False, "")
    C.check(SEC, "grantless_fast", dt < 20,
            f"took {dt:.1f}s (inference may have run!)")
    C.check(SEC, "grantless_names_reason",
            "no_grant" in (res.error or ""), (res.error or "")[:80])

    # 3. insufficient grant: deferred, zero charge, no inference
    sub3, mc3 = C.fresh_substrate()
    prov3 = GrantedCognitionProvider(
        substrate=sub3, native=C.StepVerifyNativeV1(),
        teacher=C.real_teacher())
    tiny = C.make_grant(1.0, note="borrow-native-1/b5-tiny")
    t0 = time.time()
    res = prov3.request_cognition(
        mc_id=mc3, prompt=C.step_prompt(1, 2),
        context={"purpose": C.PURPOSE, "a": 1, "b": 2,
                 "frm_grant": tiny})
    dt = time.time() - t0
    C.check(SEC, "insufficient_deferred", res.ok is False, "")
    C.check(SEC, "deferral_named",
            "insufficient_grant" in (res.error or ""),
            (res.error or "")[:80])
    C.check(SEC, "zero_charge",
            prov3.grant_consumed_s(tiny.grant_id) == 0.0, "")
    C.check(SEC, "deferral_fast", dt < 20,
            f"took {dt:.1f}s (inference may have run!)")

    # 4. non-FrmGrant refused with the confusion named
    sub4, mc4 = C.fresh_substrate()
    prov4 = GrantedCognitionProvider(
        substrate=sub4, native=C.StepVerifyNativeV1(),
        teacher=C.real_teacher())
    res = prov4.request_cognition(
        mc_id=mc4, prompt=C.step_prompt(1, 2),
        context={"purpose": C.PURPOSE, "a": 1, "b": 2,
                 "frm_grant": "not-a-grant"})
    C.check(SEC, "bad_grant_refused", res.ok is False, "")
    C.check(SEC, "confusion_named",
            "grant_not_frmgrant" in (res.error or ""),
            (res.error or "")[:100])

    ok = C.summarize(SEC)
    print(("BATTERY b5 " + "PASS") if ok else "BATTERY b5 FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
