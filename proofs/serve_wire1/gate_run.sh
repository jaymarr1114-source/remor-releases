#!/bin/bash
# SERVE-WIRE-1 gate: builtin provider through the registry into the serving path.
# Verifies: dogfooded registration, native stays native, FRM-governed escalation,
# labels survive, adversarial fail-closed, sustained load.
set -u
D="$(cd "$(dirname "$0")" && pwd)"
overall=0
for b in w1_dogfood w2_native_stays w3_escalation_labels w4_adversarial w5_sustained; do
  echo "=== $b ==="
  timeout 600 python3 "$D/$b.py" 2>&1 | tail -12
  rc=${PIPESTATUS[0]}
  if [ $rc -ne 0 ]; then
    echo "GATE STOP: $b failed (exit $rc)"
    overall=1
    break
  fi
done
if [ $overall -eq 0 ]; then echo "GATE SERVE-WIRE-1: ALL BATTERIES PASS"
else echo "GATE SERVE-WIRE-1: FAIL"; fi
exit $overall
