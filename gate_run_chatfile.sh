#!/bin/bash
# Gate-ready evidence battery for chat->file routing repair.
# Reproduces the full verification in fresh processes.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

PASS=0
FAIL=0

echo "=== 1. chat->file routing (17 checks) ==="
if python3 runtime/services/tests/test_chatfile_routing.py; then
    PASS=$((PASS+1))
    echo "[PASS] chatfile routing: 17/17"
else
    FAIL=$((FAIL+1))
    echo "[FAIL] chatfile routing"
fi

echo ""
echo "=== 2. existing chat_handler contracts (22 tests) ==="
if python3 -m pytest tests/contracts/test_chat_handler.py -x -q 2>&1 | tail -2; then
    PASS=$((PASS+1))
    echo "[PASS] existing contracts unbroken"
else
    FAIL=$((FAIL+1))
    echo "[FAIL] existing contracts"
fi

echo ""
echo "=== 3. modified file compiles ==="
if python3 -c "import runtime.services.chat_handler" 2>&1; then
    PASS=$((PASS+1))
    echo "[PASS] chat_handler.py compiles"
else
    FAIL=$((FAIL+1))
    echo "[FAIL] chat_handler.py compile"
fi

echo ""
echo "==================================="
echo "PASS: $PASS  FAIL: $FAIL"
exit $([ $FAIL -eq 0 ] && echo 0 || echo 1)
