"""Causal tests for NL intent dispatch + its metering (Tasks 1+2).

Bar: real engine, real HTTP, real sqlite, real refusal paths. Every
behavior below is revert-paired where a behavior is claimed: the
"without" case is constructed by removing the real cause (no admission,
quarantine, tampered plan), never by stubbing the machinery.

Machinery tests drive IntentDispatchService directly (the engine thread
marshal is real). HTTP tests drive POST /api/intent/dispatch and
GET /api/dispatches over a live serve() exactly as the GUI's Dispatch
view calls them. Metering tests prove the 35/day and 25-queue caps
through the REAL guarded-submit path.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.http_adapter import (  # noqa: E402
    serve, close_services)
from swarm_engine.services.scheduler import RunScheduler  # noqa: E402
from swarm_engine.services.metering import (  # noqa: E402
    build_metering_service, DAILY_CAP_CODE, QUEUE_CAP_CODE)
from swarm_engine.services.intent_dispatch_api import (  # noqa: E402
    IntentDispatchService)
from swarm_engine.synthesis.admission import Verdict  # noqa: E402

WRITE_PLAN = {
    "name": "write_note",
    "params": {"path": "str", "content": "str"},
    "steps": [{"id": "w", "op": "write_text",
               "args": {"path": {"$param": "path"},
                        "content": {"$param": "content"}}}],
    "result": {"$step": "w"},
}
WRITE_GOAL = "write a short note to a file"
WRITE_TEXT = "please write the deployment note"


def _map_plan(name, op, bound_val):
    return {
        "name": name,
        "params": {"values": "list"},
        "steps": [{
            "id": "mapped", "op": "map",
            "args": {
                "items": {"$param": "values"},
                "fn": {"$partial": {"op": op, "bound": {"b": bound_val},
                                   "free": ["a"]}},
            },
        }],
        "result": {"$step": "mapped"},
    }


def _service_on_scratch(testcase):
    tmp = tempfile.mkdtemp(prefix="intent_test_")
    testcase.addCleanup(
        lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
    sched = RunScheduler(db_path=os.path.join(tmp, "sched.db"),
                         runtime_db_path=os.path.join(tmp, "rt.db"))
    testcase.addCleanup(sched.close)
    met = build_metering_service(sched)
    svc = IntentDispatchService(os.path.join(tmp, "intent"), sched, met)
    sched.register_intent_executor(svc.execute_for_run)
    return tmp, sched, met, svc


def _quarantine_authorized(svc, cap_id, reason):
    """Authorized test-only quarantine (bypasses the stale
    quarantine_capability runtime helper which has no caller)."""
    from swarm_engine.synthesis import integrity as _integrity
    ieng, _, _ = svc._ensure()
    return svc._thread.run(
        lambda: _integrity.quarantine_everywhere(
            ieng, cap_id, reason=reason, caller=ieng.oracle))


def _admit_authorized(svc, goal, plan, name=None):
    """Authorized test-only admission wrapper (replaces the stale
    admit_capability helper which has no caller)."""
    ieng, _, _ = svc._ensure()

    def _do_admit():
        return ieng.admission.admit(goal=goal, plan=dict(plan),
                                    name=name, caller=ieng.oracle)

    res = svc._thread.run(_do_admit)
    assert res.verdict == Verdict.ADMITTED, res.reasons
    return {"ok": True, "capability_id": res.capability_id}


def _admit_writer(svc, tmp, goal=WRITE_GOAL, text=WRITE_TEXT, name="write_note"):
    svc.issue_grant("write_fs", tmp + "/*", note="test grant")
    # Authorized test-only admission: the stale admit_capability helper
    # has no caller; admit directly through the intent engine with the
    # engine-oracle caller (merged default-deny contract).
    adm = _admit_authorized(svc, goal, dict(WRITE_PLAN), name=name)
    cap_id = adm["capability_id"]
    svc.bind_goal(text, cap_id)
    return cap_id


class TestIntentMachinery(unittest.TestCase):
    """Task 1a/1b/1c at the machinery level (real engine, no HTTP)."""

    def setUp(self):
        self.tmp, self.sched, self.met, self.svc = _service_on_scratch(self)

    def test_direct_answer_unknown_intent(self):
        """'5 times 6' and a capabilities meta-question: no matching
        capability exists, so the honest outcome is the fail-closed
        refusal -- NOT an invented answer. There is no answer-generation
        substrate anywhere in the runtime."""
        for text in ("5 times 6",
                     "what capabilities do you have and what can you do?"):
            route = self.svc.route(text)
            self.assertFalse(route.ok, text)
            self.assertEqual(route.refusal, "unknown_intent", text)
            res = self.svc.dispatch_direct(text, producer="test")
            self.assertFalse(res.ok)
            self.assertEqual(res.refusal, "unknown_intent")
        # Refusals are never recorded in the audit table.
        self.assertEqual(self.svc.dispatch_history(limit=10), [])

    def test_create_file_exact_goal(self):
        """Real admission -> NL text -> real Composer execution -> real
        file on disk. Causal: without the capability, the same text
        refuses unknown_intent (revert pair)."""
        # revert: no capability admitted -> refusal
        res0 = self.svc.dispatch_direct(
            WRITE_TEXT, {"path": os.path.join(self.tmp, "r0.txt"),
                         "content": "x"},
            producer="test")
        self.assertFalse(res0.ok)
        self.assertEqual(res0.refusal, "unknown_intent")

        cap_id = _admit_writer(self.svc, self.tmp)
        target = os.path.join(self.tmp, "deploy_note.txt")
        res = self.svc.dispatch_direct(
            WRITE_TEXT, {"path": target, "content": "note-bytes-123"},
            producer="test")
        self.assertTrue(res.ok, res.as_dict())
        self.assertEqual(res.capability_id, cap_id)
        self.assertEqual(res.route_via, "exact_goal")
        self.assertTrue(res.dispatch_id.startswith("dsp_"))
        with open(target) as f:
            self.assertEqual(f.read(), "note-bytes-123")
        # audit row persisted with digests
        rows = self.svc.dispatch_history(limit=10)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["dispatch_id"], res.dispatch_id)
        self.assertEqual(row["capability_id"], cap_id)
        self.assertEqual(row["request_text"], WRITE_TEXT)
        self.assertEqual(row["ok"], 1)
        self.assertTrue(row["input_digest"])
        self.assertTrue(row["result_digest"])

    def test_toctou_quarantine_between_route_and_dispatch(self):
        """Route (genuinely ok) -> REAL quarantine_everywhere ->
        dispatch of the held route -> invocation-time re-verification
        refuses capability_unavailable. Revert pair: no quarantine ->
        the same held route dispatches fine."""
        cap_id = _admit_writer(self.svc, self.tmp)
        args = {"path": os.path.join(self.tmp, "t1.txt"), "content": "t"}

        # revert: held route without quarantine dispatches
        route_ok = self.svc.route(WRITE_TEXT)
        self.assertTrue(route_ok.ok)
        r_ok = self.svc.dispatch_with_route(route_ok, WRITE_TEXT, args,
                                            producer="test")
        self.assertTrue(r_ok.ok, r_ok.as_dict())

        # real case: quarantine lands between route and dispatch
        route = self.svc.route(WRITE_TEXT)
        self.assertTrue(route.ok)
        self.assertEqual(route.capability_id, cap_id)
        _quarantine_authorized(self.svc, cap_id, reason="test toctou probe")
        r = self.svc.dispatch_with_route(route, WRITE_TEXT, args,
                                         producer="test")
        self.assertFalse(r.ok)
        self.assertEqual(r.refusal, "capability_unavailable", r.as_dict())
        self.assertEqual(r.capability_id, cap_id)

    def test_plan_tamper_between_route_and_dispatch(self):
        """Mutating the stored plan bytes after a genuine route ->
        the fingerprint re-verification refuses capability_tampered.
        Revert: restoring the bytes -> dispatch succeeds again."""
        cap_id = _admit_writer(self.svc, self.tmp,
                               goal="write a second note",
                               text="please write the secondary note",
                               name="write_note2")
        text = "please write the secondary note"
        route = self.svc.route(text)
        self.assertTrue(route.ok)
        self.assertEqual(route.capability_id, cap_id)

        con = sqlite3.connect(self.svc.db_path)
        try:
            orig = con.execute(
                "SELECT plan_json FROM plan_capabilities WHERE "
                "capability_id=?", (cap_id,)).fetchone()[0]
            tampered = orig.replace("write_text", "write_textX")
            self.assertNotEqual(tampered, orig)
            con.execute("UPDATE plan_capabilities SET plan_json=? WHERE "
                        "capability_id=?", (tampered, cap_id))
            con.commit()
        finally:
            con.close()
        args = {"path": os.path.join(self.tmp, "m1.txt"), "content": "m"}
        r = self.svc.dispatch_direct(text, args, producer="test")
        self.assertFalse(r.ok)
        self.assertEqual(r.refusal, "capability_tampered", r.as_dict())
        self.assertFalse(os.path.exists(args["path"]))

        con = sqlite3.connect(self.svc.db_path)
        try:
            con.execute("UPDATE plan_capabilities SET plan_json=? WHERE "
                        "capability_id=?", (orig, cap_id))
            con.commit()
        finally:
            con.close()
        r2 = self.svc.dispatch_direct(text, args, producer="test")
        self.assertTrue(r2.ok, r2.as_dict())
        with open(args["path"]) as f:
            self.assertEqual(f.read(), "m")

    def test_ambiguous_intent_two_near_tied(self):
        """Two genuinely near-tied capabilities (both score 0.650, gap
        0.000 < margin 0.15) -> the real ambiguous_intent refusal; the
        refused dispatch is never recorded and nothing executes."""
        a = _admit_authorized(self.svc,
            "scale each value by three",
            _map_plan("triple_all", "multiply", 3), name="triple_all")
        b = _admit_authorized(self.svc,
            "raise each value by three",
            _map_plan("bump_all", "add", 3), name="bump_all")
        self.assertTrue(a["ok"] and b["ok"])
        self.assertNotEqual(a["capability_id"], b["capability_id"])
        text = "handle the 3 values"
        route = self.svc.route(text)
        self.assertFalse(route.ok)
        self.assertEqual(route.refusal, "ambiguous_intent", route.as_dict())
        before = len(self.svc.dispatch_history(limit=100))
        r = self.svc.dispatch_direct(text, {"values": [1, 2]},
                                      producer="test")
        self.assertFalse(r.ok)
        self.assertEqual(r.refusal, "ambiguous_intent")
        self.assertEqual(len(self.svc.dispatch_history(limit=100)), before)

    def test_bad_arguments_refused(self):
        """Missing/unknown/dunder args -> bad_arguments; the capability
        never executes (no file)."""
        _admit_writer(self.svc, self.tmp)
        target = os.path.join(self.tmp, "bad.txt")
        for args in ({"path": target},                       # missing content
                     {"path": target, "content": "x",
                      "nope": 1},                            # unknown arg
                     {"path": target, "content": "x",
                      "__x": 1}):                            # dunder arg
            r = self.svc.dispatch_direct(WRITE_TEXT, dict(args),
                                         producer="test")
            self.assertFalse(r.ok, args)
            self.assertEqual(r.refusal, "bad_arguments", r.as_dict())
        self.assertFalse(os.path.exists(target))

    def test_history_survives_fresh_process(self):
        """The audit table is a REAL sqlite table: a brand-new service
        instance on the same directory reads back the earlier row."""
        cap_id = _admit_writer(self.svc, self.tmp)
        target = os.path.join(self.tmp, "persist.txt")
        res = self.svc.dispatch_direct(
            WRITE_TEXT, {"path": target, "content": "p"}, producer="test")
        self.assertTrue(res.ok)
        # D11: release DB ownership before the second service boots on the
        # same directory (a real fresh process would have exited).
        if self.svc._eng is not None:
            self.svc._thread.run(self.svc._eng.close)
            self.svc._eng = None
        intent_dir = os.path.join(self.tmp, "intent")
        svc2 = IntentDispatchService(intent_dir, self.sched, self.met)
        rows = svc2.dispatch_history(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dispatch_id"], res.dispatch_id)
        self.assertEqual(rows[0]["capability_id"], cap_id)

    def test_empty_and_oversized_input(self):
        """Empty/oversized input refuses honestly with dedicated codes
        (never silently dropped, never crashes)."""
        r = self.svc.dispatch_direct("", producer="test")
        self.assertFalse(r.ok)
        self.assertEqual(r.refusal, "empty_intent", r.as_dict())
        route = self.svc.route("x" * 9000)
        self.assertFalse(route.ok)
        self.assertEqual(route.refusal, "oversized_input", route.as_dict())


class _HttpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="intent_http_")
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.server, self.services, self.thread, self.base = serve(self.tmp)
        with open(os.path.join(self.tmp, "api_token"), encoding="utf-8") as fh:
            self.token = fh.read().strip()
        with open(os.path.join(self.tmp, "operator.token"),
                  encoding="utf-8") as fh:
            self.op_token = fh.read().strip()
        self.op_id = self.server.svc.operator_id

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        close_services(self.services)

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + self.token}
        if method in ("POST", "PUT", "DELETE", "PATCH"):
            headers["X-Agent-Id"] = self.op_id
            headers["X-Agent-Token"] = self.op_token
        r = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=120) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")


class TestIntentHttp(_HttpBase):
    """Task 1 over the real HTTP path (the GUI Dispatch view pattern)."""

    def test_unknown_intent_is_200_refusal_body(self):
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "5 times 6", "args": {},
                              "producer": "gui:operator"})
        self.assertEqual(code, 200)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["refusal"], "unknown_intent")
        self.assertIn("reasons", obj)
        # GUI shape keys
        for k in ("ok", "result", "capability_id", "dispatch_id",
                  "route_via", "refusal", "reasons"):
            self.assertIn(k, obj, k)

    def test_missing_text_400(self):
        for body in ({}, {"text": ""}, {"text": "   "}, {"args": {}}):
            code, obj = self.req("POST", "/api/intent/dispatch", body)
            self.assertEqual(code, 400, body)
            self.assertFalse(obj["ok"])

    def test_create_file_end_to_end(self):
        intent = self.services["intent"]
        cap_id = _admit_writer(intent, self.tmp)
        target = os.path.join(self.tmp, "http_note.txt")
        code, obj = self.req(
            "POST", "/api/intent/dispatch",
            {"text": WRITE_TEXT,
             "args": {"path": target, "content": "http-bytes"},
             "producer": "gui:operator"})
        self.assertEqual(code, 200, obj)
        self.assertTrue(obj["ok"], obj)
        dsp_id = obj["dispatch_id"]
        self.assertEqual(obj["capability_id"], cap_id)
        self.assertEqual(obj["route_via"], "exact_goal")
        with open(target) as f:
            self.assertEqual(f.read(), "http-bytes")
        # history over HTTP, GUI's ?limit= honored
        code, obj = self.req("GET", "/api/dispatches?limit=50")
        self.assertEqual(code, 200)
        rows = obj["dispatches"]
        hit = [r for r in rows if r["dispatch_id"] == dsp_id]
        self.assertEqual(len(hit), 1, obj)
        row = hit[0]
        self.assertEqual(row["capability_id"], cap_id)
        self.assertEqual(row["request_text"], WRITE_TEXT)
        code, obj = self.req("GET", "/api/dispatches?limit=1")
        self.assertEqual(len(obj["dispatches"]), 1)

    def test_empty_args_dict_engages_media_synthesis_over_http(self):
        """Regression (v8 hardware defect): the GUI dispatch view always
        sends "args": {}. An empty args dict must behave identically to
        omitted args -- media arg synthesis engages -- never
        bad_arguments. A non-empty partial arg set keeps the strict
        contract (missing -> bad_arguments)."""
        intent = self.services["intent"]
        media_out = os.path.join(self.tmp, "media_out")

        eng, _, dispatcher = intent._ensure()

        def _wire():
            from swarm_engine.media.wiring import (
                admit_media_capabilities, MEDIA_PHRASINGS)
            report = admit_media_capabilities(eng, media_out_dir=media_out)
            dispatcher.media_out_dir = media_out
            return report, MEDIA_PHRASINGS["image"][0]

        report, text = intent._thread.run(_wire)
        entry = report.get("image", {})
        self.assertTrue(entry.get("admitted"), entry.get("reasons"))

        code1, obj1 = self.req("POST", "/api/intent/dispatch",
                               {"text": text, "producer": "gui:operator"})
        code2, obj2 = self.req("POST", "/api/intent/dispatch",
                               {"text": text, "args": {},
                                "producer": "gui:operator"})
        self.assertEqual(code1, 200, obj1)
        self.assertEqual(code2, 200, obj2)
        self.assertTrue(obj1["ok"], obj1)
        self.assertTrue(obj2["ok"], obj2)
        self.assertNotEqual(obj2.get("refusal"), "bad_arguments", obj2)

        # strict contract preserved for non-empty partial args
        code3, obj3 = self.req("POST", "/api/intent/dispatch",
                               {"text": text, "args": {"prompt": "x"},
                                "producer": "gui:operator"})
        self.assertEqual(code3, 200, obj3)
        self.assertFalse(obj3["ok"])
        self.assertEqual(obj3["refusal"], "bad_arguments", obj3)

    def test_quarantined_capability_refused_over_http(self):
        """Dispatch ok -> real quarantine (drops the goal binding) ->
        re-bind the quarantined id -> the router's effective-status gate
        refuses capability_unavailable over HTTP (200 body)."""
        intent = self.services["intent"]
        cap_id = _admit_writer(intent, self.tmp)
        target = os.path.join(self.tmp, "q.txt")
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": WRITE_TEXT,
                              "args": {"path": target, "content": "q"}})
        self.assertTrue(obj["ok"], obj)
        _quarantine_authorized(intent, cap_id, reason="http probe")
        # re-bind the now-quarantined id: the binding exists but the
        # capability is not effectively active -> capability_unavailable
        intent.bind_goal(WRITE_TEXT, cap_id)
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": WRITE_TEXT,
                              "args": {"path": target, "content": "q2"}})
        self.assertEqual(code, 200)
        self.assertFalse(obj["ok"])
        self.assertEqual(obj["refusal"], "capability_unavailable", obj)
        with open(target) as f:
            self.assertEqual(f.read(), "q")  # second write never happened


class TestIntentMetering(_HttpBase):
    """Task 2: NL dispatch through the REAL guarded-submit metering path."""

    def test_refused_dispatch_still_counts_as_task(self):
        """What counts: an ACCEPTED dispatch submission (gate passed) --
        even when the router refuses it. The slot is consumed by the real
        scheduler_runs INSERT, before the outcome is known."""
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "5 times 6"})
        self.assertEqual(code, 200)
        self.assertEqual(obj["refusal"], "unknown_intent")
        code, usage = self.req("GET", "/api/metering")
        self.assertEqual(usage["tasks_today"], 1, usage)

    def test_35_dispatches_then_36th_daily_cap(self):
        """35 real NL dispatches in the window -> the 36th is refused
        with the REAL daily_task_cap code (HTTP 429, typed payload).

        NOTE (conversational-minimum conversion, 2026-09-26): the probe
        texts were "unroutable probe {i} xyzzy". Those verbless fragments
        now classify as AMBIGUOUS (chat handler, no slot) under the
        Worker-1 discriminator, so they no longer exercise the metering
        gate. Converted to imperative task probes ("generate metering
        probe file {i} xyzzy") which the discriminator genuinely
        classifies as task; the metering behavior under test (35 slots
        then the REAL 429) is unchanged.
        """
        for i in range(35):
            code, obj = self.req(
                "POST", "/api/intent/dispatch",
                {"text": f"generate metering probe file {i} xyzzy",
                 "producer": "metering-test"})
            self.assertEqual(code, 200, (i, code, obj))
            self.assertEqual(obj["refusal"], "unknown_intent", (i, obj))
        code, usage = self.req("GET", "/api/metering")
        self.assertEqual(usage["tasks_today"], 35, usage)
        self.assertEqual(usage["tasks_remaining"], 0)
        code, obj = self.req("POST", "/api/intent/dispatch",
                             {"text": "generate one more metering probe "
                                      "xyzzy"})
        self.assertEqual(code, 429, obj)
        self.assertFalse(obj["ok"])
        self.assertIn("limit", obj)
        self.assertEqual(obj["limit"]["code"], DAILY_CAP_CODE, obj)
        self.assertEqual(obj["limit"]["limit"], 35)
        self.assertEqual(obj["limit"]["current"], 35)
        # the refused 36th consumed nothing
        code, usage = self.req("GET", "/api/metering")
        self.assertEqual(usage["tasks_today"], 35, usage)

    def test_queue_cap_refuses_26th(self):
        """25 real queued runs with the worker genuinely blocked -> the
        26th NL dispatch is refused with the REAL queue_cap code.

        Service-level (not socket-level): the worker is blocked by holding
        the scheduler's own RLock on this thread, and the HTTP handler
        thread would deadlock on that same lock -- so the dispatch goes
        through the identical real intent_dispatch() path on this thread.
        The 429 mapping itself is proven over HTTP by the daily-cap test.
        """
        sched = self.services["scheduler"]
        intent = self.services["intent"]
        with sched._lock:  # block the worker (RLock: same thread OK)
            for i in range(25):
                # kind="intent_dispatch" fillers: REAL queued scheduler runs
                # that drain through the real NL dispatcher (unroutable ->
                # fast refusal, no run-engine boot), keeping this test fast
                # even under load. They bypass guarded_submit (direct
                # sched.submit) so they do not consume the daily quota.
                r = sched.submit(
                    f"metering queue filler {i}",
                    metadata={"kind": "intent_dispatch",
                              "text": f"unroutable filler {i} xyzzy",
                              "args": {}, "producer": "metering-test"})
                self.assertTrue(r["ok"], (i, r))
            self.assertEqual(sched.queue_depth(), 25)
            # NOTE (conversational-minimum conversion, 2026-09-26): the
            # probe was "unroutable while queued xyzzy", now ambiguous
            # under the discriminator; converted to a genuine task probe
            # so this test still exercises the queue gate.
            code, obj = intent.intent_dispatch(
                {"text": "generate a probe while queued xyzzy",
                 "producer": "metering-test"})
            self.assertEqual(code, 429, obj)
            self.assertFalse(obj["ok"])
            self.assertEqual(obj["limit"]["code"], QUEUE_CAP_CODE, obj)
            self.assertEqual(obj["limit"]["limit"], 25)
            self.assertEqual(obj["limit"]["current"], 25)
        # lock released: the worker drains; the gate passes again
        deadline = time.time() + 60
        while sched.queue_depth() and time.time() < deadline:
            time.sleep(0.05)
        code, obj = intent.intent_dispatch({"text": "5 times 6"})
        self.assertEqual(code, 200, obj)
        self.assertEqual(obj["refusal"], "unknown_intent")


class TestIntentComposition(_HttpBase):
    """M4: cross-capability composition over the real HTTP path.

    One NL request decomposes into multiple capability legs; each leg
    runs the governed single-dispatch core (existence, plan
    fingerprint, effective_status, arg validation, real Composer
    execution, persisted record). A failed leg fails honestly, naming
    the leg -- never a partial-fake result.
    """

    def _admit_media(self):
        intent = self.services["intent"]
        media_out = os.path.join(self.tmp, "media_out")
        eng, _, dispatcher = intent._ensure()

        def _wire():
            from swarm_engine.media.wiring import admit_media_capabilities
            report = admit_media_capabilities(eng, media_out_dir=media_out)
            dispatcher.media_out_dir = media_out
            return report

        report = intent._thread.run(_wire)
        return intent, dispatcher, report

    def test_two_leg_composition_over_http(self):
        intent, dispatcher, report = self._admit_media()
        self.assertTrue(report.get("image", {}).get("admitted"), report)
        self.assertTrue(report.get("video", {}).get("admitted"), report)
        code, obj = self.req(
            "POST", "/api/intent/dispatch",
            {"text": "generate an image of a sunset and "
                     "create a short video of ocean waves",
             "args": {}, "producer": "gui:operator"})
        self.assertEqual(code, 200, obj)
        self.assertTrue(obj["ok"], obj)
        self.assertEqual(obj.get("route_via"), "composition", obj)
        res = obj["result"]
        self.assertTrue(res.get("composition"), res)
        self.assertEqual(len(res.get("legs", [])), 2, res)
        arts = res.get("artifacts", [])
        self.assertEqual(len(arts), 2, res)
        for a in arts:
            self.assertTrue(os.path.exists(a["path"]), a)
            self.assertGreater(os.path.getsize(a["path"]), 0, a)
        # both legs persisted audit rows through the governed path
        rows = dispatcher.history(limit=10)
        leg_rows = [r for r in rows
                    if (r.get("route_via") or "").startswith(
                        "composition:leg")]
        self.assertEqual(len(leg_rows), 2, rows)
        self.assertTrue(all(r["ok"] == 1 for r in leg_rows), rows)

    def test_quarantined_leg_refuses_honestly_over_http(self):
        """One leg quarantined -> the composition refuses, naming the
        leg and the quarantined capability; the healthy leg never
        executes (no partial-fake result)."""
        intent, dispatcher, report = self._admit_media()
        image_id = report.get("image", {}).get("capability_id")
        self.assertTrue(image_id, report)
        _quarantine_authorized(intent, image_id, reason="m4 probe")
        code, obj = self.req(
            "POST", "/api/intent/dispatch",
            {"text": "generate an image of a sunset and "
                     "create a short video of ocean waves",
             "args": {}, "producer": "gui:operator"})
        self.assertEqual(code, 200, obj)
        self.assertFalse(obj["ok"], obj)
        self.assertEqual(obj.get("refusal"), "composition_leg_unroutable",
                         obj)
        reasons = " ".join(obj.get("reasons", []))
        self.assertIn("generate an image of a sunset", reasons, obj)
        self.assertIn("quarantined", reasons, obj)
        self.assertIn(image_id[:12], reasons, obj)

    def test_adjective_join_stays_single_dispatch(self):
        """'a dog with brown fur and white spots' is ONE request, not a
        composition: the non-imperative fragment keeps the whole text
        on the single-dispatch path."""
        self._admit_media()
        code, obj = self.req(
            "POST", "/api/intent/dispatch",
            {"text": "make a picture of a dog with brown fur "
                     "and white spots",
             "args": {}, "producer": "gui:operator"})
        self.assertEqual(code, 200, obj)
        self.assertNotEqual(obj.get("route_via"), "composition", obj)
        # single dispatch proceeds normally (here: effect_fallback to
        # the admitted image capability); the point is only that the
        # adjective-join never becomes a composition.
        self.assertIn(obj.get("route_via"),
                      ("effect_fallback", "exact_goal", "structural"), obj)


def _admit_q9_caps(svc, media_dir):
    """Q9 shared fixture: admit the media image capability plus the
    double_and_sum / halve non-media capabilities and bind the test
    goal phrasings. Returns (image_id, double_id)."""
    ieng, _, dispatcher = svc._ensure()

    def _wire():
        from swarm_engine.media.wiring import admit_media_capabilities
        rep = admit_media_capabilities(ieng, media_out_dir=media_dir)
        dispatcher.media_out_dir = media_dir
        assert rep["image"]["admitted"], rep
        r = ieng.admit_as_engine(
            "double each value and sum them",
            {"name": "double_and_sum", "params": {"values": "list"},
             "steps": [
                 {"id": "m", "op": "data.map",
                  "args": {"items": {"$param": "values"},
                           "fn": {"$partial": {
                               "op": "computation.multiply",
                               "bound": {"b": 2}, "free": ["a"]}}}},
                 {"id": "s", "op": "computation.sum",
                  "args": {"values": {"$step": "m"}}}],
             "output": {"$step": "s"}},
            name="double_and_sum")
        assert r.ok and r.verdict == Verdict.ADMITTED, r
        double_id = r.capability_id
        r = ieng.admit_as_engine(
            "halve a number",
            {"name": "halve", "params": {"x": "num"},
             "steps": [{"id": "h", "op": "multiply",
                        "args": {"a": {"$param": "x"}, "b": 0.5}}],
             "output": {"$step": "h"}},
            name="halve")
        assert r.ok and r.verdict == Verdict.ADMITTED, r
        halve_id = r.capability_id
        ieng.capabilities.bind_goal("double the values 3, 5, 7",
                                    double_id)
        ieng.capabilities.bind_goal("double the values", double_id)
        ieng.capabilities.bind_goal("double the values 4, 6", double_id)
        ieng.capabilities.bind_goal("halve the result", halve_id)
        return rep["image"]["capability_id"], double_id
    return svc._thread.run(_wire)


class TestIntentCompositionNonMedia(_HttpBase):
    """Q9: composition with a non-media leg whose arguments are
    synthesized from the declared contract + fragment text + earlier
    legs' outputs -- never hardcoded, never harness-supplied.

    HTTP level: only phrasings the conversational discriminator
    classifies as task reach the dispatcher; machinery-level tests
    below cover pure non-media requests on the same causal path."""

    def test_media_plus_computed_leg_over_http(self):
        """Numbers come from the fragment text; the non-media leg
        really executes (leg1 result 30), media leg produces a real
        artifact."""
        image_id, double_id = _admit_q9_caps(self.services["intent"],
                                             self.tmp)
        code, obj = self.req(
            "POST", "/api/intent/dispatch",
            {"text": "generate an image of a sunset and "
                     "double the values 3, 5, 7",
             "args": {}, "producer": "gui:operator"})
        self.assertEqual(code, 200, obj)
        self.assertTrue(obj["ok"], obj)
        self.assertEqual(obj.get("route_via"), "composition", obj)
        legs = obj["result"]["legs"]
        self.assertEqual(len(legs), 2, obj)
        self.assertEqual(legs[0]["capability_id"], image_id, obj)
        self.assertEqual(legs[1]["capability_id"], double_id, obj)
        self.assertEqual(legs[1]["result"], 30, obj)
        self.assertGreaterEqual(len(obj["result"]["artifacts"]), 1, obj)


class TestIntentCompositionNonMediaMachinery(unittest.TestCase):
    """Q9 machinery level: pure non-media composition requests are
    classified 'ambiguous' by the conversational discriminator
    (swarm_engine/services/discriminator.py -- not Q9-owned, so not
    edited here) before they reach the HTTP dispatch path. These
    tests drive the real dispatcher directly -- the exact causal
    path the HTTP task gate reaches once the discriminator admits
    a phrasing."""

    def setUp(self):
        self.tmp, self.sched, self.met, self.svc = _service_on_scratch(self)

    def tearDown(self):
        self.sched.close()

    def test_chained_nonmedia_legs_machinery(self):
        """A later leg's arg comes from an earlier leg's real output:
        'halve the result' has no numbers in its fragment; x=20 comes
        from leg0's output."""
        _admit_q9_caps(self.svc, self.tmp)
        res = self.svc.dispatch_direct(
            "double the values 4, 6 then halve the result",
            {}, producer="q9-test")
        self.assertTrue(res.ok, res.as_dict())
        self.assertEqual(res.route_via, "composition", res.as_dict())
        legs = res.result["legs"]
        self.assertEqual(len(legs), 2, res.as_dict())
        self.assertEqual(legs[0]["result"], 20, res.as_dict())
        self.assertEqual(legs[1]["result"], 10.0, res.as_dict())

    def test_unsynthesizable_leg_refuses_honestly_machinery(self):
        """No numbers anywhere -> the composition fails closed naming
        the argument, instead of hallucinating one."""
        _admit_q9_caps(self.svc, self.tmp)
        res = self.svc.dispatch_direct(
            "double the values and generate an image of a sunset",
            {}, producer="q9-test")
        self.assertFalse(res.ok, res.as_dict())
        self.assertEqual(res.refusal, "composition_leg_unroutable",
                         res.as_dict())
        reasons = " ".join(res.reasons)
        self.assertIn("values", reasons, res.as_dict())
        self.assertIsNone(res.result, res.as_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)
