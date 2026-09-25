"""Batch 11 battery: cross-DB trust scope (2026-09-25).

Causally demonstrates anchor coverage for all three integrity heads::

    org:ao_repair_records        (org DB -- real OrgStore)
    scheduler:scheduler_runs     (service DB -- real RunScheduler)
    artifact:artifacts           (service DB -- real ArtifactStore)

One CrossDbAnchor journal (outside every protected DB's parent dir)
covers all three. Every negative test performs a REAL tamper/forgery
through a separate sqlite/file handle and asserts the detection
surfaces fail closed:

- internal chain audit() -> False for in-place tampering, and
- anchor verify_all() -> False for anything the internal chain cannot
  see (whole-DB rewrite with a recomputed chain, journal forgery).

Anti-simulation notes: no hardcoded digests (all heads recomputed
live); the whole-DB-rewrite test re-derives the chain with the real
module primitives (the attacker knows the algorithm -- only the
journal HMAC key is secret); the fresh-process test re-verifies from
a real subprocess with fresh objects.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.agent_org.store import OrgStore  # noqa: E402
from swarm_engine.governance.anchor import (  # noqa: E402
    AnchorVerifyError,
    ChainAuditError,
    CrossDbAnchor,
    LegacySchemaError,
)
from swarm_engine.services import scheduler as sched_mod  # noqa: E402
from swarm_engine.services import artifacts as art_mod  # noqa: E402
from swarm_engine.services.artifacts import ArtifactStore  # noqa: E402
from swarm_engine.services.http_adapter import (  # noqa: E402
    build_services,
    close_services,
    service_anchor_paths,
)
from swarm_engine.services.scheduler import (  # noqa: E402
    RunScheduler,
    migrate_legacy_scheduler_db,
)
from swarm_engine.services.artifacts import (  # noqa: E402
    migrate_legacy_artifact_db,
)

_ORG_FIELDS = {
    "repair_id": "r1",
    "agent_id": "agent-1",
    "defect_signature_json": "{}",
    "diagnosis": "d",
    "pre_digest": "a",
    "post_digest": "b",
    "spec_digest": "c",
    "verifier": "v",
    "verdict_execution_id": "e1",
    "admission_decision_id": "a1",
    "created_at": "t",
}


def _oob(db_path):
    """Separate out-of-band sqlite handle (the attacker's channel)."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class CrossDbTrustBattery(unittest.TestCase):
    """One CrossDbAnchor over three real stores."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="batch11_")
        td = self.td.name
        # Layout: journal/key live in anchor_store/, OUTSIDE the parent
        # dirs of every protected DB (org/, svc/).
        self.org_db = os.path.join(td, "org", "org.db")
        self.svc_dir = os.path.join(td, "svc")
        self.sched_db = os.path.join(self.svc_dir, "scheduler.db")
        self.art_db = os.path.join(self.svc_dir, "artifacts.db")
        self.anchor_dir = os.path.join(td, "anchor_store")
        os.makedirs(os.path.join(td, "org"))
        os.makedirs(self.svc_dir)
        os.makedirs(self.anchor_dir)
        self.journal = os.path.join(self.anchor_dir, "x.journal")
        self.key_path = os.path.join(self.anchor_dir, "x.key")

        self.org = OrgStore(self.org_db)
        self.org.insert("ao_repair_records", dict(_ORG_FIELDS))
        self.sched = RunScheduler(self.sched_db,
                                  os.path.join(td, "runtime.db"))
        self.arts = ArtifactStore(
            self.art_db, os.path.join(td, "sandbox"))

        self.anchor = CrossDbAnchor(
            self.journal, self.key_path,
            [("org", self.org_db),
             ("scheduler", self.sched_db),
             ("artifact", self.art_db)])
        self.anchor.bind([("org", self.org),
                          ("scheduler", self.sched),
                          ("artifact", self.arts)])
        self.sched.attach_anchor(self.anchor, authority="battery")
        self.arts.attach_anchor(self.anchor, authority="battery")
        self.anchor.initialize("battery-operator")

    def tearDown(self):
        try:
            self.sched.close(timeout=5)
        except Exception:
            pass
        self.td.cleanup()

    # -- helpers ------------------------------------------------------
    def _submit_queued(self):
        """A real submit() (chained+attested), kept queued for the test.

        The worker is raced with an immediate cancel; the cancel MUST
        win (it does -- the worker needs a full wakeup cycle). If the
        worker ever wins, the test fails loudly instead of going slow.
        """
        r = self.sched.submit("battery goal")
        self.assertTrue(r["ok"])
        c = self.sched.cancel_run(r["run_id"])
        self.assertTrue(c["ok"], f"cancel lost the worker race: {c}")
        return r["run_id"]

    # -- 1. inventory -------------------------------------------------
    def test_1_inventory_reports_3_of_3(self):
        heads = self.anchor.collect()
        # The three integrity heads must be covered. (The org provider
        # enumerates its full SCHEMAS inventory -- the anchor covers
        # every org table, not just ao_repair_records -- so this is a
        # superset assertion.)
        for scope in ("org:ao_repair_records",
                      "scheduler:scheduler_runs",
                      "artifact:artifacts"):
            self.assertIn(scope, heads)
        ok, msg = self.anchor.verify_all()
        self.assertTrue(ok, msg)
        audits = self.anchor.audit_providers()
        for scope in ("org:ao_repair_records",
                      "scheduler:scheduler_runs",
                      "artifact:artifacts"):
            self.assertIn(scope, audits)
            aok, amsg = audits[scope]
            self.assertTrue(aok, f"{scope}: {amsg}")

    # -- 2. scheduler out-of-band UPDATE ------------------------------
    def test_2_scheduler_out_of_band_update_detected(self):
        self._submit_queued()
        conn = _oob(self.sched_db)
        conn.execute(
            "UPDATE scheduler_runs SET status='completed' WHERE seq=1")
        conn.commit()
        conn.close()
        ok, msg = self.sched.audit()
        self.assertFalse(ok, f"scheduler audit must fail: {msg}")
        self.assertIn("row_digest mismatch", msg)
        vok, vmsg = self.anchor.verify_all()
        self.assertFalse(vok, f"anchor verify must fail: {vmsg}")

    # -- 3. artifact out-of-band UPDATE --------------------------------
    def test_3_artifact_out_of_band_update_detected(self):
        s = self.arts.save("victim", "python", "print('original')")
        conn = _oob(self.art_db)
        conn.execute("UPDATE artifacts SET code='print(\"EVIL\")' "
                     "WHERE id=?", (s["artifact_id"],))
        conn.commit()
        conn.close()
        ok, msg = self.arts.audit()
        self.assertFalse(ok, f"artifact audit must fail: {msg}")
        self.assertIn("row_digest mismatch", msg)
        vok, vmsg = self.anchor.verify_all()
        self.assertFalse(vok, f"anchor verify must fail: {vmsg}")

    # -- 4. artifact row DELETE ---------------------------------------
    def test_4_artifact_row_delete_detected(self):
        s = self.arts.save("victim", "python", "print(1)")
        self.arts.save("victim", "python", "print(2)")
        self.arts.save("victim", "python", "print(3)")
        # (a) Deleting a MIDDLE row breaks the chain -> audit fails.
        conn = _oob(self.art_db)
        conn.execute("DELETE FROM artifacts WHERE rev=2")
        conn.commit()
        conn.close()
        ok, msg = self.arts.audit()
        self.assertFalse(ok, f"artifact audit must fail: {msg}")
        self.assertIn("expected id", msg)
        vok, vmsg = self.anchor.verify_all()
        self.assertFalse(vok, f"anchor verify must fail: {vmsg}")
        # (b) Deleting the TIP row leaves a self-consistent chain --
        # audit honestly passes -- but the head changed, so the
        # EXTERNAL anchor must still fail. This is why the journal
        # exists: the internal chain alone cannot see tip deletion.
        conn = _oob(self.art_db)
        conn.execute("DELETE FROM artifacts WHERE rev=3")
        conn.commit()
        conn.close()
        ok, msg = self.arts.audit()
        self.assertTrue(ok, f"tip-deleted chain is self-consistent: {msg}")
        vok, vmsg = self.anchor.verify_all()
        self.assertFalse(vok,
                         f"anchor must catch the tip deletion: {vmsg}")

    # -- 5. whole-DB rewrite with recomputed chain --------------------
    def test_5_whole_db_rewrite_with_recomputed_chain_rejected(self):
        """The capable attacker: recompute a self-consistent chain over
        forged content. Internal audit passes (it must -- the chain is
        valid); the EXTERNAL anchor rejects (head differs from tip)."""
        self._submit_queued()
        conn = _oob(self.sched_db)
        rows = conn.execute(
            "SELECT seq, id, goal, status, conversation_id, queued_at,"
            " started_at, ended_at, outcome_json, error"
            " FROM scheduler_runs ORDER BY seq").fetchall()
        self.assertTrue(len(rows) >= 2)
        prev = sched_mod._GENESIS_DIGEST
        for (seq, run_id, goal, status, conv, qa, sa, ea, ojson,
             err) in rows:
            forged_status = "completed" if seq == 1 else status
            ordered = {"id": run_id, "goal": goal,
                       "status": forged_status, "conversation_id": conv,
                       "queued_at": qa, "started_at": sa,
                       "ended_at": ea, "outcome_json": ojson,
                       "error": err}
            digest = sched_mod._row_digest(ordered, prev)
            conn.execute(
                "UPDATE scheduler_runs SET status=?, prev_digest=?,"
                " row_digest=? WHERE seq=?",
                (forged_status, prev, digest, seq))
            prev = digest
        conn.commit()
        conn.close()
        # Internal chain is self-consistent: audit passes.
        ok, msg = self.sched.audit()
        self.assertTrue(ok, f"recomputed chain should audit clean: {msg}")
        # But the external anchor disagrees with the forged head.
        vok, vmsg = self.anchor.verify_all()
        self.assertFalse(vok,
                         f"anchor must reject the rewritten DB: {vmsg}")
        self.assertIn("anchor mismatch", vmsg)

    # -- 6. journal forgery / truncation ------------------------------
    def test_6_journal_forgery_and_truncation_rejected(self):
        self._submit_queued()
        self.assertTrue(self.anchor.verify_all()[0])
        with open(self.journal, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        self.assertTrue(len(lines) >= 2)
        # (a) forgery: rewrite the tip's heads without a valid HMAC.
        rec = json.loads(lines[-1])
        forged = dict(rec)
        forged["scope_heads"] = {
            s: "0" * 64 for s in rec["scope_heads"]}
        bad_lines = lines[:-1] + [json.dumps(forged) + "\n"]
        with open(self.journal, "w", encoding="utf-8") as fh:
            fh.writelines(bad_lines)
        vok, vmsg = self.anchor.verify_all()
        self.assertFalse(vok, f"forged journal must fail: {vmsg}")
        # (b) truncation: drop the tip record entirely.
        with open(self.journal, "w", encoding="utf-8") as fh:
            fh.writelines(lines[:-1])
        vok, vmsg = self.anchor.verify_all()
        self.assertFalse(vok, f"truncated journal must fail: {vmsg}")

    # -- 7. legitimate tombstone delete stays auditable ---------------
    def test_7_legitimate_tombstone_delete_stays_auditable(self):
        s = self.arts.save("victim", "python", "print(1)")
        aid = s["artifact_id"]
        self.assertTrue(self.anchor.verify_all()[0])
        d = self.arts.delete(aid)
        self.assertTrue(d["ok"])
        # Reads behave as deleted...
        self.assertFalse(self.arts.get(aid)["ok"])
        self.assertEqual(self.arts.list_artifacts(), [])
        # ...but the lineage is archived and auditable, and the new
        # heads verify against the journal.
        rev = self.arts.revisions(aid)
        self.assertTrue(rev["deleted"])
        self.assertEqual(len(rev["revisions"]), 2)  # rev1 + tombstone
        ok, msg = self.arts.audit()
        self.assertTrue(ok, msg)
        vok, vmsg = self.anchor.verify_all()
        self.assertTrue(vok, vmsg)

    # -- 8. fresh-process verification --------------------------------
    def test_8_fresh_process_boot_verifies(self):
        self._submit_queued()
        self.arts.save("w", "python", "print(1)")
        self.assertTrue(self.anchor.verify_all()[0])
        pylib = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "..", "pylib"))
        script = (
            "import sys; sys.path.insert(0, %r);"
            "from swarm_engine.agent_org.store import OrgStore;"
            "from swarm_engine.governance.anchor import CrossDbAnchor;"
            "from swarm_engine.services.scheduler import RunScheduler;"
            "from swarm_engine.services.artifacts import ArtifactStore;"
            "org = OrgStore(%r);"
            "sched = RunScheduler(%r, %r);"
            "arts = ArtifactStore(%r, %r);"
            "a = CrossDbAnchor(%r, %r,"
            " [(\"org\", %r), (\"scheduler\", %r), (\"artifact\", %r)]);"
            "a.bind([(\"org\", org), (\"scheduler\", sched),"
            " (\"artifact\", arts)]);"
            "ok, msg = a.verify_all();"
            "sched.close(timeout=5);"
            "print('FRESH_VERIFY', ok, msg);"
            "sys.exit(0 if ok else 1)"
        ) % (pylib, self.org_db, self.sched_db,
             os.path.join(self.td.name, "runtime.db"),
             self.art_db, os.path.join(self.td.name, "sandbox2"),
             self.journal, self.key_path,
             self.org_db, self.sched_db, self.art_db)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(
            proc.returncode, 0,
            f"fresh-process verify failed: {proc.stdout}\n{proc.stderr}")
        self.assertIn("FRESH_VERIFY True", proc.stdout)

    # -- 9. no laundering: write after tamper fails closed ------------
    def test_9_pre_write_verify_fails_closed_no_laundering(self):
        s = self.arts.save("victim", "python", "print('original')")
        before = self.anchor.record_count
        conn = _oob(self.art_db)
        conn.execute("UPDATE artifacts SET code='print(\"EVIL\")' "
                     "WHERE id=?", (s["artifact_id"],))
        conn.commit()
        conn.close()
        # A legitimate write now must FAIL CLOSED, not re-anchor the
        # tampered head.
        with self.assertRaises(AnchorVerifyError):
            self.arts.save("other", "python", "print(2)")
        self.assertEqual(self.anchor.record_count, before,
                         "journal tip must not advance over tampered data")
        vok, vmsg = self.anchor.verify_all()
        self.assertFalse(vok, vmsg)

    # -- 10. legacy scheduler DB: refused, then migrated --------------
    def test_10_legacy_scheduler_refused_then_migrated(self):
        legacy_db = os.path.join(self.td.name, "legacy_sched.db")
        conn = sqlite3.connect(legacy_db)
        conn.execute(
            "CREATE TABLE scheduler_runs ("
            " id TEXT PRIMARY KEY, goal TEXT NOT NULL,"
            " status TEXT NOT NULL, conversation_id TEXT,"
            " queued_at REAL NOT NULL, started_at REAL, ended_at REAL,"
            " outcome_json TEXT, error TEXT)")
        conn.execute(
            "INSERT INTO scheduler_runs (id, goal, status,"
            " conversation_id, queued_at, started_at, ended_at,"
            " outcome_json, error) VALUES ("
            "'run-1', 'old goal', 'completed', NULL, 111.0, 112.0,"
            " 113.0, '{\"a\":1}', NULL)")
        conn.commit()
        conn.close()
        with self.assertRaises(LegacySchemaError):
            RunScheduler(legacy_db, os.path.join(self.td.name,
                                                 "rt2.db"))
        mig = migrate_legacy_scheduler_db(legacy_db)
        self.assertTrue(mig["ok"] and mig["migrated"], mig)
        s2 = RunScheduler(legacy_db, os.path.join(self.td.name,
                                                  "rt2.db"))
        try:
            rec = s2.get_run("run-1")
            self.assertEqual(rec["goal"], "old goal")
            self.assertEqual(rec["status"], "completed")
            ok, msg = s2.audit()
            self.assertTrue(ok, msg)
        finally:
            s2.close(timeout=5)

    # -- 11. legacy artifact DB: refused, then migrated ----------------
    def test_11_legacy_artifact_refused_then_migrated(self):
        legacy_db = os.path.join(self.td.name, "legacy_art.db")
        conn = sqlite3.connect(legacy_db)
        conn.execute(
            "CREATE TABLE artifacts ("
            " id INTEGER PRIMARY KEY, artifact_key TEXT NOT NULL,"
            " name TEXT NOT NULL, language TEXT NOT NULL,"
            " rev INTEGER NOT NULL, code TEXT NOT NULL,"
            " metadata_json TEXT NOT NULL DEFAULT '{}',"
            " created_at REAL NOT NULL,"
            " UNIQUE(artifact_key, rev))")
        conn.execute(
            "INSERT INTO artifacts (id, artifact_key, name, language,"
            " rev, code, metadata_json, created_at) VALUES ("
            "7, 'k1', 'old', 'python', 1, 'print(9)', '{}', 222.0)")
        conn.commit()
        conn.close()
        with self.assertRaises(LegacySchemaError):
            ArtifactStore(legacy_db, os.path.join(self.td.name,
                                                  "sb2"))
        mig = migrate_legacy_artifact_db(legacy_db)
        self.assertTrue(mig["ok"] and mig["migrated"], mig)
        a2 = ArtifactStore(legacy_db, os.path.join(self.td.name,
                                                   "sb2"))
        g = a2.get(7)
        self.assertTrue(g["ok"])
        self.assertEqual(g["code"], "print(9)")
        ok, msg = a2.audit()
        self.assertTrue(ok, msg)
        # New saves keep AUTOINCREMENT above the migrated max id.
        s = a2.save("new", "python", "print(10)")
        self.assertGreater(s["artifact_id"], 7)

    # -- 12. boot refuses used DB with missing journal ------------------
    def test_12_boot_refuses_used_db_with_missing_journal(self):
        base = os.path.join(self.td.name, "prod")
        svc = build_services(base)
        try:
            svc["artifacts"].save("w", "python", "print(1)")
            self.assertTrue(svc["service_anchor"].verify_all()[0])
        finally:
            close_services(svc)
        # Attacker deletes the journal to cover a DB tamper.
        journal, _key = service_anchor_paths(base)
        os.remove(journal)
        with self.assertRaises(ChainAuditError):
            build_services(base)

    # -- 13. production topology via build_services ----------------------
    def test_13_production_topology_attests_per_write(self):
        base = os.path.join(self.td.name, "prod2")
        svc = build_services(base)
        try:
            anchor = svc["service_anchor"]
            self.assertEqual(sorted(anchor.collect()),
                             ["artifact:artifacts",
                              "scheduler:scheduler_runs"])
            n0 = anchor.record_count
            svc["artifacts"].save("w", "python", "print(1)")
            self.assertEqual(anchor.record_count, n0 + 1)
            r = svc["scheduler"].submit("g")
            self.assertTrue(r["ok"])
            c = svc["scheduler"].cancel_run(r["run_id"])
            self.assertTrue(c["ok"], f"cancel lost the worker race: {c}")
            self.assertEqual(anchor.record_count, n0 + 3)
            ok, msg = anchor.verify_all()
            self.assertTrue(ok, msg)
        finally:
            close_services(svc)


    # -- 14. sibling deployments under one parent do not share a journal --
    def test_14_sibling_deployments_have_independent_anchors(self):
        # Regression: with a fixed shared journal name, a second
        # build_services() whose base_dir shared a parent with the first
        # (e.g. two e2e_drive mkdtemp runs under /tmp) fail-closed at
        # boot against the first deployment's heads. Journal/key file
        # names are scoped per base_dir, so this must boot cleanly.
        base1 = os.path.join(self.td.name, "deploy_a")
        base2 = os.path.join(self.td.name, "deploy_b")
        svc1 = build_services(base1)
        try:
            r = svc1["artifacts"].save("w", "python", "print(1)")
            self.assertTrue(r["ok"], r)
            aid1 = r["artifact_id"]
            ok, msg = svc1["service_anchor"].verify_all()
            self.assertTrue(ok, msg)
            j1, _k1 = service_anchor_paths(base1)
            self.assertTrue(os.path.isfile(j1))
        finally:
            close_services(svc1)
        # Second deployment under the SAME parent: must boot fresh and
        # verify on its own journal, not fail-closed on deploy_a's heads.
        svc2 = build_services(base2)
        try:
            j2, _k2 = service_anchor_paths(base2)
            self.assertTrue(os.path.isfile(j2))
            self.assertNotEqual(j1, j2)
            ok, msg = svc2["service_anchor"].verify_all()
            self.assertTrue(ok, msg)
            r = svc2["artifacts"].save("w2", "python", "print(2)")
            self.assertTrue(r["ok"], r)
            ok, msg = svc2["service_anchor"].verify_all()
            self.assertTrue(ok, msg)
        finally:
            close_services(svc2)
        # Re-boot of the first deployment reuses its own journal and
        # still verifies (idempotent, heads intact).
        svc1b = build_services(base1)
        try:
            ok, msg = svc1b["service_anchor"].verify_all()
            self.assertTrue(ok, msg)
            self.assertTrue(svc1b["artifacts"].get(aid1)["ok"])
        finally:
            close_services(svc1b)


if __name__ == "__main__":
    unittest.main()
