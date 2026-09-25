"""
swarm_engine/synthesis/admission.py

The gate between "the planner produced something" and "the engine will run it".

Nothing reaches the capability store without passing every stage here:

    propose -> type-check -> effect-check -> permission-check
            -> smoke-test -> admit (or reject with a reason)

The stages are ordered cheapest-first so a malformed plan is rejected by the
type checker before anything is executed, and an over-privileged plan is
rejected before the smoke test rather than after it has already touched a file.

A rejection is not a failure of the engine — it is the engine working. Every
rejection carries a machine-readable reason so the improvement loop can act on
it instead of guessing.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from swarm_engine.primitives.core import (
    Effect, ExecContext, Governor, PermissionError_, PrimitiveRegistry,
)
from swarm_engine.synthesis.composer import Composer
from swarm_engine.synthesis.capability_store import (
    CapabilityRecord, CapabilityStore, plan_fingerprint,
)
try:
    from swarm_engine.governance.oracle_binding import (
        OracleRegistry, EngineOracleHandle, OracleBindingError,
        DECISION_ADMISSION_SMOKE,
    )
except ImportError:  # pragma: no cover - registry unavailable
    OracleRegistry = None  # type: ignore


class Verdict:
    ADMITTED = "admitted"
    REJECTED = "rejected"
    REUSED = "reused"


@dataclass
class AdmissionResult:
    verdict: str
    capability_id: Optional[str] = None
    record: Optional[CapabilityRecord] = None
    reasons: List[str] = field(default_factory=list)
    stage: str = ""
    analysis: Dict[str, Any] = field(default_factory=dict)
    smoke: Optional[Dict[str, Any]] = None
    elapsed_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.ADMITTED, Verdict.REUSED)

    def as_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "capability_id": self.capability_id,
                "reasons": self.reasons, "stage": self.stage,
                "analysis": self.analysis, "smoke": self.smoke,
                "elapsed_ms": round(self.elapsed_ms, 2)}


@dataclass
class SmokeTest:
    """A minimal executable check that a plan does something rather than
    nothing. Supplied by the caller when it knows the shape of good input.

    ORACLE BINDING (2026-09-25): the judge rule is an oracle. A SmokeTest
    carrying a raw ``predicate`` with no ``oracle_id`` is an UNBOUND oracle
    and is REFUSED at admission -- this kills the E1 attack where a lying
    ``lambda v: True`` got a wrong plan admitted. Declarative ``expect``
    oracles are engine-evaluated (the engine computes the comparison
    itself); the admission controller auto-registers them under the engine
    producer with source="admission:auto_bound" so the exact (args, expect)
    pair that decided admission is on the tamper-evident record. A
    pre-registered predicate oracle (``oracle_id`` set) must additionally
    still match its registered definition digest at admit time, or the
    admission is refused -- the predicate evaluated must be the predicate
    registered.
    """
    args: Dict[str, Any]
    expect: Any = None
    predicate: Any = None            # Callable[[Any], bool]
    name: str = "smoke"
    oracle_id: Optional[str] = None
    oracle_version: Optional[int] = None

    def judge(self, value: Any) -> Tuple[bool, str]:
        if self.predicate is not None:
            try:
                return bool(self.predicate(value)), "predicate"
            except Exception as exc:
                return False, f"predicate raised {type(exc).__name__}: {exc}"
        if self.expect is not None:
            if isinstance(self.expect, float) and isinstance(value, (int, float)):
                return abs(value - self.expect) < 1e-9, "float compare"
            return value == self.expect, "equality"
        return value is not None, "non-null"


class _SmokeRefused(Exception):
    """Internal: smoke scoring refused for oracle-binding reasons."""


class AdmissionController:
    """Owns the decision to let a synthesized capability into the engine."""

    def __init__(self, registry: PrimitiveRegistry, store: CapabilityStore,
                 composer: Optional[Composer] = None,
                 max_effects: Optional[Sequence[Effect]] = None,
                 require_smoke_test: bool = False,
                 oracle_registry: Optional["OracleRegistry"] = None,
                 engine_oracle: Optional["EngineOracleHandle"] = None):
        self.reg = registry
        self.store = store
        self.composer = composer or Composer(registry)
        self.max_effects = set(max_effects) if max_effects is not None else None
        self.require_smoke_test = require_smoke_test
        # Oracle binding: without a registry the controller cannot bind the
        # smoke oracle to a registered, authorized identity, so smoke
        # scoring is refused (fail closed). The engine always supplies one.
        self.oracle_registry = oracle_registry
        self.engine_oracle = engine_oracle

    def _resolve_smoke_oracle(self, smoke: SmokeTest,
                              supplier_id: Optional[str] = None
                              ) -> Tuple[Optional[str], Optional[int], str,
                                         Optional[str]]:
        """Return (oracle_id, version, via, supplier) for this smoke test,
        registering and authorizing as needed. Raises _SmokeRefused on any
        binding failure -- the caller converts it to a rejection, never a
        bypass."""
        reg = self.oracle_registry
        if reg is None:
            raise _SmokeRefused(
                "smoke oracle cannot be bound: admission controller has no "
                "oracle registry -- refusing to score")
        # Case 1: caller pre-registered the oracle and attached its id.
        if smoke.oracle_id:
            version = smoke.oracle_version or (
                reg.oracle_head(smoke.oracle_id) or {}).get("version", 1)
            ok, why = reg.verify_oracle_row(smoke.oracle_id, version)
            if not ok:
                raise _SmokeRefused(f"smoke oracle binding failed: {why}")
            if not reg.oracle_authorized(smoke.oracle_id, version,
                                         DECISION_ADMISSION_SMOKE):
                raise _SmokeRefused(
                    "smoke oracle is registered but not authorized for "
                    "admission_smoke -- refusing to score")
            if smoke.predicate is not None:
                # The predicate evaluated must still BE the registered one:
                # recompute its definition digest and compare.
                _, current_text = reg._definition_text(smoke.predicate)
                from swarm_engine.governance.oracle_binding import _digest as _d
                row = reg.oracle_version_row(smoke.oracle_id, version)
                if _d(current_text) != row["definition_digest"]:
                    raise _SmokeRefused(
                        "smoke predicate does not match its registered oracle "
                        "definition -- refusing to score")
            # Case 1 supplier: whoever registered the oracle (its recorded
            # producer) -- not the engine that merely evaluates it.
            orow = reg.oracle_version_row(smoke.oracle_id, version)
            return smoke.oracle_id, version, "bound", \
                (orow["producer_id"] if orow else supplier_id)
        # Case 2: raw predicate with no binding -- the E1 attack shape.
        # Refused outright: an opaque callable the engine cannot inspect may
        # only influence admission as a registered, authorized oracle.
        if smoke.predicate is not None:
            raise _SmokeRefused(
                "unbound predicate oracle refused: a predicate may influence "
                "admission only as a registered oracle authorized for "
                "admission_smoke (see OracleRegistry.register_oracle)")
        # Case 3: declarative expect oracle. The engine evaluates the
        # comparison itself, so the oracle content is fully visible data.
        # Auto-register under the engine producer with source tagging that
        # distinguishes controller-adopted oracles from orchestrator-built
        # ones; authorize for admission_smoke as the engine's deliberate,
        # recorded policy.
        if self.engine_oracle is None:
            raise _SmokeRefused(
                "smoke oracle cannot be bound: no engine oracle handle -- "
                "refusing to score")
        import hashlib as _hl
        import json as _json
        _canon = _json.dumps(
            {"args": smoke.args, "expect": smoke.expect}, sort_keys=True,
            separators=(",", ":"), default=str)
        name = "smoke:auto:" + _hl.sha256(_canon.encode()).hexdigest()[:12]
        oracle_id, version = self.engine_oracle.register_oracle(
            name,
            {"kind": "smoke_expect", "args": smoke.args,
             "expect": smoke.expect, "name": smoke.name},
            input_contract="smoke.args",
            output_contract="judge(value) -> (passed, via)",
            source="admission:auto_bound")
        self.engine_oracle.authorize_oracle(oracle_id, version,
                                            DECISION_ADMISSION_SMOKE)
        # Case 3 supplier: the admit() caller whose expectation this is;
        # the engine is the evaluator. supplier_id=None means "the engine
        # itself" (explicit fallback, recorded as such).
        return oracle_id, version, "auto_bound", supplier_id


    # -- main path ----------------------------------------------------------
    def admit(self, goal: str, plan: Dict[str, Any],
              smoke: Optional[SmokeTest] = None,
              name: Optional[str] = None,
              epistemic_standing: Optional[Any] = None,
              supplier_id: Optional[str] = None,
              _restore_reverification: bool = False) -> AdmissionResult:
        """Admit a plan as a capability.

        supplier_id: who SUPPLIED the smoke oracle (the driver/caller whose
        expectation this is). It is recorded on the evaluation, distinct
        from the engine that evaluates. When omitted the evaluator (the
        engine) is recorded as supplier -- accurate for engine-owned
        oracles, an explicit fallback otherwise.

        epistemic_standing: optional live query ``(predicate, semantic_id)
        -> bool`` against the governing lexicon, supplied by callers that
        own one (e.g. the grounding export). It lets the epistemic-revocation
        guard distinguish "re-admission while justification is withdrawn"
        (refused) from "re-admission after the justification was restored
        by vindicating evidence" (audited recovery). Without it the guard
        fails closed, exactly as before.
        """
        started = time.time()

        def done(res: AdmissionResult) -> AdmissionResult:
            res.elapsed_ms = (time.time() - started) * 1000
            return res

        # -- 0. reuse -------------------------------------------------------
        cap_id = plan_fingerprint(plan)
        existing = self.store.get(cap_id)
        if existing and existing.status == "active":
            # Verify the stored record is not corrupted: its plan must
            # fingerprint to its own ID. A corrupted stored plan (same
            # ID, different content) must NOT be trusted as "identical"
            # -- quarantine it and admit the new plan fresh, so the
            # primitive registration reflects the just-admitted plan.
            if plan_fingerprint(existing.plan) != cap_id:
                self.store.set_status(cap_id, "quarantined")
                self.store.log(cap_id, "corruption_detected",
                               "stored plan fingerprint mismatch: corrupted "
                               "record quarantined, fresh admission proceeding")
            else:
                self.store.bind_goal(goal, cap_id)
                return done(AdmissionResult(Verdict.REUSED, cap_id, existing,
                                            ["identical plan already admitted"],
                                            stage="reuse"))

        # -- 0b. epistemic-revocation guard ----------------------------------
        # A record with this fingerprint exists but is not active. If it
        # was quarantined by EPISTEMIC revocation (contradicted evidence,
        # or dependency on such), admitting the identical plan would
        # silently resurrect a program whose justification was withdrawn:
        # store() is INSERT OR REPLACE, so "fresh admission" would flip
        # the quarantined row back to active. Refuse instead -- unless the
        # epistemic basis has been RESTORED since (vindicating evidence
        # retired the contradiction and the governing entry is learned
        # again under the same semantic id). Vindication is an explicit,
        # audited recovery, not silent resurrection: the revocation event
        # stays in history, followed by a vindication_recovery event.
        # Integrity quarantine is different: a corrupted stored plan IS
        # repaired by admitting the honest plan fresh, so this guard does
        # not apply to it.
        if existing is not None and self._epistemically_revoked(cap_id):
            if self._epistemic_basis_restored(cap_id, epistemic_standing):
                if plan_fingerprint(existing.plan) != cap_id:
                    return done(AdmissionResult(
                        Verdict.REJECTED, "",
                        reasons=["stored plan corrupted; vindication recovery "
                                 "refused -- fail closed"],
                        stage="reuse"))
                predicate, semantic_id = self._grounding_provenance(cap_id)
                self.store.set_status(cap_id, "active")
                self.store.log(
                    cap_id, "vindication_recovery",
                    f"epistemic basis restored: predicate={predicate} "
                    f"semantic_id={semantic_id}; capability re-activated "
                    f"after live lexicon re-validation")
                existing = self.store.get(cap_id)
                # The revocation unregistered acquired.<cap_id>; vindication
                # must restore it through the same registration path fresh
                # admission uses, or the re-activated capability is not
                # callable as a primitive. Fail closed if re-registration
                # cannot complete: do not claim recovery without
                # executability.
                try:
                    self._register_capability_as_primitive(cap_id, existing)
                except Exception as exc:
                    self.store.set_status(cap_id, "quarantined")
                    self.store.log(
                        cap_id, "vindication_recovery_failed",
                        f"primitive re-registration failed: {exc!r} "
                        f"-- recovery refused, fail closed")
                    return done(AdmissionResult(
                        Verdict.REJECTED, "",
                        reasons=["vindication recovery failed: primitive "
                                 "re-registration failed -- fail closed"],
                        stage="reuse"))
                return done(AdmissionResult(
                    Verdict.REUSED, cap_id, existing,
                    ["vindicated: epistemic basis restored by live lexicon; "
                     "capability re-activated (revocation event retained)"],
                    stage="reuse"))
            return done(AdmissionResult(
                Verdict.REJECTED, "",
                reasons=[f"capability {cap_id[:12]}... was epistemically "
                         "revoked (see capability_events); identical "
                         "re-admission refused -- a revoked program cannot "
                         "re-enter through re-acquisition"],
                stage="reuse"))

        # -- 0c. deliberate-quarantine guard ---------------------------------
        # A record with this fingerprint exists, is quarantined, and the
        # quarantine was DELIBERATE (not epistemic -- handled above -- and
        # not derived). Admitting the identical plan would silently
        # resurrect it: store() is INSERT OR REPLACE (row back to active)
        # and _register_capability_as_primitive re-registers acquired.<id>,
        # making it executable again while provenance/lifecycle still
        # record quarantine -- defeating the deliberate stickiness
        # ("quarantined deliberately" in capability_store.py). Refuse;
        # restoration goes through the governed, authorized
        # integrity.restore_everywhere path. (Derived quarantines are
        # different: they are designed to auto-recover via audit_all, so
        # this guard does not apply to them.) The guard is skipped only for
        # restore_everywhere's own re-verification (see _restore_reverification
        # -- restore has already authorized this exact restore).
        if (not _restore_reverification
                and existing is not None and existing.status == "quarantined"
                and self.store._quarantine_was_deliberate(cap_id)):
            return done(AdmissionResult(
                Verdict.REJECTED, "",
                reasons=[f"capability {cap_id[:12]}... is deliberately "
                         "quarantined; identical re-admission refused -- "
                         "deliberate revocations are sticky; restore via "
                         "integrity.restore_everywhere with "
                         "trust:transition authority"],
                stage="reuse"))

        # -- 1. type / structure check --------------------------------------
        analysis = self.composer.analyze(plan)
        if not analysis.ok:
            return done(AdmissionResult(Verdict.REJECTED, reasons=analysis.errors,
                                        stage="type_check",
                                        analysis=analysis.as_dict()))

        # -- 2. effect ceiling ----------------------------------------------
        demanded = {Effect(e) if not isinstance(e, Effect) else e
                    for e in analysis.effects}
        if self.max_effects is not None:
            over = demanded - self.max_effects
            if over:
                return done(AdmissionResult(
                    Verdict.REJECTED,
                    reasons=[f"effect {e.value!r} exceeds the configured ceiling"
                             for e in sorted(over, key=lambda x: x.value)],
                    stage="effect_ceiling", analysis=analysis.as_dict()))

        # -- 3. permission check --------------------------------------------
        permitted, missing = self.composer.permitted(plan)
        if not permitted:
            return done(AdmissionResult(
                Verdict.REJECTED,
                reasons=[f"ungranted effect: {m}" for m in missing],
                stage="permission", analysis=analysis.as_dict()))

        # -- 4. smoke test ---------------------------------------------------
        # The plan is executed by the engine's own composer (honest value).
        # The JUDGE RULE is an oracle: it must be bound to a registered,
        # authorized oracle before its verdict may influence admission.
        smoke_report: Optional[Dict[str, Any]] = None
        if smoke is not None:
            try:
                oracle_id, oracle_version, oracle_via, oracle_supplier = \
                    self._resolve_smoke_oracle(smoke, supplier_id)
            except _SmokeRefused as exc:
                return done(AdmissionResult(
                    Verdict.REJECTED,
                    reasons=[f"smoke test refused: {exc}"],
                    stage="smoke_test", analysis=analysis.as_dict()))
            run = self.composer.execute_sync(plan, smoke.args, skip_check=True)
            if not run.get("success"):
                return done(AdmissionResult(
                    Verdict.REJECTED,
                    reasons=[f"smoke test raised: {run.get('error')}"],
                    stage="smoke_test", analysis=analysis.as_dict(), smoke=run))
            passed, how = smoke.judge(run.get("value"))
            smoke_report = {"passed": passed, "via": how,
                            "value": _safe(run.get("value")),
                            "elapsed_ms": run.get("elapsed_ms"),
                            "primitive_calls": run.get("primitive_calls"),
                            "oracle_id": oracle_id,
                            "oracle_version": oracle_version,
                            "oracle_via": oracle_via}
            # Bind the evaluation: this exact oracle, over this exact input,
            # produced this exact result. The binding (not the verdict alone)
            # is what admission records.
            try:
                eval_id = self.engine_oracle.evaluate(
                    oracle_id,
                    {"args": smoke.args, "capability_id": cap_id},
                    {"value": _safe(run.get("value")), "passed": passed,
                     "via": how},
                    input_ref=f"admit:{cap_id[:16]}",
                    version=oracle_version,
                    supplier_id=oracle_supplier)
                smoke_report["oracle_binding"] = {
                    "oracle_id": oracle_id, "version": oracle_version,
                    "eval_id": eval_id}
            except Exception as exc:
                return done(AdmissionResult(
                    Verdict.REJECTED,
                    reasons=[f"smoke evaluation binding failed: {exc}"],
                    stage="smoke_test", analysis=analysis.as_dict(),
                    smoke=smoke_report))
            if not passed:
                return done(AdmissionResult(
                    Verdict.REJECTED,
                    reasons=[f"smoke test produced {run.get('value')!r}, "
                             f"which failed the {how} check"],
                    stage="smoke_test", analysis=analysis.as_dict(),
                    smoke=smoke_report))
        elif self.require_smoke_test:
            return done(AdmissionResult(
                Verdict.REJECTED,
                reasons=["a smoke test is required but none was supplied"],
                stage="smoke_test", analysis=analysis.as_dict()))

        # -- 5. admit --------------------------------------------------------
        record = CapabilityRecord(
            capability_id=cap_id,
            name=name or plan.get("name") or "capability",
            goal=goal,
            plan=plan,
            ops=sorted(set(analysis.ops)) if hasattr(analysis, "ops") else _ops(plan),
            effects=sorted(e.value for e in demanded),
        )
        self.store.store(record)
        self.store.bind_goal(goal, cap_id)
        self.store.log(cap_id, "admitted",
                       f"effects={record.effects} ops={len(record.ops)}")

        # M+29.26: classify and persist portability for every admitted
        # capability, through the real public admission path rather
        # than only via a manual, out-of-band call. Never blocks or
        # alters the admission decision itself -- a capability that is
        # NON_PORTABLE/UNSUPPORTED is still admitted normally; this only
        # records whether it can ALSO run independently of the live
        # registry, reusing the existing event log rather than a new
        # schema migration.
        try:
            from swarm_engine.synthesis.portable_ir import export_portable_capability
            artifact = export_portable_capability(record.plan, record.ops, self.reg, self.store)
            self.store.log(cap_id, "portability",
                           json.dumps({"status": "PORTABLE", "artifact": artifact}))
            # M+29.29: a capability admitted as genuinely PORTABLE also
            # becomes a new, reusable primitive in the registry itself,
            # so GeneralSynthesizer's own, unmodified search can discover
            # and compose it into FUTURE capabilities exactly like any
            # other primitive -- no separate discovery mechanism is
            # built; the existing registry search IS the discovery
            # mechanism, now simply given more to search over.
            self._register_capability_as_primitive(cap_id, record)
        except Exception as e:
            self.store.log(cap_id, "portability",
                           json.dumps({"status": "NOT_PORTABLE", "reason": str(e)[:200]}))
            # Local (process) registration still occurs so Composer and
            # parent plans can call the admitted capability in this
            # engine instance. Portability remains separately recorded.
            try:
                self._register_capability_as_primitive(cap_id, record)
                self.store.log(cap_id, "local_primitive_registration",
                               "registered acquired.* for in-process composition")
            except Exception as e2:
                self.store.log(cap_id, "local_primitive_registration",
                               f"failed: {str(e2)[:200]}")

        return done(AdmissionResult(Verdict.ADMITTED, cap_id, record,
                                    ["all admission stages passed"],
                                    stage="admitted",
                                    analysis=analysis.as_dict(),
                                    smoke=smoke_report))

    # -- convenience --------------------------------------------------------
    def _epistemically_revoked(self, cap_id: str) -> bool:
        """Whether this capability id carries an epistemic revocation in
        its event history (itself contradicted, or invalidated through a
        revoked dependency). Consulted by the reuse guard so an
        identical plan cannot silently resurrect a withdrawn program."""
        try:
            for ev in self.store.events(cap_id, limit=200):
                if (ev.get("event") in ("epistemic_revocation",
                                        "dependency_revocation")):
                    return True
        except Exception:
            pass
        return False

    def _grounding_provenance(self, cap_id: str) -> Tuple[Optional[str],
                                                         Optional[str]]:
        """(predicate, semantic_id) this capability was admitted from, or
        (None, None). Fail-closed: any read problem means no provenance."""
        try:
            from swarm_engine.cognition.revocation import (
                read_grounding_provenance)
            for row in read_grounding_provenance(self.store):
                if row.get("capability_id") == cap_id:
                    return row.get("predicate"), row.get("semantic_id")
        except Exception:
            pass
        return None, None

    def _epistemic_basis_restored(self, cap_id: str,
                                  epistemic_standing: Optional[Any]) -> bool:
        """Whether the law whose withdrawal caused the epistemic revocation
        is currently valid again. The provenance names (predicate,
        semantic_id); the live lexicon (via the caller's query) decides.
        No hook, no provenance, or any failure -> False (fail closed, the
        guard refuses as before)."""
        if epistemic_standing is None:
            return False
        predicate, semantic_id = self._grounding_provenance(cap_id)
        if not predicate:
            return False
        try:
            return bool(epistemic_standing(predicate, semantic_id))
        except Exception:
            return False

    def _register_capability_as_primitive(self, cap_id: str, record: "CapabilityRecord") -> None:
        from swarm_engine.primitives.core import Primitive, Effect, ANY
        prim_name = f"acquired.{cap_id}"
        # Overwrite (not skip) on re-registration: admission is the
        # authority on the current plan for this capability ID. Skipping
        # when a name exists leaves a STALE primitive if the stored plan
        # was corrupted and then re-acquired (same deterministic ID, new
        # correct plan) -- the parent assembly would then execute the
        # corrupted plan. Overwriting with the just-admitted plan is
        # idempotent when the plan is unchanged.
        params = list((record.plan.get("params") or {}).keys())
        composer = self.composer

        def _run(**kwargs):
            out = composer.execute_sync(record.plan, kwargs)
            if not out.get("success"):
                raise RuntimeError(out.get("error", "acquired capability execution failed"))
            return out.get("value")

        # 2026-09-20: expose the admitted plan and composer for batched
        # evaluation. High-frequency callers (the N-way value-vector
        # probe) apply an acquired op to whole example vectors; going
        # through execute_sync per scalar pays asyncio.run() plus a
        # redundant static analysis per call (~1ms). The batched path
        # runs one event loop for the whole vector and skips
        # re-analysis of the already-admitted plan. Behaviorally
        # identical: same plan, same composer, failures raise the same
        # RuntimeError.
        _run._remor_plan = record.plan
        _run._remor_composer = composer

        self.reg.register(Primitive(
            name=prim_name, family="acquired",
            fn=_run, inputs={p: ANY for p in params}, output=ANY,
            effects=tuple(Effect(e) for e in record.effects) if record.effects else (Effect.PURE,),
            doc=f"acquired capability {cap_id}",
        ), overwrite=True)
        # Tag this primitive name as a sub-capability reference (not a
        # leaf, AST-extractable primitive) so export_portable_capability
        # recursively embeds ITS OWN portable plan instead of attempting
        # -- and correctly failing -- to AST-extract this closure.
        if not hasattr(self.reg, "_acquired_capability_ids"):
            self.reg._acquired_capability_ids = {}
        self.reg._acquired_capability_ids[prim_name] = cap_id

    def rehydrate_all(self, store: "CapabilityStore" = None, status: str = "active") -> List[str]:
        """Register all matching capabilities so parent plans that reference
        acquired.* children execute after a fresh process.

        Order is dependency-respecting: a capability whose plan mentions
        acquired.<child_id> is registered only after that child is registered.
        This is the general fix for multi-op structural parents that embed
        native ops (append/lift/...) plus acquired child lookups.
        """
        store = store or self.store
        records = [r for r in store.list() if (status is None or r.status == status)]
        # Build dependency edges from the persisted plan graph (acquired.<id>
        # ops and tagged bare acquired names), so a capability whose plan
        # mentions an acquired child is registered only after that child.
        deps = {}
        by_id = {r.capability_id: r for r in records}
        for r in records:
            needed = []
            try:
                from swarm_engine.cognition.revocation import plan_acquired_refs
                child_ids = plan_acquired_refs(r.plan, self.reg)
            except Exception:
                child_ids = set()
            for child_id in child_ids:
                if child_id in by_id and child_id != r.capability_id:
                    needed.append(child_id)
            deps[r.capability_id] = needed
        # Kahn topological order
        remaining = set(by_id)
        registered: List[str] = []
        while remaining:
            ready = [cid for cid in remaining if all(d not in remaining for d in deps.get(cid, []))]
            if not ready:
                # Cycle or missing dep — register residual in stable order
                ready = sorted(remaining)
            for cid in sorted(ready):
                if cid not in remaining:
                    continue
                self._register_capability_as_primitive(cid, by_id[cid])
                registered.append(cid)
                remaining.discard(cid)
        return registered

    def dry_run(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Everything except storage — what would happen if we tried?"""
        analysis = self.composer.analyze(plan)
        permitted, missing = (self.composer.permitted(plan)
                              if analysis.ok else (False, ["plan did not type-check"]))
        return {"type_ok": analysis.ok, "errors": analysis.errors,
                "effects": sorted(str(e) for e in analysis.effects),
                "permitted": permitted, "missing_grants": missing,
                "output_type": str(analysis.output_type),
                "capability_id": plan_fingerprint(plan)}


def _ops(plan: Dict[str, Any]) -> List[str]:
    out: List[str] = []

    def walk(steps):
        for s in steps or []:
            if "op" in s:
                out.append(s["op"])
            for k in ("then", "else", "body", "catch"):
                if isinstance(s.get(k), list):
                    walk(s[k])
            if isinstance(s.get("branches"), dict):
                for b in s["branches"].values():
                    walk(b)
            for v in (s.get("args") or {}).values():
                if isinstance(v, dict) and "$lambda" in v:
                    walk(v["$lambda"].get("steps"))

    walk(plan.get("steps"))
    return sorted(set(out))


def _safe(value: Any, limit: int = 200) -> Any:
    """Truncate a value so an admission report never carries a megabyte."""
    try:
        s = repr(value)
    except Exception:
        return "<unrepresentable>"
    return s if len(s) <= limit else s[:limit] + "..."
