"""HTTP adoption layer (§3/§4 operational + §5/§6 refusals): real HTTP against
a live server bound to a real engine. No mocks: urllib -> ThreadingHTTPServer
-> _Service -> SwarmEngine -> Composer/registry/DB.
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pylib"))

from swarm_engine.services.http_adapter import _Service, _Handler, run
from http.server import HTTPServer
from swarm_engine.synthesis.admission import Verdict
from swarm_engine.synthesis.integrity import quarantine_everywhere

SCRATCH = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []
PORT = 8471


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def call(method, path, body=None, raw=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


ADD_PLAN = {
    "name": "add_two", "params": {"a": "num", "b": "num"},
    "steps": [{"id": "s1", "op": "add", "args": {"a": {"$param": "a"}, "b": {"$param": "b"}}}],
    "output": {"$step": "s1"},
}


def main():
    db = os.path.join(tempfile.mkdtemp(prefix="http_", dir=SCRATCH), "eng.db")
    # The engine is thread-affine: build the service, admit, and serve all
    # in ONE thread (the server thread). Client calls run in the main thread.
    box = {}
    ready = threading.Event()

    def serve():
        server, svc = run(db, "127.0.0.1", PORT)
        res = svc.engine.admission.admit(goal="add two numbers together",
                                         plan=dict(ADD_PLAN))
        assert res.verdict == Verdict.ADMITTED, res.reasons
        box["server"] = server
        box["svc"] = svc
        box["cap_id"] = res.capability_id
        ready.set()
        server.serve_forever()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert ready.wait(timeout=120)
    server = box["server"]
    svc = box["svc"]
    cap_id = box["cap_id"]
    time.sleep(0.2)

    try:
        s, b = call("GET", "/api/health")
        check("http: health", s == 200 and b.get("ok") is True, f"{s} {b}")

        s, b = call("GET", "/api/capabilities")
        caps = b.get("capabilities", [])
        mine = [c for c in caps if c["capability_id"] == cap_id]
        check("http: capabilities lists admitted cap",
              s == 200 and len(mine) == 1 and mine[0]["effective_status"] == "active",
              f"{s} {b}")

        s, b = call("POST", "/api/intent/dispatch",
                    {"text": "add two numbers together", "args": {"a": 3, "b": 4}})
        check("http: intent dispatch works",
              s == 200 and b.get("ok") is True and b.get("result") == 7, f"{s} {b}")

        s, b = call("POST", "/api/intent/dispatch",
                    {"text": "bake a cake", "args": {}})
        check("http: unknown intent refused over http",
              s == 200 and b.get("ok") is False and b.get("refusal") == "unknown_intent",
              f"{s} {b}")

        s, b = call("POST", "/api/intent/dispatch",
                    {"text": "add two numbers together", "args": {"a": 1}})
        check("http: bad args refused over http",
              s == 200 and b.get("ok") is False and b.get("refusal") == "bad_arguments",
              f"{s} {b}")

        s, b = call("GET", "/api/dispatches")
        check("http: dispatch history", s == 200 and len(b.get("dispatches", [])) >= 1)

        # voice / media honest refusals over real HTTP
        for path, cap in [("/api/voice/stt", "voice.speech_to_text"),
                          ("/api/voice/tts", "voice.text_to_speech"),
                          ("/api/media/image", "media.generate_image"),
                          ("/api/media/video", "media.generate_video")]:
            s, b = call("POST", path, {"text": "hello", "prompt": "a cat"})
            un = (b.get("unavailable") or {})
            check(f"http: {path} -> 501 UNAVAILABLE",
                  s == 501 and un.get("classification") == "UNAVAILABLE"
                  and un.get("capability") == cap and len(un.get("missing", [])) > 0,
                  f"{s} {b}")

        s, b = call("GET", "/api/voice/status")
        check("http: voice status", s == 200 and b["text_to_speech"]["available"] is False)
        s, b = call("GET", "/api/media/status")
        check("http: media status", s == 200 and b["generate_image"]["available"] is False)

        # governed restore over HTTP
        quarantine_everywhere(svc.engine, cap_id, "http test quarantine")
        s, b = call("GET", "/api/capabilities")
        mine = [c for c in b["capabilities"] if c["capability_id"] == cap_id][0]
        check("http: quarantine visible", mine["effective_status"] == "quarantined")

        s, b = call("POST", f"/api/capabilities/{cap_id}/restore", {"reason": "http test restore"})
        check("http: restore works", s == 200 and b.get("ok") is True, f"{s} {b}")
        s, b = call("GET", "/api/capabilities")
        mine = [c for c in b["capabilities"] if c["capability_id"] == cap_id][0]
        check("http: restored effective active",
              mine["effective_status"] == "active" and mine["consistent"] is True)

        s, b = call("POST", f"/api/capabilities/{cap_id}/restore", {"reason": ""})
        check("http: restore empty reason -> 409",
              s == 409 and b.get("ok") is False, f"{s} {b}")
        s, b = call("POST", "/api/capabilities/cap_nope/restore", {"reason": "x"})
        check("http: restore unknown id -> 409", s == 409 and b.get("ok") is False)

        # dispatch works again after HTTP restore
        s, b = call("POST", "/api/intent/dispatch",
                    {"text": "add two numbers together", "args": {"a": 10, "b": 5}})
        check("http: dispatch after restore", s == 200 and b.get("result") == 15)

        # malformed input handling
        s, b = call("POST", "/api/intent/dispatch", raw=b"{not json")
        check("http: malformed JSON -> 400", s == 400, f"{s} {b}")
        s, b = call("GET", "/api/nope")
        check("http: unknown route -> 404", s == 404)
    finally:
        server.shutdown()

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
