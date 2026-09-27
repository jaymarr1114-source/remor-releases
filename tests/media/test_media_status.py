"""Honest media status() contract (Worker B, media honesty-gaps repair).

status() must probe the real substrate per medium and report
available=True ONLY when the substrate actually delivered an artifact --
never hardcoded. This suite asserts the contract shape and cross-checks
each claim against an INDEPENDENT genuine probe of the same substrate
path, so a hardcoded lie cannot pass:

- every entry carries available / classification / method / bounds / probe
- available True  -> classification "PROVEN BUT BOUNDED"
- available False -> classification "UNAVAILABLE" + non-empty "reason"
- entry["available"] == what an independent minimal generation through
  the real substrate path actually delivered (claim matches reality)

The suite is environment-agnostic: it never hardcodes which media are
available, it derives the expectation from the independent probe. In an
env without piper, song/voice honestly report unavailable; in an env with
piper + ffmpeg, all four report available.

Usage: PYTHONPATH=pylib python3 -m pytest tests/media/test_media_status.py -q
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services import media as media_front  # noqa: E402

SCRATCH = os.path.dirname(os.path.abspath(__file__))

MEDIA_KEYS = ("generate_image", "generate_video", "assemble_song",
              "synthesize_voice")


def _independent_probe(medium):
    """Independent minimal generation through the real substrate path.

    Returns (delivered: bool, detail: str). Deliberately separate from the
    front's own probes: same substrate, independently driven.
    """
    d = tempfile.mkdtemp(prefix="status_xcheck_", dir=SCRATCH)
    try:
        if medium == "generate_image":
            from swarm_engine.media.image import generate
            out = os.path.join(d, "x.png")
            res = generate("status cross-check", out, width=16, height=16,
                           seed=3)
        elif medium == "generate_video":
            from swarm_engine.media.video import generate
            out = os.path.join(d, "x.mp4")
            res = generate("status cross-check", out, duration_s=0.5, fps=8,
                           width=160, height=90, seed=3)
        elif medium == "assemble_song":
            from swarm_engine.media.song import assemble_song
            out = os.path.join(d, "x.wav")
            res = assemble_song("status cross-check",
                                {"style": "ballad", "bars": 2}, out,
                                os.path.join(d, "work"))
        elif medium == "synthesize_voice":
            from swarm_engine.media.voice import synthesize
            out = os.path.join(d, "x.wav")
            res = synthesize("status cross-check", out, voice="default")
        else:
            raise KeyError(medium)
        delivered = (isinstance(res, dict) and res.get("ok") is True
                     and os.path.isfile(out) and os.path.getsize(out) > 0)
        detail = ("delivered" if delivered
                  else f"not delivered: {res.get('error') if isinstance(res, dict) else res!r}")
        return delivered, detail
    except Exception as exc:
        return False, f"independent probe raised {type(exc).__name__}: {exc}"
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


class TestMediaStatusHonest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.status = media_front.status()
        cls.reality = {m: _independent_probe(m) for m in MEDIA_KEYS}

    def test_all_media_keys_present(self):
        for m in MEDIA_KEYS:
            self.assertIn(m, self.status, f"status() missing {m}")

    def test_entry_shape(self):
        for m in MEDIA_KEYS:
            entry = self.status[m]
            for key in ("available", "classification", "method", "bounds",
                        "probe"):
                self.assertIn(key, entry, f"{m} entry missing {key!r}")
            self.assertIsInstance(entry["available"], bool)
            self.assertTrue(entry["bounds"], f"{m} bounds empty")
            self.assertTrue(entry["probe"], f"{m} probe detail empty")

    def test_classification_matches_availability(self):
        for m in MEDIA_KEYS:
            entry = self.status[m]
            if entry["available"]:
                self.assertEqual(entry["classification"],
                                 "PROVEN BUT BOUNDED", m)
                self.assertNotIn("reason", entry,
                                 f"{m}: available entry must not carry a reason")
            else:
                self.assertEqual(entry["classification"], "UNAVAILABLE", m)
                self.assertTrue(entry.get("reason"),
                                f"{m}: unavailable entry must name a reason")

    def test_claim_matches_reality_per_medium(self):
        """The causal core: status()'s claim == what the substrate did."""
        for m in MEDIA_KEYS:
            delivered, detail = self.reality[m]
            self.assertEqual(self.status[m]["available"], delivered,
                             f"{m}: status claims "
                             f"{self.status[m]['available']} but independent "
                             f"probe: {detail}")

    def test_unavailable_reason_names_cause(self):
        for m in MEDIA_KEYS:
            entry = self.status[m]
            if not entry["available"]:
                self.assertTrue(len(entry["reason"]) > 10,
                                f"{m}: reason too vague: {entry['reason']!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
