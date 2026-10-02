#!/bin/bash
# LEARNED-ESCALATION-1 gate: learned signals with heuristic fallback.
set -u
D="$(cd "$(dirname "$0")" && pwd)"
timeout 120 python3 "$D/l1_signals.py" 2>&1 | tail -3
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ]; then echo "GATE LEARNED-ESCALATION-1: ALL BATTERIES PASS"
else echo "GATE LEARNED-ESCALATION-1: FAIL"; fi
exit $rc
