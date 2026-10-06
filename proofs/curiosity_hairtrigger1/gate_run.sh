#!/bin/bash
# CURIOSITY-HAIRTRIGGER-1 gate: the "no teacher" hair-trigger fires.
# Runs the proof battery in a fresh process, then the curiosity regressions.
set -e
WT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$WT"

echo "=== b1-b6: hair-trigger proof battery ==="
python3 proofs/curiosity_hairtrigger1/proof_battery.py
echo ""

echo "=== regressions: tests/curiosity/ ==="
python3 -m pytest tests/curiosity/ -q 2>&1 | tail -2
echo ""

echo "GATE CURIOSITY-HAIRTRIGGER-1: ALL BATTERIES PASS"
