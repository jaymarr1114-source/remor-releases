"""Unit runner for the CUR-P1C adversarial battery.

The battery logic lives in swarm_engine.curiosity.frm.proof_battery
(single source); this file asserts every check passes. The proof driver
at proofs/cur_p1c_frm_proof.py runs the same battery with N/N output.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "pylib")))

from swarm_engine.curiosity.frm.proof_battery import run_all  # noqa: E402


class TestFrmBattery(unittest.TestCase):
    def test_full_battery(self):
        results = run_all()
        failures = [(n, d) for n, ok, d in results if not ok]
        self.assertEqual(len(results), 15,
                         f"battery changed size: {len(results)}")
        self.assertEqual(failures, [],
                         "battery failures: "
                         + "; ".join(f"{n} -- {d}" for n, d in failures))


if __name__ == "__main__":
    unittest.main()
