#!/usr/bin/env bash
# CREATIVITY-BUDGET-1 evidence battery: every check in a fresh process,
# run sequentially. Exit 0 iff every check passes.
set -u
cd "$(dirname "$0")"

PASS=0
FAIL=0
FAILED_CHECKS=""

# The measurement replay is the heavy check; run it last.
CHECKS="grant_within_envelope refusal_beyond_envelope expansion_authorized expansion_expired expansion_wrong_scope ceiling_stop estimate_divergence split_requests_aggregated split_across_work_ids_bounded monetary_unpriced monetary_declared_enforced malformed_inputs measured_proposal"

for c in $CHECKS; do
  echo "=== check: $c ==="
  if python3 proof_budget.py --check "$c"; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    FAILED_CHECKS="$FAILED_CHECKS $c"
  fi
  echo ""
done

echo "=============================="
echo "PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -ne 0 ]; then
  echo "FAILED:$FAILED_CHECKS"
  exit 1
fi
echo "CREATIVITY-BUDGET-1 battery: all checks green"
exit 0
