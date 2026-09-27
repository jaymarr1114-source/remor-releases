"""REMOR video generation substrate.

Method (stated honestly): procedural animation, NOT learned video synthesis.
Every pixel of every frame is computed by this module (numpy/PIL): evolving
value-noise fields, flowing analytic gradient fields, and drifting geometric
motifs. The prompt seeds the parameters and steers mood/palette/motion, but it
does NOT depict objects, people, scenes, or text -- there is no learned model
here, so "a cat on a beach" yields an abstract animation whose palette/motion
is seeded from that string, not a picture of a cat.

Pipeline:
    prompt --seed--> RNG --> palette + motion params
    numpy/PIL render N RGB frames (one at a time, streamed)
    --> pipe raw RGB24 frames to ffmpeg (libx264, yuv420p) --> playable .mp4

Honest bounds:
    - Not learned video: no objects/people/text depiction. Abstract motion only.
    - Caps: duration (0, 30] s, resolution <= 1280x720, fps <= 60. These exist
      because rendering is CPU-bound: ~230k pixels/frame at 640x360, and each
      frame costs a few dozen full-frame numpy ops. On a 2-CPU box, 4 s of
      24 fps 640x360 costs on the order of 10-20 s wall clock (render + encode
      pipelined). Cost scales ~linearly with pixels * frames.
    - Deterministic for (prompt, seed, dims, fps, duration): same inputs give
      byte-comparable decoded frames (rendering is pure numpy; encoding uses
      fixed ffmpeg settings).
    - No network, no credentials, no GPU. Requires ffmpeg on PATH.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time

import numpy as np
from PIL import Image

METHOD = "procedural-animation"

MAX_DURATION_S = 30.0
MIN_DURATION_S = 0.05
MAX_WIDTH, MAX_HEIGHT = 1280, 720
MIN_DIM = 16
MAX_FPS = 60

# ---------------------------------------------------------------------------
# Prompt steering: mood keywords -> palette + motion bias. Anything else falls
# back to a hash-derived palette. The prompt never controls depicted content
# (there is none) -- only mood, palette, and motion character.
# ---------------------------------------------------------------------------

_MOODS = {
    "calm": ("calm", "ocean"),
    "ocean": ("calm", "ocean"),
    "sea": ("calm", "ocean"),
    "dawn": ("calm", "ocean"),
    "peaceful": ("calm", "ocean"),
    "sunset": ("sunset", "ember"),
    "warm": ("sunset", "ember"),
    "ember": ("sunset", "ember"),
    "fire": ("sunset", "ember"),
    "dusk": ("sunset", "ember"),
    "storm": ("storm", "slate"),
    "dark": ("storm", "slate"),
    "night": ("storm", "slate"),
    "rain": ("storm", "slate"),
    "forest": ("forest", "moss"),
    "green": ("forest", "moss"),
    "nature": ("forest", "moss"),
    "spring": ("forest", "moss"),
    "neon": ("neon", "violet"),
    "cyber": ("neon", "violet"),
    "electric": ("neon", "violet"),
    "city": ("neon", "violet"),
    "desert": ("desert", "sand"),
    "sand": ("desert", "sand"),
    "gold": ("desert", "sand"),
    "autumn": ("desert", "sand"),
}

_PALETTES = {
    "calm":   [(8, 20, 46), (16, 52, 92), (30, 110, 140), (120, 200, 210), (235, 248, 250)],
    "ocean":  [(4, 30, 60), (10, 70, 110), (24, 130, 150), (110, 205, 220), (240, 250, 252)],
    "sunset": [(28, 12, 44), (90, 30, 70), (170, 60, 60), (240, 140, 70), (255, 230, 170)],
    "ember":  [(20, 8, 10), (80, 20, 24), (160, 55, 30), (230, 130, 50), (255, 220, 150)],
    "storm":  [(10, 12, 18), (35, 42, 55), (70, 80, 95), (130, 145, 160), (215, 225, 235)],
    "slate":  [(14, 16, 22), (40, 46, 58), (80, 88, 100), (140, 150, 162), (220, 228, 236)],
    "forest": [(6, 24, 12), (16, 60, 30), (40, 110, 55), (120, 170, 90), (225, 240, 200)],
    "moss":   [(10, 28, 16), (24, 66, 34), (52, 116, 60), (130, 175, 95), (230, 242, 205)],
    "neon":   [(10, 6, 30), (40, 20, 90), (90, 40, 160), (170, 80, 230), (240, 200, 255)],
    "violet": [(12, 8, 34), (46, 24, 96), (100, 46, 170), (180, 90, 235), (245, 205, 255)],
    "desert": [(40, 24, 12), (110, 70, 30), (190, 130, 60), (240, 200, 130), (255, 245, 220)],
    "sand":   [(46, 28, 14), (118, 76, 34), (198, 138, 64), (244, 206, 136), (255, 248, 224)],
}


def _pick_mood(prompt: str, seed: int) -> tuple[str, str]:
    lowered = prompt.lower()
    for kw, moods in _MOODS.items():
        if kw in lowered:
            return moods
    names = sorted(_PALETTES)
    rng = np.random.default_rng(seed ^ 0x5EED)
    a = names[int(rng.integers(len(names)))]
    b = names[int(rng.integers(len(names)))]
    return a, b


def _palette_lut(name: str) -> np.ndarray:
    stops = np.asarray(_PALETTES[name], dtype=np.float32)  # (5, 3)
    xs = np.linspace(0, 1, len(stops))
    xi = np.linspace(0, 1, 256)
    lut = np.stack([np.interp(xi, xs, stops[:, c]) for c in range(3)], axis=1)
    return lut.astype(np.uint8)  # (256, 3)


def _noise_texture(rng: np.random.Generator, h: int, w: int) -> np.ndarray:
    """A smooth value-noise field, built once and scrolled per frame."""
    gh, gw = max(h // 12, 8), max(w // 12, 8)
    grid = rng.random((gh, gw), dtype=np.float32)
    img = Image.fromarray((grid * 255).astype(np.uint8), mode="L")
    big = img.resize((w * 2, h * 2), Image.BICUBIC)
    return np.asarray(big, dtype=np.float32) / 255.0  # (2h, 2w)


def _validate(prompt, out_path, duration_s, fps, width, height):
    if not isinstance(prompt, str) or not prompt.strip():
        return "prompt must be a non-empty string"
    if not isinstance(duration_s, (int, float)) or isinstance(duration_s, bool):
        return "duration_s must be a number"
    if not (MIN_DURATION_S < float(duration_s) <= MAX_DURATION_S):
        return f"duration_s must be in ({MIN_DURATION_S}, {MAX_DURATION_S}] seconds"
    if not isinstance(fps, int) or isinstance(fps, bool) or not (1 <= fps <= MAX_FPS):
        return f"fps must be an integer in [1, {MAX_FPS}]"
    for name, val, cap in (("width", width, MAX_WIDTH), ("height", height, MAX_HEIGHT)):
        if not isinstance(val, int) or isinstance(val, bool) or not (MIN_DIM <= val <= cap):
            return f"{name} must be an integer in [{MIN_DIM}, {cap}]"
    if not isinstance(out_path, str) or not out_path.strip():
        return "out_path must be a non-empty path string"
    if shutil.which("ffmpeg") is None:
        return "ffmpeg not found on PATH; cannot encode video"
    return None


def _render_frame(t: float, ctx: dict) -> np.ndarray:
    """Render one RGB frame at time t (seconds). All numpy/PIL, no learned model."""
    H, W = ctx["H"], ctx["W"]
    X, Y = ctx["X"], ctx["Y"]
    rng = ctx["rng"]
    P = ctx["params"]

    # --- flowing analytic field: layered sinusoids with time-evolving phase ---
    f = (P["a1"] * np.sin(2 * np.pi * (P["fx1"] * X + P["fy1"] * Y + P["ft1"] * t))
         + P["a2"] * np.sin(2 * np.pi * (P["fx2"] * (X + Y) + P["ft2"] * t + P["ph2"]))
         + P["a3"] * np.sin(2 * np.pi * (P["fx3"] * (X - Y) - P["ft3"] * t + P["ph3"])))
    f = f / (P["a1"] + P["a2"] + P["a3"]) * 0.5 + 0.5  # -> [0, 1]

    # --- scrolling value-noise field (two octaves, opposite drift) ---
    n1 = ctx["noise1"]
    n2 = ctx["noise2"]
    sx1 = int(t * P["drift1"]) % W
    sy1 = int(t * P["drift1"] * 0.6) % H
    sx2 = int(t * P["drift2"]) % W
    sy2 = int(t * P["drift2"] * 1.7) % H
    win1 = np.roll(np.roll(n1[:H, :W], sx1, axis=1), sy1, axis=0)
    win2 = np.roll(np.roll(n2[:H, :W], sx2, axis=1), -sy2, axis=0)
    noise = 0.65 * win1 + 0.35 * win2

    # --- drifting geometric motifs: soft discs on Lissajous paths ---
    motif = np.zeros((H, W), dtype=np.float32)
    for m in ctx["motifs"]:
        cx = 0.5 + m["ax"] * np.sin(2 * np.pi * m["wx"] * t + m["px"])
        cy = 0.5 + m["ay"] * np.cos(2 * np.pi * m["wy"] * t + m["py"])
        dx = (X - cx) / m["r"]
        dy = (Y - cy) / m["r"]
        d2 = dx * dx + dy * dy
        glow = np.exp(-np.clip(d2, 0, 9) * 2.2)
        motif += m["amp"] * glow
    motif = np.clip(motif, 0, 1)

    # --- composite scalar field -> dual-palette blend ---
    v = np.clip(0.55 * f + 0.30 * noise + 0.35 * motif, 0, 1)
    w = np.clip(0.5 + 0.5 * np.sin(2 * np.pi * (P["ft1"] * 0.5 * t + 0.5 * (X + Y))), 0, 1)
    idx = np.clip((v * 255).astype(np.int32), 0, 255)
    cA = ctx["lutA"][idx].astype(np.float32)
    cB = ctx["lutB"][idx].astype(np.float32)
    frame = cA * (1.0 - w[..., None]) + cB * w[..., None]
    return np.clip(frame, 0, 255).astype(np.uint8)


def generate(
    prompt: str,
    out_path: str,
    duration_s: float = 4.0,
    fps: int = 24,
    width: int = 640,
    height: int = 360,
    seed: int | None = None,
) -> dict:
    """Generate a video from a text prompt via procedural animation.

    Args:
        prompt: steering text (mood/palette/motion); must be non-empty.
        out_path: destination .mp4 path (parent dirs are created).
        duration_s: (0.05, 30] seconds.
        fps: integer in [1, 60].
        width: integer in [16, 1280] (snapped down to even for yuv420p).
        height: integer in [16, 720] (snapped down to even for yuv420p).
        seed: optional int; defaults to sha256(prompt)-derived seed.

    Returns:
        dict with ok, out_path, duration_s, fps, width, height, n_frames,
        seed, method, and elapsed_s. On refusal: ok=False with an error string.
    """
    err = _validate(prompt, out_path, duration_s, fps, width, height)
    if err is not None:
        return {"ok": False, "error": err, "method": METHOD}

    # yuv420p requires even dimensions; snap down (never up, to respect caps).
    width = width - (width % 2)
    height = height - (height % 2)
    duration_s = float(duration_s)
    n_frames = max(1, int(round(duration_s * fps)))

    if seed is None:
        seed = int(hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()[:16], 16) % (2**63)
    rng = np.random.default_rng(seed)

    moodA, moodB = _pick_mood(prompt, seed)

    params = {
        "a1": float(rng.uniform(0.5, 1.0)), "a2": float(rng.uniform(0.3, 0.8)),
        "a3": float(rng.uniform(0.2, 0.6)),
        "fx1": float(rng.uniform(1.0, 3.0)), "fy1": float(rng.uniform(1.0, 3.0)),
        "ft1": float(rng.uniform(0.05, 0.25)),
        "fx2": float(rng.uniform(1.5, 4.0)), "ft2": float(rng.uniform(0.05, 0.20)),
        "ph2": float(rng.uniform(0, 2 * np.pi)),
        "fx3": float(rng.uniform(1.5, 4.0)), "ft3": float(rng.uniform(0.05, 0.20)),
        "ph3": float(rng.uniform(0, 2 * np.pi)),
        "drift1": float(rng.uniform(8, 40)), "drift2": float(rng.uniform(4, 20)),
    }
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    ctx = {
        "H": height, "W": width,
        "X": xs / max(width - 1, 1), "Y": ys / max(height - 1, 1),
        "rng": rng, "params": params,
        "noise1": _noise_texture(rng, height, width),
        "noise2": _noise_texture(rng, height, width),
        "lutA": _palette_lut(moodA), "lutB": _palette_lut(moodB),
        "motifs": [
            {"ax": float(rng.uniform(0.15, 0.35)), "ay": float(rng.uniform(0.10, 0.30)),
             "wx": float(rng.uniform(0.03, 0.12)), "wy": float(rng.uniform(0.03, 0.12)),
             "px": float(rng.uniform(0, 2 * np.pi)), "py": float(rng.uniform(0, 2 * np.pi)),
             "r": float(rng.uniform(0.08, 0.20)), "amp": float(rng.uniform(0.25, 0.6))}
            for _ in range(3)
        ],
    }

    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)

    t0 = time.monotonic()
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-framerate", str(fps), "-i", "-",
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "21",
        "-movflags", "+faststart",
        out_path,
    ]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as e:
        return {"ok": False, "error": f"failed to launch ffmpeg: {e}", "method": METHOD}

    encode_error = None
    stderr_bytes = b""
    try:
        assert proc.stdin is not None
        try:
            for i in range(n_frames):
                t = i / fps
                frame = _render_frame(t, ctx)
                proc.stdin.write(frame.tobytes())
        except BrokenPipeError:
            encode_error = "ffmpeg closed the pipe mid-render"
        # communicate() closes stdin and reaps ffmpeg; do NOT close stdin first.
        _, stderr_bytes = proc.communicate(timeout=120)
        if encode_error is None and proc.returncode != 0:
            encode_error = (f"ffmpeg exited {proc.returncode}: "
                            f"{stderr_bytes.decode(errors='replace')[:500]}")
    except Exception as e:  # noqa: BLE001 - report, don't crash
        proc.kill()
        return {"ok": False, "error": f"render/encode failed: {e}", "method": METHOD}

    elapsed = time.monotonic() - t0
    if encode_error is not None:
        return {"ok": False, "error": encode_error, "method": METHOD}
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        return {"ok": False, "error": "ffmpeg produced no output file", "method": METHOD}

    return {
        "ok": True,
        "out_path": out_path,
        "duration_s": n_frames / fps,
        "fps": fps,
        "width": width,
        "height": height,
        "n_frames": n_frames,
        "seed": int(seed),
        "method": METHOD,
        "elapsed_s": round(elapsed, 2),
    }
