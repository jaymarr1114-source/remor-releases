#!/bin/bash
# PLUGIN-1 gate battery: every probe in a FRESH sequential process.
# Exit 0 only when everything is green. Re-run by Felix = the gate.
#
# Batteries run SEQUENTIALLY (never in parallel): on the 2-core host
# concurrent proofs contend and produce spurious failures that look
# like regressions.
set -u
WT="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$WT/../.." && pwd)"
PROOF="$WT"
export PYTHONPATH="$ROOT/pylib"
SCRATCH="$PROOF/scratch"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"
export PLUGIN1_SCRATCH="$SCRATCH"

fail=0
step() {
  name="$1"; shift
  echo "=== $name $* ==="
  ( cd "$PROOF" && python3 "$name" "$@" ) || { echo "FAIL: $name $*"; fail=1; }
}

step make_fixtures.py
step p1a_register.py
step p1b_persistence.py
step p2_happy_path.py
step p3_unsigned_refused.py
step p4_escape_probe.py
step p4b_jail_redirect.py
step p567_resource_probes.py cpu
step p567_resource_probes.py mem
step p567_resource_probes.py fsize
step p8_protocol_violation.py
step p9_unknown.py
step p10_http_seam.py
step p11_timeout.py

echo "=== canonical suites: seam behavior ==="
( cd "$ROOT" && python3 -m pytest tests/backend/test_execute_api.py \
    tests/contracts/test_execute_api.py -q ) || { echo "FAIL: execute_api suites"; fail=1; }
( cd "$ROOT" && python3 -m pytest tests/backend/test_sandbox.py -q ) || { echo "FAIL: test_sandbox"; fail=1; }

echo "=== shared inventory: post-retirement (entry retired at landing) ==="
# PLUGIN_REGISTRY_ABSENT was retired at gate/landing time (main chat):
# the availability.py entry is deleted and REPORT_CODES no longer lists
# it. The full coming-soon suite must now pass clean — no retirement
# signal, because there is nothing left to retire.
#
# Honest containment claim (PLUGIN-FIX-1, 2026-10-01): the plugin
# runtime enforces a real jail — fresh mount + network namespaces,
# pivot_root into the per-run dir (host root detached), read-only
# interpreter binds, rlimits. Filesystem escape is PREVENTED (p4: the
# host canary never comes into existence; p4b: absolute writes are
# neutralized inside the jail); network is absent. A user namespace is
# added only where the host lets the uid map be written — verified by
# probe, never assumed; the per-run posture disclosure states exactly
# what was established. If the jail cannot be established the run is
# refused with PLUGIN_CONTAINMENT_FAILED: a plugin never runs
# uncontained. No seccomp/syscall filtering — stated, not hidden.
( cd "$ROOT" && python3 -m pytest tests/contracts/test_coming_soon.py -q ) \
    || { echo "FAIL: test_coming_soon (post-retirement)"; fail=1; }
echo "retirement confirmed: PLUGIN_REGISTRY_ABSENT gone, suite green"

if [ "$fail" -eq 0 ]; then
  echo "PLUGIN-1 GATE: ALL GREEN"
else
  echo "PLUGIN-1 GATE: RED"
  exit 1
fi
