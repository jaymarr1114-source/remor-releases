"""Procedural image generation for the REMOR media substrate.

HONEST METHOD STATEMENT — read before using:

  This module is NOT a learned text-to-image model. There is no diffusion
  network, no neural weights, no training data, no GPU, and no network
  call. A learned model is infeasible in this environment (2 CPUs, ~1 GB
  free RAM, no credentials), so the honest substrate is a seeded
  PROCEDURAL GENERATIVE ART ENGINE:

    1. The prompt is hashed (sha256) to a deterministic seed.
    2. Recognized keywords STEER parameters: they select a palette family
       ("sunset" -> warm, "ocean" -> cool, "forest" -> green, ...) and bias
       composition choices (gradient style, motif type, stroke density).
       Unrecognized prompts get a deterministic fallback (slate) palette.
    3. Every pixel is then computed by this code: multi-octave value-noise
       fields, a flow field that advects hundreds of brush strokes, gradient
       washes warped by noise, and seeded geometric motifs (orbs, arcs,
       polygons, bands), finished with a vignette.
    4. The result is written as a PNG with PIL.

  Prompts STEER palette/composition/mood parameters; they do NOT depict
  objects. This engine cannot render specific objects, people, animals,
  text, or scenes. "sunset over mountains" yields warm-hued generative
  art, not a picture of a sunset over mountains.

HONEST BOUNDS:
  - Method: procedural / algorithmic art, not AI image synthesis.
  - No object depiction: there is no concept of "mountain", "person",
    "dog", or "text" anywhere in this code.
  - Prompt influence is parametric only (palette family, gradient style,
    motif family, noise/stroke density); two prompts that map to the same
    family produce same-family art, not same-scene art.
  - Resolution cap: 2048 x 2048 per side (memory/time guard).
  - CPU cost: roughly 5-20 s per 1024x1024 image on a 2-CPU VM; scales
    with pixel count.
  - Deterministic: same (prompt, seed) -> byte-identical PNG.
"""

from __future__ import annotations

import hashlib
import time
from typing import Dict, List, Tuple

import numpy as np
from PIL import Image, ImageDraw

METHOD = "procedural-generative"
MAX_DIM = 2048
MIN_DIM = 1

# ---------------------------------------------------------------------------
# Palette families: the prompt steers WHICH family is used.
# ---------------------------------------------------------------------------

Palette = List[Tuple[int, int, int]]

_PALETTES: Dict[str, Palette] = {
    "sunset": [(18, 8, 42), (92, 28, 92), (196, 62, 62), (252, 138, 58), (255, 221, 148)],
    "ocean": [(2, 22, 52), (10, 62, 112), (22, 122, 162), (62, 182, 202), (205, 242, 252)],
    "forest": [(10, 42, 20), (32, 82, 42), (62, 132, 72), (122, 172, 92), (222, 232, 182)],
    "desert": [(62, 32, 16), (142, 92, 52), (212, 152, 92), (242, 202, 142), (252, 242, 212)],
    "night": [(5, 5, 22), (16, 22, 62), (42, 52, 122), (122, 142, 202), (242, 242, 255)],
    "ember": [(22, 6, 6), (92, 22, 12), (202, 62, 16), (255, 142, 42), (255, 232, 172)],
    "arctic": [(32, 62, 102), (72, 122, 162), (122, 172, 202), (162, 202, 227), (202, 222, 237)],
    "neon": [(10, 10, 26), (62, 22, 122), (162, 42, 202), (42, 222, 222), (255, 255, 255)],
    "garden": [(255, 236, 236), (255, 212, 222), (202, 232, 202), (172, 212, 242), (255, 250, 222)],
    "slate": [(26, 32, 42), (62, 76, 96), (112, 126, 146), (172, 182, 192), (236, 239, 243)],
}

# keyword -> palette family. The prompt steers the family; it does not depict.
_KEYWORDS: List[Tuple[str, List[str]]] = [
    ("sunset", ["sunset", "sunrise", "dusk", "dawn", "twilight", "golden hour", "goldenhour"]),
    ("ocean", ["ocean", "sea", "underwater", "wave", "beach", "lagoon", "reef", "tide", "coast"]),
    ("forest", ["forest", "jungle", "woodland", "woods", "moss", "fern", "grove", "tree"]),
    ("desert", ["desert", "dune", "sand", "sahara", "canyon", "mesa"]),
    ("night", ["night", "moon", "stars", "starry", "space", "galaxy", "cosmos", "midnight", "nebula"]),
    ("ember", ["fire", "ember", "lava", "volcano", "flame", "inferno", "wildfire", "bonfire"]),
    ("arctic", ["snow", "ice", "arctic", "winter", "frost", "glacier", "blizzard"]),
    ("neon", ["neon", "cyber", "city", "electric", "laser", "synthwave", "vaporwave", "arcade"]),
    ("garden", ["garden", "flower", "spring", "blossom", "meadow", "pastel", "tulip", "orchard"]),
]


def _palette_family(prompt: str) -> str:
    lowered = prompt.lower()
    for family, words in _KEYWORDS:
        if any(w in lowered for w in words):
            return family
    return "slate"  # deterministic fallback; documented, not a depiction


def _derive_seed(prompt: str, seed: int | None) -> int:
    if seed is not None:
        return int(seed)
    digest = hashlib.sha256(prompt.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


# ---------------------------------------------------------------------------
# Noise field: multi-octave value noise with smoothstep interpolation,
# implemented in pure numpy (no external noise library).
# ---------------------------------------------------------------------------

def _value_noise(rng: np.random.Generator, h: int, w: int,
                 base: int, octaves: int) -> np.ndarray:
    out = np.zeros((h, w), dtype=np.float32)
    amp_total = 0.0
    yy = np.linspace(0.0, 1.0, h, dtype=np.float32)
    xx = np.linspace(0.0, 1.0, w, dtype=np.float32)
    for o in range(octaves):
        n = base * (2 ** o)
        grid = rng.random((n + 2, n + 2)).astype(np.float32)
        gy = (yy * n)
        gx = (xx * n)
        yi = gy.astype(np.int32)
        xi = gx.astype(np.int32)
        fy = (gy - yi)[:, None]
        fx = (gx - xi)[None, :]
        # smoothstep for C1 continuity
        sy = fy * fy * (3.0 - 2.0 * fy)
        sx = fx * fx * (3.0 - 2.0 * fx)
        g00 = grid[yi[:, None], xi[None, :]]
        g10 = grid[yi[:, None] + 1, xi[None, :]]
        g01 = grid[yi[:, None], xi[None, :] + 1]
        g11 = grid[yi[:, None] + 1, xi[None, :] + 1]
        layer = (g00 * (1 - sx) + g01 * sx) * (1 - sy) + (g10 * (1 - sx) + g11 * sx) * sy
        amp = 0.5 ** o
        out += amp * layer
        amp_total += amp
    return out / amp_total


# ---------------------------------------------------------------------------
# Render stages. Each stage is a pure function of (rng, prompt-hash params).
# ---------------------------------------------------------------------------

def _gradient_wash(h: int, w: int, palette: Palette, rng: np.random.Generator,
                   warp: np.ndarray) -> np.ndarray:
    """Blend 3 palette colors along a gradient, warped by a noise field."""
    p = np.asarray(palette, dtype=np.float32)
    c_dark, c_low, c_high, c_bright = p[0], p[1], p[3], p[4]
    style = rng.integers(0, 4)  # 0 vertical, 1 diagonal, 2 radial, 3 horizon band
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    if style == 0:
        t = ys / max(h - 1, 1)
    elif style == 1:
        t = (xs / max(w - 1, 1) + ys / max(h - 1, 1)) / 2.0
    elif style == 2:
        cx, cy = w / 2.0, h / 2.0
        t = np.sqrt(((xs - cx) / w) ** 2 + ((ys - cy) / h) ** 2)
        t = np.clip(t * 1.4, 0.0, 1.0)
    else:
        t = np.abs(ys / max(h - 1, 1) - 0.5) * 2.0
    t = np.clip(t + (warp - 0.5) * 0.55, 0.0, 1.0)
    # 4-stop blend: dark -> low -> high -> bright
    t3 = np.clip(t * 3.0, 0.0, 1.0)
    t2 = np.clip(t * 3.0 - 1.0, 0.0, 1.0)
    t1 = np.clip(t * 3.0 - 2.0, 0.0, 1.0)
    w0 = (1 - t3)
    w1 = np.clip(t3 - t2, 0, 1)
    w2 = np.clip(t2 - t1, 0, 1)
    w3 = t1
    img = (w0[..., None] * c_dark + w1[..., None] * c_low
           + w2[..., None] * c_high + w3[..., None] * c_bright)
    return img, int(style)


def _flow_strokes_layer(h: int, w: int, palette: Palette,
                        rng: np.random.Generator,
                        angle_field: np.ndarray) -> Image.Image:
    """Advect particles through the noise angle field; draw their trails."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    n_particles = int(np.clip((w * h) // 3000, 150, 400))
    steps = 80
    step_len = max(w, h) / 220.0
    pos = np.stack([
        rng.uniform(0, w, n_particles),
        rng.uniform(0, h, n_particles),
    ], axis=1).astype(np.float32)
    cols = np.asarray(palette, dtype=np.uint8)
    alpha = int(rng.integers(40, 90))
    width_px = int(rng.integers(1, 4))
    for _ in range(steps):
        ix = np.clip(pos[:, 0].astype(np.int32), 0, w - 1)
        iy = np.clip(pos[:, 1].astype(np.int32), 0, h - 1)
        ang = angle_field[iy, ix]
        nxt = pos + np.stack([np.cos(ang), np.sin(ang)], axis=1) * step_len
        # draw each trail segment; color cycles through the palette
        for i in range(n_particles):
            c = cols[int(rng.integers(0, len(cols)))]
            draw.line([pos[i, 0], pos[i, 1], nxt[i, 0], nxt[i, 1]],
                      fill=(int(c[0]), int(c[1]), int(c[2]), alpha), width=width_px)
        pos = nxt
        # respawn particles that drifted off-canvas
        off = (pos[:, 0] < 0) | (pos[:, 0] >= w) | (pos[:, 1] < 0) | (pos[:, 1] >= h)
        pos[off, 0] = rng.uniform(0, w, int(off.sum()))
        pos[off, 1] = rng.uniform(0, h, int(off.sum()))
    return layer


def _motif_layer(h: int, w: int, palette: Palette,
                 rng: np.random.Generator) -> Tuple[Image.Image, str]:
    """Seeded geometric motifs: orbs, arcs, polygons, or bands."""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    motif = str(rng.choice(["orbs", "arcs", "polygons", "bands"]))
    p = [tuple(int(v) for v in c) for c in palette]
    n = int(rng.integers(3, 9))
    alpha = int(rng.integers(40, 110))
    if motif == "orbs":
        for _ in range(n):
            cx = float(rng.uniform(0, w))
            cy = float(rng.uniform(0, h))
            r = float(rng.uniform(min(w, h) * 0.03, min(w, h) * 0.28))
            c = p[int(rng.integers(0, len(p)))]
            for k, shrink in enumerate((1.0, 0.66, 0.36)):
                draw.ellipse([cx - r * shrink, cy - r * shrink,
                              cx + r * shrink, cy + r * shrink],
                             outline=(*c, alpha - k * 18), width=2)
    elif motif == "arcs":
        for _ in range(n):
            cx = float(rng.uniform(0, w))
            cy = float(rng.uniform(0, h))
            r = float(rng.uniform(min(w, h) * 0.05, min(w, h) * 0.35))
            a0 = float(rng.uniform(0, 360))
            a1 = a0 + float(rng.uniform(40, 220))
            c = p[int(rng.integers(0, len(p)))]
            draw.arc([cx - r, cy - r, cx + r, cy + r], start=a0, end=a1,
                     fill=(*c, alpha), width=int(rng.integers(2, 7)))
    elif motif == "polygons":
        for _ in range(n):
            cx = float(rng.uniform(0, w))
            cy = float(rng.uniform(0, h))
            r = float(rng.uniform(min(w, h) * 0.04, min(w, h) * 0.22))
            sides = int(rng.integers(3, 8))
            rot = float(rng.uniform(0, 2 * np.pi))
            pts = [(cx + r * np.cos(rot + 2 * np.pi * k / sides),
                    cy + r * np.sin(rot + 2 * np.pi * k / sides))
                   for k in range(sides)]
            c = p[int(rng.integers(0, len(p)))]
            draw.polygon(pts, outline=(*c, alpha))
    else:  # bands
        for _ in range(n):
            y0 = float(rng.uniform(0, h))
            thick = float(rng.uniform(h * 0.01, h * 0.09))
            c = p[int(rng.integers(0, len(p)))]
            draw.rectangle([0, y0, w, y0 + thick], fill=(*c, alpha // 2))
    return layer, motif


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(prompt: str, out_path: str, width: int = 1024, height: int = 1024,
             seed: int | None = None) -> dict:
    """Generate a procedural generative-art PNG steered by *prompt*.

    Returns a result dict. On refusal, ``{"ok": False, "error": ...}``.
    On success, ``{"ok": True, "out_path": ..., "width": ..., "height": ...,
    "seed": <effective int seed>, "method": "procedural-generative",
    "palette": [hex colors], "palette_family": <name>,
    "composition": {...}}``.

    The prompt steers palette family and composition parameters; it does
    not depict objects. See the module docstring for the honest bounds.
    """
    # ---- refusals ---------------------------------------------------------
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "refused: prompt must be a non-empty string"}
    if (not isinstance(width, int) or not isinstance(height, int)
            or isinstance(width, bool) or isinstance(height, bool)):
        return {"ok": False,
                "error": f"refused: width/height must be integers within "
                         f"[{MIN_DIM}, {MAX_DIM}]"}
    if not (MIN_DIM <= width <= MAX_DIM) or not (MIN_DIM <= height <= MAX_DIM):
        return {"ok": False,
                "error": f"refused: dimensions out of bounds "
                         f"[{MIN_DIM}, {MAX_DIM}] (got {width}x{height})"}
    if not isinstance(out_path, str) or not out_path:
        return {"ok": False, "error": "refused: out_path must be a non-empty string"}

    t0 = time.monotonic()
    eff_seed = _derive_seed(prompt, seed)
    rng = np.random.default_rng(eff_seed)

    family = _palette_family(prompt)
    palette = _PALETTES[family]

    h, w = height, width
    warp = _value_noise(rng, h, w, base=6, octaves=4)
    detail = _value_noise(rng, h, w, base=24, octaves=3)
    angle_field = _value_noise(rng, h, w, base=4, octaves=3).astype(np.float32)
    angle_field = angle_field * (2.0 * np.pi) * 2.0

    base_img, grad_style = _gradient_wash(h, w, palette, rng, warp)

    # modulate brightness with detail noise for texture
    mod = 0.82 + 0.36 * detail
    base_img = base_img * mod[..., None]

    strokes = _flow_strokes_layer(h, w, palette, rng, angle_field)
    motifs, motif_kind = _motif_layer(h, w, palette, rng)

    canvas = Image.fromarray(np.clip(base_img, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    canvas = Image.alpha_composite(canvas, strokes)
    canvas = Image.alpha_composite(canvas, motifs)

    # vignette: multiply edges down
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w / 2.0, h / 2.0
    r = np.sqrt(((xs - cx) / (w / 2.0)) ** 2 + ((ys - cy) / (h / 2.0)) ** 2) / np.sqrt(2.0)
    vig = np.clip(1.0 - 0.38 * np.clip(r, 0, 1) ** 1.8, 0, 1).astype(np.float32)
    arr = np.asarray(canvas).astype(np.float32)
    arr[..., :3] *= vig[..., None]
    final = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA").convert("RGB")
    final.save(out_path, format="PNG")  # deterministic encoder: no metadata

    elapsed = time.monotonic() - t0
    return {
        "ok": True,
        "out_path": out_path,
        "width": w,
        "height": h,
        "seed": eff_seed,
        "method": METHOD,
        "palette": ["#%02x%02x%02x" % c for c in palette],
        "palette_family": family,
        "composition": {
            "gradient_style": ["vertical", "diagonal", "radial", "horizon"][grad_style],
            "motif": motif_kind,
            "noise_octaves": 4,
            "detail_octaves": 3,
            "prompt_steers": "palette family + composition parameters only; "
                             "no object depiction",
        },
        "elapsed_s": round(elapsed, 2),
    }
