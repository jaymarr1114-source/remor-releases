#!/bin/bash
set -u
D="$(cd "$(dirname "$0")" && pwd)"
timeout 30 python3 "$D/calib_consume.py" 2>&1 | tail -3
rc=${PIPESTATUS[0]}
if [ $rc -eq 0 ]; then echo "GATE QWEN3-CALIB-1: ALL BATTERIES PASS"
else echo "GATE QWEN3-CALIB-1: FAIL"; fi
exit $rc
