#!/bin/bash
# AUTO-ROUTE-1 gate: the full evidence battery in fresh sequential
# processes. One command, visible pass/fail per battery.
# Heavy batteries run sequentially; the two real-8B batteries (p2, p3)
# take ~2-3 min each. Do NOT run while another 8B inference is active.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TREE="$(cd "$HERE/../.." && pwd)"

pass=0; fail=0
run_battery() {
  echo "=== battery: $1 ==="
  if ( cd "$HERE" && python3 "$1" ); then
    pass=$((pass+1)); echo "--- $1: BATTERY PASS ---"
  else
    fail=$((fail+1)); echo "--- $1: BATTERY FAIL ---"
  fi
}

# Refuse to run if a foreign llama process is already active (our own
# student server is started below; anything else means contention).
if pgrep -f "llama-cli" >/dev/null 2>&1; then
  echo "FAIL: a llama-cli (8B) process is already running; refusing to contend on this 2-core host."
  exit 1
fi

"$TREE/distill1/server_lifecycle.sh" start || { echo "FAIL: student server would not start"; exit 1; }
trap '"$TREE/distill1/server_lifecycle.sh" stop' EXIT

run_battery p9_no_double_classifier.py
run_battery p4_grantless_both.py
run_battery p8_anti_masquerade.py
run_battery p1_fast_only.py
run_battery p5_revoked_mid_escalation.py
run_battery p7_budget_exhausted.py
run_battery p6_deep_transport_down.py
run_battery p2_explicit_escalation.py
run_battery p3_heuristic_escalation.py

echo "==="
echo "AUTO-ROUTE-1 GATE: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
