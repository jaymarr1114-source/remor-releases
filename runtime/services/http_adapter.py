"""REMOR unified HTTP adoption layer (stdlib only).

One server, two route groups, no duplicated routes.

Engine front (Track 2B/3 — served from the engine-bound _Service):
  POST /api/intent/dispatch        NL tool dispatch (real Composer path)
  GET  /api/dispatches             dispatch history (audit log)
  GET  /api/capabilities           capability inventory + effective_status
  POST /api/capabilities/<id>/restore  governed capability recovery
  GET  /api/dispatch_evidence/<id> dispatch evidence (Track 3, read-only)
  GET  /api/voice/status           voice substrate status (UNAVAILABLE)
  POST /api/voice/stt | /api/voice/tts -> 501 honest refusal
  GET  /api/media/status           media substrate status (UNAVAILABLE)
  POST /api/media/image | /api/media/video -> 501 honest refusal
  GET  /api/health                 liveness

Third-track services (backend-FF — runs/projects/files/artifacts):
  POST /api/runs                       {goal, examples?, conversation_id?}
  GET  /api/runs
  GET  /api/runs/<id>
  GET  /api/runs/<id>/events
  POST /api/runs/<id>/pause|resume|stop|cancel
  POST /api/projects                   {kind: blank|zip|dir, project_id?, path?}
  GET  /api/projects
  GET  /api/projects/<id>
  POST /api/projects/<id>/transition   {to_state, reason?}
  POST /api/projects/<id>/run_loop     {max_rounds?}
  GET  /api/projects/<id>/loop/<job_id>
  POST /api/projects/<id>/loop/<job_id>/stop
  GET  /api/files?path=<rel>           list_dir
  GET  /api/files/content?path=<rel>   read_text
  POST /api/files/write                {path, content}
  POST /api/artifacts                  {name, language, code, metadata?}
  GET  /api/artifacts
  GET  /api/artifacts/<id>?revision=n
  GET  /api/artifacts/<id>/revisions
  DELETE /api/artifacts/<id>
  POST /api/artifacts/<id>/run         {revision?, timeout?}

Threading discipline (KD-2): the SwarmEngine is thread-affine (the oracle
registry holds a thread-bound sqlite connection). This server is
single-threaded BY DESIGN: the engine is constructed on the serving thread
and every request is handled on that same thread. Do not wrap this in a
ThreadingHTTPServer. `serve()` starts the serving thread itself, so the
engine still boots on the thread that serves.

JSON discipline: every route uses the strict reader (256KB cap, 400 on
malformed or non-object JSON). Service-dict results map via _status_for:
ok -> 200, "not found"/"unknown ..." -> 404, illegal transition -> 409,
else 400. Nothing here fabricates data.

Run: python3 -m swarm_engine.services.http_adapter --db <path> --dir <svcdata>
     --port 8471
"""
from __future__ import annotations

import argparse
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, Optional, Tuple

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.services import voice as voice_svc
from swarm_engine.services import media as media_svc
from swarm_engine.services.unavailable import CapabilityUnavailable
from swarm_engine.synthesis.integrity import (
    RestoreRefused, effective_status, restore_everywhere)
from swarm_engine.synthesis.intent_router import IntentRouter
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher

MAX_BODY = 256 * 1024


class _Handler(BaseHTTPRequestHandler):
    server_version = "REMOR/1.0"

    # -- plumbing ---------------------------------------------------------
    def _send(self, code: int, obj: Any):
        blob = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _read_json(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise ValueError("malformed JSON")
        if not isinstance(obj, dict):
            raise ValueError("top-level JSON must be an object")
        return obj

    def _result(self, res: Dict[str, Any], ok_status: int = 200):
        """Map a service result dict to an HTTP response (FF _status_for)."""
        if res.get("ok"):
            return self._send(ok_status, res)
        return self._send(_status_for(res), res)

    def log_message(self, fmt, *args):  # keep stderr quiet in tests
        pass

    # -- routing ----------------------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        svc: "_Service" = self.server.svc  # type: ignore
        # --- engine front (Track 2B/3) ---
        if path == "/api/health":
            return self._send(200, {"ok": True})
        if path == "/api/capabilities":
            return self._send(200, {"capabilities": svc.capabilities()})
        if path == "/api/dispatches":
            return self._send(200, {"dispatches": svc.dispatcher.history()})
        m = re.fullmatch(r"/api/dispatch_evidence/([A-Za-z0-9_\-]+)", path)
        if m:
            return self._send(*svc.dispatch_evidence(m.group(1)))
        if path == "/api/voice/status":
            return self._send(200, voice_svc.status())
        if path == "/api/media/status":
            return self._send(200, media_svc.status())
        # --- third-track services (FF group, proven order) ---
        try:
            ff = svc.ff
            scheduler = ff["scheduler"]
            projects = ff["projects"]
            files = ff["files"]
            artifacts = ff["artifacts"]
            if path == "/api/runs":
                return self._send(200, scheduler.list_runs(
                    limit=int((qs.get("limit") or ["50"])[0])))
            if path.startswith("/api/runs/") and path.endswith("/events"):
                rid = path.split("/")[3]
                evs = scheduler.events(rid)
                if evs is None:
                    return self._send(404, {"error": "run not found"})
                return self._send(200, evs)
            if path.startswith("/api/runs/"):
                rec = scheduler.get_run(path.split("/")[3])
                if rec is None:
                    return self._send(404, {"error": "run not found"})
                return self._send(200, rec)
            if path == "/api/projects":
                return self._send(200, projects.list_projects())
            if "/loop/" in path and path.startswith("/api/projects/"):
                parts = path.split("/")
                pid, job_id = parts[3], parts[5]
                return self._result(projects.loop_status(job_id))
            if path.startswith("/api/projects/"):
                return self._result(projects.get_project(path.split("/")[3]))
            if path == "/api/files":
                return self._result(
                    files.list_dir((qs.get("path") or [""])[0]))
            if path == "/api/files/content":
                return self._result(
                    files.read_text((qs.get("path") or [""])[0]))
            if path == "/api/artifacts":
                return self._send(200, artifacts.list_artifacts())
            if path.startswith("/api/artifacts/") and path.endswith("/revisions"):
                return self._result(artifacts.revisions(
                    _aid(path.split("/")[3])))
            if path.startswith("/api/artifacts/"):
                rev = (qs.get("revision") or [None])[0]
                rev = int(rev) if rev is not None else None
                return self._result(artifacts.get(_aid(path.split("/")[3]),
                                                  revision=rev))
        except Exception as exc:  # honest 500, never silent
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        svc: "_Service" = self.server.svc  # type: ignore
        try:
            body = self._read_json()
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        # --- engine front (Track 2B/3) ---
        if path == "/api/intent/dispatch":
            code, obj = svc.intent_dispatch(body)
            return self._send(code, obj)
        m = re.fullmatch(r"/api/capabilities/([A-Za-z0-9_.\-]+)/restore", path)
        if m:
            return self._send(*svc.restore_capability(m.group(1), body))
        if path == "/api/voice/stt":
            return self._send(*svc.unavailable(
                lambda: voice_svc.transcribe()))
        if path == "/api/voice/tts":
            return self._send(*svc.unavailable(
                lambda: voice_svc.speak(body.get("text", ""))))
        if path == "/api/media/image":
            return self._send(*svc.unavailable(
                lambda: media_svc.generate_image(body.get("prompt", ""))))
        if path == "/api/media/video":
            return self._send(*svc.unavailable(
                lambda: media_svc.generate_video(body.get("prompt", ""))))
        # --- third-track services (FF group, proven order) ---
        try:
            ff = svc.ff
            scheduler = ff["scheduler"]
            projects = ff["projects"]
            files = ff["files"]
            artifacts = ff["artifacts"]
            if path == "/api/runs":
                return self._result(scheduler.submit(
                    body.get("goal", ""), examples=body.get("examples"),
                    metadata=body.get("metadata"),
                    conversation_id=body.get("conversation_id")))
            if path.startswith("/api/runs/"):
                parts = path.split("/")
                rid, action = parts[3], parts[4] if len(parts) > 4 else ""
                fn = {"pause": scheduler.pause_run,
                      "resume": scheduler.resume_run,
                      "stop": scheduler.stop_run,
                      "cancel": scheduler.cancel_run}.get(action)
                if fn is None:
                    return self._send(404, {"error": "not found"})
                return self._result(fn(rid))
            if path == "/api/projects":
                return self._result(projects.create(body))
            if path.startswith("/api/projects/") and path.endswith("/transition"):
                return self._result(projects.transition(
                    path.split("/")[3], body.get("to_state", ""),
                    reason=body.get("reason", "")))
            if path.startswith("/api/projects/") and path.endswith("/run_loop"):
                return self._result(projects.run_loop(
                    path.split("/")[3],
                    max_rounds=int(body.get("max_rounds", 4))))
            if "/loop/" in path and path.endswith("/stop") \
                    and path.startswith("/api/projects/"):
                parts = path.split("/")
                return self._result(projects.stop_loop(parts[5]))
            if path == "/api/files/write":
                return self._result(files.write_text(
                    body.get("path", ""), body.get("content", "")))
            if path == "/api/artifacts":
                return self._result(artifacts.save(
                    body.get("name", ""), body.get("language", ""),
                    body.get("code", ""), metadata=body.get("metadata")))
            if path.startswith("/api/artifacts/") and path.endswith("/run"):
                res = artifacts.run(
                    _aid(path.split("/")[3]),
                    revision=body.get("revision"),
                    timeout=float(body.get("timeout", 30)))
                return self._result(res)
        except Exception as exc:  # honest 500, never silent
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path
        svc: "_Service" = self.server.svc  # type: ignore
        try:
            if path.startswith("/api/artifacts/"):
                return self._result(svc.ff["artifacts"].delete(
                    _aid(path.split("/")[3])))
            return self._send(404, {"error": "not found"})
        except Exception as exc:
            return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})


class _Service:
    """The engine-bound service object the handler delegates to.

    Builds BOTH fronts: the Track 2B/3 engine front (SwarmEngine +
    IntentRouter + NLToolDispatcher, booting on the constructing thread —
    the serving thread — per the KD-2 thread-affinity discipline) and the
    third-track service dict (scheduler, projects, files, artifacts).
    """

    def __init__(self, db_path: str, base_dir: Optional[str] = None):
        db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.engine = SwarmEngine(db_path=db_path)
        self.router = IntentRouter(self.engine)
        self.dispatcher = NLToolDispatcher(self.engine, self.router)
        if base_dir is None:
            base_dir = os.path.join(
                os.path.dirname(os.path.abspath(db_path)), "service_data")
        self.base_dir = base_dir
        self.ff = build_services(base_dir)

    # -- capability inventory --------------------------------------------
    def capabilities(self):
        import sqlite3
        out = []
        con = sqlite3.connect(self.engine.db_path)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT capability_id, name, goal, version, status "
                "FROM plan_capabilities ORDER BY created_at DESC LIMIT 1000"
            ).fetchall()
        finally:
            con.close()
        for r in rows:
            try:
                eff = effective_status(self.engine, r["capability_id"])
            except Exception as exc:
                eff = {"effective": "unknown", "error": str(exc)}
            out.append({
                "capability_id": r["capability_id"],
                "name": r["name"],
                "goal": r["goal"],
                "version": r["version"],
                "store_status": r["status"],
                "effective_status": eff.get("effective"),
                "consistent": eff.get("consistent"),
                "trust": eff.get("trust"),
                "lifecycle": eff.get("lifecycle"),
            })
        return out

    # -- intent dispatch ---------------------------------------------------
    def intent_dispatch(self, body: Dict[str, Any]):
        text = body.get("text")
        args = body.get("args")
        producer = body.get("producer") or "http:operator"
        res = self.dispatcher.dispatch(text, args, producer=producer)
        return 200, res.as_dict()

    # -- dispatch evidence (Track 3) --------------------------------------
    def dispatch_evidence(self, evidence_id: str):
        """Read one dispatch-evidence record from the organizational store.

        Read-only: this endpoint never creates trust, it only exposes the
        chained evidence rows. The org store is located by deployment
        convention (<engine-db-dir>/agent_org.db); absent -> 404.
        """
        import os
        import sqlite3
        org_db = os.path.join(os.path.dirname(self.engine.db_path),
                              "agent_org.db")
        if not os.path.exists(org_db):
            return 404, {"error": "no organizational store at this deployment"}
        con = sqlite3.connect(org_db)
        try:
            con.row_factory = sqlite3.Row
            row = con.execute(
                "SELECT evidence_id, dispatch_id, agent_id, assignment_id,"
                " request_text, route_via, route_score, capability_id,"
                " capability_version, plan_fingerprint, args_json,"
                " input_digest, result_json, result_digest, ok, error,"
                " evidence_json, created_at FROM ao_dispatch_evidence"
                " WHERE evidence_id=? ORDER BY seq DESC LIMIT 1",
                (evidence_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            return 404, {"error": f"unknown dispatch evidence {evidence_id!r}"}
        return 200, {"evidence": dict(row)}

    # -- governed restore ---------------------------------------------------
    def restore_capability(self, capability_id: str, body: Dict[str, Any]):
        reason = body.get("reason") or ""
        try:
            actions = restore_everywhere(
                self.engine, capability_id,
                authority=self.engine.oracle, reason=reason)
        except RestoreRefused as exc:
            return 409, {"ok": False, "refusal": str(exc),
                         "capability_id": capability_id}
        except Exception as exc:
            return 500, {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                         "capability_id": capability_id}
        return 200, {"ok": True, "capability_id": capability_id,
                      "actions": {k: v for k, v in actions.items()
                                  if k != "record"}}

    # -- honest refusals -----------------------------------------------------
    @staticmethod
    def unavailable(fn):
        try:
            fn()
        except CapabilityUnavailable as exc:
            return 501, {"ok": False, "unavailable": exc.as_dict()}
        return 200, {"ok": True}


def _status_for(result: Dict[str, Any]) -> int:
    if result.get("ok"):
        return 200
    err = str(result.get("error", "")).lower()
    if "not found" in err or "unknown " in err:
        return 404
    if "not legal" in err or "illegal" in err:
        return 409
    return 400


def _aid(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        raise ValueError(f"bad artifact id: {s!r}")


def build_services(base_dir: str) -> Dict[str, Any]:
    """Create all third-track services on scratch dirs.

    Returns the service dict (scheduler/projects/files/artifacts/base_dir).
    No engine boots here: the scheduler boots its engine on its own worker
    thread, and the projects engine proxy boots on first touch (the project
    loop worker), preserving the KD-2 thread-affinity discipline.
    """
    from swarm_engine.services.scheduler import RunScheduler
    from swarm_engine.services.projects import ProjectService
    from swarm_engine.services.files import ScopedFileService
    from swarm_engine.services.artifacts import ArtifactStore

    os.makedirs(base_dir, exist_ok=True)
    sched_db = os.path.join(base_dir, "scheduler.db")
    runtime_db = os.path.join(base_dir, "runtime.db")
    projects_db = os.path.join(base_dir, "projects.db")
    projects_root = os.path.join(base_dir, "projects")
    browser_root = os.path.join(base_dir, "browser")
    sandbox_dir = os.path.join(base_dir, "artifact_sandbox")
    artifacts_db = os.path.join(base_dir, "artifacts.db")
    for d in (projects_root, browser_root, sandbox_dir):
        os.makedirs(d, exist_ok=True)

    scheduler = RunScheduler(db_path=sched_db, runtime_db_path=runtime_db)
    projects = ProjectService(
        db_path=projects_db,
        projects_root=projects_root,
        engine=_lazy_engine(os.path.join(base_dir, "projects_runtime.db")),
    )
    files = ScopedFileService(root=browser_root, writable=True)
    artifacts = ArtifactStore(db_path=artifacts_db, sandbox_dir=sandbox_dir)
    service_anchor = _attach_service_anchor(
        base_dir, scheduler, artifacts, authority="remor:services")
    return {
        "scheduler": scheduler, "projects": projects,
        "files": files, "artifacts": artifacts,
        "base_dir": base_dir, "service_anchor": service_anchor,
    }


def service_anchor_paths(base_dir: str) -> Tuple[str, str]:
    """(journal_path, key_path) for the service anchor of one deployment.

    The anchor dir stays ``<base_dir>/../anchor_store`` -- outside the
    databases' parent dir, so the multi-DB placement check passes -- but
    the journal/key file names are scoped to THIS base_dir
    (``services_<basename>.anchor.{journal,key}``), matching the
    ``default_anchor_paths()`` per-database convention.

    A fixed shared journal name is a real defect: two ``build_services()``
    deployments whose base_dirs share one parent (e.g. two e2e runs with
    mkdtemp dirs directly under /tmp) would share one journal, and the
    second boot fail-closes against the first deployment's heads.
    Scoping the file names keeps each deployment's anchor independent
    while a re-boot of the SAME base_dir reuses its own journal.
    """
    base_abs = os.path.abspath(base_dir)
    stem = "services_" + (os.path.basename(base_abs) or "root")
    anchor_dir = os.path.normpath(os.path.join(base_abs, "..", "anchor_store"))
    return (os.path.join(anchor_dir, stem + ".anchor.journal"),
            os.path.join(anchor_dir, stem + ".anchor.key"))


def _attach_service_anchor(base_dir: str, scheduler, artifacts,
                           authority: str = "remor:services"):
    """Create, bind, and initialize-or-verify the service anchor journal.

    One CrossDbAnchor covers both service DBs (scopes
    ``scheduler:scheduler_runs`` + ``artifact:artifacts``). The journal
    and key live in ``<base_dir>/../anchor_store`` -- outside every
    protected DB's parent dir (``base_dir``), enforced by the anchor's
    multi-DB placement check -- with file names scoped per base_dir via
    service_anchor_paths() so sibling deployments never share a journal.

    Boot discipline (fail-closed):
    - journal exists -> verify_all() must pass, else raise;
    - journal missing -> both chain tables must be pristine (empty),
      then initialize(authority); a non-empty chain table with no
      journal is REFUSED (possible journal-deletion cover);
    - legacy (pre-chain) schema -> the stores already raised
      LegacySchemaError at construction; the operator migrates
      explicitly via migrate_legacy_*_db().
    """
    from swarm_engine.governance.anchor import (
        ChainAuditError, CrossDbAnchor)
    journal, key_path = service_anchor_paths(base_dir)
    os.makedirs(os.path.dirname(journal), exist_ok=True)
    anchor = CrossDbAnchor(
        journal, key_path,
        [("scheduler", scheduler.db_path),
         ("artifact", artifacts.db_path)])
    anchor.bind([("scheduler", scheduler), ("artifact", artifacts)])
    scheduler.attach_anchor(anchor, authority=authority)
    artifacts.attach_anchor(anchor, authority=authority)
    if anchor.journal_exists():
        ok, msg = anchor.verify_all()
        if not ok:
            raise ChainAuditError(
                f"service anchor verify failed at boot: {msg}")
    else:
        pristine = _chain_table_empty(
            scheduler.db_path, "scheduler_runs") and _chain_table_empty(
            artifacts.db_path, "artifacts")
        if not pristine:
            raise ChainAuditError(
                "service anchor journal is missing but the service "
                "databases are not pristine: refusing boot (possible "
                "journal-deletion cover). Restore the journal from "
                "backup or re-initialize explicitly.")
        anchor.initialize(authority)
    return anchor


def _chain_table_empty(db_path: str, table: str) -> bool:
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    finally:
        conn.close()


def _lazy_engine(db_path: str):
    """Engine proxy that boots on first attribute use (i.e. on the thread
    that first touches it — the project loop worker — satisfying the
    engine's thread-affinity for its sqlite connections)."""

    class _Proxy:
        _engine = None

        def _boot(self):
            if self._engine is None:
                from swarm_engine.core.engine import SwarmEngine
                self._engine = SwarmEngine(db_path=db_path)
            return self._engine

        def __getattr__(self, name):
            return getattr(self._boot(), name)

    return _Proxy()


def close_services(svc: Dict[str, Any]) -> None:
    """Best-effort shutdown of the third-track service dict."""
    try:
        svc["scheduler"].close()
    except Exception:
        pass


def run(db_path: str, host: str = "127.0.0.1", port: int = 8471,
        base_dir: Optional[str] = None) -> Tuple[HTTPServer, "_Service"]:
    """Create the service and its server. Single-threaded BY DESIGN: the
    engine is thread-affine (OracleRegistry holds a thread-bound sqlite
    connection -- known pre-existing limitation), so all requests are
    handled in the serving thread, the same thread that constructs the
    engine here. Call run() and serve_forever() in the same thread
    (main() does; the track2b/track2_new tests do). Do not wrap this in a
    ThreadingHTTPServer."""
    svc = _Service(db_path, base_dir)
    server = HTTPServer((host, port), _Handler)
    server.svc = svc  # type: ignore
    return server, svc


def serve(base_dir: str, host: str = "127.0.0.1", port: int = 0):
    """Start the unified adapter in a background thread.

    The background thread IS the serving thread: it constructs the
    _Service (the SwarmEngine boots there, satisfying thread-affinity) and
    then serves single-threaded. Returns
    (server, services_dict, thread, base_url); services_dict is the
    third-track service dict for close_services().
    """
    db_path = os.path.join(os.path.abspath(base_dir), "engine.db")
    server = HTTPServer((host, port), _Handler)
    ready = threading.Event()
    box: Dict[str, Any] = {}

    def target():
        svc = _Service(db_path, base_dir)
        server.svc = svc  # type: ignore
        box["svc"] = svc
        ready.set()
        server.serve_forever()

    thread = threading.Thread(target=target, daemon=True, name="remor-adapter")
    thread.start()
    if not ready.wait(timeout=180):
        raise RuntimeError("unified adapter: service failed to start")
    svc = box["svc"]
    return server, svc.ff, thread, f"http://{host}:{server.server_address[1]}"


def main():
    ap = argparse.ArgumentParser(
        description="REMOR unified HTTP API (engine front + third-track services)")
    ap.add_argument("--db", default=None,
                    help="engine DB path (default: <dir>/engine.db)")
    ap.add_argument("--dir", default=os.path.join(os.getcwd(), "remor_service_data"),
                    help="third-track service data dir")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8471)
    args = ap.parse_args()
    base_dir = os.path.abspath(args.dir)
    db_path = args.db or os.path.join(base_dir, "engine.db")
    server, svc = run(db_path, args.host, args.port, base_dir=base_dir)
    print(f"REMOR API on http://{args.host}:{args.port} "
          f"db={db_path} dir={base_dir}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        close_services(svc.ff)


if __name__ == "__main__":
    main()
