#!/bin/bash
# USER-PROVIDER-1 gate: user provider via the public contract.
set -u
D="$(cd "$(dirname "$0")" && pwd)"
timeout 120 python3 "$D/u1_contract.py" 2>&1 | tail -3
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ]; then echo "GATE USER-PROVIDER-1: ALL BATTERIES PASS"
else echo "GATE USER-PROVIDER-1: FAIL"; fi
exit $rc
