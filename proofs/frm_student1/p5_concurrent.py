"""P5: concurrent turns respect the grant's max_concurrent."""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import SubstrateDouble, make_grant
from governed_student import GovernedStudent, R_DEFERRED_CONCURRENT
import server_client

# Pin the transport so the test is about concurrency, not the model.
calls = []
orig_turn = server_client.turn


def slow_turn(prompt, **kw):
    calls.append(prompt)
    import time
    time.sleep(3)
    return "slow answer", 3.0, 10.0, None


server_client.turn = slow_turn
try:
    sub = SubstrateDouble()
    gs = GovernedStudent(sub, "mc-gate-5")
    grant = make_grant(budget_s=400.0, max_concurrent=1)
    results = {}

    def first():
        results["first"] = gs.turn("first turn", frm_grant=grant)

    t = threading.Thread(target=first)
    t.start()
    import time
    time.sleep(0.5)  # let the first turn go in-flight
    res2 = gs.turn("second turn", frm_grant=grant)
    t.join()
    assert results["first"]["ok"] is True, f"first must serve: {results['first']}"
    assert res2["ok"] is False, f"second must defer, got {res2}"
    assert R_DEFERRED_CONCURRENT in res2["error"], f"wrong: {res2['error']}"
    assert sub.calls and len(sub.calls) == 1, \
        f"only the served turn charges: {sub.calls}"
    print("PASS p5_concurrent: 2nd turn deferred at max_concurrent=1")
finally:
    server_client.turn = orig_turn
