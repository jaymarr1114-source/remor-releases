"""Governed natural-language tool dispatch (§4).

dispatch(text, args, producer):
  1. Route the text through IntentRouter -> exactly one capability_id
     (or a refusal; unknown/ambiguous/unavailable intents never dispatch).
  2. Validate the arguments: must be a JSON object of JSON scalar values
     (str/int/float/bool/None) or plain lists/dicts thereof; no callables,
     no objects, no oversized payloads. Then check names and kinds against
     the capability's declared plan params (composer._spec + coerce).
  3. Re-verify at invocation time: the record still exists, its stored
     plan still fingerprints to its id (tamper check), and
     effective_status is still "active" across the tri-system. A
     capability quarantined between routing and dispatch is refused.
  4. Execute through the real Composer path (execute_sync) -- never a
     caller-supplied callable. There is no code path by which the
     request text or args become executable code.
  5. Persist a dispatch record: request text, route, capability id +
     version, input digest, result digest, producer, outcome. These are
     operational audit rows (plain table, not part of the tamper-evident
     trust chain -- documented bound).

Refusals are fail-closed and machine-readable (DispatchResult.refusal).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.synthesis.capability_store import plan_fingerprint
from swarm_engine.synthesis.composer import _spec
from swarm_engine.primitives.core import coerce
from swarm_engine.synthesis.integrity import effective_status
from swarm_engine.synthesis.intent_router import IntentRouter

MAX_ARGS_BYTES = 64 * 1024
MAX_ARG_DEPTH = 6
_DISPATCH_DDL = """
CREATE TABLE IF NOT EXISTS intent_dispatches (
  dispatch_id       TEXT PRIMARY KEY,
  ts                REAL NOT NULL,
  producer          TEXT,
  request_text      TEXT NOT NULL,
  route_via         TEXT,
  route_score       REAL,
  capability_id     TEXT NOT NULL,
  capability_version INTEGER,
  input_digest      TEXT,
  result_digest     TEXT,
  ok                INTEGER NOT NULL,
  error             TEXT
)
"""


@dataclass
class _RouteInfo:
    """Minimal route descriptor for _record (routed or direct dispatch)."""
    via: Optional[str] = None
    score: Optional[float] = None


@dataclass
class DispatchResult:
    ok: bool
    result: Any = None
    capability_id: Optional[str] = None
    dispatch_id: Optional[str] = None
    route_via: Optional[str] = None
    refusal: Optional[str] = None
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "result": self.result,
            "capability_id": self.capability_id,
            "dispatch_id": self.dispatch_id,
            "route_via": self.route_via,
            "refusal": self.refusal,
            "reasons": self.reasons,
        }


class NLToolDispatcher:
    """NL -> capability dispatcher. Bound to one engine."""

    def __init__(self, engine, router: Optional[IntentRouter] = None):
        self.engine = engine
        self.router = router or IntentRouter(engine)
        con = sqlite3.connect(engine.db_path)
        try:
            con.execute(_DISPATCH_DDL)
            con.commit()
        finally:
            con.close()

    # -- argument validation ------------------------------------------------
    def _check_json_value(self, v: Any, depth: int, path: str) -> Optional[str]:
        """Return an error string, or None if the value is acceptable."""
        if depth > MAX_ARG_DEPTH:
            return f"{path}: nesting exceeds depth {MAX_ARG_DEPTH}"
        if v is None or isinstance(v, (bool, int, float, str)):
            if isinstance(v, str) and len(v) > 8192:
                return f"{path}: string arg exceeds 8192 chars"
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                return f"{path}: non-finite float refused"
            return None
        if isinstance(v, list):
            if len(v) > 1000:
                return f"{path}: list arg exceeds 1000 items"
            for i, item in enumerate(v):
                err = self._check_json_value(item, depth + 1, f"{path}[{i}]")
                if err:
                    return err
            return None
        if isinstance(v, dict):
            if len(v) > 256:
                return f"{path}: dict arg exceeds 256 keys"
            for k, item in v.items():
                if not isinstance(k, str):
                    return f"{path}: non-string key refused"
                if k.startswith("__"):
                    return f"{path}: dunder key {k!r} refused"
                err = self._check_json_value(item, depth + 1, f"{path}.{k}")
                if err:
                    return err
            return None
        return f"{path}: value of type {type(v).__name__} refused (JSON scalars only)"

    def _validate_args(self, args: Any, plan: Dict[str, Any]):
        """Returns (ok, coerced_or_error)."""
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return False, "args must be a JSON object"
        try:
            blob = json.dumps(args)
        except (TypeError, ValueError):
            return False, "args are not JSON-serializable"
        if len(blob.encode("utf-8")) > MAX_ARGS_BYTES:
            return False, f"args exceed {MAX_ARGS_BYTES} bytes"
        for k, v in args.items():
            if not isinstance(k, str):
                return False, "arg names must be strings"
            if k.startswith("__"):
                return False, f"arg name {k!r} refused"
            err = self._check_json_value(v, 0, k)
            if err:
                return False, err
        params = plan.get("params") or {}
        unknown = [k for k in args if k not in params]
        if unknown:
            return False, f"unknown args for this capability: {unknown}"
        missing = [k for k in params if k not in args]
        if missing:
            return False, f"missing required args: {missing}"
        coerced = {}
        for k, v in args.items():
            spec = _spec(params[k])
            ok_c, cv = coerce(v, spec)
            if not ok_c:
                return False, (f"arg {k!r} value {v!r} does not coerce to "
                               f"declared kind {params[k]!r}")
            coerced[k] = cv
        return True, coerced

    # -- dispatch -----------------------------------------------------------
    def _record(self, dispatch_id, producer, text, route, cap_id, version,
                input_digest, result_digest, ok, error):
        con = sqlite3.connect(self.engine.db_path)
        try:
            con.execute(
                "INSERT INTO intent_dispatches "
                "(dispatch_id, ts, producer, request_text, route_via, "
                " route_score, capability_id, capability_version, "
                " input_digest, result_digest, ok, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (dispatch_id, time.time(), producer, text,
                 route.via if route else None,
                 route.score if route else None,
                 cap_id, version, input_digest, result_digest,
                 1 if ok else 0, error))
            con.commit()
        finally:
            con.close()

    def dispatch(self, text: str, args: Optional[Dict[str, Any]] = None,
                 producer: Optional[str] = None) -> DispatchResult:
        # 1. route
        route = self.router.route(text)
        if not route.ok:
            return DispatchResult(
                ok=False, refusal=route.refusal, reasons=route.reasons)

        cap_id = route.capability_id
        return self._dispatch_validated(
            text, cap_id, args, producer, route_via=route.via,
            route_score=route.score)

    def dispatch_by_id(self, capability_id: str,
                       args: Optional[Dict[str, Any]] = None,
                       producer: Optional[str] = None,
                       request_text: str = "") -> DispatchResult:
        """Governed direct dispatch by capability identity (no NL routing).

        Used when the caller already knows WHICH capability to run -- e.g.
        an agent acting on admitted organizational knowledge that names the
        capability. This is NOT a bypass: every invocation-time check the
        routed path performs (existence, plan fingerprint, effective
        status, argument validation, real Composer execution, persisted
        record) runs identically. Only the NL routing step is skipped, and
        the record marks route_via="direct_by_id" so the provenance is
        explicit. Refusals are identical to dispatch()'s.
        """
        if not isinstance(capability_id, str) or not capability_id:
            return DispatchResult(ok=False, refusal="invalid_input",
                                  reasons=["capability_id must be a string"])
        return self._dispatch_validated(
            request_text, capability_id, args, producer,
            route_via="direct_by_id", route_score=0.0)

    def _dispatch_validated(self, text: str, cap_id: str,
                            args: Optional[Dict[str, Any]],
                            producer: Optional[str],
                            route_via: Optional[str],
                            route_score: Optional[float]) -> DispatchResult:
        """Shared governed core: re-verify, validate args, execute, record."""
        rec = self.engine.capabilities.get(cap_id)
        if rec is None:
            return DispatchResult(ok=False, refusal="capability_vanished",
                                  reasons=["capability no longer in store"])

        # invocation-time re-verification (TOCTOU between selection and run)
        try:
            fp = plan_fingerprint(rec.plan)
        except Exception as exc:
            return DispatchResult(ok=False, refusal="plan_unreadable",
                                  capability_id=cap_id,
                                  reasons=[f"stored plan unreadable: {exc!r}"])
        if fp != cap_id:
            return DispatchResult(
                ok=False, refusal="capability_tampered", capability_id=cap_id,
                reasons=["stored plan no longer fingerprints to its id"])
        eff = effective_status(self.engine, cap_id)
        if eff.get("effective") != "active" or not eff.get("consistent"):
            return DispatchResult(
                ok=False, refusal="capability_unavailable",
                capability_id=cap_id,
                reasons=[f"effective status at dispatch: {eff.get('effective')}"])

        # argument validation against the declared params
        ok_a, coerced_or_err = self._validate_args(args, rec.plan)
        if not ok_a:
            return DispatchResult(ok=False, refusal="bad_arguments",
                                  capability_id=cap_id,
                                  reasons=[coerced_or_err])
        return self._execute_and_record(text, rec, coerced_or_err, producer,
                                        route_via, route_score)

    def _execute_and_record(self, text: str, rec: Any,
                            coerced: Dict[str, Any],
                            producer: Optional[str],
                            route_via: Optional[str],
                            route_score: Optional[float]) -> DispatchResult:
        """Execute through the real Composer path and persist the record."""
        cap_id = rec.capability_id
        # 4. execute through the real Composer path
        dispatch_id = "dsp_" + uuid.uuid4().hex[:16]
        input_digest = hashlib.sha256(
            json.dumps(coerced, sort_keys=True).encode()).hexdigest()
        route = _RouteInfo(via=route_via, score=route_score)
        try:
            exec_res = self.engine.composer.execute_sync(
                dict(rec.plan), dict(coerced))
        except Exception as exc:
            self._record(dispatch_id, producer, text, route, cap_id,
                         rec.version, input_digest, None, False,
                         f"execution raised: {exc!r}")
            return DispatchResult(ok=False, refusal="execution_failed",
                                  capability_id=cap_id, dispatch_id=dispatch_id,
                                  route_via=route_via,
                                  reasons=[f"execution raised: {exc!r}"])
        if not isinstance(exec_res, dict) or not exec_res.get("success", True):
            err = (exec_res.get("error") if isinstance(exec_res, dict)
                   else "unknown execution failure")
            self._record(dispatch_id, producer, text, route, cap_id,
                         rec.version, input_digest, None, False, str(err))
            return DispatchResult(ok=False, refusal="execution_failed",
                                  capability_id=cap_id, dispatch_id=dispatch_id,
                                  route_via=route_via,
                                  reasons=[f"execution failed: {err}"])
        value = exec_res.get("value", exec_res.get("result", exec_res))
        result_digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
        self._record(dispatch_id, producer, text, route, cap_id,
                     rec.version, input_digest, result_digest, True, None)
        return DispatchResult(ok=True, result=value, capability_id=cap_id,
                              dispatch_id=dispatch_id, route_via=route_via,
                              reasons=[f"dispatched via {route_via}",
                                       f"dispatch_id={dispatch_id}"])

    def history(self, capability_id: Optional[str] = None,
                limit: int = 50) -> List[Dict[str, Any]]:
        con = sqlite3.connect(self.engine.db_path)
        try:
            con.row_factory = sqlite3.Row
            if capability_id:
                rows = con.execute(
                    "SELECT * FROM intent_dispatches WHERE capability_id=? "
                    "ORDER BY ts DESC LIMIT ?", (capability_id, limit)).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM intent_dispatches ORDER BY ts DESC LIMIT ?",
                    (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
