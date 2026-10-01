#!/bin/bash
# BORROW-NATIVE-1 gate: the first genuine borrow-to-native cycle.
# Runs b1..b5 sequentially in fresh processes. b2 (real 8B inference,
# ~20-30 min) and b3 (distillation) are checkpointed under proofs/;
# pass --force to redo all inference/synthesis from scratch.
set -u
D="$(cd "$(dirname "$0")" && pwd)"
FORCE="${1:-}"
overall=0
for b in b1_native_gap b2_borrow b3_distill b4_independence b5_adversarial; do
  echo "=== $b ==="
  if [ "$FORCE" = "--force" ]; then
    timeout 3000 python3 "$D/$b.py" --force 2>&1 | tail -3
  else
    timeout 3000 python3 "$D/$b.py" 2>&1 | tail -3
  fi
  rc=${PIPESTATUS[0]}
  if [ $rc -ne 0 ]; then
    echo "GATE STOP: $b failed (exit $rc)"
    overall=1
    break
  fi
done
if [ $overall -eq 0 ]; then echo "GATE BORROW-NATIVE-1: ALL BATTERIES PASS"
else echo "GATE BORROW-NATIVE-1: FAIL"; fi
exit $overall
