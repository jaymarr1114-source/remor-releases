"""ACQ-STT-1: /api/voice/stt route proof — real HTTP against a live server.

Proves the full call path: POST /api/voice/stt -> _stt_audio_from_body ->
voice.transcribe -> faster-whisper -> 200 with a real transcript.
Also: 400 on missing audio. The 501 (no substrate) path is proven by the
negative control (pre-wire tree) and the substrate-less existing test.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

WORKTREE = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(WORKTREE, "pylib"))
sys.path.insert(0, WORKTREE)
sys.path.insert(0, os.path.join(WORKTREE, "distill1"))  # governed_student

from swarm_engine.services.http_adapter import run  # noqa: E402

PORT = 8479
SCRATCH = tempfile.mkdtemp(prefix="acqstt1_route_")
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))


def call(method, path, body=None, headers=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def main() -> int:
    fixture = sys.argv[1]
    with open(fixture, "rb") as fh:
        wav_b64 = base64.b64encode(fh.read()).decode()

    db = os.path.join(SCRATCH, "eng.db")
    box, ready = {}, threading.Event()

    def serve():
        server, svc = run(db, "127.0.0.1", PORT)
        token_path = os.path.join(svc.base_dir, "api_token")
        with open(token_path, encoding="utf-8") as fh:
            box["bearer"] = fh.read().strip()
        box["server"] = server
        ready.set()
        server.serve_forever()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert ready.wait(timeout=120), "server did not start"
    BH = {"Authorization": "Bearer " + box["bearer"]}
    time.sleep(0.5)

    try:
        # 1. Real audio -> 200 with a real transcript from the model.
        s, b = call("POST", "/api/voice/stt", {"audio_b64": wav_b64},
                    headers=BH)
        check("route: POST /api/voice/stt -> 200 real transcript",
              s == 200 and b.get("ok") is True
              and "quick brown fox" in b.get("transcript", ""),
              f"{s} {str(b)[:200]}")
        check("route: transcript carries engine provenance",
              b.get("engine") == "faster-whisper"
              and "faster-whisper-base" in b.get("model", ""),
              f"{str(b)[:200]}")

        # 2. audio_path variant.
        s, b = call("POST", "/api/voice/stt", {"audio_path": fixture},
                    headers=BH)
        check("route: audio_path -> 200 real transcript",
              s == 200 and b.get("ok") is True
              and "quick brown fox" in b.get("transcript", ""),
              f"{s} {str(b)[:200]}")

        # 3. Missing audio -> 400, not a fabricated transcript.
        s, b = call("POST", "/api/voice/stt", {"text": "hello"}, headers=BH)
        check("route: no audio -> 400",
              s == 400 and b.get("ok") is False, f"{s} {b}")

        # 4. Voice status reports STT available (planning-visible).
        s, b = call("GET", "/api/voice/status", headers=BH)
        stt = b.get("speech_to_text", {})
        check("route: /api/voice/status shows STT available",
              s == 200 and stt.get("available") is True
              and stt.get("classification") == "PROVEN",
              f"{s} {str(b)[:200]}")
    finally:
        box["server"].shutdown()

    print(f"route_test: {len(PASS)} PASS, {len(FAIL)} FAIL")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
