"""Causal tests for Contract 8: services/evidence.py.

Real sqlite on scratch tempfiles. The store starts empty (honest emptiness,
never seeded). Causal fresh-process rehydration is tested by writing from a
subprocess and reading back in this process. Adversarial cases: wrong kinds
refused, oversized text refused, traversal-shaped and SQL-shaped inputs
stored as inert data.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.evidence import (  # noqa: E402
    DOCUMENTS, KINDS, MAX_DOCUMENT_BYTES, MAX_ENTRY_TEXT, EvidenceStore,
    routes_for_evidence)
from swarm_engine.services.contract_types import (  # noqa: E402
    contract_unavailable)


def _store():
    td = tempfile.TemporaryDirectory()
    return td, EvidenceStore(os.path.join(td.name, "evidence.db"))


class TestEmptyHonesty(unittest.TestCase):
    def test_fresh_store_is_empty(self):
        td, store = _store()
        try:
            for kind in KINDS:
                r = store.list_entries(kind=kind)
                self.assertTrue(r["ok"])
                self.assertTrue(r["empty"])
                self.assertEqual(r["entries"], [])
            r = store.list_entries()
            self.assertTrue(r["empty"])
            self.assertEqual(r["by_kind"],
                             {"hypothesis": 0, "observation": 0,
                              "inference": 0})
            for name in DOCUMENTS:
                d = store.get_document(name)
                self.assertTrue(d["ok"])
                self.assertFalse(d["exists"])
        finally:
            td.cleanup()


class TestEntries(unittest.TestCase):
    def test_add_get_list_roundtrip(self):
        td, store = _store()
        try:
            r = store.add_entry("hypothesis",
                                "Plan composition may need two components.",
                                source="agent")
            self.assertTrue(r["ok"])
            entry_id = r["id"]
            g = store.get_entry(entry_id)
            self.assertTrue(g["ok"])
            self.assertEqual(g["entry"]["kind"], "hypothesis")
            self.assertEqual(g["entry"]["text"],
                             "Plan composition may need two components.")
            self.assertEqual(g["entry"]["source"], "agent")
            store.add_entry("observation", "Run failed with KeyError.")
            store.add_entry("inference", "Therefore validate inputs.")
            l = store.list_entries()
            self.assertEqual(len(l["entries"]), 3)
            self.assertFalse(l["empty"])
            self.assertEqual(l["by_kind"],
                             {"hypothesis": 1, "observation": 1,
                              "inference": 1})
            h = store.list_entries(kind="hypothesis")
            self.assertEqual(len(h["entries"]), 1)
            o = store.list_entries(kind="observation")
            self.assertEqual(len(o["entries"]), 1)
        finally:
            td.cleanup()

    def test_wrong_kind_refused(self):
        td, store = _store()
        try:
            for bad in ("theory", "Hypothesis", "", "note", None):
                r = store.add_entry(bad, "some text")
                self.assertFalse(r["ok"], f"kind {bad!r} must be refused")
            self.assertTrue(store.list_entries()["empty"])
        finally:
            td.cleanup()

    def test_empty_text_refused(self):
        td, store = _store()
        try:
            for bad in ("", "   "):
                r = store.add_entry("observation", bad)
                self.assertFalse(r["ok"])
            self.assertTrue(store.list_entries()["empty"])
        finally:
            td.cleanup()

    def test_oversized_text_refused(self):
        td, store = _store()
        try:
            big = "x" * (MAX_ENTRY_TEXT + 1)
            r = store.add_entry("observation", big)
            self.assertFalse(r["ok"])
            self.assertIn("exceeds", r["error"])
            ok_text = "x" * MAX_ENTRY_TEXT
            self.assertTrue(store.add_entry("observation", ok_text)["ok"])
        finally:
            td.cleanup()

    def test_sql_shaped_text_is_inert(self):
        td, store = _store()
        try:
            evil = "'); DROP TABLE evidence_entries; --"
            r = store.add_entry("inference", evil, source="';--")
            self.assertTrue(r["ok"])
            g = store.get_entry(r["id"])
            self.assertEqual(g["entry"]["text"], evil)
            # table still intact
            l = store.list_entries()
            self.assertEqual(len(l["entries"]), 1)
        finally:
            td.cleanup()

    def test_unknown_entry_not_found(self):
        td, store = _store()
        try:
            r = store.get_entry(424242)
            self.assertFalse(r["ok"])
            self.assertIn("not found", r["error"])
        finally:
            td.cleanup()


class TestFreshProcessRehydration(unittest.TestCase):
    """Causal: write in one process, kill it, read back in this one."""

    def test_write_kill_readback(self):
        td = tempfile.TemporaryDirectory()
        try:
            db = os.path.join(td.name, "evidence.db")
            writer = (
                "import sys, json\n"
                "sys.path.insert(0, %r)\n"
                "from swarm_engine.services.evidence import EvidenceStore\n"
                "s = EvidenceStore(%r)\n"
                "s.add_entry('hypothesis', 'survives process death', 't')\n"
                "s.save_document('soul.md', '# soul')\n" % (os.path.abspath(
                    # Synced 2026-09-26 (phase 3b): canonical layout is
                    # <root>/tests/contracts/ -> pylib is two levels up.
                    os.path.join(os.path.dirname(__file__), "..", "..",
                                 "pylib")), db)
            )
            proc = subprocess.run([sys.executable, "-c", writer],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            store = EvidenceStore(db)
            l = store.list_entries(kind="hypothesis")
            self.assertEqual(len(l["entries"]), 1)
            self.assertEqual(l["entries"][0]["text"], "survives process death")
            d = store.get_document("soul.md")
            self.assertTrue(d["exists"])
            self.assertEqual(d["markdown"], "# soul")
        finally:
            td.cleanup()


class TestDocuments(unittest.TestCase):
    def test_save_get_roundtrip(self):
        td, store = _store()
        try:
            self.assertTrue(
                store.save_document("theory.md", "# theory\ntext")["ok"])
            d = store.get_document("theory.md")
            self.assertTrue(d["exists"])
            self.assertEqual(d["markdown"], "# theory\ntext")
            self.assertIn("updated_at", d)
            # name with a space is allowed verbatim
            self.assertTrue(store.save_document(
                "hypothetical inferences.md", "# inferences")["ok"])
            d = store.get_document("hypothetical inferences.md")
            self.assertTrue(d["exists"])
        finally:
            td.cleanup()

    def test_unlisted_names_refused(self):
        td, store = _store()
        try:
            for bad in ("evil.md", "../soul.md", "SOUL.md", "soul",
                        "../../etc/passwd", "theory.md.bak"):
                r = store.save_document(bad, "x")
                self.assertFalse(r["ok"], f"name {bad!r} must be refused")
                g = store.get_document(bad)
                self.assertFalse(g["ok"])
        finally:
            td.cleanup()

    def test_oversized_document_refused(self):
        td, store = _store()
        try:
            big = "x" * (MAX_DOCUMENT_BYTES + 1)
            r = store.save_document("soul.md", big)
            self.assertFalse(r["ok"])
            self.assertIn("exceeds", r["error"])
        finally:
            td.cleanup()

    def test_engine_never_invents_documents(self):
        td, store = _store()
        try:
            # no save has happened: documents must report absent, not canned
            for name in DOCUMENTS:
                d = store.get_document(name)
                self.assertFalse(d["exists"])
                self.assertNotIn("markdown", d)
        finally:
            td.cleanup()


class TestContractTypes(unittest.TestCase):
    def test_unavailable_shape(self):
        r = contract_unavailable("evidence.engine_writer",
                                 "the engine does not write documents",
                                 "engine document writer")
        self.assertEqual(r, {
            "ok": False,
            "unavailable": {
                "code": "evidence.engine_writer",
                "reason": "the engine does not write documents",
                "gui": "coming_soon",
                "missing_substrate": "engine document writer",
            },
        })


class TestRoutes(unittest.TestCase):
    def test_route_table_handlers(self):
        td, store = _store()
        try:
            routes = routes_for_evidence(store)
            # 5 original + verify + substrate/get + substrate/list
            # (EVIDENCE-WIRE-1).
            self.assertEqual(len(routes), 8)
            a = routes[("POST", "/api/evidence/add")](
                {"kind": "observation", "text": "saw a crash", "source": "t"})
            self.assertTrue(a["ok"])
            g = routes[("POST", "/api/evidence/get")]({"id": a["id"]})
            self.assertEqual(g["entry"]["text"], "saw a crash")
            l = routes[("POST", "/api/evidence/list")]({"kind": "observation"})
            self.assertEqual(len(l["entries"]), 1)
            s = routes[("POST", "/api/evidence/documents/save")](
                {"name": "soul.md", "markdown": "# soul"})
            self.assertTrue(s["ok"])
            d = routes[("POST", "/api/evidence/documents/get")](
                {"name": "soul.md"})
            self.assertTrue(d["exists"])
            d2 = routes[("POST", "/api/evidence/documents/get")](
                {"name": "theory.md"})
            self.assertFalse(d2["exists"])
            json.dumps(d)
            bad = routes[("POST", "/api/evidence/add")](
                {"kind": "bogus", "text": "x"})
            self.assertFalse(bad["ok"])
        finally:
            td.cleanup()


if __name__ == "__main__":
    unittest.main()
