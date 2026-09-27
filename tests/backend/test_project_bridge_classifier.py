#!/usr/bin/env python3
"""M+29.05: repair-path classifier matches the "test" marker on the path
BASENAME only (ProjectExecutor._synthesize_repair_candidate).

A scratch dir like ".../contestwork/..." (substring "test") must not divert
source files into test_paths. The requirement description usually embeds the
full path, so the description word-checks ignore the embedded path text.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "pylib"))

from swarm_engine.acquisition.project_bridge import (  # noqa: E402
    ProjectExecutor, ProjectRequirement)


def _executor():
    # _synthesize_repair_candidate only needs _extract_path_from_req;
    # bypass the heavy engine wiring.
    return ProjectExecutor.__new__(ProjectExecutor)


def _req(name, path, words):
    return ProjectRequirement(name=name, description=f"{words} {path}")


class TestRepairPathClassifier(unittest.TestCase):
    def _classify(self, reqs):
        """Run the classifier with synthesis stubbed; record (source, tests)."""
        calls = []

        def fake_synth(sp, test_paths):
            calls.append((sp, list(test_paths)))
            return None

        with mock.patch(
            "swarm_engine.acquisition.repair_synthesis."
            "synthesize_repair_for_paths",
            side_effect=fake_synth,
        ):
            result = _executor()._synthesize_repair_candidate(
                None, reqs, {}, "failed")
        self.assertIsNone(result)
        return calls

    def test_dir_substring_test_is_source(self):
        calls = self._classify([
            _req("src", "/tmp/contest/src/foo.py", "write corrected"),
            _req("checks", "/tmp/contest/tests/test_foo.py",
                 "execute behavioral"),
        ])
        by_src = {sp: tps for sp, tps in calls}
        self.assertIn("/tmp/contest/src/foo.py", by_src)
        self.assertIn("/tmp/contest/tests/test_foo.py",
                      by_src["/tmp/contest/src/foo.py"])

    def test_latest_run_is_source(self):
        calls = self._classify([
            _req("src", "/tmp/proj/src/latest_run.py", "write corrected"),
        ])
        self.assertEqual([sp for sp, _ in calls],
                         ["/tmp/proj/src/latest_run.py"])

    def test_basename_marker_still_test(self):
        calls = self._classify([
            _req("checks", "/tmp/proj/tests/test_bar.py",
                 "execute behavioral"),
        ])
        self.assertEqual(calls, [])

    def test_description_word_test_still_classifies(self):
        # "test" as a real description word (not part of the embedded path)
        # must still route the file to test_paths.
        calls = self._classify([
            _req("checks", "/tmp/proj/src/add.py", "test the add function"),
        ])
        self.assertEqual(calls, [])


    def test_description_word_latest_still_source(self):
        # "latest" as a description word must not divert a source file.
        calls = self._classify([
            _req("src", "/tmp/proj/src/add.py", "fix latest behavior of"),
        ])
        self.assertEqual([sp for sp, _ in calls], ["/tmp/proj/src/add.py"])


class TestRepairSynthesisEndToEnd(unittest.TestCase):
    def test_synthesis_under_test_substring_scratch_dir(self):
        work = tempfile.mkdtemp(prefix="wfix_e2e_")
        self.addCleanup(shutil.rmtree, work, True)
        root = os.path.join(work, "contestwork")
        src = os.path.join(root, "src", "add.py")
        tst = os.path.join(root, "tests", "test_add.py")
        os.makedirs(os.path.dirname(src))
        os.makedirs(os.path.dirname(tst))
        with open(src, "w") as f:
            f.write("def add(x, y):\n    return x - y\n")
        with open(tst, "w") as f:
            f.write("assert add(2, 3) == 5\nassert add(10, 4) == 14\n")
        order = [
            ProjectRequirement(name="write_src",
                               description=f"write corrected source at {src}"),
            ProjectRequirement(name="run_checks",
                               description=f"execute behavioral checks in {tst}"),
        ]
        cand = _executor()._synthesize_repair_candidate(
            None, order, {}, "failed")
        self.assertIsNotNone(cand)
        self.assertEqual(cand.func_name, "add")
        self.assertGreaterEqual(cand.examples_satisfied, 1)
        # The candidate must genuinely repair the defect.
        ns = {}
        exec(cand.content, ns)  # noqa: S102 - test executes synthesized code
        self.assertEqual(ns["add"](2, 3), 5)
        self.assertEqual(ns["add"](10, 4), 14)


if __name__ == "__main__":
    unittest.main()
