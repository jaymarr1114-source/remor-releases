#!/bin/bash
# ROUTER-INLET-1 gate: the full evidence battery in fresh sequential
# processes. One command, visible pass/fail per battery, exit 0 only
# when every battery passes.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TREE="$(cd "$HERE/../.." && pwd)"
LC="$TREE/distill1/server_lifecycle.sh"

# Refuse to run under foreign load: measurements must be uncontended.
# pgrep -x matches the process NAME only (the real binary's comm is
# "llama-cli"); a -f full-cmdline match false-positives on shells whose
# own command line merely mentions llama-cli (e.g. quiet-wait loops).
if pgrep -x "llama-cli" >/dev/null 2>&1 || pgrep -x "llama-server" >/dev/null 2>&1; then
  echo "REFUSE: a foreign llama inference process is active; gate needs a quiet host"
  exit 1
fi

"$LC" start >/dev/null 2>&1 || { echo "could not start student server"; exit 1; }
trap '"$LC" stop >/dev/null 2>&1' EXIT

pass=0; fail=0
run_battery() {
  echo "=== battery: $1 ==="
  if (cd "$HERE" && python3 "$1"); then
    echo "--- $1: BATTERY PASS ---"; pass=$((pass+1))
  else
    echo "--- $1: BATTERY FAIL ---"; fail=$((fail+1))
  fi
}

# p2 (real 8B) runs after the cheap batteries so a cheap failure
# doesn't waste a ~2-minute inference.
run_battery p1_fast_turn.py
run_battery p3_issuance_charging.py
run_battery p4_adversarial.py
run_battery p5_sustained_load.py
run_battery p2_think_hard.py

echo "==="
echo "ROUTER-INLET-1 GATE: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
