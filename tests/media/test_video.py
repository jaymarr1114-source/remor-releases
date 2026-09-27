"""Tests for the REMOR procedural video generation substrate.

End-to-end: generate a real 4 s / 24 fps / 640x360 video from a real prompt,
then DECODE the file independently (ffprobe + ffmpeg frame extraction) and
assert: valid MP4, exact frame count, exact dimensions, duration, H.264
yuv420p stream, non-static frames, and non-blank frames.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

REPO = os.path.expanduser("~/workspace/remor_media")
sys.path.insert(0, REPO)

from runtime.media.video import generate, METHOD  # noqa: E402


def ffprobe_json(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames",
         "-show_entries", "stream=index,codec_name,pix_fmt,width,height,nb_read_frames,duration",
         "-show_entries", "format=duration,size",
         "-of", "json", path],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def extract_frame(path, frame_index, dest_png):
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", path,
         "-vf", f"select=eq(n\\,{frame_index})", "-vframes", "1", dest_png],
        check=True,
    )
    return np.asarray(Image.open(dest_png), dtype=np.float32)


class TestProceduralVideo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="remor_video_test_")
        cls.out = os.path.join(cls.tmp, "ocean_dawn.mp4")
        import time
        t0 = time.monotonic()
        cls.result = generate(
            "a calm ocean at dawn, gentle waves", cls.out,
            duration_s=4.0, fps=24, width=640, height=360, seed=20260926,
        )
        cls.gen_wall_s = time.monotonic() - t0
        assert cls.result["ok"], f"generation failed: {cls.result.get('error')}"
        cls.probe = ffprobe_json(cls.out)
        print(f"\n[video] generate()+encode wall time: {cls.gen_wall_s:.1f}s "
              f"(module reports elapsed_s={cls.result.get('elapsed_s')})")

    def test_result_contract(self):
        r = self.result
        self.assertTrue(r["ok"])
        self.assertEqual(r["method"], "procedural-animation")
        self.assertEqual(r["n_frames"], 96)
        self.assertEqual((r["width"], r["height"]), (640, 360))
        self.assertEqual(r["fps"], 24)
        self.assertAlmostEqual(r["duration_s"], 4.0, places=6)
        self.assertEqual(r["seed"], 20260926)
        self.assertTrue(os.path.getsize(self.out) > 10_000)

    def test_decoded_stream_properties(self):
        streams = [s for s in self.probe["streams"] if s.get("codec_name") == "h264"]
        self.assertTrue(streams, "no H.264 stream found")
        v = streams[0]
        self.assertEqual(v["pix_fmt"], "yuv420p")
        self.assertEqual(v["width"], 640)
        self.assertEqual(v["height"], 360)

    def test_decoded_frame_count(self):
        v = self.probe["streams"][0]
        n = int(v["nb_read_frames"])
        self.assertTrue(abs(n - 96) <= 1, f"decoded {n} frames, expected 96±1")

    def test_decoded_duration(self):
        dur = float(self.probe["format"]["duration"])
        self.assertTrue(abs(dur - 4.0) <= 0.3, f"duration {dur:.3f}s, expected 4.0±0.3")

    def test_frames_not_static_and_not_blank(self):
        first = extract_frame(self.out, 0, os.path.join(self.tmp, "f0.png"))
        mid = extract_frame(self.out, 48, os.path.join(self.tmp, "f48.png"))
        last = extract_frame(self.out, 95, os.path.join(self.tmp, "f95.png"))
        for name, fr in (("first", first), ("middle", mid), ("last", last)):
            self.assertEqual(fr.shape, (360, 640, 3), f"{name} frame shape")
            var = float(np.var(fr))
            self.assertGreater(var, 20.0, f"{name} frame looks blank (var={var:.2f})")
        mad_fm = float(np.mean(np.abs(first - mid)))
        mad_ml = float(np.mean(np.abs(mid - last)))
        mad_fl = float(np.mean(np.abs(first - last)))
        print(f"[video] mean-abs frame diffs: first↔mid={mad_fm:.2f}, "
              f"mid↔last={mad_ml:.2f}, first↔last={mad_fl:.2f}")
        self.assertGreater(mad_fm, 1.0, "frames appear static (first vs middle)")
        self.assertGreater(mad_fl, 1.0, "frames appear static (first vs last)")

    def test_prompt_steers_output(self):
        out2 = os.path.join(self.tmp, "ember_dusk.mp4")
        r2 = generate("a burning ember sunset, intense heat", out2,
                      duration_s=1.0, fps=8, width=320, height=180, seed=7)
        self.assertTrue(r2["ok"], r2.get("error"))
        self.assertEqual(r2["n_frames"], 8)
        f_a = extract_frame(self.out, 0, os.path.join(self.tmp, "pa.png"))
        f_b = extract_frame(out2, 0, os.path.join(self.tmp, "pb.png"))
        b_small = np.asarray(Image.open(os.path.join(self.tmp, "pb.png"))
                            .resize((640, 360)), dtype=np.float32)
        mad = float(np.mean(np.abs(f_a - b_small)))
        self.assertGreater(mad, 3.0, "different prompts should steer palette/motion")

    def test_refusals(self):
        base = dict(out_path=os.path.join(self.tmp, "x.mp4"))
        bad = [
            dict(prompt="", duration_s=4.0, fps=24, width=640, height=360),
            dict(prompt="   ", duration_s=4.0, fps=24, width=640, height=360),
            dict(prompt="ok", duration_s=0, fps=24, width=640, height=360),
            dict(prompt="ok", duration_s=-1, fps=24, width=640, height=360),
            dict(prompt="ok", duration_s=31, fps=24, width=640, height=360),
            dict(prompt="ok", duration_s=4.0, fps=0, width=640, height=360),
            dict(prompt="ok", duration_s=4.0, fps=120, width=640, height=360),
            dict(prompt="ok", duration_s=4.0, fps=24, width=2000, height=360),
            dict(prompt="ok", duration_s=4.0, fps=24, width=640, height=1080),
        ]
        for kw in bad:
            r = generate(out_path=base["out_path"], **kw)
            self.assertFalse(r["ok"], f"expected refusal for {kw}")
            self.assertIn("error", r)
            self.assertEqual(r["method"], METHOD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
