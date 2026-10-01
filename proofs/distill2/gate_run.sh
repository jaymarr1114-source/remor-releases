#!/bin/bash
# DISTILL-2 gate battery — reproduces the mission's full evidence in fresh
# sequential processes. Every check prints PASS/FAIL.
#
# What it does:
#   1. Verifies the llama-server binary exists (no rebuild needed).
#   2. Verifies the student weights are the pinned DISTILL-1 weights (sha256).
#   3. Verifies the license evidence is intact (Apache 2.0 at pinned rev).
#   4. Starts the persistent server via the lifecycle script, waits healthy.
#   5. Measures warm steady-state turns through the server vs the 2s budget.
#   6. Re-verifies the anti-wrapper proof (localhost only, no external conns).
#   7. Re-runs the DISTILL-1 ticket against server-path measurements, seals honestly.
#   8. Stops the server (so the fallback never contends with it for CPU).
#   9. Verifies the cold-start fallback path (process-spawning) still works.
set -u
TREE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$TREE" || exit 1
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $1 -- $2"; }

SRV="/home/hatch/workspace/tools/llama.cpp-b11284/llama-b11284/llama-server"
WEIGHTS="/home/hatch/workspace/models/qwen3-0_6b/Qwen3-0.6B-Q8_0.gguf"
PIN="9465e63a22add5354d9bb4b99e90117043c7124007664907259bd16d043bb031"

# 1. server binary
[ -x "$SRV" ] && ok "server_binary_present" || bad "server_binary_present" "no $SRV"

# 2. weights pinned
if [ -f "$WEIGHTS" ]; then
  H="$(sha256sum "$WEIGHTS" | cut -d' ' -f1)"
  [ "$H" = "$PIN" ] && ok "weights_pinned" || bad "weights_pinned" "got ${H:0:16}..."
else
  bad "weights_pinned" "weights missing"
fi

# 3. license evidence intact
grep -q "Apache" proofs/distill1_license/LICENSE 2>/dev/null \
  && ok "license_evidence" || bad "license_evidence" "license file missing"

# 4. start server, wait healthy
bash distill1/server_lifecycle.sh start > /tmp/d2_start.log 2>&1
bash distill1/server_lifecycle.sh health > /dev/null 2>&1 \
  && ok "server_healthy" || { bad "server_healthy" "$(tail -1 /tmp/d2_start.log)"; }

# 5. warm steady-state latency through the server
# warm-up: page the model in (first turns are slow, not representative)
for i in 1 2 3; do
  python3 distill1/server_client.py --turn "warm up $i" \
    --policy distill1/policy.txt > /dev/null 2>&1
done
python3 distill1/server_latency.py --turns distill1/turns_heldout.json \
  --policy distill1/policy.txt --out /tmp/d2_latency.json > /tmp/d2_lat.log 2>&1
if [ $? -eq 0 ]; then
  ok "latency_within_budget"
else
  bad "latency_within_budget" "$(tail -1 /tmp/d2_lat.log)"
fi
python3 - <<'PYEOF'
import json
d = json.load(open('/tmp/d2_latency.json'))
print(f"latency detail: n={len(d['turns'])} mean={d['mean_wall_s']}s "
      f"max={d['max_wall_s']}s verdict={d['verdict']}")
PYEOF

# 5b. cold-start (server start + first turn), documented separately — not budget-gated
bash distill1/server_lifecycle.sh stop > /dev/null 2>&1
COLD_START=$(date +%s.%N)
bash distill1/server_lifecycle.sh start > /tmp/d2_coldstart.log 2>&1
python3 distill1/server_client.py --turn "hello" --policy distill1/policy.txt \
  > /tmp/d2_first.log 2>&1
COLD_END=$(date +%s.%N)
COLD_S=$(python3 -c "print(f'{float('$COLD_END')-float('$COLD_START'):.1f}')")
echo "cold-start (start+first turn): ${COLD_S}s [documented, not gated]"
echo "$COLD_S" > /tmp/d2_cold_s.txt

# 6. anti-wrapper: client talks only to localhost; no external connections
python3 - <<'PYEOF'
import re, sys
src = open('distill1/server_client.py').read()
# literal URLs must be localhost/127.0.0.1; the host/port are parameters
# whose defaults are asserted below
urls = re.findall(r'https?://[^\s"\']+', src)
ext = [u for u in urls if '127.0.0.1' not in u and 'localhost' not in u
       and '{host}' not in u and '{port}' not in u]
if ext:
    print("non-local URL:", ext); sys.exit(1)
m = re.search(r'DEFAULT_HOST\s*=\s*"([^"]+)"', src)
mp = re.search(r'DEFAULT_PORT\s*=\s*(\d+)', src)
if not m or m.group(1) not in ("127.0.0.1", "localhost"):
    print("DEFAULT_HOST is not loopback"); sys.exit(1)
print(f"client defaults: {m.group(1)}:{mp.group(1)} (loopback)")
PYEOF
[ $? -eq 0 ] && ok "client_localhost_only" || bad "client_localhost_only" "non-local URL in client"
# live check: the server process itself holds no non-loopback TCP connections
SRVPID=$(cat distill1/server/server.pid 2>/dev/null)
EXT_CONNS=$(ss -tnp state established 2>/dev/null | grep "pid=$SRVPID" | \
            grep -v "127.0.0.1" | wc -l)
[ "$EXT_CONNS" -eq 0 ] && ok "no_external_connections" \
  || bad "no_external_connections" "$EXT_CONNS non-loopback server connections"

# 7. ticket re-run against server-path measurements (sealed honestly)
python3 distill1/reseal_ticket.py > /tmp/d2_ticket.log 2>&1
if [ $? -eq 0 ]; then
  ok "ticket_resealed"
  tail -3 /tmp/d2_ticket.log
else
  bad "ticket_resealed" "$(tail -2 /tmp/d2_ticket.log)"
fi

# 8. stop the server BEFORE the fallback check — the CLI fallback must not
#    contend with the server for CPU (heavy proofs run sequentially)
bash distill1/server_lifecycle.sh stop > /dev/null 2>&1
bash distill1/server_lifecycle.sh status | grep -q "not running" \
  && ok "server_stopped_clean" || bad "server_stopped_clean" "server still up"

# 9. cold-start fallback path still works (process-spawning, reference only —
#    not budget-gated; it exists so a dead server never strands the user).
#    Runs with the server DOWN so the two never contend.
timeout 180 python3 distill1/student_run.py --gguf "$WEIGHTS" \
  --turns distill1/turns_heldout.json --out /tmp/d2_fallback.json \
  --policy distill1/policy.txt --max-tokens 10 > /tmp/d2_cold.log 2>&1
if [ $? -eq 0 ] && [ -s /tmp/d2_fallback.json ]; then
  ok "fallback_path_works"
else
  bad "fallback_path_works" "$(tail -1 /tmp/d2_cold.log)"
fi

echo "==="
echo "DISTILL-2 GATE: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
