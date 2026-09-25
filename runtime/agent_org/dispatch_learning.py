"""Dispatch-to-organizational-learning evidence (Track 3, 2026-09-25).

This module closes the Track 2B gap "organizational learning from dispatch:
ABSENT". It adds NO parallel learning architecture: dispatch evidence is
recorded in the organizational trust core (the hash-chained, externally
anchored ``ao_dispatch_evidence`` table in agent_org/store.py), reviewed by
the existing ReviewBoard through the real IndependentValidator in a real
subprocess, and admitted into the existing ao_experiences L2 table through a
new governed admission function. Every trust property of the existing chain
is preserved:

* the raw ``intent_dispatches`` operational row is NEVER trusted and NEVER
  authorizes knowledge -- it is an operational audit log (plain table, no
  chain, no anchor);
* the evidence document is attributable to the exact agent + assignment that
  performed the dispatch (agent_id is an engine-issued identity, not the
  free-form ``producer`` string the dispatcher records);
* the independent review RE-DERIVES the result: it re-fetches the stored
  capability plan, re-checks the plan fingerprint, and re-executes the plan
  with the recorded arguments in a subprocess through a fresh Composer over
  the base primitive vocabulary. The agent's claims determine nothing;
* the verdict binds the canonical evidence-document bytes
  (artifact_kind="dispatch_evidence") and is stored by the ReviewBoard
  itself with the authorized verifier identity;
* admission derives trust ONLY from that stored admitted verdict, exactly
  like promote()/record_l3().

Bound (documented, not hidden): re-execution rebuilds the base primitive
vocabulary via ``build_registry()``. A capability whose plan composes OTHER
acquired capabilities (``acquired.<id>`` ops) cannot be re-executed this way
and its dispatch evidence is refused at review -- fail closed.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.agent_org.store import canonical, digest, now

# New artifact kind, parallel to "work_product" / "generality" / "synthesis".
# A verdict of this kind authorizes ONLY dispatch-knowledge admission; it
# never authorizes work-product acceptance, L2 promotion of agent
# candidates, or L3 synthesis (kind-scoped, like the existing kinds).
DISPATCH_EVIDENCE_KIND = "dispatch_evidence"

# artifact_ref prefix for the technique-generality verdicts that authorize
# dispatch-knowledge admission (kind "generality", same semantics as
# promote(): held-out behavioural verification of the technique code).
DISPATCH_KNOWLEDGE_REF_PREFIX = "dispatch_knowledge:"


# ---------------------------------------------------------------------------
# Re-execution harness.
#
# This is fixed REVIEW-PROCEDURE code, not the artifact under review (the
# artifact is the evidence document; its digest is what the verdict binds).
# It runs in a real subprocess via run_code: it rebuilds the base primitive
# vocabulary, re-executes the stored plan with the recorded canonical
# arguments, and returns the recomputed digests. The digest formulas are
# byte-identical to NLToolDispatcher.dispatch()'s.
#
# The subprocess must be able to ``import swarm_engine``: the driver sets
# PYTHONPATH to the runtime tree before review (run_code inherits the
# environment). If the import fails the harness raises, the case fails, and
# the verdict is refused -- fail closed, never silently skipped.
# ---------------------------------------------------------------------------
_DISPATCH_REEXEC_HARNESS = '''
def verify_dispatch(plan_json, args_json):
    """Re-execute a stored capability plan; return recomputed digests.

    Total over string inputs: malformed plans/args produce a clean
    {"ok": False, "error": ...} failure, never an unhandled raise, so the
    review procedure cannot be crashed by hostile input.
    """
    import hashlib as _hl
    import json as _json

    def _fail(err):
        return {"ok": False, "input_digest": None, "result_digest": None,
                "error": str(err)[:200]}

    try:
        plan = _json.loads(plan_json)
        args = _json.loads(args_json)
    except Exception as exc:
        return _fail(f"malformed input: {exc}")
    try:
        from swarm_engine.primitives import build_registry
        from swarm_engine.synthesis.composer import Composer
        res = Composer(build_registry()).execute_sync(plan, args)
    except Exception as exc:
        return _fail(f"re-execution failed: {exc}")
    ok = isinstance(res, dict) and bool(res.get("success", False))
    if isinstance(res, dict):
        value = res.get("value", res.get("result", res))
    else:
        value = res
    return {
        "ok": ok,
        "input_digest": _hl.sha256(
            _json.dumps(args, sort_keys=True).encode("utf-8")).hexdigest(),
        "result_digest": _hl.sha256(
            _json.dumps(value, sort_keys=True, default=str)
            .encode("utf-8")).hexdigest(),
    }
'''.lstrip()


@dataclass
class DispatchEvidence:
    """One attributable dispatch-evidence record."""
    evidence_id: str
    dispatch_id: str
    agent_id: str
    assignment_id: str
    request_text: str
    route_via: Optional[str]
    # Stored as canonical strings: the evidence table's columns are all
    # TEXT (codebase-wide pattern), so numeric values are normalized to
    # their string forms at capture time. This keeps the chain digest
    # stable across the TEXT round-trip (float 1.0 -> "1.0", int 1 -> "1").
    route_score: Optional[str]
    capability_id: str
    capability_version: Optional[str]
    plan_fingerprint: str
    args_json: str          # canonical JSON of the coerced arguments
    input_digest: str
    result_json: str        # canonical JSON of the result value ("null" if none)
    result_digest: Optional[str]
    ok: bool
    error: Optional[str]
    evidence_json: str      # canonical evidence document (verdict binds this)
    created_at: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "dispatch_id": self.dispatch_id,
            "agent_id": self.agent_id,
            "assignment_id": self.assignment_id,
            "request_text": self.request_text,
            "route_via": self.route_via,
            "route_score": self.route_score,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "plan_fingerprint": self.plan_fingerprint,
            "args_json": self.args_json,
            "input_digest": self.input_digest,
            "result_json": self.result_json,
            "result_digest": self.result_digest,
            "ok": self.ok,
            "error": self.error,
            "evidence_json": self.evidence_json,
            "created_at": self.created_at,
        }


def build_evidence_doc(*, evidence_id: str, dispatch_id: str, ts: float,
                       agent_id: str, assignment_id: str, request_text: str,
                       route_via: Optional[str], route_score: Optional[str],
                       capability_id: str, capability_version: Optional[str],
                       plan_fingerprint: str, args: Dict[str, Any],
                       input_digest: str, result_value: Any,
                       result_digest: Optional[str], ok: bool,
                       error: Optional[str]) -> str:
    """Build the canonical evidence document (the byte string the verdict
    binds to). Fixed field order; canonical() sorts keys anyway."""
    doc = {
        "evidence_id": evidence_id,
        "dispatch_id": dispatch_id,
        "dispatched_at": ts,
        "agent_id": agent_id,
        "assignment_id": assignment_id,
        "request_text": request_text,
        "route_via": route_via,
        "route_score": route_score,
        "capability_id": capability_id,
        "capability_version": capability_version,
        "plan_fingerprint": plan_fingerprint,
        "args": args,
        "input_digest": input_digest,
        "result": result_value,
        "result_digest": result_digest,
        "ok": bool(ok),
        "error": error,
    }
    return canonical(doc)


def _operational_row(engine: Any, dispatch_id: str) -> Dict[str, Any]:
    """Read the engine-written operational dispatch row. This row is evidence
    of WHAT the dispatcher recorded, never trusted on its own."""
    con = sqlite3.connect(engine.db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM intent_dispatches WHERE dispatch_id=?",
            (dispatch_id,)).fetchone()
    finally:
        con.close()
    if row is None:
        raise KeyError(f"no operational dispatch row for {dispatch_id!r}")
    return dict(row)


def capture_dispatch_evidence(engine: Any, store: Any, dispatch_id: str,
                              agent_id: str, assignment_id: str,
                              args: Dict[str, Any],
                              result_value: Any) -> str:
    """Capture attributable dispatch evidence for one real dispatch.

    The caller supplies the arguments it dispatched with and the result
    value it observed. NOTHING supplied here is trusted: the operational
    row (written by the engine's dispatcher, not the agent) is re-read and
    the supplied bytes must reproduce its recorded digests EXACTLY, or
    capture refuses. The independent review later re-derives the result a
    second time in a subprocess.

    Returns the evidence_id. Raises on any inconsistency (fail closed).
    """
    op = _operational_row(engine, dispatch_id)
    if not op["ok"]:
        raise ValueError(
            f"capture refused: dispatch {dispatch_id} was not successful "
            f"(refusal/error={op.get('error')!r}); only successful dispatches "
            "produce learnable evidence")
    # Agent + assignment binding: the named agent must exist, must not be
    # destroyed, and the assignment must belong to that agent. The
    # dispatcher's free-form producer string is never trusted for this.
    agent_row = store.latest("ao_agents", "agent_id", agent_id)
    if agent_row is None:
        raise ValueError(
            f"capture refused: unknown agent {agent_id!r}")
    if agent_row.get("state") == "DESTROYED":
        raise ValueError(
            f"capture refused: agent {agent_id} is DESTROYED")
    asg_row = store.latest("ao_assignments", "assignment_id", assignment_id)
    if asg_row is None:
        raise ValueError(
            f"capture refused: unknown assignment {assignment_id!r}")
    if asg_row.get("agent_id") != agent_id:
        raise ValueError(
            "capture refused: assignment "
            f"{assignment_id} belongs to agent {asg_row.get('agent_id')!r}, "
            f"not {agent_id!r} (agent forgery refused)")
    # Capability identity at capture time.
    rec = engine.capabilities.get(op["capability_id"])
    if rec is None:
        raise ValueError(
            f"capture refused: capability {op['capability_id']} vanished")
    from swarm_engine.synthesis.capability_store import plan_fingerprint
    from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher
    fp = plan_fingerprint(rec.plan)
    # Re-derive the coerced arguments through the dispatcher's OWN
    # validator, so the evidence binds exactly the bytes the dispatcher
    # executed with -- never a caller-coerced copy.
    _disp = NLToolDispatcher(engine)
    vok, coerced_or_err = _disp._validate_args(args, rec.plan)
    if not vok:
        raise ValueError(
            "capture refused: supplied args do not validate against the "
            f"stored plan ({coerced_or_err})")
    coerced = coerced_or_err
    # The caller-supplied bytes must reproduce the engine-recorded digests.
    args_canonical = json.dumps(coerced, sort_keys=True)
    input_digest = hashlib.sha256(args_canonical.encode("utf-8")).hexdigest()
    if not _const_eq(input_digest, str(op["input_digest"])):
        raise ValueError(
            "capture refused: supplied args do not reproduce the recorded "
            f"input_digest for {dispatch_id}")
    result_canonical = json.dumps(result_value, sort_keys=True, default=str)
    result_digest = hashlib.sha256(
        result_canonical.encode("utf-8")).hexdigest()
    if not _const_eq(result_digest, str(op["result_digest"])):
        raise ValueError(
            "capture refused: supplied result does not reproduce the "
            f"recorded result_digest for {dispatch_id}")

    evidence_id = "dsp_ev_" + digest(dispatch_id + agent_id)[:16]
    # Duplicate/replay guard: the same dispatch attributed to the same
    # agent always yields the same evidence_id. A second capture is not
    # new evidence -- refuse it instead of writing a duplicate row that
    # could be mistaken for independent corroboration.
    if store.latest("ao_dispatch_evidence", "evidence_id",
                    evidence_id) is not None:
        raise ValueError(
            f"capture refused: evidence {evidence_id} already exists for "
            f"dispatch {dispatch_id} / agent {agent_id} (replay refused)")
    # Normalize numerics to canonical strings BEFORE building the doc and
    # the row: the evidence table is all-TEXT, so the chain digest must be
    # computed over the exact strings that survive the round-trip.
    rs = op["route_score"]
    route_score_s = None if rs is None else str(float(rs))
    cv = op["capability_version"]
    cap_version_s = None if cv is None else str(int(cv))
    doc = build_evidence_doc(
        evidence_id=evidence_id, dispatch_id=dispatch_id, ts=op["ts"],
        agent_id=agent_id, assignment_id=assignment_id,
        request_text=op["request_text"], route_via=op["route_via"],
        route_score=route_score_s, capability_id=op["capability_id"],
        capability_version=cap_version_s, plan_fingerprint=fp,
        args=coerced, input_digest=input_digest, result_value=result_value,
        result_digest=result_digest, ok=True, error=None)
    store.insert("ao_dispatch_evidence", {
        "evidence_id": evidence_id, "dispatch_id": dispatch_id,
        "agent_id": agent_id, "assignment_id": assignment_id,
        "request_text": op["request_text"], "route_via": op["route_via"],
        "route_score": route_score_s,
        "capability_id": op["capability_id"],
        "capability_version": cap_version_s,
        "plan_fingerprint": fp, "args_json": args_canonical,
        "input_digest": input_digest, "result_json": result_canonical,
        "result_digest": result_digest, "ok": "1", "error": None,
        "evidence_json": doc, "created_at": now()})
    return evidence_id


def _const_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


def get_evidence(store: Any, evidence_id: str) -> DispatchEvidence:
    row = store.latest("ao_dispatch_evidence", "evidence_id", evidence_id)
    if row is None:
        raise KeyError(f"unknown dispatch evidence {evidence_id!r}")
    return DispatchEvidence(
        evidence_id=row["evidence_id"], dispatch_id=row["dispatch_id"],
        agent_id=row["agent_id"], assignment_id=row["assignment_id"],
        request_text=row["request_text"], route_via=row["route_via"],
        route_score=row["route_score"], capability_id=row["capability_id"],
        capability_version=row["capability_version"],
        plan_fingerprint=row["plan_fingerprint"], args_json=row["args_json"],
        input_digest=row["input_digest"], result_json=row["result_json"],
        result_digest=row["result_digest"], ok=row["ok"] == "1",
        error=row["error"], evidence_json=row["evidence_json"],
        created_at=row["created_at"])


def list_evidence(store: Any) -> List[DispatchEvidence]:
    rows = store.rows("ao_dispatch_evidence")
    seen: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        seen[r["evidence_id"]] = r
    return [get_evidence(store, eid) for eid in seen]
