#!/bin/bash
# PLUGIN-1 gate battery: every probe in a FRESH sequential process.
# Exit 0 only when everything is green. Re-run by Felix = the gate.
#
# Batteries run SEQUENTIALLY (never in parallel): on the 2-core host
# concurrent proofs contend and produce spurious failures that look
# like regressions.
set -u
WT="$(cd "$(dirname "$0")" && pwd)"
PROOF="$WT/proofs/plugin1"
export PYTHONPATH="$WT/pylib"
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
step p567_resource_probes.py cpu
step p567_resource_probes.py mem
step p567_resource_probes.py fsize
step p8_protocol_violation.py
step p9_unknown.py
step p10_http_seam.py
step p11_timeout.py

echo "=== canonical suites: seam behavior ==="
( cd "$WT" && python3 -m pytest tests/backend/test_execute_api.py \
    tests/contracts/test_execute_api.py -q ) || { echo "FAIL: execute_api suites"; fail=1; }
( cd "$WT" && python3 -m pytest tests/backend/test_sandbox.py -q ) || { echo "FAIL: test_sandbox"; fail=1; }

echo "=== shared inventory: retirement signal (expected, gate-time) ==="
# The 8 non-route tests must still pass: availability.py and REPORT_CODES
# are untouched (retirement happens at gate/landing time in main chat).
( cd "$WT" && python3 -m pytest tests/contracts/test_coming_soon.py -q \
    --deselect "tests/contracts/test_coming_soon.py::ComingSoonContractTest::test_route_backed_entries_match_live_handlers" ) \
    || { echo "FAIL: test_coming_soon non-route tests"; fail=1; }
# The route-backed test MUST now fail on exactly the external-bots entry:
# the route is real, so the probe gets a typed honest refusal instead of
# the 501. That failure IS the retirement signal for the gate.
sig_out=$(cd "$WT" && python3 -m pytest \
    "tests/contracts/test_coming_soon.py::ComingSoonContractTest::test_route_backed_entries_match_live_handlers" 2>&1)
echo "$sig_out" | grep -q "1 failed" \
    || { echo "FAIL: retirement signal absent (route may have regressed to 501)"; fail=1; }
echo "$sig_out" | grep -q "external-bots: expected typed unavailability" \
    || { echo "FAIL: retirement signal is not the external-bots entry"; fail=1; }
echo "$sig_out" | grep -q "PLUGIN_UNKNOWN" \
    || { echo "FAIL: retirement signal has the wrong refusal code"; fail=1; }
echo "retirement signal confirmed: external-bots entry is stale, route is real"

if [ "$fail" -eq 0 ]; then
  echo "PLUGIN-1 GATE: ALL GREEN"
else
  echo "PLUGIN-1 GATE: RED"
  exit 1
fi
