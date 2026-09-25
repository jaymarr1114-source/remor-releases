#!/usr/bin/env python3
"""Batch 10 regression: CapabilitySpec.from_requirement example normalization.

Pins the behavior proven live during finisher verification (I-12): the
three accepted example forms (tuple, explicit input/output dict, JSON
two-list) normalize identically, and malformed/mixed forms are rejected.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "..", "pylib"))

from swarm_engine.acquisition.pipeline import CapabilityRequirement
from swarm_engine.acquisition.strategies import CapabilitySpec


def _req():
    return CapabilityRequirement(name="r1", description="test")


class TestExampleNormalization(unittest.TestCase):
    def test_tuple_form(self):
        spec = CapabilitySpec.from_requirement(
            _req(), [({"x": 1}, 2)])
        self.assertEqual(spec.input_names, ["x"])

    def test_explicit_dict_form(self):
        spec = CapabilitySpec.from_requirement(
            _req(), [{"input": {"x": 1}, "output": 2}])
        self.assertEqual(spec.input_names, ["x"])

    def test_json_two_list_form(self):
        # What JSON-decoded HTTP callers submit.
        spec = CapabilitySpec.from_requirement(
            _req(), [[{"x": 1}, 2]])
        self.assertEqual(spec.input_names, ["x"])

    def test_all_forms_identical(self):
        a = CapabilitySpec.from_requirement(_req(), [({"x": 1}, 2)])
        b = CapabilitySpec.from_requirement(
            _req(), [{"input": {"x": 1}, "output": 2}])
        c = CapabilitySpec.from_requirement(_req(), [[{"x": 1}, 2]])
        self.assertEqual(a.input_names, b.input_names)
        self.assertEqual(b.input_names, c.input_names)

    def test_malformed_string_rejected(self):
        with self.assertRaises(ValueError):
            CapabilitySpec.from_requirement(_req(), ["garbage"])

    def test_mixed_valid_malformed_rejected(self):
        with self.assertRaises(ValueError):
            CapabilitySpec.from_requirement(
                _req(), [({"x": 1}, 2), "garbage"])

    def test_dict_wrong_keys_rejected(self):
        with self.assertRaises(ValueError):
            CapabilitySpec.from_requirement(
                _req(), [{"wrong": {"x": 1}, "keys": 2}])


if __name__ == "__main__":
    unittest.main()
