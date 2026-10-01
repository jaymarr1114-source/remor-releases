#!/bin/bash
# DISTILL-1 gate_run.sh — reproduces the mission's full evidence battery
# in fresh sequential processes. Felix's gate is one command.
#
# What it does:
#   1. Validates the 4 delta records against the charter schema.
#   2. Verifies the technique pack module imports and renders.
#   3. Validates the ticket schema and reports its sealed status.
#   4. Verifies model weights exist on disk with pinned SHA-256.
#   5. Verifies license evidence (Apache 2.0 at pinned revision).
#   6. Reports the blinded eval tally (pre-registered rubric).
#
# What it does NOT do (requires hours, done once, evidence attached):
#   - Re-run teacher demonstrations (see distill1/teacher_demos.json).
#   - Re-run base/student held-out inference (see *_heldout_out.json).
#   - Re-run the latency battery (see ticket evidence).
#
# Exit 0 if all checks pass, 1 otherwise.
set -u
TREE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$TREE" || exit 1
PASS=0; FAIL=0

check() { # $1=name $2=condition(0=pass)
  if [ "$2" -eq 0 ]; then echo "PASS: $1"; PASS=$((PASS+1));
  else echo "FAIL: $1"; FAIL=$((FAIL+1)); fi
}

export PYTHONPATH="$TREE/pylib:$TREE"

# 1. Delta records validate
python3 - <<'PYEOF'
import sys, json, glob
sys.path.insert(0, "pylib")
from swarm_engine.intellect.delta_capture import validate_delta
bad = []
for p in sorted(glob.glob("distill1/delta_records/*.json")):
    errs = validate_delta(json.load(open(p)))
    if errs: bad.append((p, errs))
if bad:
    print(bad); sys.exit(1)
print(f"4 delta records VALID")
PYEOF
check "delta_records_validate" $?

# 2. Technique pack imports and renders
python3 -c "
import sys; sys.path.insert(0, '.')
from distill1.student_policy import render_policy, provenance, TECHNIQUES
assert len(TECHNIQUES) == 4, 'expected 4 techniques'
p = render_policy()
assert len(p) > 50, 'policy too short'
prov = provenance()
assert len(prov['delta_records']) == 4
print('technique pack OK:', list(TECHNIQUES.keys()))
"
check "technique_pack_imports" $?

# 3. Ticket schema valid and sealed
python3 -c "
import sys, json; sys.path.insert(0, '.')
from distill1.ticket import validate
t = json.load(open('distill1/ticket_distill1.json'))
errs = validate(t)
assert not errs, errs
print('ticket', t['ticket_id'], '->', t['result']['status'])
print('checks:', json.dumps(t['result']['checks']))
"
check "ticket_valid" $?

# 4. Weights on disk with pinned hash
python3 - <<'PYEOF'
import hashlib
path = "/home/hatch/workspace/models/qwen3-0_6b/Qwen3-0.6B-Q8_0.gguf"
want = "9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"
h = hashlib.sha256()
with open(path, "rb") as fh:
    for c in iter(lambda: fh.read(1 << 20), b""): h.update(c)
got = h.hexdigest()
assert got == want, f"hash mismatch: {got}"
print(f"weights OK: {path} ({got[:16]}...)")
PYEOF
check "weights_pinned" $?

# 5. License evidence
python3 -c "
lic = open('proofs/distill1_license/LICENSE').read()
assert 'Apache License' in lic and 'Version 2.0, January 2004' in lic, 'not Apache 2.0'
import json
meta = json.load(open('proofs/distill1_license/model_metadata.json'))
assert meta['pinned_revision'] == '23749fefcc72300e3a2ad315e1317431b06b590a'
assert meta['license'] == 'apache-2.0'
print('license OK: Apache 2.0 at', meta['pinned_revision'][:12])
"
check "license_evidence" $?

# 6. Blinded eval tally reproduces
python3 distill1/eval_grade.py --tally distill1/grading_sheet_scored.json \
  --key distill1/grading_sheet.json.key.json > /tmp/gate_tally.json 2>&1
check "eval_tally_runs" $?
python3 -c "
import json
t = json.load(open('/tmp/gate_tally.json'))
print('quality bar:', 'PASS' if t['pass_bar']['PASS'] else 'FAIL',
      f\"(student={t['student']['mean']} base={t['base']['mean']})\")
print('NOTE: ticket DISTILL-1 sealed FAIL on latency (see ticket).')
"
check "eval_tally_reports" $?

echo "==="
echo "gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
