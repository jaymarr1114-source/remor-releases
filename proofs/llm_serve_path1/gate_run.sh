#!/bin/bash
# LLM-SERVE-PATH-1 gate: sufficiency battery + provider interface + wiring.
# b1/b4/b5/b7 need real 8B inference (~2-3 min each); b2 reuses b1's
# timings; b3/b6 need no inference. Runs sequentially in fresh processes.
set -u
D="$(cd "$(dirname "$0")" && pwd)"
overall=0
for b in b1_correctness b2_latency b3_errors b4_governance b5_isolation b6_registry b7_end_to_end; do
  echo "=== $b ==="
  if [ "$b" = "b1_correctness" ]; then
    timeout 1800 python3 "$D/$b.py" 2>&1 | tee "$D/b1_run.out" | tail -5
  else
    timeout 1800 python3 "$D/$b.py" 2>&1 | tail -8
  fi
  rc=${PIPESTATUS[0]}
  if [ $rc -ne 0 ]; then
    echo "GATE STOP: $b failed (exit $rc)"
    overall=1
    break
  fi
done
if [ $overall -eq 0 ]; then echo "GATE LLM-SERVE-PATH-1: ALL BATTERIES PASS"
else echo "GATE LLM-SERVE-PATH-1: FAIL"; fi
exit $overall
