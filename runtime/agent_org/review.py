"""Independent review board.

submit_for_review(wp_id): SUBMITTED -> UNDER_REVIEW (work product AND agent).

review(wp_id, spec, cases): runs the implementation through
IndependentValidator with the REAL subprocess runner, the oracle registry,
and the engine oracle handle. Every case is registered as an engine oracle
authorized for the "verification" decision class (done inside the
Verifier); the Arbiter refuses evidence without verified bindings. Returns
the Verdict.

The review path NEVER reads work_product.self_reported_success -- there is
no code path from that field to any verdict.

accept/reject REQUIRE a live EngineOracleHandle bound to remor:engine
(None, or any other producer, raises AuthorityError). The decision is
recorded as a chained ao_manager_decisions row with producer remor:engine.
accept() creates an ExperienceCandidate (L1); reject() does not.

VERDICT BINDING (mission 2026-09-25): every verification execution stores
one authoritative row in ao_review_verdicts, bound to the EXACT code that
was verified (code_digest), the artifact kind/ref, the verifier identity,
a unique execution_id, and the verification standard (spec_digest).
Admission paths (accept(), ExperienceStore.promote(), ExperienceStore.record_l3())
derive trust ONLY from these stored rows -- never from caller-supplied claims.
A verdict for code X authorizes nothing for code Y != X, and a verdict of one
artifact_kind never authorizes an admission scoped to another kind
(kind-scoped: "work_product" -> accept, "generality" -> L2 promote,
"synthesis" -> L3 record).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from swarm_engine.acquisition.semantic import Case
from swarm_engine.agent_org.exceptions import (
    AuthorityError,
    LifecycleError,
    VerificationFailed,
)
from swarm_engine.agent_org.store import OrgStore, digest, now, canonical
from swarm_engine.agent_org.subprocess_runner import run_code
from swarm_engine.governance.anchor import (
    AnchorMissing,
    AnchorMismatch,
    AnchorStore,
    collect_anchor_heads,
    default_anchor_paths,
)
from swarm_engine.agent_org.work_product import (
    get_work_product,
    row_to_work_product,
    WorkProduct,
)
from swarm_engine.verification.independent import (
    Arbiter,
    IndependentValidator,
    Verdict,
)

try:
    from swarm_engine.governance.oracle_binding import EngineOracleHandle
except Exception:  # pragma: no cover
    EngineOracleHandle = None  # type: ignore

_ENGINE = "remor:engine"

# Identity of the verification procedure authorized to produce verdict rows
# that admission paths trust. Set only by code in this module that actually
# EXECUTED IndependentValidator; never taken from caller input.
VERIFIER_ID = "independent-validator:subprocess:bound-oracles"

_WP_FLOW = {
    "SUBMITTED": ("UNDER_REVIEW",),
    "UNDER_REVIEW": ("ACCEPTED", "REJECTED"),
    "DRAFT": ("SUBMITTED",),
    "ACCEPTED": (),
    "REJECTED": (),
}


class _DispatchEvidenceSpec:
    """Spec for dispatch-evidence re-execution review.

    input_names must match the re-execution harness's parameters (the
    Critic checks the harness references them). examples[0] seeds the
    adversary's hostile/novelty probes; examples[1:] become held-out
    generalisation cases (deterministic re-execution satisfies them).
    """

    def __init__(self, evidence_id: str, args: Dict[str, Any],
                 expected: Dict[str, Any]):
        self.input_names = ["plan_json", "args_json"]
        self.description = (
            "dispatch-evidence re-execution: re-run the stored capability "
            f"plan with the recorded canonical args for {evidence_id}; the "
            "recomputed input/result digests must equal the recorded ones")
        self.examples = [(dict(args), dict(expected)),
                         (dict(args), dict(expected))]


def _require_engine(engine: Any) -> EngineOracleHandle:
    if EngineOracleHandle is None or not isinstance(engine, EngineOracleHandle):
        raise AuthorityError(
            "review decisions require a live EngineOracleHandle")
    if engine.producer_id != _ENGINE:
        raise AuthorityError(
            f"review decisions require producer 'remor:engine', got "
            f"{engine.producer_id!r}")
    return engine


class ReviewBoard:
    def __init__(self, store: OrgStore, oregistry: Any, engine: Any,
                 agents: Any, experience: Any,
                 anchor_store: Optional[AnchorStore] = None):
        self.store = store
        self.oregistry = oregistry
        self.engine = _require_engine(engine)
        self.agents = agents
        self.experience = experience
        # External trust anchor over org+oracle chain heads. Optional so
        # existing direct constructions keep working; defaults to the
        # anchor paths derived from the store's db path. The same instance
        # should be shared with RemorOrganization (it is, via boot()).
        self.anchor = (anchor_store if anchor_store is not None
                       else AnchorStore(*default_anchor_paths(store.db_path)))

    # -- reads ----------------------------------------------------------
    def get_work_product(self, wp_id: str) -> WorkProduct:
        return get_work_product(self.store, wp_id)

    # -- review flow ----------------------------------------------------
    def _set_wp_state(self, wp_id: str, to_state: str, actor: str,
                      reason: str = "") -> WorkProduct:
        wp = self.get_work_product(wp_id)
        if to_state not in _WP_FLOW.get(wp.state, ()):
            raise LifecycleError(
                f"work product {wp_id}: {wp.state} -> {to_state} not allowed")
        row = self.store.latest("ao_work_products", "wp_id", wp_id)
        new_row = dict(row)
        new_row["state"] = to_state
        fields = {k: new_row[k] for k in new_row
                  if k not in ("seq", "prev_digest", "row_digest")}
        self.store.insert("ao_work_products", fields)
        self.store.insert("ao_work_product_events", {
            "wp_id": wp_id, "from_state": wp.state, "to_state": to_state,
            "actor": actor, "reason": reason, "created_at": now()})
        return self.get_work_product(wp_id)

    def submit_for_review(self, wp_id: str,
                          actor: str = _ENGINE) -> WorkProduct:
        wp = self._set_wp_state(wp_id, "UNDER_REVIEW", actor,
                                reason="submitted for independent review")
        self.agents.transition(wp.agent_id, "UNDER_REVIEW", actor=actor,
                               reason=f"work product {wp_id} under review")
        return wp

    # -- verification execution -----------------------------------------
    def _run_validator(self, code: str, entrypoint: str, spec: Any,
                       cases: Sequence[Case]) -> Verdict:
        """Execute the real independent validator. The ONLY path that
        produces verdicts admission trusts."""
        validator = IndependentValidator(
            run_code,
            Arbiter(oracle_registry=self.oregistry, require_binding=True),
            seed=0,
            oracle_registry=self.oregistry,
            engine_oracle=self.engine)
        return validator.validate(code, entrypoint, spec, list(cases))

    @staticmethod
    def _spec_digest(spec: Any, cases: Sequence[Case]) -> str:
        """Bind the verification standard: what was asked of the code."""
        return digest(canonical({
            "inputs": list(getattr(spec, "input_names", [])),
            "description": str(getattr(spec, "description", "")),
            "cases": [(c.label, c.kind, c.must_fail,
                       canonical(c.args)) for c in cases],
        }))

    def _store_verdict(self, verdict: Verdict, code: str, spec: Any,
                       cases: Sequence[Case], artifact_kind: str,
                       artifact_ref: str, wp_id: str = "") -> str:
        """Persist the authoritative verdict row, bound to the exact code
        that was verified. Returns the execution_id."""
        execution_id = "vex_" + digest(
            digest(code) + artifact_kind + artifact_ref + now())[:16]
        evd = verdict.evidence or {}
        # Anchor pre-write check (F1-F3 repair): the journal commits us
        # to its heads BEFORE this insert lands. If a journal exists, the
        # current live heads must already match its tip -- a mismatch
        # means a crash between a previous insert and its anchor (or
        # tampering), and we perform NO insert: recovery is the audited
        # transition(reason="recovery") path, never a silently re-anchored
        # mislabeled write. If no journal exists, refuse outright: there
        # is no lazy genesis anymore, so a genuine write can never
        # silently re-anchor an attacker's database (F3).
        if self.anchor.journal_exists():
            ok, amsg = self.anchor.verify(
                collect_anchor_heads(self.store, self.oregistry))
            if not ok:
                raise AnchorMismatch(
                    f"verdict store refused: {amsg}")
        else:
            raise AnchorMissing(
                "verdict store refused: anchor journal not found -- run "
                "`anchor_admin.py init` to create the genesis record "
                "explicitly before storing verdicts")
        self.store.insert("ao_review_verdicts", {
            "wp_id": wp_id,
            "admitted": "1" if verdict.admitted else "0",
            "reasons_json": json.dumps(list(verdict.reasons), sort_keys=True,
                                       default=str),
            "bindings": json.dumps({
                "n_findings": len(evd.get("findings", [])),
                "cases": evd.get("cases"),
                "adversarial": evd.get("adversarial"),
                "failed_levels": evd.get("failed_levels")}, sort_keys=True,
                default=str),
            "code_digest": digest(code),
            "artifact_kind": artifact_kind,
            "artifact_ref": artifact_ref,
            "verifier": VERIFIER_ID,
            "execution_id": execution_id,
            "spec_digest": self._spec_digest(spec, cases),
            "created_at": now()})
        # Anchor the new chain heads OUTSIDE the database: a whole-DB
        # rewrite with recomputed internal chains cannot forge the journal
        # (the HMAC key lives outside the DB). The recorded authority is
        # the engine producer that executed the verification -- agent
        # identities stay the producers' own claims, as before.
        # Crash between the insert above and this anchor -> the next boot
        # (or require_admitted_verdict) sees a head mismatch and fails
        # closed; recover via an audited transition (reason="recovery").
        self.anchor.anchor(
            collect_anchor_heads(self.store, self.oregistry),
            reason="verdict",
            authority=getattr(self.engine, "producer_id", None))
        return execution_id

    def _latest_verdict(self, wp_id: str) -> Optional[Dict[str, Any]]:
        return self.store.latest("ao_review_verdicts", "wp_id", wp_id)

    def latest_artifact_verdict(self, code_digest: str,
                                artifact_kind: str) -> Optional[Dict[str, Any]]:
        """Latest stored verdict for an exact artifact version + kind."""
        rows = self.store.rows("ao_review_verdicts")
        matches = [r for r in rows
                   if r.get("code_digest") == code_digest
                   and r.get("artifact_kind") == artifact_kind]
        if not matches:
            return None
        return max(matches, key=lambda r: r["seq"])

    def require_admitted_verdict(self, code_digest: str,
                                 artifact_kind: str) -> Dict[str, Any]:
        """Return the latest stored admitted verdict row for an exact
        artifact version + kind, or raise VerificationFailed.

        Trust derivation lives here: admission paths never read caller
        evidence. Refuses when: no verdict row exists for this digest+kind,
        the latest row is not admitted, the row was not produced by the
        authorized verification procedure (verifier != VERIFIER_ID -- a
        caller-forged row cannot carry a legitimate execution), or the
        verdict chain fails audit (tamper-evident). Because the store is
        append-only, a later rejected verdict supersedes an earlier
        admitted one: the latest row governs.
        """
        ok, msg = self.store.audit("ao_review_verdicts")
        if not ok:
            raise VerificationFailed(
                "verdict binding refused: ao_review_verdicts chain broken "
                f"({msg})")
        # The single trust-derivation point is now anchor-aware: even an
        # internally consistent rewritten database is refused unless its
        # heads match the externally anchored heads.
        ok, amsg = self.anchor.verify(
            collect_anchor_heads(self.store, self.oregistry))
        if not ok:
            raise VerificationFailed(f"anchor mismatch: {amsg}")
        row = self.latest_artifact_verdict(code_digest, artifact_kind)
        if row is None:
            raise VerificationFailed(
                "verdict binding refused: no stored independent verdict for "
                f"artifact digest {code_digest[:12]} kind {artifact_kind!r} "
                "(verify through the ReviewBoard first)")
        if row.get("admitted") != "1":
            raise VerificationFailed(
                "verdict binding refused: latest stored verdict is not "
                f"admitted (execution {row.get('execution_id')})")
        if row.get("verifier") != VERIFIER_ID:
            raise VerificationFailed(
                "verdict binding refused: verdict row not produced by the "
                "authorized verification procedure "
                f"(verifier={row.get('verifier')!r})")
        return row

    def verify_generality(self, code: str, entrypoint: str, spec: Any,
                          cases: Sequence[Case],
                          candidate_id: str) -> Verdict:
        """Independently verify an L1 candidate's generality on held-out
        cases.

        Executes the real IndependentValidator and persists the
        authoritative verdict row bound to digest(code) with
        artifact_kind="generality" and artifact_ref=candidate_id. This is
        the ONLY verification path whose verdict can authorize L2 promotion
        via ExperienceStore.promote(): a generality verdict never
        authorizes L3 synthesis admission, and a synthesis verdict never
        authorizes promotion (kind-scoped).
        """
        if not code:
            raise VerificationFailed("verify_generality: empty code")
        verdict = self._run_validator(code, entrypoint or "selftest",
                                      spec, list(cases))
        self._store_verdict(verdict, code, spec, list(cases),
                            artifact_kind="generality",
                            artifact_ref=candidate_id)
        return verdict

    def review(self, wp_id: str, spec: Any,
               cases: Sequence[Case]) -> Verdict:
        """Independently verify the work product's implementation.

        NOTE: self_reported_success is never read here. The verdict comes
        only from measured behaviour under isolation plus oracle-bound
        evidence.
        """
        wp = self.get_work_product(wp_id)
        if not wp.artifacts:
            raise VerificationFailed(
                f"work product {wp_id} has no artifacts to verify")
        code = wp.artifacts[0].get("code", "")
        if not code:
            raise VerificationFailed(
                f"work product {wp_id}: empty implementation artifact")
        entrypoint = wp.entrypoint or "selftest"
        verdict = self._run_validator(code, entrypoint, spec, list(cases))
        # Bind the verdict to the work product AND the exact code verified:
        # accept() later requires this row to show an admitted verdict for
        # the current artifact bytes, so review can never be silently
        # skipped nor its verdict reused for modified code. (Admission with
        # require_binding=True already implies every required-level finding
        # carried a verified oracle binding; the summary is stored for
        # audit, not re-scored.)
        self._store_verdict(verdict, code, spec, list(cases),
                            artifact_kind="work_product",
                            artifact_ref=str(wp.outputs.get("technique", "")),
                            wp_id=wp_id)
        return verdict

    def verify_artifact(self, code: str, entrypoint: str, spec: Any,
                        cases: Sequence[Case], artifact_ref: str) -> Verdict:
        """Independently verify a REMOR-side artifact (e.g. a synthesized
        codec) that is not an agent work product.

        Executes the real IndependentValidator and persists the
        authoritative verdict row bound to digest(code). This is the ONLY
        verification path whose verdict can authorize L3 admission via
        ExperienceStore.record_l3(): the driver must call this (not its own
        validator) so the verdict is stored, not merely claimed.
        """
        if not code:
            raise VerificationFailed("verify_artifact: empty code")
        verdict = self._run_validator(code, entrypoint or "selftest",
                                      spec, list(cases))
        self._store_verdict(verdict, code, spec, list(cases),
                            artifact_kind="synthesis",
                            artifact_ref=artifact_ref)
        return verdict

    def verify_dispatch_evidence(self, evidence_id: str) -> Verdict:
        """Independently verify one captured dispatch-evidence record.

        The review RE-DERIVES the dispatch result; the agent's claims
        determine nothing:

        1. The evidence row is loaded from the chained organizational
           table (never from caller arguments).
        2. The CURRENT stored capability plan is re-fetched and its
           fingerprint must equal the recorded plan fingerprint -- a
           swapped, stale, or tampered-with plan is refused here.
        3. The plan is re-executed with the recorded canonical arguments in
           a real subprocess (fresh Composer over the rebuilt base
           primitive vocabulary), through the real IndependentValidator.
           The recomputed input/result digests must equal the recorded
           ones -- a forged or replaced result is refused here.

        The verdict is bound to the canonical evidence-document bytes
        (artifact_kind="dispatch_evidence", artifact_ref=evidence_id) and
        stored by this method itself. This is the ONLY verification path
        whose verdict can authorize dispatch-knowledge admission via
        ExperienceStore.admit_dispatch_knowledge().

        NOTE on procedure vs artifact: the executed code is the fixed
        review-procedure re-execution harness
        (dispatch_learning._DISPATCH_REEXEC_HARNESS); the artifact under
        review is the evidence document. The verdict's code_digest binds
        the evidence bytes; the spec_digest binds the verification
        standard (plan + recorded args + expected digests).
        """
        from swarm_engine.agent_org.dispatch_learning import (
            DISPATCH_EVIDENCE_KIND,
            _DISPATCH_REEXEC_HARNESS,
            get_evidence,
        )
        from swarm_engine.synthesis.capability_store import plan_fingerprint

        ev = get_evidence(self.store, evidence_id)
        # Attribution re-check (capture checked this too; the review does not
        # trust capture): the assignment must belong to the named agent.
        # Note: agent liveness is NOT re-checked here -- the evidence
        # documents a historical dispatch, and destroying the agent must
        # not rewrite that history. Liveness gates new captures only.
        asg_row = self.store.latest(
            "ao_assignments", "assignment_id", ev.assignment_id)
        if asg_row is None or asg_row.get("agent_id") != ev.agent_id:
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: assignment/agent "
                "binding broken -- refused")
        eng = getattr(self, "dispatch_engine", None)
        if eng is None:
            raise VerificationFailed(
                "dispatch evidence review requires a full SwarmEngine "
                "attached as review.dispatch_engine")
        rec = eng.capabilities.get(ev.capability_id)
        if rec is None:
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: capability "
                f"{ev.capability_id} vanished")
        live_fp = plan_fingerprint(rec.plan)
        if live_fp != ev.plan_fingerprint:
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: stored plan fingerprint "
                f"mismatch (recorded {ev.plan_fingerprint[:16]}... vs live "
                f"{live_fp[:16]}...): stale capability version or tampered "
                "plan -- refused")
        # Version currency: the evidence must describe the CURRENT
        # capability version, not a superseded one.
        if str(getattr(rec, "version", None)) != str(ev.capability_version):
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: capability version "
                f"changed since capture (recorded {ev.capability_version} "
                f"vs live {getattr(rec, 'version', None)}) -- refused")
        # Operational-row cross-check: the engine's dispatch record must
        # exist and agree with the evidence on every binding field. The
        # operational row is not trusted on its own; a divergence between
        # it and the evidence means one of them was tampered with.
        from swarm_engine.agent_org.dispatch_learning import (
            _operational_row,
        )
        try:
            op = _operational_row(eng, ev.dispatch_id)
        except KeyError:
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: no operational dispatch "
                f"row for {ev.dispatch_id} -- refused")
        _binding_pairs = [
            ("request_text", ev.request_text, op.get("request_text")),
            ("route_via", ev.route_via, op.get("route_via")),
            ("capability_id", ev.capability_id, op.get("capability_id")),
            ("input_digest", ev.input_digest, op.get("input_digest")),
            ("result_digest", ev.result_digest, op.get("result_digest")),
        ]
        for field, ev_val, op_val in _binding_pairs:
            if (ev_val or None) != (op_val or None):
                raise VerificationFailed(
                    f"dispatch evidence {evidence_id}: operational row "
                    f"diverges on {field} -- refused")
        op_rs = op.get("route_score")
        if (ev.route_score or None) != (
                None if op_rs is None else str(float(op_rs))):
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: operational row diverges "
                "on route_score -- refused")
        op_cv = op.get("capability_version")
        if (ev.capability_version or None) != (
                None if op_cv is None else str(int(op_cv))):
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: operational row diverges "
                "on capability_version -- refused")
        if not op.get("ok") or not ev.ok:
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: dispatch not recorded "
                "as successful -- refused")
        # Canonical-document consistency: the evidence JSON the verdict
        # binds must reproduce every row field exactly. A mismatched
        # document (swapped bytes under a valid chain) is refused here.
        try:
            doc = json.loads(ev.evidence_json)
        except (json.JSONDecodeError, TypeError) as exc:
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: evidence_json does not "
                f"parse ({exc}) -- refused")
        for field in ("evidence_id", "dispatch_id", "agent_id",
                      "assignment_id", "request_text", "route_via",
                      "route_score", "capability_id", "capability_version",
                      "plan_fingerprint", "input_digest", "result_digest",
                      "error"):
            if doc.get(field) != getattr(ev, field):
                raise VerificationFailed(
                    f"dispatch evidence {evidence_id}: evidence document "
                    f"diverges from the chained row on {field} -- refused")
        if not doc.get("ok"):
            raise VerificationFailed(
                f"dispatch evidence {evidence_id}: evidence document not "
                "marked ok -- refused")
        plan_json = json.dumps(rec.plan, sort_keys=True)
        expected = {"ok": True, "input_digest": ev.input_digest,
                    "result_digest": ev.result_digest}
        case = Case(
            args={"plan_json": plan_json, "args_json": ev.args_json},
            expect=expected, label=f"re-execute {evidence_id}")
        spec = _DispatchEvidenceSpec(evidence_id, case.args, expected)
        verdict = self._run_validator(_DISPATCH_REEXEC_HARNESS,
                                      "verify_dispatch", spec, [case])
        self._store_verdict(verdict, ev.evidence_json, spec, [case],
                            artifact_kind=DISPATCH_EVIDENCE_KIND,
                            artifact_ref=evidence_id)
        return verdict

    # -- decisions (engine-only) ----------------------------------------

    def verify_repair(self, repair_id: str, agent_id: str,
                      defect_signature: Dict[str, Any], diagnosis: str,
                      pre_code: str, post_code: str, entrypoint: str,
                      spec: Any, cases: Sequence[Case]) -> Verdict:
        """Independently verify a repair instance.

        Executes the real IndependentValidator against the REPAIRED
        artifact bytes (post_code) under subprocess isolation with
        oracle-bound case judgments. The agent's diagnosis and its claim
        that the repair succeeded are recorded (diagnosis) but NEVER read
        by the verdict path -- there is no code path from them to the
        verdict.

        Persists the authoritative verdict row bound to digest(post_code)
        with artifact_kind="repair" and artifact_ref=repair_id, and
        inserts the structured repair record into ao_repair_records.
        This is the ONLY verification path whose verdict can authorize
        repair admission via admit_repair(): a "repair" verdict never
        authorizes any other admission kind.
        """
        if not post_code:
            raise VerificationFailed("verify_repair: empty repaired code")
        if not repair_id:
            raise VerificationFailed("verify_repair: empty repair_id")
        verdict = self._run_validator(post_code, entrypoint or "selftest",
                                      spec, list(cases))
        execution_id = self._store_verdict(
            verdict, post_code, spec, list(cases),
            artifact_kind="repair", artifact_ref=repair_id)
        self.store.insert("ao_repair_records", {
            "repair_id": repair_id,
            "agent_id": agent_id,
            "defect_signature_json": json.dumps(defect_signature,
                                                sort_keys=True, default=str),
            "diagnosis": (diagnosis or "")[:2000],
            "pre_digest": digest(pre_code or ""),
            "post_digest": digest(post_code),
            "spec_digest": self._spec_digest(spec, list(cases)),
            "verifier": VERIFIER_ID,
            "verdict_execution_id": execution_id,
            "admission_decision_id": "",
            "created_at": now()})
        return verdict


    def get_repair_record(self, repair_id: str) -> Dict[str, Any]:
        row = self.store.latest("ao_repair_records", "repair_id", repair_id)
        if row is None:
            raise KeyError(f"unknown repair {repair_id!r}")
        return row


    def admit_repair(self, repair_id: str, engine_handle: Any) -> str:
        """Admit an independently verified repair. Returns decision_id.

        Requires a live engine handle AND a stored admitted independent
        verdict (artifact_kind="repair") for the EXACT post-repair bytes
        recorded at verify time: verification cannot be skipped, and a
        verdict for bytes X never authorizes different bytes Y (this is
        what makes rollback-to-unverified and wrong-bytes attacks fail).
        Trust is derived from the stored verdict row only.
        """
        _require_engine(engine_handle)
        rec = self.get_repair_record(repair_id)
        if rec.get("admission_decision_id"):
            raise LifecycleError(
                f"repair {repair_id}: already admitted "
                f"(decision {rec['admission_decision_id']})")
        # The single trust-derivation point: the stored verdict for the
        # exact post bytes must exist, be admitted, and come from the
        # authorized verification procedure. Any tampering with the
        # record's digests, the verdict chain, or the anchor refuses here.
        vrow = self.require_admitted_verdict(rec["post_digest"], "repair")
        if vrow.get("artifact_ref") != repair_id:
            raise VerificationFailed(
                f"repair {repair_id}: stored verdict binds a different "
                f"repair ref ({vrow.get('artifact_ref')!r}) -- refused")
        decision_id = self._record_decision(
            None, "ACCEPT", "repair_admission",
            {"repair_id": repair_id,
             "agent_id": rec["agent_id"],
             "pre_digest": rec["pre_digest"],
             "post_digest": rec["post_digest"],
             "verdict_execution_id": vrow.get("execution_id"),
             "spec_digest": vrow.get("spec_digest")})
        # Append the admission to the repair record (new chained row).
        new_row = dict(rec)
        new_row["admission_decision_id"] = decision_id
        fields = {k: new_row[k] for k in new_row
                  if k not in ("seq", "prev_digest", "row_digest")}
        self.store.insert("ao_repair_records", fields)
        return decision_id

    # -- decisions (engine-only) ----------------------------------------

    def _record_decision(self, wp_id: Optional[str], verdict: str,
                         kind: str, reasons: Any) -> str:
        decision_id = "dec_" + digest(
            (wp_id or "") + verdict + kind + now())[:16]
        self.store.insert("ao_manager_decisions", {
            "decision_id": decision_id, "wp_id": wp_id, "verdict": verdict,
            "producer": _ENGINE, "kind": kind,
            "reasons_json": json.dumps(reasons, sort_keys=True, default=str),
            "created_at": now()})
        return decision_id

    def accept(self, wp_id: str, engine_handle: Any) -> str:
        """Accept a reviewed work product. Returns the candidate_id.

        Requires a live engine handle AND a stored admitted independent
        verdict for this work product AND for the exact artifact bytes
        currently attached: review cannot be skipped, and a verdict for
        code v1 never authorizes modified code v2 (version binding).
        """
        _require_engine(engine_handle)
        wp = self.get_work_product(wp_id)
        if wp.state != "UNDER_REVIEW":
            raise LifecycleError(
                f"work product {wp_id} is {wp.state}, not UNDER_REVIEW: "
                f"accept refused")
        row = self._latest_verdict(wp_id)
        if row is None or row.get("admitted") != "1":
            raise VerificationFailed(
                f"work product {wp_id}: accept refused -- no admitted "
                f"independent verdict on record (review first)")
        current_code = wp.artifacts[0].get("code", "") if wp.artifacts else ""
        if row.get("code_digest") != digest(current_code):
            raise VerificationFailed(
                f"work product {wp_id}: accept refused -- artifact modified "
                f"after verification (verdict binds "
                f"{str(row.get('code_digest'))[:12]}, current "
                f"{digest(current_code)[:12]}): re-verify first")
        self._record_decision(wp_id, "ACCEPT", "review",
                              {"wp_id": wp_id,
                               "agent_id": wp.agent_id,
                               "code_digest": (wp.artifacts[0].get("digest")
                                               if wp.artifacts else ""),
                               "verdict_execution_id":
                                   row.get("execution_id")})
        self._set_wp_state(wp_id, "ACCEPTED", _ENGINE,
                           reason="independent review accepted")
        self.agents.transition(wp.agent_id, "ACCEPTED", actor=_ENGINE,
                               reason=f"work product {wp_id} accepted")
        candidate_id = self.experience.submit_candidate(
            wp_id=wp_id, agent_id=wp.agent_id,
            problem_class=str(wp.outputs.get("problem_class",
                                             "byte_codec")),
            technique_name=str(wp.outputs.get("technique", "unknown")),
            description=str(wp.outputs.get("notes", ""))[:500],
            code=wp.artifacts[0]["code"] if wp.artifacts else "",
            entrypoint=wp.entrypoint or "selftest",
            io_contract=dict(wp.outputs.get("io_contract",
                                            {"input": "bytes",
                                             "output": "bytes"})),
            tags=list(wp.outputs.get("tags", [])),
            params=dict(wp.outputs.get("params", {})),
            evidence_refs=dict(wp.evidence_refs))
        return candidate_id

    def reject(self, wp_id: str, engine_handle: Any,
               reasons: Any) -> None:
        _require_engine(engine_handle)
        wp = self.get_work_product(wp_id)
        if wp.state != "UNDER_REVIEW":
            raise LifecycleError(
                f"work product {wp_id} is {wp.state}, not UNDER_REVIEW: "
                f"reject refused")
        self._record_decision(wp_id, "REJECT", "review",
                              {"wp_id": wp_id, "reasons": reasons})
        self._set_wp_state(wp_id, "REJECTED", _ENGINE,
                           reason="independent review rejected")
        self.agents.transition(wp.agent_id, "REJECTED", actor=_ENGINE,
                               reason=f"work product {wp_id} rejected")

    def audit_decisions(self) -> Any:
        return self.store.audit("ao_manager_decisions")
