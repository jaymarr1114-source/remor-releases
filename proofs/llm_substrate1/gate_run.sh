#!/bin/bash
# LLM-SUBSTRATE-1 gate battery: every probe in a FRESH sequential process.
# Exit 0 only when everything is green. Re-run by Felix = the gate.
#
# Batteries run SEQUENTIALLY (never in parallel): on the 2-core host
# concurrent proofs contend and produce spurious failures. P2 and P6 each
# perform one real Qwen3 inference (~2-4 min on this host); the rest are
# fast. Do NOT parallelize.
set -u
WT="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$WT/../.." && pwd)"
PROOF="$WT"
export PYTHONPATH="$ROOT/pylib"
SCRATCH="$PROOF/scratch"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"
export LLM_SUBSTRATE1_SCRATCH="$SCRATCH"

fail=0
step() {
  name="$1"; shift
  echo "=== $name $* ==="
  ( cd "$PROOF" && python3 "$name" "$@" ) || { echo "FAIL: $name $*"; fail=1; }
}

step p1_factory_wired.py
step p2_substrate_run.py
step p3_grantless_refused.py
step p4_insufficient_deferred.py
step p5_http_seam.py
step p6_runner_e2e.py

echo "=== canonical suites: agent api ==="
( cd "$ROOT" && python3 -m pytest tests/backend/test_agent_api.py \
    tests/contracts/test_agent_api.py -q ) || { echo "FAIL: agent_api suites"; fail=1; }

if [ "$fail" -eq 0 ]; then
  echo "LLM-SUBSTRATE-1 GATE: ALL GREEN"
else
  echo "LLM-SUBSTRATE-1 GATE: RED"
  exit 1
fi
