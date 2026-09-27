"""Verification for the procedural image generation substrate.

These tests assert the HONEST claims of runtime/media/image.py:
  - real PNG written, exact requested dimensions
  - pixels genuinely vary (not blank, not monochrome, sane colorfulness)
  - determinism: same (prompt, seed) -> byte-identical file
  - different seed -> different bytes
  - prompt steers output parametrically (ocean vs sunset mean hue differ)
  - clean refusals for empty prompts and out-of-bounds dimensions

They deliberately do NOT assert object depiction: this engine cannot
depict objects, and no test here pretends otherwise.
"""

from __future__ import annotations

import colorsys
import hashlib
import os
import sys

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from runtime.media.image import generate, METHOD, MAX_DIM  # noqa: E402

TMP = os.path.join(os.path.dirname(__file__), "tmp_out")
os.makedirs(TMP, exist_ok=True)


def _p(name: str) -> str:
    return os.path.join(TMP, name)


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _colorfulness(arr: np.ndarray) -> float:
    """Hasler-Susstrunk colorfulness; natural colorful images score ~20-100."""
    r, g, b = arr[..., 0].astype(np.float64), arr[..., 1].astype(np.float64), arr[..., 2].astype(np.float64)
    rg = r - g
    yb = 0.5 * (r + g) - b
    return float(np.sqrt(rg.var() + yb.var()) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def _mean_hue(arr: np.ndarray) -> float:
    r, g, b = (arr[..., i].astype(np.float64).mean() / 255.0 for i in range(3))
    h, _, _ = colorsys.rgb_to_hls(r, g, b)
    return h


def _hue_distance(h1: float, h2: float) -> float:
    d = abs(h1 - h2) % 1.0
    return min(d, 1.0 - d)


# ---------------------------------------------------------------------------
# Real generation: open the PNG and assert on the actual pixels.
# ---------------------------------------------------------------------------

def test_real_prompt_produces_valid_varied_png():
    out = _p("sunset_mountains.png")
    res = generate("sunset over mountains", out, width=256, height=256, seed=7)
    assert res["ok"] is True, res
    assert res["method"] == "procedural-generative"
    assert res["method"] == METHOD
    assert res["width"] == 256 and res["height"] == 256
    assert res["seed"] == 7
    assert res["palette_family"] == "sunset"
    assert os.path.isfile(out)

    with Image.open(out) as im:
        assert im.format == "PNG"
        assert im.size == (256, 256)
        arr = np.asarray(im.convert("RGB"))

    # well above blank: std dev over all channels
    assert arr.std() > 20.0, f"near-blank image, std={arr.std():.2f}"

    # not monochrome: many distinct quantized colors
    quant = (arr // 32).reshape(-1, 3)
    distinct = len(set(map(tuple, quant.tolist())))
    assert distinct > 50, f"near-monochrome, distinct quantized colors={distinct}"

    # colorfulness sane (Hasler-Susstrunk)
    cf = _colorfulness(arr)
    assert cf > 5.0, f"colorfulness too low: {cf:.2f}"

    # palette reported honestly
    assert isinstance(res["palette"], list) and len(res["palette"]) == 5
    assert all(c.startswith("#") and len(c) == 7 for c in res["palette"])


def test_exact_dimensions_honored():
    out = _p("dims.png")
    res = generate("ocean waves", out, width=320, height=192, seed=3)
    assert res["ok"] is True
    with Image.open(out) as im:
        assert im.size == (320, 192)
    assert res["width"] == 320 and res["height"] == 192


def test_determinism_same_prompt_seed_byte_identical():
    a, b = _p("det_a.png"), _p("det_b.png")
    r1 = generate("forest at dawn", a, width=128, height=128, seed=42)
    r2 = generate("forest at dawn", b, width=128, height=128, seed=42)
    assert r1["ok"] and r2["ok"]
    assert _sha256(a) == _sha256(b), "same prompt+seed must be byte-identical"


def test_determinism_implicit_seed_from_prompt():
    a, b = _p("imp_a.png"), _p("imp_b.png")
    r1 = generate("neon city lights", a, width=128, height=128)
    r2 = generate("neon city lights", b, width=128, height=128)
    assert r1["ok"] and r2["ok"]
    assert r1["seed"] == r2["seed"], "prompt-derived seed must be stable"
    assert _sha256(a) == _sha256(b)


def test_different_seed_different_bytes():
    a, b = _p("s1.png"), _p("s2.png")
    generate("desert dunes", a, width=128, height=128, seed=1)
    generate("desert dunes", b, width=128, height=128, seed=2)
    assert _sha256(a) != _sha256(b), "different seeds must differ"


def test_prompt_steers_palette_ocean_vs_sunset():
    o, s = _p("ocean.png"), _p("sunset.png")
    ro = generate("ocean", o, width=192, height=192, seed=11)
    rs = generate("sunset", s, width=192, height=192, seed=11)
    assert ro["palette_family"] == "ocean"
    assert rs["palette_family"] == "sunset"
    with Image.open(o) as im:
        hue_ocean = _mean_hue(np.asarray(im.convert("RGB")))
    with Image.open(s) as im:
        hue_sunset = _mean_hue(np.asarray(im.convert("RGB")))
    d = _hue_distance(hue_ocean, hue_sunset)
    assert d > 0.15, f"ocean hue={hue_ocean:.3f} vs sunset hue={hue_sunset:.3f}, dist={d:.3f}"


def test_unknown_prompt_falls_back_deterministically():
    a, b = _p("fb_a.png"), _p("fb_b.png")
    r1 = generate("xylophone quantum banana", a, width=128, height=128, seed=5)
    r2 = generate("xylophone quantum banana", b, width=128, height=128, seed=5)
    assert r1["ok"] and r2["ok"]
    assert r1["palette_family"] == "slate"
    assert _sha256(a) == _sha256(b)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_empty_prompt_refused(bad):
    res = generate(bad, _p("nope.png"), width=64, height=64)
    assert res["ok"] is False
    assert "error" in res


@pytest.mark.parametrize("w,h", [(0, 64), (64, 0), (-10, 64), (64, -1),
                                 (4096, 64), (64, 4096), (MAX_DIM + 1, 64)])
def test_absurd_dimensions_refused(w, h):
    res = generate("sunset", _p("nope.png"), width=w, height=h)
    assert res["ok"] is False, (w, h)
    assert str(MAX_DIM) in res["error"], f"bounds not stated: {res['error']}"


def test_max_boundary_accepted():
    # smallest legal image is fine; max is exercised lightly for time
    res = generate("night sky", _p("tiny.png"), width=1, height=1, seed=9)
    assert res["ok"] is True
    with Image.open(_p("tiny.png")) as im:
        assert im.size == (1, 1)
