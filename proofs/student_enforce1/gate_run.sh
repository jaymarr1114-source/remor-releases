#!/bin/bash
# STUDENT-ENFORCE-1 gate: single enforcement core serves both paths.
set -u
D="$(cd "$(dirname "$0")" && pwd)"
timeout 120 python3 "$D/e1_core.py" 2>&1 | tail -4
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ]; then echo "GATE STUDENT-ENFORCE-1: ALL BATTERIES PASS"
else echo "GATE STUDENT-ENFORCE-1: FAIL"; fi
exit $rc
