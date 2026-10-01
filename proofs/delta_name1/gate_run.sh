#!/usr/bin/env bash
# DELTA-NAME-1 gate: reproduces the mission's full evidence battery in fresh
# sequential processes. Run from the worktree root.
set -u
WT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$WT"
export PYTHONPATH="$WT:$WT/pylib"
pass=0; fail=0

ok()   { echo "PASS: $1"; pass=$((pass+1)); }
bad()  { echo "FAIL: $1"; fail=$((fail+1)); }

echo "=== b1: byte-identical equivalence (old class vs new builder) ==="
if python3 proofs/delta_name1/b1_equivalence.py; then ok "b1 equivalence"; else bad "b1 equivalence"; fi

echo "=== b2: exactly one class DeltaRecord in the tree ==="
n=$(grep -rn "class DeltaRecord" --include="*.py" runtime/ pylib/ tests/ 2>/dev/null | grep -v __pycache__ | wc -l)
if [ "$n" -eq 1 ]; then ok "b2 one DeltaRecord class"; else bad "b2 found $n DeltaRecord classes"; fi
grep -rn "class DeltaRecord" --include="*.py" runtime/ pylib/ tests/ 2>/dev/null | grep -v __pycache__

echo "=== b3: ingest module surface ==="
if python3 -c "
from runtime.acquisition import ingest
assert hasattr(ingest, '_m1_delta_dict'), 'missing _m1_delta_dict'
assert not hasattr(ingest, 'DeltaRecord'), 'DeltaRecord still present'
print('ingest surface OK')
"; then ok "b3 ingest surface"; else bad "b3 ingest surface"; fi

echo "=== b4: acquisition test suites ==="
if python3 -m pytest tests/acquisition/ -q 2>&1 | tail -1 | grep -q "passed"; then
  python3 -m pytest tests/acquisition/ -q 2>&1 | tail -1
  ok "b4 acquisition suites"
else bad "b4 acquisition suites"; fi

echo "=== b5: M1->M2 adapter still produces a real M2 DeltaRecord ==="
if python3 -c "
from runtime.acquisition.loop_driver import CognitionLoop
from swarm_engine.acquisition.delta import DeltaRecord
m1 = {'delta_id': 'd1', 'X': 'obj',
      'Y': {'y_id': 'y1', 'action_count': 1, 'source': 's'},
      'Z': {'capabilities': [], 'primitives': [], 'vocabulary_size': 0, 'at': 0.0},
      'gap': 'g', 'T': {'technique_name': 't', 'required_tools': []},
      'E': [], 'D': [], 'V': {'status': 'unverified', 'method': None}, 'C': None}
rec = CognitionLoop._adapt_m1_to_m2(m1, [])
assert isinstance(rec, DeltaRecord), type(rec)
print('adapter OK:', rec.objective, '/', rec.technique)
"; then ok "b5 adapter"; else bad "b5 adapter"; fi

echo "=================================================="
echo "DELTA-NAME-1 GATE: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
