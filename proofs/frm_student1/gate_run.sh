#!/bin/bash
# FRM-STUDENT-1 gate: the governed student inlet.
# Reproduces the full evidence battery in fresh sequential processes.
# The llama-server must be up for the live-turn checks (P6, P8).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TREE="$(cd "$HERE/../.." && pwd)"
cd "$TREE" || exit 1

pass=0; fail=0
run() {
  local name="$1"; shift
  local out
  if out=$(python3 "$HERE/$1" 2>&1); then
    echo "$out" | tail -2
    pass=$((pass+1))
  else
    echo "$out" | tail -5
    echo "FAIL: $name"; fail=$((fail+1))
  fi
}

echo "--- starting llama-server (DISTILL-2 path) ---"
bash distill1/server_lifecycle.sh start || { echo "server failed to start"; exit 1; }

run "p1_grantless"        p1_grantless.py
run "p2_fakegrant"        p2_fakegrant.py
run "p3_enforcement"      p3_enforcement.py
run "p4_exhausted"        p4_exhausted.py
run "p5_concurrent"       p5_concurrent.py
run "p6_real_turn"        p6_real_turn.py
run "p7_server_down"      p7_server_down.py
run "p8_interface_intact" p8_interface_intact.py

echo "--- stopping llama-server ---"
bash distill1/server_lifecycle.sh stop >/dev/null 2>&1

echo "==="
if [ "$fail" -eq 0 ]; then
  echo "FRM-STUDENT-1 GATE: $pass passed, 0 failed"
else
  echo "FRM-STUDENT-1 GATE: $pass passed, $fail FAILED"; exit 1
fi
