#!/bin/bash
# RD-EASYPAIR-1 gate: reproduces the mission's full evidence battery in
# fresh sequential processes. Felix's independent re-run is this one
# command (from anywhere):
#
#   proofs/rd_easypair1/gate_run.sh
#
# Batteries run SEQUENTIALLY (never in parallel): on the 2-core host
# concurrent proofs contend and produce spurious failures that look
# like regressions.
#
# Batteries:
#   1. python-bench: discovery beacon validation + pairing protocol
#      over real TLS against a fake tablet + phone route handlers
#      (tap-to-pair, tablet consent/token flow, auth refusals).
#   2. java-compile + java-unit: the tablet-side pure-Java proto/
#      (DiscoveryBeacon, PairingServer state machine, frame dispatch).
#   3. gui: the tap-to-pair UI in headless Chromium against the real
#      inproc backend + a real fake tablet (scan -> code comparison ->
#      confirm -> paired; cancel path; no JS errors).
#
# Exit 0 only if every battery passes.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0

# Toolchain: the Java batteries need javac. If it is not already on PATH,
# fall back to the workspace JDKs used by the mobile builds (checked in
# order); fail with a clear message if none is found.
if ! command -v javac >/dev/null 2>&1; then
  for CAND in "$HOME/workspace/remor_mobile/jdk/jdk-17.0.20.1+1/bin" \
              "$HOME/workspace/sdks/rd-target-android-1/jdk17/bin"; do
    if [ -x "$CAND/javac" ]; then
      export PATH="$CAND:$PATH"
      echo "gate: using javac at $CAND"
      break
    fi
  done
fi
if ! command -v javac >/dev/null 2>&1; then
  echo "gate: ERROR: javac not found on PATH and no workspace JDK matched" >&2
  exit 1
fi

run_battery() {
  local name="$1"; shift
  echo "=================================================================="
  echo "BATTERY: $name"
  echo "=================================================================="
  if "$@"; then
    echo "BATTERY PASS: $name"
    PASS=$((PASS + 1))
  else
    echo "BATTERY FAIL: $name"
    FAIL=$((FAIL + 1))
  fi
  echo
}

# 1. Python bench battery
run_battery "python-bench (discovery, pairing, routes)" \
  python3 tests/test_easypair.py

# 2. Java unit tests
JAVA_CLASSES="/tmp/rd-easypair-gate-classes"
run_battery "java-compile (proto/)" \
  bash -c "rm -rf '$JAVA_CLASSES' && mkdir -p '$JAVA_CLASSES' && \
    javac -encoding UTF-8 -d '$JAVA_CLASSES' \
      runtime/remote_dispatch/android/app/app/src/main/java/com/remor/dispatchtarget/proto/*.java"
if [ -d "$JAVA_CLASSES" ]; then
  run_battery "java-unit (PairingServer, DiscoveryBeacon)" \
    bash -c "javac -encoding UTF-8 -cp '$JAVA_CLASSES' -d '$JAVA_CLASSES' \
      tests/java/com/remor/dispatchtarget/proto/PairingServerTest.java && \
      java -cp '$JAVA_CLASSES' com.remor.dispatchtarget.proto.PairingServerTest"
fi

# 3. GUI test
run_battery "gui (tap-to-pair in headless Chromium)" \
  python3 tests/test_gui.py

echo "=================================================================="
echo "GATE RESULT: $PASS batteries passed, $FAIL failed"
echo "=================================================================="
if [ "$FAIL" -ne 0 ]; then
  echo "GATE: FAIL"
  exit 1
fi
echo "GATE: PASS"
