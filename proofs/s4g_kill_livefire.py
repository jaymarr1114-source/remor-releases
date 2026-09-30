"""S4G-KILL-CAUSAL live-fire: kill mid-flight through the REAL /api/remote
kill path, observed AT THE TARGET. Plus the four adversarial contrasts.

BENCH (never device proof): Xvfb :99, real RemoteDispatchTarget (X11),
real pinned-TLS channel, real RemoteDispatchService.kill().

T1 kill mid-flight: 2000-char type in a background thread; poll the live
   DOM until typing is observably in flight; svc.kill() mid-flight;
   assert at the target: thread interrupted, no post-kill growth,
   session KILLED, indicator off, target_relay == "relayed".
T2 kill with no live action.
T3 kill after the action completed (no rollback claimed).
T4 channel disconnect WITHOUT kill: typing runs to completion
   (disconnect != kill).
T5 kill racing action completion: either honest outcome, no crash.

Run: python3 proofs/s4g_kill_livefire.py   (repo root; DISPLAY=:99)
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from s4g_kill_common import (Bench, CHECKS, PAGE, PAGE_PATH, check,
                             count_keystroke_liveness, pkill_chrome,
                             slow_substrate_keystroke, wait_for)

SCRATCH = os.path.join(HERE, "s4g_kill_scratch")


def main():
    assert os.environ.get("DISPLAY") == ":99", "needs Xvfb on :99"
    pkill_chrome()
    with open(PAGE_PATH, "w") as f:
        f.write(PAGE)

    bench = Bench(SCRATCH)
    try:
        t1_kill_midflight(bench)
        pkill_chrome()  # each browser test gets a clean slate
        t2_kill_idle(bench)
        t3_kill_after_done(bench)
        pkill_chrome()
        t4_disconnect_without_kill(bench)
        pkill_chrome()
        t5_kill_racing_completion(bench)
    finally:
        bench.close()
        pkill_chrome()
    print(f"\nS4G live-fire: {sum(1 for _, ok, _ in CHECKS if ok)}/"
          f"{len(CHECKS)} checks green")


def t1_kill_midflight(bench):
    print("\n=== T1: kill mid-flight via real /api/remote/kill ===",
          flush=True)
    sid, token, sess = bench.new_session()
    check("T1 session live", bench.session_state(sid) == "live")
    cdp, browser_pid = bench.launch_browser(sess)

    # Slow-substrate simulation (see s4g_kill_common): widens the
    # injection loop to ~3.5s so the kill reliably lands mid-loop.
    # Mid-flight progress is observed AT THE TARGET by counting the
    # REAL per-keystroke is_live() kill checks (count_keystroke_liveness):
    # Xlib buffers injected keystrokes until sync(), so the DOM only
    # ever shows the action's final state -- the in-flight moment is
    # not DOM-observable.
    text = "Ab3" * 666 + "Xy"          # exactly 2000 chars
    assert len(text) == 2000
    outcome = {}

    def do_type():
        try:
            outcome["reply"] = sess.act({"type": "type", "text": text})
        except Exception as e:  # noqa: BLE001
            outcome["error"] = e

    cursor = bench.target._cursor
    assert cursor is not None
    with slow_substrate_keystroke(), count_keystroke_liveness(cursor) \
            as live_calls:
        th = threading.Thread(target=do_type, daemon=True)
        th.start()
        # in-flight when the target has really executed >300 per-keystroke
        # liveness checks (fast in-process poll, no CDP slowness)
        n_inflight = wait_for(
            lambda: live_calls["n"] > 300 and live_calls["n"],
            timeout=30, what=">300 real is_live checks at the target")
        check("T1 type observably in flight at the target",
              300 < n_inflight < 2000,
              f"is_live calls={n_inflight}")

        t_kill = time.time()
        resp = bench.kill_via_api(sid, token)
        check("T1 API kill ok + relayed to target",
              resp.get("ok") and resp.get("target_relay") == "relayed",
              str({k: resp.get(k) for k in ("ok", "target_relay",
                                            "target_relay_detail",
                                            "state")}))
        th.join(timeout=60)
        check("T1 type thread finished after kill", not th.is_alive())
        err = str(outcome.get("error", ""))
        check("T1 in-flight type was interrupted at the target",
              "interrupted" in err,
              err[:120] or f"reply={outcome.get('reply')}")
        n_at_kill = live_calls["n"]
        time.sleep(1.5)
        check("T1 no further keystroke checks after kill "
              "(injection loop dead)",
              live_calls["n"] == n_at_kill,
              f"at_kill={n_at_kill} later={live_calls['n']}")
        print(f"    kill landed after {n_at_kill} keystrokes; "
              f"kill round-trip {time.time() - t_kill:.2f}s", flush=True)

    # Post-repair the kill also terminates the session-launched browser
    # (Repair 2): the app is gone, so read the DOM only if it survived.
    try:
        final_dom = bench.input_len(cdp)
    except Exception:
        final_dom = None
    if final_dom is None:
        try:
            os.kill(browser_pid, 0)
            check("T1 kill terminated the session-launched browser",
                  False, f"pid={browser_pid} survived the kill")
        except ProcessLookupError:
            check("T1 kill terminated the session-launched browser "
                  "(causal termination incl. launched proc)",
                  True, f"pid={browser_pid} gone")
    else:
        check("T1 interrupted keystrokes never reached the app "
              "(unflushed Xlib buffer dropped)",
              final_dom < 2000, f"dom_len={final_dom}")
    check("T1 session KILLED at target store",
          bench.session_state(sid) == "killed")
    check("T1 live indicator off (server-verified)",
          not bench.target.indicator_live())
    try:
        sess.act({"type": "move", "x": 10, "y": 10})
        check("T1 action after kill refused", False, "accepted!")
    except Exception as e:  # noqa: BLE001
        check("T1 action after kill refused", "kill" in str(e).lower(),
              str(e)[:80])
    cdp.close()
    try:
        sess.channel.close()
    except Exception:
        pass


def t2_kill_idle(bench):
    print("\n=== T2: kill with no live action ===", flush=True)
    sid, token, sess = bench.new_session()
    resp = bench.kill_via_api(sid, token)
    check("T2 idle kill relayed",
          resp.get("ok") and resp.get("target_relay") == "relayed",
          str(resp.get("target_relay")))
    check("T2 session KILLED", bench.session_state(sid) == "killed")
    try:
        sess.channel.close()
    except Exception:
        pass


def t3_kill_after_done(bench):
    print("\n=== T3: kill after the action completed ===", flush=True)
    sid, token, sess = bench.new_session()
    cdp, _ = bench.launch_browser(sess)
    r = sess.act({"type": "type", "text": "donemarker"})
    check("T3 action completed before kill",
          r and r[0]["typed_chars"] == 10)
    val = wait_for(
        lambda: cdp.evaluate("document.getElementById('t').value")
        or None, timeout=15, what="typed value")
    resp = bench.kill_via_api(sid, token)
    check("T3 kill relayed after completion",
          resp.get("ok") and resp.get("target_relay") == "relayed")
    check("T3 completed work NOT rolled back (honest)",
          val == "donemarker", repr(val))
    check("T3 session KILLED", bench.session_state(sid) == "killed")
    cdp.close()
    try:
        sess.channel.close()
    except Exception:
        pass


def t4_disconnect_without_kill(bench):
    print("\n=== T4: channel disconnect WITHOUT kill ===", flush=True)
    sid, token, sess = bench.new_session()
    cdp, _ = bench.launch_browser(sess)
    base = bench.input_len(cdp) or 0
    text = "Qw" * 1000               # 2000 chars
    outcome = {}

    def do_type():
        try:
            outcome["reply"] = sess.act({"type": "type", "text": text})
        except Exception as e:  # noqa: BLE001
            outcome["error"] = e

    # Slow-substrate simulation + target-side liveness counter, as in T1.
    cursor = bench.target._cursor
    assert cursor is not None
    with slow_substrate_keystroke(), count_keystroke_liveness(cursor) \
            as live_calls:
        th = threading.Thread(target=do_type, daemon=True)
        th.start()
        n_mid = wait_for(
            lambda: live_calls["n"] > 300 and live_calls["n"],
            timeout=30, what=">300 real is_live checks at the target")
        check("T4 typing in flight before disconnect", True,
              f"is_live calls={n_mid}")
        # disconnect the control channel WITHOUT any kill
        sess.channel.close()
        th.join(timeout=120)
    check("T4 type thread finished after disconnect", not th.is_alive())
    # Closing OUR channel end races the in-flight request: the waiting
    # thread may surface a local socket error from OUR close. That is a
    # controller-side artifact, not target behavior -- what must hold is
    # that the TARGET never refused or killed the orphaned action.
    if "error" in outcome:
        err = str(outcome["error"])
        check("T4 thread error (if any) is our local close, not a "
              "target refusal",
              "refused" not in err.lower() and "kill" not in err.lower()
              and "interrupt" not in err.lower(), err[:120])
        r = None
    else:
        r = outcome.get("reply")
        check("T4 orphaned type reply: all chars typed",
              r and r[0]["typed_chars"] == 2000, str(r)[:80])
    final = wait_for(
        lambda: (bench.input_len(cdp) or 0) >= base + 2000
        and bench.input_len(cdp), timeout=30, what="full text in DOM")
    check("T4 full 2000 chars landed (disconnect != kill)",
          final == base + 2000, f"base={base} final={final}")
    check("T4 session still LIVE (no kill happened)",
          bench.session_state(sid) == "live")
    # cleanup: kill it properly now
    resp = bench.kill_via_api(sid, token)
    check("T4 cleanup kill relayed",
          resp.get("ok") and resp.get("target_relay") == "relayed")
    cdp.close()


def t5_kill_racing_completion(bench):
    print("\n=== T5: kill racing action completion ===", flush=True)
    sid, token, sess = bench.new_session()
    # No browser: the race is kill vs one fast move action. Either
    # honest outcome is accepted; what must not happen is a crash or a
    # false claim.
    outcome = {}

    def do_move():
        try:
            outcome["reply"] = sess.act({"type": "move", "x": 111,
                                         "y": 222})
        except Exception as e:  # noqa: BLE001
            outcome["error"] = e

    th = threading.Thread(target=do_move, daemon=True)
    th.start()
    # kill immediately: it races the move
    resp = bench.kill_via_api(sid, token)
    th.join(timeout=60)
    check("T5 kill API did not crash the race",
          resp.get("ok") and resp.get("target_relay") == "relayed",
          str(resp.get("target_relay")))
    if "error" in outcome:
        err = str(outcome["error"]).lower()
        check("T5 race lost by the move: refused honestly",
              "kill" in err or "live" in err or "interrupt" in err
              or "refused" in err, str(outcome["error"])[:100])
    else:
        r = outcome.get("reply")
        check("T5 race won by the move: completed honestly",
              r and r[0].get("x") == 111, str(r)[:80])
    check("T5 session KILLED either way",
          bench.session_state(sid) == "killed")
    try:
        sess.channel.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
