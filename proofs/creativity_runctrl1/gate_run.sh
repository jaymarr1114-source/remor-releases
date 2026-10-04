#!/usr/bin/env bash
# CREATIVITY-RUNCTRL-1 gate battery: fresh sequential processes, one command.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT_ROOT="${WT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
export WT_ROOT
export PYTHONPATH="$WT_ROOT:$WT_ROOT/pylib:${PYTHONPATH:-}"
FAIL=0
for i in 1 2 3; do
  echo "=== runctrl battery: fresh run $i/3 ==="
  python3 "$SCRIPT_DIR/proof_runctrl.py" || FAIL=1
done
echo "=== compile check ==="
python3 -m py_compile "$WT_ROOT/runtime/creativity/run_controller.py" || FAIL=1
if [ "$FAIL" -eq 0 ]; then
  echo "GATE_RUN: ALL GREEN"
else
  echo "GATE_RUN: FAILURES PRESENT"
fi
exit "$FAIL"
