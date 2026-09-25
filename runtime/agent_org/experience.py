"""Organizational experience: what REMOR learns, not what it remembers.

Levels:
- L1 (candidate): an accepted work product's technique, awaiting generality
  proof. submit_candidate() records it.
- L2 (verified): promote() ran the implementation on held-out generality
  cases via subprocess through an IndependentValidator; ALL must pass.
  This gate is what makes it Learn rather than Remember.
- L3 (synthesized): record_l3() inserts a synthesized technique with
  derived_from lineage ONLY when a stored, engine-executed independent
  verdict (artifact_kind="synthesis") exists for the exact code bytes.
  There is no caller-supplied validation_evidence parameter: trust is
  derived from the persisted verdict row, never from caller claims.
  The driver verifies Z through ReviewBoard.verify_artifact() first.

get_relevant() returns technique + contracts + code digest only -- never
agent-private state. Destroying an agent never deletes experience rows
(no cascading deletes anywhere in this schema).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.agent_org.exceptions import AuthorityError, VerificationFailed
from swarm_engine.agent_org.review import VERIFIER_ID
from swarm_engine.agent_org.store import OrgStore, digest, now

try:
    from swarm_engine.governance.oracle_binding import EngineOracleHandle
except Exception:  # pragma: no cover
    EngineOracleHandle = None  # type: ignore

_ENGINE = "remor:engine"


@dataclass
class ExperienceCandidate:
    candidate_id: str
    wp_id: str
    agent_id: str
    problem_class: str
    technique_name: str
    description: str
    code: str
    entrypoint: str
    io_contract: Dict[str, Any]
    tags: List[str]
    params: Dict[str, Any]
    evidence_refs: Dict[str, Any]
    created_at: str


@dataclass
class OrganizationalExperience:
    exp_id: str
    level: str  # "L2" | "L3"
    technique_name: str
    code: str
    entrypoint: str
    problem_class: str
    tags: List[str]
    io_contract: Dict[str, Any]
    params: Dict[str, Any]
    derived_from: List[str]  # exp_ids; empty for L2
    validation_evidence: Dict[str, Any]
    code_digest: str
    created_at: str
    # Provenance: who/what the trust rests on. discovered_by is the
    # agent_id for agent discoveries, "remor:engine" for syntheses.
    # verdict_execution_id is the persisted ao_review_verdicts row this
    # experience's trust was derived from.
    discovered_by: str = ""
    origin: str = ""  # "agent_discovery" | "remor_synthesis"
    verdict_execution_id: str = ""


def _candidate_from_row(row: Dict[str, Any]) -> ExperienceCandidate:
    return ExperienceCandidate(
        candidate_id=row["candidate_id"], wp_id=row["wp_id"],
        agent_id=row["agent_id"], problem_class=row["problem_class"],
        technique_name=row["technique_name"],
        description=row["description"], code=row["code"],
        entrypoint=row["entrypoint"],
        io_contract=json.loads(row["io_contract_json"]),
        tags=json.loads(row["tags_json"]),
        params=json.loads(row["params_json"]),
        evidence_refs=json.loads(row["evidence_refs_json"]),
        created_at=row["created_at"])


def _experience_from_row(row: Dict[str, Any]) -> OrganizationalExperience:
    return OrganizationalExperience(
        exp_id=row["exp_id"], level=row["level"],
        technique_name=row["technique_name"], code=row["code"],
        entrypoint=row["entrypoint"], problem_class=row["problem_class"],
        tags=json.loads(row["tags_json"]),
        io_contract=json.loads(row["io_contract_json"]),
        params=json.loads(row["params_json"]),
        derived_from=json.loads(row["derived_from_json"]),
        validation_evidence=json.loads(row["validation_evidence_json"]),
        code_digest=row["code_digest"], created_at=row["created_at"],
        discovered_by=row.get("discovered_by") or "",
        origin=row.get("origin") or "",
        verdict_execution_id=row.get("verdict_execution_id") or "")


class _GeneralitySpec:
    """Minimal spec for the IndependentValidator's adversary/critic.

    input_names and examples drive hostile/novelty/generalisation case
    generation; examples carry no expectations here (the held-out cases
    themselves carry the judgments).
    """

    def __init__(self, examples):
        self.input_names = ["data_hex"]
        self.examples = examples


class _DispatchTechniqueSpec:
    """Spec for the dispatch-technique generality gauntlet.

    input_names must name the technique entrypoint's parameter (the Critic
    checks the code references it). examples carry (args, expected) pairs;
    examples[1:] become the held-out generalisation cases.
    """

    def __init__(self, cases):
        self.input_names = ["user_text"]
        self.description = (
            "dispatch-technique generality: map unseen request texts to "
            "(dispatch_text, args); must refuse non-sort requests")
        self.examples = [(dict(c.args), c.expect) for c in cases]


class ExperienceStore:
    def __init__(self, store: OrgStore, review_board: Any = None):
        self.store = store
        self._board = review_board

    def attach_verifier(self, review_board: Any) -> None:
        """Bind the engine's ReviewBoard as the verification authority.

        Called once by RemorOrganization after wiring. promote() and
        record_l3() refuse (fail closed) until this is attached: knowledge
        derivation requires the engine's verification authority, never a
        caller-supplied validator.
        """
        self._board = review_board

    def _require_board(self) -> Any:
        if self._board is None:
            raise AuthorityError(
                "knowledge derivation requires the engine ReviewBoard "
                "(no verifier attached)")
        return self._board

    # -- L1 -------------------------------------------------------------
    def submit_candidate(self, wp_id: str, agent_id: str, problem_class: str,
                         technique_name: str, description: str, code: str,
                         entrypoint: str, io_contract: Dict[str, Any],
                         tags: List[str], params: Dict[str, Any],
                         evidence_refs: Dict[str, Any]) -> str:
        candidate_id = "exp_cand_" + digest(wp_id + technique_name)[:16]
        self.store.insert("ao_experience_candidates", {
            "candidate_id": candidate_id, "wp_id": wp_id,
            "agent_id": agent_id, "problem_class": problem_class,
            "technique_name": technique_name, "description": description,
            "code": code, "entrypoint": entrypoint or "selftest",
            "io_contract_json": json.dumps(io_contract, sort_keys=True),
            "tags_json": json.dumps(list(tags), sort_keys=True),
            "params_json": json.dumps(params, sort_keys=True),
            "evidence_refs_json": json.dumps(evidence_refs, sort_keys=True,
                                             default=str),
            "created_at": now()})
        return candidate_id

    def list_candidates(self) -> List[ExperienceCandidate]:
        rows = self.store.rows("ao_experience_candidates")
        latest: Dict[str, Dict] = {}
        for row in rows:
            latest[row["candidate_id"]] = row
        return [_candidate_from_row(r) for r in latest.values()]

    def get_candidate(self, candidate_id: str) -> ExperienceCandidate:
        row = self.store.latest("ao_experience_candidates", "candidate_id",
                                candidate_id)
        if row is None:
            raise KeyError(f"unknown candidate {candidate_id!r}")
        return _candidate_from_row(row)

    # -- L1 -> L2 gate --------------------------------------------------
    def promote(self, candidate_id: str,
                generality_cases: List[Any],
                derived_from: Optional[List[str]] = None
                ) -> Tuple[bool, Any]:
        """Promote a candidate to L2 organizational experience.

        The generality gauntlet is executed by the ENGINE's ReviewBoard
        (verify_generality): the verdict is persisted as an authoritative
        ao_review_verdicts row (artifact_kind="generality") bound to the
        exact candidate bytes, and L2 trust is derived from that stored row
        -- never from a caller-supplied validator. There is deliberately no
        validator parameter: a caller must not choose the oracle that
        judges its own candidate.

        derived_from optionally records lineage: the exp_ids this
        technique was adapted from (Track 1: B's T2 derived from A's T1).
        The ids are validated to reference existing experiences; lineage
        is a claim about derivation, not a trust input.

        ALL held-out cases must pass or the candidate stays a candidate.
        Returns (True, exp_id) or (False, reasons).
        """
        cand = self.get_candidate(candidate_id)
        if not generality_cases:
            return False, ["no generality cases supplied: cannot promote"]
        derived_from = list(derived_from or [])
        # Lineage must reference real experiences; unknown ids are refused
        # (fail closed) rather than recorded as unvalidated claims.
        for parent in derived_from:
            if self.store.latest("ao_experiences", "exp_id", parent) is None:
                raise KeyError(
                    f"promote: unknown lineage parent {parent!r}: refused")
        board = self._require_board()
        spec = _GeneralitySpec(
            [(dict(c.args), None) for c in generality_cases[:2]])
        verdict = board.verify_generality(
            cand.code, cand.entrypoint or "selftest", spec,
            list(generality_cases), candidate_id)
        if not verdict.admitted:
            return False, list(verdict.reasons)
        row = board.require_admitted_verdict(digest(cand.code), "generality")
        exp_id = "exp_" + digest(candidate_id + "L2")[:16]
        self.store.insert("ao_experiences", {
            "exp_id": exp_id, "level": "L2",
            "technique_name": cand.technique_name, "code": cand.code,
            "entrypoint": cand.entrypoint,
            "problem_class": cand.problem_class,
            "tags_json": json.dumps(cand.tags, sort_keys=True),
            "io_contract_json": json.dumps(cand.io_contract, sort_keys=True),
            "params_json": json.dumps(cand.params, sort_keys=True),
            "derived_from_json": json.dumps(derived_from, sort_keys=True),
            "validation_evidence_json": json.dumps(
                {"verdict": "admitted",
                 "execution_id": row["execution_id"],
                 "reasons": json.loads(row["reasons_json"]),
                 "bindings": json.loads(row["bindings"]),
                 "spec_digest": row["spec_digest"]},
                sort_keys=True, default=str),
            "code_digest": digest(cand.code),
            "discovered_by": cand.agent_id,
            "origin": "agent_discovery",
            "verdict_execution_id": row["execution_id"],
            "created_at": now()})
        return True, exp_id

    # -- L3 (engine-admitted synthesis) ---------------------------------
    def record_l3(self, technique_name: str, code: str, entrypoint: str,
                  problem_class: str, tags: List[str],
                  io_contract: Dict[str, Any], derived_from: List[str],
                  engine: Any,
                  params: Optional[Dict[str, Any]] = None
                  ) -> OrganizationalExperience:
        """Record a synthesized L3 experience.

        Trust is derived ONLY from the stored independent verdict for this
        exact artifact version: the caller must first verify the
        synthesized code through ReviewBoard.verify_artifact()
        (engine-executed, verdict persisted with artifact_kind="synthesis").
        There is deliberately no validation_evidence parameter -- a caller
        cannot manufacture high-trust state by constructing evidence
        claiming verification succeeded. `params` is operational metadata
        (e.g. synthesis measurements), never trust evidence.

        Requires a live EngineOracleHandle for remor:engine, a non-empty
        derived_from list whose ids all exist as experiences, and a latest
        stored verdict for digest(code)/"synthesis" that is admitted and
        produced by the authorized verification procedure. Refuses (raises)
        otherwise; the verdict chain is audited on every call.
        """
        if (EngineOracleHandle is None
                or not isinstance(engine, EngineOracleHandle)):
            raise AuthorityError(
                "record_l3 requires a live EngineOracleHandle")
        if engine.producer_id != _ENGINE:
            raise AuthorityError(
                f"record_l3 requires producer 'remor:engine', got "
                f"{engine.producer_id!r}")
        if not isinstance(derived_from, list) or not derived_from:
            raise ValueError("record_l3: derived_from must be a non-empty list")
        for dep_id in derived_from:
            try:
                self.get_experience(dep_id)
            except KeyError:
                raise VerificationFailed(
                    f"record_l3 refused: unknown derived_from experience "
                    f"{dep_id!r}")
        board = self._require_board()
        vrow = board.require_admitted_verdict(digest(code), "synthesis")
        exp_id = "exp_" + digest(code + technique_name + "L3")[:16]
        decision_id = "dec_" + digest(exp_id + "synthesis" + now())[:16]
        validation_evidence = {
            "verdict": "admitted",
            "execution_id": vrow["execution_id"],
            "reasons": json.loads(vrow["reasons_json"]),
            "bindings": json.loads(vrow["bindings"]),
            "spec_digest": vrow["spec_digest"],
            "verifier": vrow["verifier"],
        }
        self.store.insert("ao_manager_decisions", {
            "decision_id": decision_id, "wp_id": None, "verdict": "ACCEPT",
            "producer": _ENGINE, "kind": "synthesis",
            "reasons_json": json.dumps(
                {"technique": technique_name, "exp_id": exp_id,
                 "derived_from": list(derived_from),
                 "verdict_execution_id": vrow["execution_id"]},
                sort_keys=True, default=str),
            "created_at": now()})
        self.store.insert("ao_experiences", {
            "exp_id": exp_id, "level": "L3",
            "technique_name": technique_name, "code": code,
            "entrypoint": entrypoint or "selftest",
            "problem_class": problem_class,
            "tags_json": json.dumps(list(tags), sort_keys=True),
            "io_contract_json": json.dumps(io_contract, sort_keys=True),
            "params_json": json.dumps(dict(params or {}), sort_keys=True),
            "derived_from_json": json.dumps(list(derived_from)),
            "validation_evidence_json": json.dumps(
                validation_evidence, sort_keys=True, default=str),
            "code_digest": digest(code),
            "discovered_by": _ENGINE,
            "origin": "remor_synthesis",
            "verdict_execution_id": vrow["execution_id"],
            "created_at": now()})
        return self.get_experience(exp_id)

    # -- dispatch-knowledge admission (Track 3) ---------------------------
    def admit_dispatch_knowledge(
            self, evidence_id: str, technique_name: str, code: str,
            entrypoint: str, problem_class: str, tags: List[str],
            io_contract: Dict[str, Any], params: Dict[str, Any],
            generality_cases: List[Any], agent_id: str) -> Tuple[bool, Any]:
        """Admit dispatch-derived technique knowledge as L2 organizational
        experience.

        The technique (e.g. a request-text -> (dispatch_text, args) mapper
        distilled from a verified dispatch) becomes organizational knowledge
        ONLY when two independent facts are both established from STORED
        verdicts, never from caller claims:

        1. the dispatch really happened as recorded: the engine's
           ReviewBoard must hold a latest ADMITTED verdict with
           artifact_kind="dispatch_evidence" for digest(evidence_json) --
           derived via require_admitted_verdict (chain audit + external
           anchor + authorized verifier identity, latest-governs);
        2. the technique generalizes: the technique code passes the
           generality gauntlet (board.verify_generality, kind="generality")
           on the supplied held-out cases -- a memorized replay of the one
           demonstrated dispatch fails here.

        Writes an L2 ao_experiences row with origin="dispatch_discovery",
        discovered_by=agent_id, derived_from=[evidence_id]. Returns
        (True, exp_id) or (False, reasons). Raises on missing evidence or
        missing dispatch-evidence verdict (fail closed).
        """
        from swarm_engine.agent_org.dispatch_learning import (
            DISPATCH_EVIDENCE_KIND, DISPATCH_KNOWLEDGE_REF_PREFIX,
            get_evidence,
        )
        board = self._require_board()
        ev = get_evidence(self.store, evidence_id)  # KeyError if unknown
        exp_id = "exp_" + digest(
            evidence_id + technique_name + "dispatchL2")[:16]
        # Duplicate/replay guard (checked BEFORE the expensive generality
        # subprocess): the same evidence + technique name always yields the
        # same exp_id. Re-admitting it would create a second row that looks
        # like independent knowledge -- refuse instead.
        if self.store.latest("ao_experiences", "exp_id", exp_id) is not None:
            raise VerificationFailed(
                f"admission refused: experience {exp_id} already admitted "
                f"from evidence {evidence_id} (replay refused)")
        # currency gate: the evidence must describe the capability's CURRENT
        # plan. A verdict admitted at T1 must not authorize knowledge about
        # a plan swapped at T2 (attack E2).
        engine = getattr(board, "dispatch_engine", None)
        if engine is None:
            raise VerificationFailed(
                "dispatch admission requires an engine-bound review board")
        from swarm_engine.synthesis.capability_store import plan_fingerprint
        cap = engine.capabilities.get(ev.capability_id)
        if cap is None:
            raise VerificationFailed(
                f"capability {ev.capability_id} no longer exists")
        # (evidence stores the version as its canonical string; compare
        # against the live version's string form)
        if str(cap.version) != str(ev.capability_version):
            raise VerificationFailed(
                "stale dispatch evidence: capability version changed "
                f"{ev.capability_version} -> {cap.version}")
        if plan_fingerprint(cap.plan) != ev.plan_fingerprint:
            raise VerificationFailed(
                "stale dispatch evidence: live plan fingerprint no longer "
                "matches the verified evidence")
        ev_row = board.require_admitted_verdict(
            digest(ev.evidence_json), DISPATCH_EVIDENCE_KIND)
        if not generality_cases:
            return False, ["no generality cases supplied: cannot admit"]
        spec = _DispatchTechniqueSpec(generality_cases)
        gen_verdict = board.verify_generality(
            code, entrypoint or "selftest", spec, list(generality_cases),
            DISPATCH_KNOWLEDGE_REF_PREFIX + evidence_id)
        if not gen_verdict.admitted:
            return False, list(gen_verdict.reasons)
        gen_row = board.require_admitted_verdict(digest(code), "generality")
        validation_evidence = {
            "verdict": "admitted",
            "dispatch_evidence_execution_id": ev_row["execution_id"],
            "generality_execution_id": gen_row["execution_id"],
            "reasons": json.loads(gen_row["reasons_json"]),
            "bindings": json.loads(gen_row["bindings"]),
            "spec_digest": gen_row["spec_digest"],
            "verifier": gen_row["verifier"],
        }
        self.store.insert("ao_experiences", {
            "exp_id": exp_id, "level": "L2",
            "technique_name": technique_name, "code": code,
            "entrypoint": entrypoint or "selftest",
            "problem_class": problem_class,
            "tags_json": json.dumps(list(tags), sort_keys=True),
            "io_contract_json": json.dumps(io_contract, sort_keys=True),
            "params_json": json.dumps(dict(params or {}), sort_keys=True),
            "derived_from_json": json.dumps([evidence_id]),
            "validation_evidence_json": json.dumps(
                validation_evidence, sort_keys=True, default=str),
            "code_digest": digest(code),
            "discovered_by": agent_id,
            "origin": "dispatch_discovery",
            "verdict_execution_id": ev_row["execution_id"],
            "created_at": now()})
        return True, exp_id

    # -- reads ----------------------------------------------------------
    def get_experience(self, exp_id: str) -> OrganizationalExperience:
        row = self.store.latest("ao_experiences", "exp_id", exp_id)
        if row is None:
            raise KeyError(f"unknown experience {exp_id!r}")
        return _experience_from_row(row)

    def list_experiences(
            self, problem_class: Optional[str] = None) -> List[OrganizationalExperience]:
        rows = self.store.rows("ao_experiences")
        latest: Dict[str, Dict] = {}
        for row in rows:
            latest[row["exp_id"]] = row
        out = [_experience_from_row(r) for r in latest.values()]
        if problem_class is not None:
            out = [e for e in out if e.problem_class == problem_class]
        return out

    def get_relevant(self, problem_class: str) -> List[Dict[str, Any]]:
        """L2 records for a problem class: technique + contracts + digest.

        No code, no agent-private state. What the organization shares is
        the contracted capability, not any agent's internals. L3 rows are
        excluded: relevance here means verified primitives (L2); the
        driver addresses L3 explicitly via get_experience/list_experiences.
        """
        out = []
        for exp in self.list_experiences(problem_class):
            if exp.level != "L2":
                continue
            out.append({
                "exp_id": exp.exp_id,
                "level": exp.level,
                "technique_name": exp.technique_name,
                "problem_class": exp.problem_class,
                "io_contract": dict(exp.io_contract),
                "tags": list(exp.tags),
                "params": dict(exp.params),
                "code_digest": exp.code_digest,
                "derived_from": list(exp.derived_from),
            })
        return out
