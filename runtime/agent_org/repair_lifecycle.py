"""Repair lifecycle: versioning, conflict detection, rollback, quarantine.

Phase 4 (organizational learning, item 5). This module governs the
LIFECYCLE of independently verified repairs across versions of one
defect. It sits ABOVE ReviewBoard.verify_repair/admit_repair:

  verify_repair   -- independently verifies ONE repair instance
                     (review.py, unchanged; untouched by this module)
  admit_repair    -- admits ONE verified repair (review.py, or worker 1's
                     RepairService wrapping it -- duck-typed, never
                     imported here)
  admit_versioned -- THIS module: versioned admission with conflict
                     detection (which repair governs this defect?)

Design invariants (all fail-closed):

* A "defect root" identifies the lineage: digest over (family,
  pre_digest) of the defect signature. Repairs of the same defect
  (same family, same pre-repair bytes) are versions of ONE lineage.
* The ao_repair_lineage table is the lifecycle's sole authority on
  which repair version currently governs a defect. This module is the
  ONLY writer of its status rows (no other runtime code inserts into
  it; verified by grep in the battery notes).
* Every lifecycle event (versioned admission, supersession,
  revocation, rollback, quarantine, unquarantine) writes BOTH a
  lineage row AND a chained ao_manager_decisions row
  (kind="repair_lifecycle:..." mirroring ReviewBoard._record_decision's
  format), so every lifecycle transition is journaled and auditable.
* Rollback is LAWFUL: authorized caller + verdict-bound bytes
  (require_admitted_verdict on the EXACT bytes) + governed guard
  (ProjectModificationGuard.apply_repair) + chained decisions. Never a
  raw file swap. Fail-closed ordering: the prior version's verdict is
  re-confirmed BEFORE any disk write and BEFORE any revocation row is
  written.
* A revoked repair can never be re-admitted. A quarantined repair
  cannot be admitted, rolled back to, or superseded-from while
  quarantined -- and the quarantine FREEZES the defect's lineage: no
  versioned admission for the defect proceeds until the quarantine is
  lifted (unquarantine) or the repair is revoked. A "fresh" admission
  during the hold would route around it, so it refuses. All refusals
  are explicit errors, never silent downgrades.

What this module deliberately does NOT do:

* It never verifies a repair itself: trust always comes from the
  ReviewBoard's stored verdict rows (kind="repair"), exactly as before.
* It never weakens review.py's checks: review.py is read-only here.
* It never accepts caller-supplied bytes as "the repair": post bytes
  are copied from the independently verified ao_repair_records row
  (post_digest) and, for rollback, re-checked against the stored
  verdict before being re-applied.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from swarm_engine.agent_org.store import canonical, digest, now


class RepairLifecycleError(Exception):
    """A repair-lifecycle operation refused. The message is the explicit
    reason; it is never swallowed or downgraded."""


# Status values for ao_repair_lineage.status. The lifecycle is the only
# writer; readers take the latest row per (defect_root, repair_id).
STATUS_ADMITTED = "admitted"
STATUS_SUPERSEDED = "superseded"
STATUS_REVOKED = "revoked"
STATUS_QUARANTINED = "quarantined"
_ACTIVE_STATUSES = (STATUS_ADMITTED,)


def defect_root_of(defect_signature: Dict[str, Any]) -> str:
    """Stable identity of a defect lineage: digest over the defect
    family plus the pre-repair artifact digest. Two repairs with the
    same family and the same pre bytes are versions of one defect."""
    return digest(canonical({
        "family": defect_signature.get("family"),
        "pre_digest": defect_signature.get("pre_digest"),
    }))


def _target_path_of(defect_signature: Dict[str, Any]) -> str:
    """The repaired file path as recorded in the defect signature."""
    return str(defect_signature.get("target_path")
               or defect_signature.get("file")
               or defect_signature.get("path") or "")


class RepairLifecycle:
    """Governed repair versioning over one RemorOrganization.

    org: a RemorOrganization (duck-typed: needs .store, .oregistry,
    .review). The org is also how the ReviewBoard's anchor and verifier
    checks stay in play -- this module never reimplements them.
    """

    def __init__(self, org: Any):
        self.org = org
        self.store = org.store
        self.oregistry = org.oregistry
        self.review = org.review

    # -- authorization ------------------------------------------------
    @staticmethod
    def _require_repair_apply(registry: Any, agents: Any, caller: Any,
                              op: str, target: str = "") -> str:
        """The caller must be token-authenticated and hold
        'agent:repair_apply'. Returns the authenticated agent_id."""
        from swarm_engine.governance.caller_authorization import (
            AgentDirectory, require_authorized)
        from swarm_engine.governance.oracle_binding import (
            DECISION_REPAIR_APPLY)
        return require_authorized(registry, agents, caller,
                                  DECISION_REPAIR_APPLY, op, target=target)

    def _agents(self) -> Any:
        from swarm_engine.governance.caller_authorization import (
            AgentDirectory)
        return AgentDirectory(self.oregistry)

    # -- decisions / lineage journaling --------------------------------
    def _record_decision(self, kind: str, reasons: Dict[str, Any]) -> str:
        """Chained ao_manager_decisions row, mirroring
        ReviewBoard._record_decision's format (decision_id, wp_id=None,
        verdict, producer=remor:engine, kind, reasons_json). The decision
        content always includes the AUTHORIZED caller identity; the
        producer stays the engine (the journal's writer), exactly as in
        review.py's decision rows."""
        decision_id = "dec_" + digest("repair_lifecycle" + kind + now())[:16]
        self.store.insert("ao_manager_decisions", {
            "decision_id": decision_id, "wp_id": None,
            "verdict": "REPAIR_LIFECYCLE", "producer": "remor:engine",
            "kind": kind,
            "reasons_json": json.dumps(reasons, sort_keys=True, default=str),
            "created_at": now()})
        return decision_id

    def _append_lineage(self, *, repair_id: str, defect_root: str,
                        version: int, supersedes_repair_id: str,
                        status: str, post_code: str, target_path: str,
                        decision_id: str, recorded_by: str,
                        reason: str) -> str:
        lineage_id = "lin_" + digest(
            repair_id + defect_root + status + now())[:16]
        self.store.insert("ao_repair_lineage", {
            "lineage_id": lineage_id, "repair_id": repair_id,
            "defect_root": defect_root, "version": str(version),
            "supersedes_repair_id": supersedes_repair_id,
            "status": status, "post_code": post_code,
            "target_path": target_path, "decision_id": decision_id,
            "recorded_by": recorded_by, "reason": reason,
            "created_at": now()})
        return lineage_id

    # -- lineage reads ---------------------------------------------------
    def lineage_history(self, defect_root: str) -> List[Dict[str, Any]]:
        """All lineage rows for one defect, oldest first."""
        return self.store.rows("ao_repair_lineage", "defect_root",
                               defect_root)

    def repair_history(self, repair_id: str) -> List[Dict[str, Any]]:
        """All lineage rows for one repair id, oldest first."""
        return self.store.rows("ao_repair_lineage", "repair_id", repair_id)

    def latest_status(self, repair_id: str) -> Optional[str]:
        row = self.store.latest("ao_repair_lineage", "repair_id",
                                repair_id)
        return row["status"] if row else None

    def _latest_per_repair(
            self, defect_root: str) -> Dict[str, Dict[str, Any]]:
        """Latest lineage row per repair_id for one defect (history is
        append-only; the last row per repair is its current status)."""
        latest: Dict[str, Dict[str, Any]] = {}
        for row in self.lineage_history(defect_root):
            latest[row["repair_id"]] = row
        return latest

    def current_admitted(self, defect_root: str) -> Optional[Dict[str, Any]]:
        """The lineage row that currently governs this defect: the latest
        row, across repairs, whose repair's LATEST status is 'admitted'.
        A repair with an old admitted row but a newer superseded /
        revoked / quarantined row does NOT govern -- only its latest
        status counts. Quarantined, superseded, and revoked versions are
        excluded."""
        best: Optional[Dict[str, Any]] = None
        for row in self._latest_per_repair(defect_root).values():
            if row.get("status") != STATUS_ADMITTED:
                continue
            if best is None or row["seq"] > best["seq"]:
                best = row
        return best

    def _refuse_if_terminal(self, repair_id: str, op: str) -> None:
        """A revoked repair can never be re-admitted; a quarantined
        repair cannot be admitted, rolled back to, or superseded-from."""
        status = self.latest_status(repair_id)
        if status == STATUS_REVOKED:
            raise RepairLifecycleError(
                f"{op} refused: repair {repair_id} was revoked "
                "(revocation is permanent; re-admission refused)")
        if status == STATUS_QUARANTINED:
            raise RepairLifecycleError(
                f"{op} refused: repair {repair_id} is quarantined "
                "(quarantine blocks admission, rollback-to, and "
                "supersession until unquarantined)")

    def _resolve_post_code(self, repair_id: str, rec: Dict[str, Any],
                             repair_service: Any, caller: Any) -> str:
        """Resolve the EXACT verified post bytes for a repair.

        The chain tables store only the post_digest, never the bytes, so
        the bytes are resolved through the duck-typed repair service's
        get_repair() -- and then DIGEST-CHECKED against the chain-stored
        post_digest. Any bytes hashing to the verified digest ARE the
        verified bytes (collision resistance); a mismatch refuses. This
        keeps the bytes source untrusted-by-construction while the
        binding stays authoritative. The read is caller-authenticated
        (the caller of the lifecycle operation).
        """
        get_repair = getattr(repair_service, "get_repair", None)
        post_code = ""
        if callable(get_repair):
            try:
                enriched = get_repair(repair_id, caller=caller)
            except Exception:
                enriched = None
            if isinstance(enriched, dict):
                post_code = str(enriched.get("post_code") or "")
        if not post_code:
            raise RepairLifecycleError(
                f"admit_versioned refused: exact verified post bytes for "
                f"{repair_id} unavailable (repair_service.get_repair did "
                f"not supply digest-bound post_code)")
        if digest(post_code) != rec.get("post_digest"):
            raise RepairLifecycleError(
                f"admit_versioned refused: supplied post bytes for "
                f"{repair_id} do not reproduce the recorded post_digest "
                f"(wrong bytes -- refused)")
        return post_code

    def _defect_root_from_record(self, rec: Dict[str, Any]) -> str:
        try:
            sig = json.loads(rec["defect_signature_json"])
        except (TypeError, ValueError, KeyError) as exc:
            raise RepairLifecycleError(
                f"lifecycle refused: repair record has an unreadable "
                f"defect signature ({exc})")
        if not isinstance(sig, dict):
            raise RepairLifecycleError(
                "lifecycle refused: defect signature is not an object")
        return defect_root_of(sig), sig

    def _out_of_lifecycle_admissions(self, defect_root: str,
                                     exclude_repair_id: str
                                     ) -> List[str]:
        """Sibling-path parity: repairs admitted via review.admit_repair
        (or raw SQL) that carry an admission_decision_id but have NO
        lineage row at all bypassed the governed versioning path. They
        are treated as conflicting admissions, not silently absorbed."""
        conflicts = []
        for rec in self.store.rows("ao_repair_records"):
            if rec.get("repair_id") == exclude_repair_id:
                continue
            if not rec.get("admission_decision_id"):
                continue
            try:
                sig = json.loads(rec["defect_signature_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(sig, dict) and defect_root_of(sig) == defect_root:
                if not self.repair_history(rec["repair_id"]):
                    conflicts.append(rec["repair_id"])
        return conflicts

    # -- versioned admission ---------------------------------------------
    def admit_versioned(self, repair_id: str, caller: Any,
                        repair_service: Any,
                        supersedes: Optional[str] = None) -> str:
        """Admit a verified repair as a versioned lineage event.

        repair_service: duck-typed worker-1 RepairService (needs
        admit_repair(repair_id, caller)); NEVER imported here.

        1. Load the repair record; compute the defect root.
        2. Conflict detection: if another repair currently governs this
           defect and supersedes does not name it -> refuse, naming the
           competitor. If supersedes names a repair that is not the
           currently-admitted one -> refuse (stale lineage claim).
        3. Delegate the actual admission to the repair service (which
           derives trust from the stored independent verdict only).
        4. Journal: the superseded repair -> 'superseded' lineage row;
           the new repair -> 'admitted' lineage row with version = old
           version + 1 (or 1 for a first admission), carrying the
           verified post bytes and target path from the repair record.

        Returns the lineage decision_id.
        """
        rec = self.review.get_repair_record(repair_id)  # KeyError if unknown
        defect_root, sig = self._defect_root_from_record(rec)
        # The exact verified post bytes, digest-bound to the chain-stored
        # post_digest (resolved via the duck-typed service; refused when
        # unavailable or mismatched).
        post_code = self._resolve_post_code(repair_id, rec, repair_service,
                                            caller=caller)
        self._refuse_if_terminal(repair_id, "admit_versioned")

        # Quarantine freezes the lineage: while ANY repair of this defect
        # is quarantined, no versioned admission proceeds (a fresh
        # "first admission" during the hold would route around the
        # quarantine). The hold is lifted by unquarantine or revocation.
        frozen = [rid for rid, row in
                  self._latest_per_repair(defect_root).items()
                  if row.get("status") == STATUS_QUARANTINED
                  and rid != repair_id]
        if frozen:
            raise RepairLifecycleError(
                f"admit_versioned refused: defect {defect_root[:16]}... "
                f"lineage frozen while repair(s) {sorted(frozen)} "
                f"quarantined (unquarantine or revoke first)")

        # Sibling-path parity: any admission for this defect that bypassed
        # the governed versioning path is a conflict, whether or not a
        # lineage head already exists.
        conflicts = self._out_of_lifecycle_admissions(defect_root,
                                                      repair_id)
        if conflicts:
            raise RepairLifecycleError(
                f"admit_versioned refused: defect {defect_root[:16]}... "
                f"has out-of-lifecycle admissions "
                f"{sorted(conflicts)} (admitted outside versioned "
                f"control -- conflict refused)")

        current = self.current_admitted(defect_root)
        if current is not None:
            current_id = current["repair_id"]
            if supersedes != current_id:
                raise RepairLifecycleError(
                    f"admit_versioned refused: defect {defect_root[:16]}... "
                    f"is currently governed by admitted repair "
                    f"{current_id!r} (version {current['version']}); "
                    f"admission of {repair_id!r} requires "
                    f"supersedes={current_id!r} (conflict refused)")
            if current_id == repair_id:
                raise RepairLifecycleError(
                    f"admit_versioned refused: repair {repair_id!r} is "
                    f"already the admitted version for this defect")
        elif supersedes is not None:
            # No admitted lineage, but a supersedes claim: check it names
            # something real, else refuse (stale claim).
            hist = self.repair_history(supersedes)
            if not hist or self.latest_status(
                    supersedes) != STATUS_SUPERSEDED:
                raise RepairLifecycleError(
                    f"admit_versioned refused: supersedes={supersedes!r} "
                    f"is not a superseded repair of this defect "
                    f"(stale or fabricated lineage claim)")

        # The actual trust derivation happens inside the repair service
        # (review.admit_repair: stored verdict, exact-byte binding,
        # refusal on double admission). This delegates; it never
        # reimplements.
        service_decision = repair_service.admit_repair(repair_id, caller)
        _ = service_decision  # journaled below via the fresh record read

        rec2 = self.review.get_repair_record(repair_id)
        target_path = _target_path_of(sig)
        new_version = (int(current["version"]) + 1
                       if current is not None else 1)
        decision_id = self._record_decision(
            "repair_lifecycle:admit_versioned", {
                "repair_id": repair_id,
                "defect_root": defect_root,
                "supersedes": supersedes or "",
                "version": new_version,
                "post_digest": digest(post_code),
                "target_path": target_path,
                "service_decision_id":
                    rec2.get("admission_decision_id") or ""})
        if current is not None:
            self._append_lineage(
                repair_id=current["repair_id"], defect_root=defect_root,
                version=int(current["version"]),
                supersedes_repair_id=repair_id,
                status=STATUS_SUPERSEDED, post_code=current["post_code"],
                target_path=current["target_path"],
                decision_id=decision_id, recorded_by="repair_lifecycle",
                reason=f"superseded by {repair_id}")
        self._append_lineage(
            repair_id=repair_id, defect_root=defect_root,
            version=new_version,
            supersedes_repair_id=supersedes or "",
            status=STATUS_ADMITTED, post_code=post_code,
            target_path=target_path, decision_id=decision_id,
            recorded_by="repair_lifecycle",
            reason=("first admission" if current is None
                    else f"supersedes {supersedes}"))
        return decision_id

    # -- revocation --------------------------------------------------------
    def revoke_repair(self, repair_id: str, caller: Any,
                      reason: str = "") -> str:
        """Post-admission revocation: the repair stops governing the
        defect and can NEVER be re-admitted. The caller must hold
        'agent:repair_apply'. Journals a chained decision
        (kind='repair_lifecycle:revocation') and a 'revoked' lineage row."""
        authed = self._require_repair_apply(
            self.oregistry, self._agents(), caller, "revoke_repair",
            target=repair_id)
        current_rows = self.repair_history(repair_id)
        if not current_rows:
            raise RepairLifecycleError(
                f"revoke_repair refused: unknown repair {repair_id!r} "
                f"(no lineage history)")
        latest = current_rows[-1]
        if latest.get("status") == STATUS_REVOKED:
            raise RepairLifecycleError(
                f"revoke_repair refused: repair {repair_id!r} is already "
                f"revoked")
        defect_root = latest["defect_root"]
        decision_id = self._record_decision(
            "repair_lifecycle:revocation", {
                "repair_id": repair_id, "defect_root": defect_root,
                "revoked_by": authed, "reason": (reason or "")[:2000],
                "prior_status": latest.get("status")})
        self._append_lineage(
            repair_id=repair_id, defect_root=defect_root,
            version=int(latest.get("version") or 0),
            supersedes_repair_id=latest.get("supersedes_repair_id") or "",
            status=STATUS_REVOKED, post_code=latest.get("post_code") or "",
            target_path=latest.get("target_path") or "",
            decision_id=decision_id, recorded_by=authed,
            reason=(reason or "")[:2000] or "revoked")
        return decision_id

    # -- rollback ----------------------------------------------------------
    def rollback(self, repair_id: str, caller: Any, guard: Any) -> str:
        """Lawfully roll back an admitted repair to the latest prior
        version that is not revoked and not quarantined.

        guard: a caller-bound ProjectModificationGuard (bound to the
        workspace root) -- the ONLY file-write path used here.

        Fail-closed ordering:
          1. authorize the caller (agent:repair_apply) -- refused callers
             touch nothing;
          2. find the prior version (latest non-revoked, non-quarantined
             admitted lineage row before the bad one) -- none -> refuse;
          3. re-confirm review.require_admitted_verdict(digest(prior
             post bytes), "repair") still holds -- if not, refuse BEFORE
             any disk write and BEFORE any revocation row;
          4. re-apply the prior bytes through the governed guard
             (apply_repair re-authorizes the caller itself);
          5. journal: revoke the bad repair + restore the prior version
             to 'admitted' with version+1, reason 'rollback from <bad>'.
        """
        authed = self._require_repair_apply(
            self.oregistry, self._agents(), caller, "rollback",
            target=repair_id)
        history = self.repair_history(repair_id)
        if not history:
            raise RepairLifecycleError(
                f"rollback refused: unknown repair {repair_id!r}")
        bad_latest = history[-1]
        if bad_latest.get("status") != STATUS_ADMITTED:
            raise RepairLifecycleError(
                f"rollback refused: repair {repair_id!r} is not the "
                f"currently-admitted version (status "
                f"{bad_latest.get('status')!r})")
        defect_root = bad_latest["defect_root"]
        prior = None
        for row in reversed(self.lineage_history(defect_root)):
            if row.get("repair_id") == repair_id:
                continue
            if row.get("status") != STATUS_ADMITTED:
                continue
            # The candidate's LATEST lineage status must not be revoked
            # or quarantined: a revoked/quarantined repair is not a
            # lawful rollback target, even though an older admitted row
            # exists in its history. A candidate whose latest status is
            # 'superseded' (the normal case -- superseded by the very
            # repair being rolled back) IS lawful: rollback restores
            # exactly such a version.
            if self.latest_status(row["repair_id"]) in (
                    STATUS_REVOKED, STATUS_QUARANTINED):
                continue
            prior = row
            break
        if prior is None:
            raise RepairLifecycleError(
                f"rollback refused: no prior non-revoked, "
                f"non-quarantined admitted version for defect "
                f"{defect_root[:16]}... (first admission has nothing "
                f"to roll back to)")
        prior_code = str(prior.get("post_code") or "")
        prior_digest = digest(prior_code)
        if not prior_code:
            raise RepairLifecycleError(
                f"rollback refused: prior version {prior['repair_id']!r} "
                f"has no stored post bytes (cannot lawfully restore)")
        # Fail closed: the stored verdict must STILL bind these exact
        # bytes (chain audit + external anchor + authorized verifier
        # identity + latest-governs). Checked BEFORE any disk write.
        self.review.require_admitted_verdict(prior_digest, "repair")
        target_path = prior.get("target_path") or ""
        if not target_path:
            raise RepairLifecycleError(
                f"rollback refused: prior version {prior['repair_id']!r} "
                f"records no target path")
        res = guard.apply_repair(target_path, prior_code, caller=caller,
                                 verify=None, repair_id=repair_id)
        if not res.committed:
            raise RepairLifecycleError(
                f"rollback refused: governed re-apply failed "
                f"({res.reason}) -- disk unchanged")
        bad_version = int(bad_latest.get("version") or 0)
        rev_reason = f"rolled back to {prior['repair_id']} (rollback)"
        rev_decision = self._record_decision(
            "repair_lifecycle:revocation", {
                "repair_id": repair_id, "defect_root": defect_root,
                "revoked_by": authed, "reason": rev_reason,
                "prior_status": STATUS_ADMITTED})
        self._append_lineage(
            repair_id=repair_id, defect_root=defect_root,
            version=bad_version,
            supersedes_repair_id=prior["repair_id"],
            status=STATUS_REVOKED, post_code=bad_latest.get("post_code"),
            target_path=bad_latest.get("target_path"),
            decision_id=rev_decision, recorded_by=authed,
            reason=rev_reason)
        new_version = bad_version + 1
        restore_decision = self._record_decision(
            "repair_lifecycle:rollback", {
                "defect_root": defect_root, "bad_repair_id": repair_id,
                "restored_repair_id": prior["repair_id"],
                "restored_by": authed, "version": new_version,
                "post_digest": prior_digest, "target_path": target_path})
        self._append_lineage(
            repair_id=prior["repair_id"], defect_root=defect_root,
            version=new_version, supersedes_repair_id=repair_id,
            status=STATUS_ADMITTED, post_code=prior_code,
            target_path=target_path, decision_id=restore_decision,
            recorded_by=authed,
            reason=f"rollback from {repair_id}")
        return restore_decision

    # -- quarantine --------------------------------------------------------
    def quarantine_repair(self, repair_id: str, caller: Any,
                          reason: str = "") -> str:
        """Quarantine a repair: it stops governing the defect while the
        quarantine stands (excluded from current_admitted), and cannot
        be admitted, rolled back to, or superseded-from. The caller must
        hold 'agent:repair_apply'. Journals a chained decision and a
        'quarantined' lineage row."""
        authed = self._require_repair_apply(
            self.oregistry, self._agents(), caller, "quarantine_repair",
            target=repair_id)
        history = self.repair_history(repair_id)
        if not history:
            raise RepairLifecycleError(
                f"quarantine_repair refused: unknown repair {repair_id!r}")
        latest = history[-1]
        if latest.get("status") == STATUS_QUARANTINED:
            raise RepairLifecycleError(
                f"quarantine_repair refused: repair {repair_id!r} is "
                f"already quarantined")
        if latest.get("status") == STATUS_REVOKED:
            raise RepairLifecycleError(
                f"quarantine_repair refused: repair {repair_id!r} is "
                f"revoked (terminal)")
        decision_id = self._record_decision(
            "repair_lifecycle:quarantine", {
                "repair_id": repair_id,
                "defect_root": latest["defect_root"],
                "quarantined_by": authed,
                "reason": (reason or "")[:2000],
                "prior_status": latest.get("status")})
        self._append_lineage(
            repair_id=repair_id, defect_root=latest["defect_root"],
            version=int(latest.get("version") or 0),
            supersedes_repair_id=latest.get("supersedes_repair_id") or "",
            status=STATUS_QUARANTINED,
            post_code=latest.get("post_code") or "",
            target_path=latest.get("target_path") or "",
            decision_id=decision_id, recorded_by=authed,
            reason=(reason or "")[:2000] or "quarantined")
        return decision_id

    def unquarantine_repair(self, repair_id: str, caller: Any,
                            reason: str = "") -> str:
        """Lift a quarantine: the repair resumes governing the defect as
        the admitted version (new admitted lineage head, version+1).
        The caller must hold 'agent:repair_apply'. Journals a chained
        decision and the restored 'admitted' lineage row."""
        authed = self._require_repair_apply(
            self.oregistry, self._agents(), caller, "unquarantine_repair",
            target=repair_id)
        history = self.repair_history(repair_id)
        if not history:
            raise RepairLifecycleError(
                f"unquarantine_repair refused: unknown repair {repair_id!r}")
        latest = history[-1]
        if latest.get("status") != STATUS_QUARANTINED:
            raise RepairLifecycleError(
                f"unquarantine_repair refused: repair {repair_id!r} is "
                f"not quarantined (status {latest.get('status')!r})")
        decision_id = self._record_decision(
            "repair_lifecycle:unquarantine", {
                "repair_id": repair_id,
                "defect_root": latest["defect_root"],
                "unquarantined_by": authed,
                "reason": (reason or "")[:2000],
                "version": int(latest.get("version") or 0) + 1})
        self._append_lineage(
            repair_id=repair_id, defect_root=latest["defect_root"],
            version=int(latest.get("version") or 0) + 1,
            supersedes_repair_id="",
            status=STATUS_ADMITTED,
            post_code=latest.get("post_code") or "",
            target_path=latest.get("target_path") or "",
            decision_id=decision_id, recorded_by=authed,
            reason=(reason or "")[:2000] or "unquarantined")
        return decision_id

    def apply_admitted(self, repair_id: str, caller: Any,
                       guard: Any) -> Dict[str, Any]:
        """Apply a repair's currently-admitted bytes through a governed guard.

        guard: a caller-bound ProjectModificationGuard (bound to the
        workspace root) -- the ONLY file-write path used here.

        Fail-closed: the caller must hold 'agent:repair_apply'; the
        repair's latest lineage status must be 'admitted' (revoked,
        quarantined, or superseded versions refuse); the stored bytes are
        re-confirmed against the still-binding 'repair' verdict BEFORE
        any disk write. Returns the guard's change record.
        """
        from swarm_engine.agent_org.store import digest as _digest
        authed = self._require_repair_apply(
            self.oregistry, self._agents(), caller, "repair apply",
            target=repair_id)
        latest = self.store.latest("ao_repair_lineage", "repair_id",
                                   repair_id)
        if latest is None:
            raise RepairLifecycleError(
                f"apply_admitted refused: repair {repair_id!r} has no "
                "lineage (never admitted through the lifecycle)")
        if latest.get("status") != STATUS_ADMITTED:
            raise RepairLifecycleError(
                f"apply_admitted refused: repair {repair_id!r} status is "
                f"{latest.get('status')!r}, not 'admitted'")
        post_code = latest.get("post_code") or ""
        target_path = latest.get("target_path") or ""
        if not post_code or not target_path:
            raise RepairLifecycleError(
                f"apply_admitted refused: repair {repair_id!r} has no "
                "stored bytes/target to apply")
        # The verdict for these exact bytes must still bind -- a revoked
        # verdict (or a superseded one) refuses BEFORE any disk write.
        self.review.require_admitted_verdict(_digest(post_code), "repair")
        result = guard.apply_repair(target_path, post_code, caller=caller,
                                    verify=None, repair_id=repair_id)
        self._record_decision("repair_lifecycle:apply", {
            "repair_id": repair_id,
            "defect_root": latest.get("defect_root"),
            "applied_by": authed,
            "target_path": target_path,
            "post_digest": _digest(post_code)})
        return {"repair_id": repair_id, "target_path": target_path,
                "applied_by": authed,
                "change": (result.to_dict() if hasattr(result, "to_dict")
                           else str(result))}
