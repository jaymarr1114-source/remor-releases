#!/bin/bash
# MEMORY-UNIFY-1 gate: inventory-and-surgery per James's U-9 directive.
# Runs b1..b4 sequentially in fresh processes.
set -u
D="$(cd "$(dirname "$0")" && pwd)"
overall=0
for b in b1_inventory b2_live_path b3_helpers b4_regressions; do
  echo "=== $b ==="
  timeout 600 python3 "$D/$b.py" 2>&1 | tail -5
  rc=${PIPESTATUS[0]}
  if [ $rc -ne 0 ]; then
    echo "GATE STOP: $b failed (exit $rc)"
    overall=1
    break
  fi
done
if [ $overall -eq 0 ]; then echo "GATE MEMORY-UNIFY-1: ALL BATTERIES PASS"
else echo "GATE MEMORY-UNIFY-1: FAIL"; fi
exit $overall
