"""Top-level wiring: RemorOrganization.boot(work_dir).

Creates the two sidecar databases (agent_org.db + oracle.db), the engine
oracle handle, seeds the three templates, and exposes every subsystem:

  .factory      AgentFactory
  .agents       AgentRegistry
  .templates    TemplateRegistry
  .assignments  AssignmentManager
  .runner       AgentRunner
  .review       ReviewBoard
  .experience   ExperienceStore
  .performance  PerformanceLog
  .synthesizer  OrgSynthesizer
  .relationships RelationshipFinder
  .engine       EngineOracleHandle (producer remor:engine)
  .oregistry    OracleRegistry
  .store        OrgStore
  .anchor       AnchorStore (external trust anchor over org+oracle heads)
  .work_dir     str

audit() audits both databases plus the manager-decision chain.
close() releases both databases.

TRUST ANCHOR: boot() builds the AnchorStore outside the database
directory and, when a journal already exists, verifies the live chain
heads against it -- a mismatch raises AnchorMismatch and boot fails
closed. With no journal and PRISTINE databases (nothing beyond
constructor-time bootstrap rows), boot succeeds (fresh deployment); the
operator must then run `anchor_admin.py init` before any verdict is
stored, because genesis is explicit-only. With no journal and a
NON-PRISTINE database, boot raises AnchorMismatch: absence is not
silently re-anchorable (journal deletion + rewrite + one genuine write
can no longer capture trust).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from typing import Any, Dict, Optional

from swarm_engine.agent_org.assignment import AssignmentManager
from swarm_engine.agent_org.exceptions import OrgError
from swarm_engine.agent_org.experience import ExperienceStore
from swarm_engine.agent_org.factory import AgentFactory
from swarm_engine.agent_org.performance import PerformanceLog
from swarm_engine.agent_org.registry import AgentRegistry
from swarm_engine.agent_org.review import ReviewBoard
from swarm_engine.agent_org.runner import AgentRunner
from swarm_engine.agent_org.store import OrgStore
from swarm_engine.agent_org.synthesis import OrgSynthesizer, RelationshipFinder
from swarm_engine.agent_org.templates import TemplateRegistry, seed_templates
from swarm_engine.governance.anchor import (
    AnchorMismatch,
    AnchorStore,
    collect_anchor_heads,
    default_anchor_paths,
)
from swarm_engine.governance.oracle_binding import OracleRegistry
from swarm_engine.governance.trust_migrations import migrate_engine_backfill


def _table_row_counts(db_path: str) -> Dict[str, int]:
    """Per-table row counts for a sqlite database (read-only)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()]
        return {table: conn.execute(
            f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in tables}
    finally:
        conn.close()


def _databases_pristine(store_db_path: str,
                        oregistry_db_path: str) -> bool:
    """True when both databases hold only constructor-time bootstrap rows.

    The org store never writes at construction, so pristine means every
    table empty. The oracle registry seeds its own bootstrap rows at
    construction (root authorizations with timestamps, plus one
    attestation per process under a fresh engine token), so "empty"
    cannot mean zero rows there: instead the live per-table counts are
    compared against a throwaway pristine reference registry. Content
    comparison is impossible (timestamps/token differ per process), but
    the per-table ROW COUNT at construction is deterministic.
    """
    for count in _table_row_counts(store_db_path).values():
        if count > 0:
            return False
    fd, ref_path = tempfile.mkstemp(prefix="anchor_pristine_", suffix=".db")
    os.close(fd)
    try:
        ref = OracleRegistry(ref_path)
        try:
            ref_counts = _table_row_counts(ref_path)
        finally:
            ref.close()
        live_counts = _table_row_counts(oregistry_db_path)
        for table in set(live_counts) | set(ref_counts):
            if live_counts.get(table, 0) != ref_counts.get(table, 0):
                return False
        return True
    finally:
        try:
            os.remove(ref_path)
        except OSError:
            pass


class RemorOrganization:
    def __init__(self, work_dir: str, store: OrgStore,
                 oregistry: OracleRegistry,
                 anchor_store: Optional[AnchorStore] = None):
        self.work_dir = work_dir
        self.store = store
        self.oregistry = oregistry
        # External trust anchor over org+oracle chain heads. Built from the
        # store's db path so the journal/key live OUTSIDE the database
        # directory. boot() passes the instance it verified; direct
        # construction falls back to the default paths.
        self.anchor = (anchor_store if anchor_store is not None
                       else AnchorStore(*default_anchor_paths(store.db_path)))
        self.engine = oregistry.engine_handle()
        self.templates = TemplateRegistry(store, self.engine)
        seed_templates(self.templates)
        self.agents = AgentRegistry(store, oregistry, self.engine,
                                    os.path.join(work_dir, "agents"))
        self.factory = AgentFactory(self.agents, self.templates)
        self.assignments = AssignmentManager(store, oregistry, self.engine,
                                             self.agents)
        self.experience = ExperienceStore(store)
        self.runner = AgentRunner(store, self.assignments, self.agents,
                                  self.engine)
        self.review = ReviewBoard(store, oregistry, self.engine,
                                  self.agents, self.experience,
                                  anchor_store=self.anchor)
        # Knowledge derivation (promote/record_l3) requires the engine's
        # verification authority: the ExperienceStore derives trust only
        # from persisted ReviewBoard verdict rows, never caller oracles.
        self.experience.attach_verifier(self.review)
        self.performance = PerformanceLog(store)
        self.synthesizer = OrgSynthesizer()
        self.relationships = RelationshipFinder()

    @classmethod
    def boot(cls, work_dir: str) -> "RemorOrganization":
        os.makedirs(work_dir, exist_ok=True)
        store = OrgStore(os.path.join(work_dir, "agent_org.db"))
        oregistry = OracleRegistry(os.path.join(work_dir, "oracle.db"))
        anchor_store = AnchorStore(*default_anchor_paths(store.db_path))
        # Fail closed: an existing journal commits us to its heads. A
        # rewritten database whose heads no longer match the anchored heads
        # refuses to boot -- the anchor never auto-trusts the newest DB.
        if anchor_store.journal_exists():
            ok, msg = anchor_store.verify(
                collect_anchor_heads(store, oregistry))
            if not ok:
                # Trust-schema migration: the code's root decision-class
                # set may have grown since the journal tip (the engine's
                # bootstrap backfill appends visible GRANT events for the
                # new root classes). migrate_engine_backfill() records an
                # AUDITED, SIGNED migration transition for EXACTLY that
                # delta and nothing else; any other difference keeps
                # refusing (fail-closed preserved).
                migrated = migrate_engine_backfill(
                    store, oregistry, anchor_store)
                if migrated:
                    ok, msg = anchor_store.verify(
                        collect_anchor_heads(store, oregistry))
                if not ok:
                    raise AnchorMismatch(f"boot refused: {msg}")
        else:
            # No journal: this is only a fresh deployment when the
            # databases are PRISTINE (nothing beyond constructor-time
            # bootstrap rows). A used database with no journal means the
            # journal was deleted -- or writes happened without one --
            # and boot REFUSES rather than trust-on-first-use: the
            # operator restores the journal or runs an audited recovery,
            # but nothing silently re-anchors (F3).
            if not _databases_pristine(store.db_path, oregistry.db_path):
                raise AnchorMismatch(
                    "boot refused: anchor journal missing but the "
                    "databases are not pristine (possible journal "
                    "deletion); restore the journal or run an audited "
                    "recovery -- never silently re-anchor")
        # No journal and pristine databases: fresh deployment. Genesis is
        # explicit-only -- run `anchor_admin.py init` before any verdict
        # is stored (ReviewBoard._store_verdict raises AnchorMissing
        # without a journal, and require_admitted_verdict refuses with
        # "anchor journal not found").
        return cls(work_dir, store, oregistry, anchor_store=anchor_store)

    def audit(self) -> Dict[str, Any]:
        return {
            "agent_org": self.store.audit_all(),
            "oracle": self.oregistry.audit_all(),
            "decisions": self.review.audit_decisions(),
        }

    def close(self) -> None:
        try:
            self.store.close()
        finally:
            self.oregistry.close()
