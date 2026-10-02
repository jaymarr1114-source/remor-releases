#!/bin/bash
# gate_run.sh — TRACE-GUIDED-SYNTH-1 full evidence battery.
#
# Runs every battery in a FRESH SEQUENTIAL process (one python3 invocation
# per battery, in order). set -e stops at the first failure. Inference
# batteries are checkpointed + resumable: a reboot costs at most one
# inference call, not the battery.
#
# Expected wall time: ~60-90 min (dominated by 24-32 real Qwen3-8B
# inference calls at ~1.5-3 min each, run sequentially to avoid the
# 2-core contention that corrupts proof results).
set -euo pipefail

P="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$P"

run() {
  echo "=================================================================="
  echo "GATE: $*"
  echo "=================================================================="
  python3 "$@"
}

# t1: native gap (no inference) — both purposes refused by name.
run t1_native_gap.py

# t8: Route B regression (no inference) — the refactor changed nothing.
run t8_route_b_regression.py

# t5: adversarial fail-closed (no inference; t5c runs one fresh-synthesis).
run t5_adversarial.py

# Technique 1: step_verify — borrow traces, distill, independence, antimem.
run borrow_traces.py step_verify
run t3_distill.py step_verify
run t4_independence.py step_verify
run t6_antimem.py step_verify

# Technique 2: shout_verify — same cycle, different op vocabulary.
run borrow_traces.py shout_verify
run t3_distill.py shout_verify
run t4_independence.py shout_verify
run t6_antimem.py shout_verify

echo "=================================================================="
echo "GATE COMPLETE: all TRACE-GUIDED-SYNTH-1 batteries green"
echo "=================================================================="
