"""S4G-KILL-CAUSAL gap probes: the two causal-termination gaps found by
code inspection, demonstrated live.

T6 launch_app survival: a process launched via remote dispatch is real
   remote work at the target. U-9: kill must terminate it. Probe:
   launch /bin/sleep 300, kill via the real API, then check the pid
   AT THE TARGET (os.kill(pid, 0)). Pre-repair: still alive (gap).
T7 batch liveness: _dispatch_actions threads is_live through to type()
   only; move/click/scroll/key never consult it between actions. Probe:
   slow the target's move (0.3s, simulating a duration-bearing
   substrate -- labeled simulation of substrate timing, NOT X11-native
   behavior), run a 3-move batch, kill mid-move-1 via the real API,
   count executed moves at the target. Pre-repair: 3 (gap);
   post-repair: 1.

Run: python3 proofs/s4g_kill_gaps.py   (repo root; DISPLAY=:99)
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from s4g_kill_common import Bench, CHECKS, check

SCRATCH = os.path.join(HERE, "s4g_kill_gaps_scratch")


def t6_launch_app_survival(bench):
    print("\n=== T6: launched process must die with the kill ===",
          flush=True)
    sid, token, sess = bench.new_session()
    r = sess.act({"type": "launch_app", "app": "/bin/sleep",
                  "argv": ["/bin/sleep", "300"]})
    pid = r[0].get("pid", 0)
    check("T6 sleep launched via remote dispatch", pid > 0,
          f"pid={pid}")
    os.kill(pid, 0)  # alive before kill
    check("T6 process alive before kill", True, f"pid={pid}")

    resp = bench.kill_via_api(sid, token)
    check("T6 kill relayed",
          resp.get("ok") and resp.get("target_relay") == "relayed",
          str(resp.get("target_relay")))
    time.sleep(1.5)  # grace past any async teardown
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    check("T6 launched process terminated by the kill (U-9)",
          not alive,
          f"pid={pid} still alive after kill" if alive else "gone")
    if alive:
        # bench cleanup: do not leak the sleeper
        os.kill(pid, 15)
    try:
        sess.channel.close()
    except Exception:
        pass


def t7_batch_liveness(bench):
    print("\n=== T7: kill between batched actions ===", flush=True)
    sid, token, sess = bench.new_session()

    # Force cursor creation, then wrap move with a counting delayer.
    # The delay SIMULATES a duration-bearing substrate (e.g. Android
    # AccessibilityService actions); the dispatch-loop logic under test
    # is substrate-agnostic. Labeled as such.
    sess.act({"type": "move", "x": 5, "y": 5})
    cursor = bench.target._cursor
    assert cursor is not None
    orig_move = cursor.move
    calls = []

    def slow_move(x, y):
        calls.append((x, y))
        time.sleep(0.3)
        return orig_move(x, y)

    cursor.move = slow_move
    try:
        outcome = {}

        def do_batch():
            try:
                outcome["reply"] = sess.act_batch([
                    {"type": "move", "x": 100, "y": 100},
                    {"type": "move", "x": 200, "y": 200},
                    {"type": "move", "x": 300, "y": 300},
                ])
            except Exception as e:  # noqa: BLE001
                outcome["error"] = e

        th = threading.Thread(target=do_batch, daemon=True)
        th.start()
        time.sleep(0.15)  # mid-move-1
        resp = bench.kill_via_api(sid, token)
        check("T7 kill relayed mid-batch",
              resp.get("ok") and resp.get("target_relay") == "relayed")
        th.join(timeout=60)
        check("T7 batch thread finished", not th.is_alive())
        check("T7 no actions executed after the kill (U-9)",
              len(calls) == 1,
              f"moves executed={len(calls)} "
              f"(error={str(outcome.get('error'))[:80]})")
    finally:
        cursor.move = orig_move
        try:
            sess.channel.close()
        except Exception:
            pass


def main():
    assert os.environ.get("DISPLAY") == ":99", "needs Xvfb on :99"
    bench = Bench(SCRATCH)
    try:
        t6_launch_app_survival(bench)
        t7_batch_liveness(bench)
    finally:
        bench.close()
    print(f"\nS4G gap probes: {sum(1 for _, ok, _ in CHECKS if ok)}/"
          f"{len(CHECKS)} checks green")


if __name__ == "__main__":
    main()
