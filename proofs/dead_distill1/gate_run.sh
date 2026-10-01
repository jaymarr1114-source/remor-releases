#!/usr/bin/env bash
# DEAD-DISTILL-1 gate: reproduces the mission's full evidence battery in
# fresh sequential processes. Run from the worktree root.
# NEVER run proof batteries in parallel on the 2-core host.
set -u
GATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TREE="$(cd "$GATE_DIR/../.." && pwd)"
cd "$TREE"
export PYTHONPATH="$TREE:$TREE/pylib"

pass=0; fail=0
ok() { echo "PASS: $1"; pass=$((pass+1)); }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

echo "=== b0: no references to removed names remain ==="
if grep -rn "DistillationController" --include="*.py" runtime/ pylib/ tests/ 2>/dev/null | grep -v __pycache__ | grep -q .; then
  bad "DistillationController references remain"
else
  ok "zero DistillationController references"
fi
if grep -rn "DeltaSession" --include="*.py" runtime/ pylib/ tests/ 2>/dev/null | grep -v __pycache__ | grep -q .; then
  bad "DeltaSession references remain"
else
  ok "zero DeltaSession references"
fi
if grep -rn "only delta source" --include="*.py" runtime/ pylib/ 2>/dev/null | grep -v __pycache__ | grep -q .; then
  bad "false 'only delta source' claim remains"
else
  ok "false 'only delta source' claim gone"
fi
if [ -f runtime/core/distillation_controller.py ]; then
  bad "distillation_controller.py still exists"
else
  ok "distillation_controller.py deleted"
fi

echo "=== b1: live-path behavioral fingerprint byte-identical ==="
python3 "$GATE_DIR/b1_capture.py" > /tmp/dd1_gate_after.json 2>&1
if diff -q "$GATE_DIR/before.json" /tmp/dd1_gate_after.json > /dev/null 2>&1; then
  ok "live DistillationLoop/distill_driver/validate_delta fingerprint byte-identical to pre-removal baseline"
else
  bad "live-path fingerprint differs from baseline"
  diff "$GATE_DIR/before.json" /tmp/dd1_gate_after.json | head -10
fi

echo "=== b2: surviving delta_capture tests green ==="
if python3 -m pytest tests/intellect/test_delta_capture.py -q 2>&1 | tail -1 | grep -q "13 passed"; then
  ok "13/13 surviving delta_capture tests pass"
else
  bad "delta_capture tests failed"
  python3 -m pytest tests/intellect/test_delta_capture.py -q 2>&1 | tail -3
fi

echo "=== b3: live distillation imports intact ==="
if python3 -c "
from swarm_engine.acquisition.distill import DistillationLoop
from runtime.acquisition.distill_driver import run_distillation_sweep, find_pending_deltas
from runtime.intellect.delta_capture import validate_delta, emit_delta, DeltaRefused
print('all live imports OK')
" 2>&1 | grep -q "all live imports OK"; then
  ok "live DistillationLoop/distill_driver/delta_capture imports intact"
else
  bad "live import broken"
fi

echo "=================================================="
echo "DEAD-DISTILL-1 GATE: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
