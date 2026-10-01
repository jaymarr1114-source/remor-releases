#!/usr/bin/env bash
# REF-FIX-1 gate: reproduces the mission's entire evidence battery in fresh
# sequential processes. Exit 0 only if everything passes.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TREE="$(cd "$HERE/../.." && pwd)"
cd "$TREE" || exit 1

fail() { echo "GATE FAIL: $1"; exit 1; }

echo "=== b0: module imports cleanly in a fresh process ==="
python3 -c "import sys; sys.path.insert(0,'pylib'); from swarm_engine.acquisition.atomic_operators import tests_for_graph, synthesize_from_operator_graph; print('import ok')" \
  || fail "import"

echo "=== b1: behavioral equivalence vs the committed pre-fix baseline ==="
python3 proofs/ref_fix1/b1_capture.py proofs/ref_fix1/gate_after.json \
  || fail "b1 capture"
python3 - <<'EOF' || fail "b1 equivalence"
import json, sys
before = json.load(open('proofs/ref_fix1/before.json'))
after = json.load(open('proofs/ref_fix1/gate_after.json'))
assert set(before) == set(after), "case sets differ"
diffs = [k for k in before if before[k]['sha256'] != after[k]['sha256']]
if diffs:
    print("BYTE DIFFS:", diffs); sys.exit(1)
print(f"b1 equivalence: {len(after)} cases byte-identical to pre-fix baseline")
EOF

echo "=== b2: adversarial (former-NameError inputs) + caller contract ==="
python3 proofs/ref_fix1/b2_adversarial.py || fail "b2 adversarial"

echo "=== b3: no unbound 'tests' name remains in the module ==="
if grep -nE '(^|[^_.a-zA-Z"'"'"'])tests(\.append|\[)' runtime/acquisition/atomic_operators.py; then
  fail "bare 'tests' reference still present"
fi
echo "b3 clean: no bare 'tests' references"

echo "REF-FIX-1 GATE: ALL GREEN"
