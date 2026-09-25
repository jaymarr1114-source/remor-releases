"""Plugin registry (§7), voice/media refusal plumbing (§5/§6), and
fresh-process persistence of the dispatch audit log."""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pylib"))

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.services import voice as voice_svc
from swarm_engine.services import media as media_svc
from swarm_engine.services.providers import (
    ProviderAdapter, ProviderRegistry)
from swarm_engine.services.unavailable import CapabilityUnavailable
from swarm_engine.synthesis.admission import Verdict
from swarm_engine.synthesis.intent_router import IntentRouter
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher

SCRATCH = os.path.dirname(os.path.abspath(__file__))
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


class _TestAdapter(ProviderAdapter):
    name = "test-echo"

    def operations(self):
        return ["echo"]

    def call(self, operation, payload):
        assert operation == "echo"
        return {"ok": True, "echo": payload}


ADD_PLAN = {
    "name": "add_two", "params": {"a": "num", "b": "num"},
    "steps": [{"id": "s1", "op": "add", "args": {"a": {"$param": "a"}, "b": {"$param": "b"}}}],
    "output": {"$step": "s1"},
}


def main():
    # ---- provider registry ----
    reg = ProviderRegistry()
    check("providers: empty registry lists none", reg.list() == [])
    try:
        reg.call("nope", "echo", {})
        check("providers: unknown provider refused", False)
    except CapabilityUnavailable as e:
        check("providers: unknown provider refused ABSENT",
              e.classification == "ABSENT" and e.missing, str(e))
    rec = reg.register(_TestAdapter())
    check("providers: register", rec.name == "test-echo" and rec.operations == ["echo"])
    out = reg.call("test-echo", "echo", {"k": "v"}, producer="t")
    check("providers: call routes to adapter", out == {"ok": True, "echo": {"k": "v"}})
    try:
        reg.call("test-echo", "nope", {})
        check("providers: unknown operation refused", False)
    except CapabilityUnavailable as e:
        check("providers: unknown operation refused", True)
    try:
        reg.register(lambda op, p: {})  # raw callable, not an adapter
        check("providers: raw callable rejected", False)
    except ValueError:
        check("providers: raw callable rejected", True)
    try:
        reg.call("test-echo", "echo", ["not", "a", "dict"])
        check("providers: non-dict payload rejected", False)
    except ValueError:
        check("providers: non-dict payload rejected", True)
    check("providers: unregister", reg.unregister("test-echo") is True)
    check("providers: unregister idempotent", reg.unregister("test-echo") is False)
    try:
        reg.call("test-echo", "echo", {})
        check("providers: unregistered refused", False)
    except CapabilityUnavailable:
        check("providers: unregistered refused", True)

    # ---- voice/media refusals ----
    for fn, cap in [(lambda: voice_svc.transcribe(), "voice.speech_to_text"),
                    (lambda: voice_svc.speak("hi"), "voice.text_to_speech"),
                    (lambda: media_svc.generate_image("a cat"), "media.generate_image"),
                    (lambda: media_svc.generate_video("a cat"), "media.generate_video")]:
        try:
            fn()
            check(f"refusal: {cap} raises", False)
        except CapabilityUnavailable as e:
            check(f"refusal: {cap} raises UNAVAILABLE",
                  e.classification == "UNAVAILABLE" and len(e.missing) > 0, str(e))
    vs = voice_svc.status()
    check("refusal: voice status honest",
          vs["speech_to_text"]["available"] is False
          and vs["text_to_speech"]["available"] is False)
    ms = media_svc.status()
    check("refusal: media status honest",
          ms["generate_image"]["available"] is False
          and ms["generate_video"]["available"] is False)

    # ---- fresh-process persistence of dispatch log + routing ----
    db = os.path.join(tempfile.mkdtemp(prefix="fresh_", dir=SCRATCH), "eng.db")
    eng = SwarmEngine(db_path=db)
    r = eng.admission.admit(goal="add two numbers together", plan=dict(ADD_PLAN))
    assert r.verdict == Verdict.ADMITTED
    disp = NLToolDispatcher(eng, IntentRouter(eng))
    d = disp.dispatch("add two numbers together", {"a": 11, "b": 22}, producer="fresh-test")
    assert d.ok and d.result == 33
    did = d.dispatch_id
    del eng

    probe = os.path.join(SCRATCH, "probe_dispatch_fresh.py")
    with open(probe, "w") as f:
        f.write(
            "import sys, os, json\n"
            "sys.path.insert(0, os.path.join(r'%s', '..', 'pylib'))\n" % SCRATCH +
            "from swarm_engine.core.engine import SwarmEngine\n"
            "from swarm_engine.synthesis.intent_router import IntentRouter\n"
            "from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher\n"
            "eng = SwarmEngine(db_path=r'%s')\n" % db +
            "disp = NLToolDispatcher(eng, IntentRouter(eng))\n"
            "hist = disp.history()\n"
            "d2 = disp.dispatch('add two numbers together', {'a': 1, 'b': 2})\n"
            "print(json.dumps({'n': len(hist), 'did': hist[0]['dispatch_id'] if hist else None,\n"
            "                 'ok': d2.ok, 'result': d2.result}))\n")
    out = subprocess.run([sys.executable, probe], capture_output=True, text=True, timeout=300)
    try:
        info = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:
        info = {}
        print("probe stderr:", out.stderr[-800:])
    check("fresh process: dispatch log persisted",
          info.get("n") == 1 and info.get("did") == did, str(info))
    check("fresh process: router+dispatch work after restart",
          info.get("ok") is True and info.get("result") == 3, str(info))

    print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
