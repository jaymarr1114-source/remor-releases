"""t6: anti-memorization — the promoted primitive on novel adversarial tasks.

Fresh inputs never seen in any demo, with claimed values chosen to break
correlational shortcuts (parity, off-by-one, magnitude). All via the owned
primitive; no teacher, no grant.
"""
import sys
import os
import json
import random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tg_common as C

SEC = "t6"
os.environ["REMOR_TEST_MODE"] = "1"

SHOUT_FRESH = [
    ("the night sky glitters", "THE NIGHT SKY GLITTERS", True),
    ("the night sky glitters", "THE NIGHT SKY GLITTER", False),
    ("whispering winds wander", "WHISPERING WINDS WANDER", True),
    ("whispering winds wander", "WHISPERING WINDZ WANDER", False),
    ("a stitch in time", "A STITCH IN TIME", True),
    ("a stitch in time", "A STITCH IN TIM", False),
    ("curiosity killed the cat", "CURIOSITY KILLED THE CAT", True),
    ("curiosity killed the cat", "CURIOSITY KILLED THE BAT", False),
    ("honest work endures", "HONEST WORK ENDURES", True),
    ("honest work endures", "HONEST WORK ENDURE5", False),
    ("prove it on hardware", "PROVE IT ON HARDWARE", True),
    ("prove it on hardware", "PROVE IT ON HARDWAR", False),
    ("measure twice cut once", "MEASURE TWICE CUT ONCE", True),
    ("measure twice cut once", "MEASURE TWICE CUT ONCE ", False),
    ("the wall is crossed", "THE WALL IS CROSSED", True),
    ("the wall is crossed", "THE WALL IS CROSED", False),
    ("distill then verify", "DISTILL THEN VERIFY", True),
    ("distill then verify", "DISTILL THEN VERIF", False),
    ("no teacher needed", "NO TEACHER NEEDED", True),
    ("no teacher needed", "NO TEACHER NEEDEDD", False),
]


def main():
    technique = sys.argv[1]
    assert technique in C.TECHNIQUES, technique
    tech = C.TECHNIQUES[technique]
    sec = f"{SEC}/{technique}"

    with open(C.distill_result_path(technique)) as fh:
        dr = json.load(fh)
    C.check(sec, "distill_succeeded", dr.get("success") is True,
            dr.get("reason"))
    pname = dr["promoted_name"]

    from runtime.core.engine import SwarmEngine
    eng = SwarmEngine(db_path=C.engine_db(technique), _test_allow_shared=True)
    C.check(sec, "primitive_registered",
            eng.primitives.get(pname) is not None, pname)

    if technique == "step_verify":
        rng = random.Random(20261001)
        tasks = []
        for _ in range(40):
            a = rng.randint(-500, 2000)
            b = rng.randint(-500, 2000)
            s = a + b
            if rng.random() < 0.5:
                claimed = s
            else:
                claimed = s + rng.choice([-10, -5, -3, -2, -1, 1, 2, 3, 5, 10])
            tasks.append(((a, b, claimed), s == claimed))
    else:
        tasks = [((t, c), e) for t, c, e in SHOUT_FRESH]

    bad = 0
    for task, expected in tasks:
        got = eng.primitives.invoke_sync(pname, **tech["input"](task))
        if got != expected:
            bad += 1
            print(f"[{sec}] MISMATCH task={str(task)[:70]} got={got!r} "
                  f"want={expected!r}", flush=True)
    C.check(sec, "adversarial_all_correct", bad == 0,
            f"{len(tasks) - bad}/{len(tasks)}")
    print("T6_DONE")


if __name__ == "__main__":
    main()
