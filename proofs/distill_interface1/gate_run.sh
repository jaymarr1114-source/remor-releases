#!/bin/bash
set -u
D="$(cd "$(dirname "$0")" && pwd)"
timeout 30 python3 "$D/di1_contracts.py" 2>&1 | tail -3
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ]; then echo "GATE DISTILL-INTERFACE-FREEZE-1: ALL BATTERIES PASS"
else echo "GATE DISTILL-INTERFACE-FREEZE-1: FAIL"; fi
exit $rc
