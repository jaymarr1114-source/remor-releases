#!/bin/bash
# Canonical root gate dispatcher.
#
# Convention (fixed 2026-10-01 after PLUGIN-1 clobbered the previous root
# gate): each mission's evidence battery lives at
# proofs/<mission>/gate_run.sh and is NEVER overwritten by a later
# mission. This root script dispatches the latest landed mission gates.
# When a new mission lands, append its gate invocation below — do not
# replace this file's contents with a single mission's battery.
#
# Batteries run SEQUENTIALLY (never in parallel): on the 2-core host
# concurrent proofs contend and produce spurious failures that look
# like regressions.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"

fail=0
run_gate() {
  echo "=== gate: $1 ==="
  bash "$ROOT/$1" || { echo "FAIL: $1"; fail=1; }
}

run_gate "proofs/plugin1/gate_run.sh"
run_gate "proofs/rd_easypair1/gate_run.sh"
run_gate "proofs/audio1/gate_run.sh"
run_gate "proofs/recurrence1/gate_run.sh"
run_gate "proofs/distill1/gate_run.sh"
run_gate "proofs/distill2/gate_run.sh"
run_gate "proofs/llm_substrate1/gate_run.sh"

if [ "$fail" -eq 0 ]; then
  echo "ROOT GATE: ALL GREEN"
else
  echo "ROOT GATE: RED"
  exit 1
fi
