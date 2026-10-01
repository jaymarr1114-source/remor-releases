"""p5: sustained load -- 10 sequential production turns through the inlet.

No grant leakage, no state corruption, stable latency profile."""
import time
from common import make_service, check

svc = make_service()
walls = []
gids = []
prompts = [
    "Say hello in one short sentence.",
    "Name one primary color. One sentence.",
    "What is the capital of France? One sentence.",
    "Name one planet. One sentence.",
    "Say goodbye in one short sentence.",
    "What is 7 plus 5? One sentence.",
    "Name one ocean. One sentence.",
    "What color is grass? One sentence.",
    "Name one continent. One sentence.",
    "Say thanks in one short sentence.",
]
for i, p in enumerate(prompts):
    t0 = time.monotonic()
    r = svc.chat(p)
    walls.append(time.monotonic() - t0)
    check(f"p5_turn{i}_ok", r["ok"] is True and r["fast"]["ok"] is True,
          r["fast"].get("error"))
    check(f"p5_turn{i}_label", r["served"]["label"] == "[final]")
    gid = r["inlet"]["student_grant_id"]
    gids.append(gid)
    # Capture immediately: the student prunes per-grant consumption on
    # epoch rollover (correct per-epoch accounting).
    check(f"p5_turn{i}_charged", svc.grant_consumed_s(gid) > 0)

check("p5_distinct_grants", len(set(gids)) == 10,
      f"{len(set(gids))} distinct")

# No leakage: the student's per-grant ledger holds only grants this
# inlet issued (pruned-by-epoch grants are legitimately absent).
ledger = svc._student._consumed
check("p5_no_leakage", set(ledger.keys()) <= set(gids),
      f"ledger={len(ledger)} grants, phantom="
      f"{set(ledger.keys()) - set(gids)}")
check("p5_ledger_nonempty", len(ledger) > 0)

# Stable latency: no turn more than 4x the median (no blowup).
walls_sorted = sorted(walls)
median = walls_sorted[len(walls_sorted) // 2]
worst = max(walls)
check("p5_stable_latency", worst <= 4 * median,
      f"median={median:.2f}s worst={worst:.2f}s")
print(f"p5 walls: {[round(w, 2) for w in walls]}")
print("BATTERY p5 PASS")
