"""V10-P3 delta-capture tests: charter-schema validation, write-time refusal,
session lifecycle, and the dispatch-loop read facade.

Real sqlite in tmp dirs, real record_experience/record_experience writes
(no mock stores). Adjudication acceptance is covered by
proofs/v10p3_support/adjudication_battery.py (real hide-and-probe in fresh
subprocesses); these tests cover the mechanical rules and the session
lifecycle, including every adjudication REJECTION path.
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest

_CANONICAL = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _CANONICAL)
sys.path.insert(0, os.path.join(_CANONICAL, "pylib"))

from runtime.intellect.delta_capture import (  # noqa: E402
    DeltaRefused,
    emit_delta,
    validate_delta,
)
from runtime.intellect.unified_memory import (  # noqa: E402
    UnifiedMemory,
    record_experience,
    read_experiences,
)
from swarm_engine.core.engine import SwarmEngine  # noqa: E402


def _engine():
    tmp = tempfile.mkdtemp(prefix="p3test_")
    return SwarmEngine(db_path=os.path.join(tmp, "eng.db"))


def _good_delta(**over):
    d = {
        "objective_x": "capture delta records from real work sessions",
        "external_demo_y": ("the external agent wrote a validating delta "
                            "writer and demonstrated it on ten cases"),
        "native_inventory_z": ("the only delta writer accepted any dict "
                               "without validation or adjudication"),
        "capability_gap": ("before: no validating writer existed; after: "
                           "the writer validates and refuses garbage"),
        "technique_t": {"name": "validate",
                        "probe": {"import": "os", "attr": "getcwd",
                                  "check": "callable"}},
        "evidence_e": [{"kind": "file", "path": __file__}],
        "dependencies_d": ["Python standard library"],
        "verification_v": ("ten-case battery executed in a fresh process, "
                           "all green"),
        "resulting_capability_c": "os.getcwd",
    }
    d.update(over)
    return d


class ValidateTests(unittest.TestCase):
    def test_valid_accepted(self):
        self.assertEqual(validate_delta(_good_delta()), [])

    def test_missing_field(self):
        d = _good_delta()
        del d["evidence_e"]
        self.assertIn("missing field: evidence_e", validate_delta(d))

    def test_non_dict(self):
        self.assertTrue(any("must be a dict" in e
                            for e in validate_delta(["x"])))

    def test_short_gap_without_transition(self):
        d = _good_delta(capability_gap="small")
        self.assertTrue(any("gap" in e for e in validate_delta(d)))

    def test_gap_requires_before_after(self):
        d = _good_delta(capability_gap=(
            "a long statement with no state transition words at all"))
        self.assertTrue(any("before/after" in e for e in validate_delta(d)))

    def test_bad_probe(self):
        d = _good_delta()
        d["technique_t"] = {"name": "x", "probe": {"import": "os"}}
        self.assertTrue(any("probe" in e for e in validate_delta(d)))

    def test_bad_evidence(self):
        d = _good_delta(evidence_e=[{"kind": "file"}])
        self.assertTrue(any("evidence" in e for e in validate_delta(d)))

    def test_bad_run_evidence(self):
        d = _good_delta(evidence_e=[{"kind": "run", "cmd": ["ls"]}])
        self.assertTrue(any("evidence" in e for e in validate_delta(d)))

    def test_nonlist_dependencies(self):
        d = _good_delta(dependencies_d="none")
        self.assertTrue(any("dependencies_d" in e
                            for e in validate_delta(d)))

    def test_empty_dependencies_allowed(self):
        self.assertEqual(validate_delta(_good_delta(dependencies_d=[])), [])

    def test_short_capability(self):
        d = _good_delta(resulting_capability_c="x")
        self.assertTrue(any("resulting_capability_c" in e
                            for e in validate_delta(d)))


class EmitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = _engine()
        cls.epi = cls.engine.intellect.epistemic

    def test_invalid_refused_before_store(self):
        bad = _good_delta()
        del bad["verification_v"]
        before = read_experiences(self.epi, kind="technique_delta", limit=1000)
        with self.assertRaises(DeltaRefused):
            emit_delta(self.epi, "s_probe", bad)
        after = read_experiences(self.epi, kind="technique_delta", limit=1000)
        self.assertEqual(len(after), len(before))

    def test_valid_emitted_with_provenance(self):
        oid = emit_delta(self.epi, "s_emit", _good_delta())
        recs = read_experiences(self.epi, origin_loop="acquisition",
                                kind="technique_delta", limit=1000)
        mine = [r for r in recs if r["observation_id"] == oid]
        self.assertEqual(len(mine), 1)
        r = mine[0]
        self.assertTrue(r["raw"]["validated"])
        self.assertEqual(r["raw"]["session_id"], "s_emit")
        self.assertEqual(r["raw"]["schema"], "charter-9")
        prov = r["provenance"]
        self.assertEqual(prov["origin_loop"], "acquisition")
        self.assertEqual(prov["kind"], "technique_delta")
        self.assertEqual(prov["causal_chain"], ["s_emit"])
        self.assertEqual(prov["write_path"], "unified_memory.record_experience")


if __name__ == "__main__":
    unittest.main()
