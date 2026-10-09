"""HTTP adoption layer (§3/§4 operational + §5/§6 refusals): real HTTP against
a live server bound to a real engine. No mocks: urllib -> ThreadingHTTPServer
-> _Service -> SwarmEngine -> Composer/registry/DB.

Merged contract: every route requires the Bearer <redacted> (<base_dir>/api_token);
every mutating route additionally requires operator authentication
(X-Agent-Id/X-Agent-Token, the real operator credential from
<base_dir>/operator.token). The dedicated restore adversarial assertions
(no credentials / forged token / no-grant agent) are preserved as-is,
strengthened only with the Bearer <redacted> must pass to reach them.
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


def call(method, path, body=None, raw=None, headers=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
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

    import queue as _queue
    cmd_q = _queue.Queue()
    res_q = _queue.Queue()

    def serve():
        server, svc = run(db, "127.0.0.1", PORT)
        def _drain():
            while True:
                try:
                    fn = cmd_q.get_nowait()
                except _queue.Empty:
                    return
                try:
                    res_q.put(("ok", fn()))
                except Exception as exc:  # noqa: BLE001
                    res_q.put(("error", exc))
        server.service_actions = _drain
        # Main-engine admission: serves the capabilities / quarantine /
        # restore lifecycle assertions below.
        res = svc.engine.admission.admit(goal="add two numbers together",
                                         plan=dict(ADD_PLAN),
                                         caller=svc.engine.oracle)
        assert res.verdict == Verdict.ADMITTED, res.reasons
        box["server"] = server
        box["svc"] = svc
        box["cap_id"] = res.capability_id
        # Merged architecture (phase 4c proven): /api/intent/dispatch routes
        # through the intent service's DEDICATED engine, not svc.engine.
        # Admit the same plan there (engine-oracle caller) so dispatch can
        # route to it; the main-engine admission above serves the
        # capabilities/quarantine/restore lifecycle.
        intent_svc = svc.ff["intent"]
        ieng, _, _ = intent_svc._ensure()

        def _admit_intent():
            return ieng.admission.admit(goal="add two numbers together",
                                        plan=dict(ADD_PLAN),
                                        caller=ieng.oracle)

        ires = intent_svc._thread.run(_admit_intent)
        assert ires.verdict == Verdict.ADMITTED, ires.reasons
        from swarm_engine.governance.caller_authorization import AgentDirectory
        from swarm_engine.governance.oracle_binding import (
            DECISION_RESTORE, DECISION_TRUST_TRANSITION)
        agents = AgentDirectory(svc.engine.oracle_registry)
        cred = agents.register_agent(
            svc.engine.oracle, source="http test",
            decision_classes=(DECISION_RESTORE, DECISION_TRUST_TRANSITION))
        # Phase 3: restore also requires a covering HTTP permission. The
        # dedicated restore agent holds effectively-unlimited HTTP scope
        # plus the downstream restore/transition classes.
        agents.issue_http_permission(
            svc.engine.oracle, cred.agent_id, "effectively-unlimited")
        box["agent_headers"] = {"X-Agent-Id": cred.agent_id,
                                "X-Agent-Token": cred.token}
        nocred = agents.register_agent(svc.engine.oracle, source="http test no-grant")
        box["nogrant_headers"] = {"X-Agent-Id": nocred.agent_id,
                                  "X-Agent-Token": nocred.token}
        # Phase 3: the real operator credential, read from the token file
        # _Service provisioned at first boot (mode 0600). This is the
        # credential an actual operator would use.
        token_path = os.path.join(svc.base_dir, "operator.token")
        with open(token_path, "r", encoding="utf-8") as fh:
            operator_token = fh.read().strip()
        assert len(operator_token) == 64 and all(
            c in "0123456789abcdef" for c in operator_token), \
            f"operator token malformed: {token_path}"
        box["op_headers"] = {"X-Agent-Id": svc.operator_id,
                             "X-Agent-Token": operator_token}
        # Merged contract: the Bearer <redacted> gates EVERY route. Read the
        # real API token _Service provisioned at first boot (mode 0600).
        api_token_path = os.path.join(svc.base_dir, "api_token")
        with open(api_token_path, "r", encoding="utf-8") as fh:
            bearer_token = fh.read().strip()
        assert len(bearer_token) == 64 and all(
            c in "0123456789abcdef" for c in bearer_token), \
            f"api token malformed: {api_token_path}"
        box["bearer"] = bearer_token
        ready.set()
        server.serve_forever()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert ready.wait(timeout=120)

    def on_server_thread(fn):
        """Run fn() in the server (engine) thread; the engine is
        thread-affine, so direct engine calls must not run here."""
        cmd_q.put(fn)
        status, val = res_q.get(timeout=120)
        if status == "error":
            raise val
        return val
    server = box["server"]
    svc = box["svc"]
    cap_id = box["cap_id"]
    op = box["op_headers"]
    # Merged credential sets: Bearer on every call; operator on mutating
    # calls; the restore agent's credential on restore calls.
    BH = {"Authorization": "Bearer " + box["bearer"]}
    OPH = dict(BH)
    OPH.update(op)
    AH = dict(BH)
    AH.update(box["agent_headers"])
    NGH = dict(BH)
    NGH.update(box["nogrant_headers"])
    time.sleep(0.2)

    try:
        s, b = call("GET", "/api/health", headers=BH)
        check("http: health", s == 200 and b.get("ok") is True, f"{s} {b}")

        s, b = call("GET", "/api/capabilities", headers=BH)
        caps = b.get("capabilities", [])
        mine = [c for c in caps if c["capability_id"] == cap_id]
        check("http: capabilities lists admitted cap",
              s == 200 and len(mine) == 1 and mine[0]["effective_status"] == "active",
              f"{s} {b}")

        s, b = call("POST", "/api/intent/dispatch",
                    {"text": "add two numbers together", "args": {"a": 3, "b": 4}},
                    headers=OPH)
        check("http: intent dispatch works",
              s == 200 and b.get("ok") is True and b.get("result") == 7, f"{s} {b}")

        s, b = call("POST", "/api/intent/dispatch",
                    {"text": "bake a cake", "args": {}}, headers=OPH)
        check("http: ambiguous intent clarified over http (no dispatch)",
              s == 200 and b.get("mode") == "clarify"
              and b.get("kind") == "ambiguous",
              f"{s} {b}")

        s, b = call("POST", "/api/intent/dispatch",
                    {"text": "add two numbers together", "args": {"a": 1}},
                    headers=OPH)
        check("http: bad args refused over http",
              s == 200 and b.get("ok") is False and b.get("refusal") == "bad_arguments",
              f"{s} {b}")

        s, b = call("GET", "/api/dispatches", headers=BH)
        check("http: dispatch history", s == 200 and len(b.get("dispatches", [])) >= 1)

        # voice: honestly unavailable (not admitted at boot; piper absent).
        # The two endpoints carry different merged 501 shapes: stt uses the
        # typed UNAVAILABLE shape, tts the medium_not_admitted refusal.
        # ACQ-STT-1: stt is substrate-aware now — 200 with real transcripts
        # when the governed faster-whisper substrate is present, honest 501
        # without it (original intent preserved). Missing audio is 400.
        import base64 as _b64, io as _io, wave as _wave
        _buf = _io.BytesIO()
        with _wave.open(_buf, "wb") as _w:
            _w.setnchannels(1); _w.setsampwidth(2); _w.setframerate(16000)
            _w.writeframes(b"\x00" * 16000 * 2)
        _tiny_wav_b64 = _b64.b64encode(_buf.getvalue()).decode()
        s, b = call("POST", "/api/voice/stt", {"audio_b64": _tiny_wav_b64},
                    headers=BH)
        un = b.get("unavailable") or {}
        check("http: /api/voice/stt honest (200 with substrate / 501 without)",
              (s == 200 and b.get("ok") is True)
              or (s == 501
                  and un.get("capability") == "voice.speech_to_text"),
              f"{s} {str(b)[:160]}")
        s, b = call("POST", "/api/voice/stt", {"text": "hello"}, headers=BH)
        check("http: /api/voice/stt no audio -> 400",
              s == 400 and b.get("ok") is False, f"{s} {b}")
        s, b = call("POST", "/api/voice/tts", {"text": "hello"}, headers=BH)
        check("http: /api/voice/tts -> 501 honest unavailability",
              s == 501 and b.get("ok") is False
              and b.get("refusal") == "medium_not_admitted",
              f"{s} {b}")

        # media image/video: ADMITTED at boot (phase 4c proven) -- the
        # merged contract generates real bytes, not 501s.
        for path in ["/api/media/image", "/api/media/video"]:
            s, b = call("POST", path, {"text": "hello", "prompt": "a cat"},
                        headers=BH)
            res = b.get("result") or {}
            check(f"http: {path} -> 200 real media",
                  s == 200 and b.get("ok") is True and res.get("ok") is True
                  and isinstance(res.get("out_path"), str),
                  f"{s} {str(b)[:160]}")

        s, b = call("GET", "/api/voice/status", headers=BH)
        check("http: voice status", s == 200 and b["text_to_speech"]["available"] is False)
        s, b = call("GET", "/api/media/status", headers=BH)
        check("http: media status",
              s == 200 and b["generate_image"]["available"] is True
              and b["generate_video"]["available"] is True, f"{s} {b}")

        # governed restore over HTTP
        on_server_thread(lambda: quarantine_everywhere(
            svc.engine, cap_id, "http test quarantine",
            caller=svc.engine.oracle))
        s, b = call("GET", "/api/capabilities", headers=BH)
        mine = [c for c in b["capabilities"] if c["capability_id"] == cap_id][0]
        check("http: quarantine visible", mine["effective_status"] == "quarantined")

        s, b = call("POST", f"/api/capabilities/{cap_id}/restore",
                    {"reason": "http test restore"}, headers=AH)
        check("http: restore works", s == 200 and b.get("ok") is True, f"{s} {b}")
        s, b = call("GET", "/api/capabilities", headers=BH)
        mine = [c for c in b["capabilities"] if c["capability_id"] == cap_id][0]
        check("http: restored effective active",
              mine["effective_status"] == "active" and mine["consistent"] is True)

        # adversarial: no credentials -> 401 (Bearer <redacted> first)
        s, b = call("POST", f"/api/capabilities/{cap_id}/restore",
                    {"reason": "http test restore"})
        check("http: restore without credentials -> 401",
              s == 401 and b.get("ok") is False, f"{s} {b}")
        # adversarial: forged agent token -> 401 (Bearer valid, agent
        # credential forged)
        forged = dict(AH); forged["X-Agent-Token"] = "f" * 64
        s, b = call("POST", f"/api/capabilities/{cap_id}/restore",
                    {"reason": "http test restore"}, headers=forged)
        check("http: restore forged token -> 401",
              s == 401 and b.get("ok") is False, f"{s} {b}")
        # adversarial: authenticated but unauthorized -> 403
        s, b = call("POST", f"/api/capabilities/{cap_id}/restore",
                    {"reason": "http test restore"},
                    headers=NGH)
        check("http: restore unauthorized agent -> 403",
              s == 403 and b.get("ok") is False, f"{s} {b}")
        s, b = call("POST", f"/api/capabilities/{cap_id}/restore", {"reason": ""},
                    headers=AH)
        check("http: restore empty reason -> 409",
              s == 409 and b.get("ok") is False, f"{s} {b}")
        s, b = call("POST", "/api/capabilities/cap_nope/restore", {"reason": "x"},
                    headers=AH)
        check("http: restore unknown id -> 409", s == 409 and b.get("ok") is False)

        # dispatch works again after HTTP restore
        s, b = call("POST", "/api/intent/dispatch",
                    {"text": "add two numbers together", "args": {"a": 10, "b": 5}},
                    headers=OPH)
        check("http: dispatch after restore", s == 200 and b.get("result") == 15)

        # malformed input handling (400 precedes the operator gate, but the
        # Bearer <redacted> first)
        s, b = call("POST", "/api/intent/dispatch", raw=b"{not json",
                    headers=BH)
        check("http: malformed JSON -> 400", s == 400, f"{s} {b}")
        s, b = call("GET", "/api/nope", headers=BH)
        check("http: unknown route -> 404", s == 404)
    finally:
        server.shutdown()

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
