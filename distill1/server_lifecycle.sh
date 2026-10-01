#!/bin/bash
# DISTILL-2: llama-server lifecycle — start / stop / health / status.
# The server holds the DISTILL-1 student resident; turns go through it
# instead of spawning a fresh llama-cli per turn.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
TREE="$(cd "$HERE/../.." && pwd)"
SRV="/home/hatch/workspace/tools/llama.cpp-b11284/llama-b11284/llama-server"
WEIGHTS="/home/hatch/workspace/models/qwen3-0_6b/Qwen3-0.6B-Q8_0.gguf"
PORT="${DISTILL2_PORT:-18080}"
PIDF="$HERE/server/server.pid"
LOGF="$HERE/server/server.log"
mkdir -p "$HERE/server"

pid_of() { [ -f "$PIDF" ] && cat "$PIDF" || echo ""; }

healthy() { curl -s -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"ok"'; }

case "${1:-status}" in
  start)
    if healthy; then echo "already up (port $PORT)"; exit 0; fi
    p="$(pid_of)"; [ -n "$p" ] && kill -9 "$p" 2>/dev/null
    [ -x "$SRV" ] || { echo "no llama-server at $SRV"; exit 1; }
    [ -f "$WEIGHTS" ] || { echo "no weights at $WEIGHTS"; exit 1; }
    "$SRV" -m "$WEIGHTS" -c 512 -t 2 --host 127.0.0.1 --port "$PORT" \
        --log-disable > "$LOGF" 2>&1 &
    echo $! > "$PIDF"
    for i in $(seq 1 30); do
      healthy && { echo "up (pid $(cat "$PIDF"), port $PORT)"; exit 0; }
      sleep 2
    done
    echo "FAILED to become healthy; tail of $LOGF:"; tail -5 "$LOGF"; exit 1
    ;;
  stop)
    p="$(pid_of)"
    if [ -n "$p" ]; then kill "$p" 2>/dev/null; sleep 2; kill -9 "$p" 2>/dev/null; fi
    rm -f "$PIDF"; echo "stopped"
    ;;
  health)
    healthy && echo "healthy" || { echo "not healthy"; exit 1; }
    ;;
  status)
    p="$(pid_of)"
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
      echo "running (pid $p, port $PORT)"; healthy && echo "healthy" || echo "not responding"
    else
      echo "not running"
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|health|status}"; exit 1
    ;;
esac
