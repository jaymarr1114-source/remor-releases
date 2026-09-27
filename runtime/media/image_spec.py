"""Deterministic spec-driven image renderer (worker B, 2026-09-26).

Renders a raster PNG from a declarative spec. This is NOT a text-to-image
model: there is no learned network, no weights, no training data, no GPU.
The renderer draws exactly what the spec declares -- solid/gradient
backgrounds, geometric shapes (circles, rects, polygons) at spec'd
positions and colors, and text via a real TTF (DejaVuSans).

CAN (honest capability statement):
  - deterministic compositions from an explicit spec: solid or gradient
    backgrounds; circles/rects/polygons at exact spec'd positions, sizes
    and colors; text rendered with DejaVuSans at a spec'd size/position/
    color;
  - byte-identical PNG for identical (spec, seed).
CANNOT (honest bounds, kept in every result under "bounds"):
  - depict arbitrary subjects. The ``subject`` field is a descriptive
    label carried in the result metadata -- it is NOT depicted. A spec
    saying {"subject": "a sunset"} with no shapes renders the seeded
    geometric default composition, not a sunset. Prompt-steered depiction
    remains the job of the procedural-generative ``swarm_engine.media.image``
    substrate (with its own honest bounds).

Spec schema (strict; violations raise ValueError -- fail closed):
  {
    "subject": str (required, non-empty -- descriptive label only),
    "width": int 1..2048 (required),
    "height": int 1..2048 (required),
    "style": "flat" | "gradient" (required -- background treatment),
    "seed": int (required -- determinism root),
    "background": optional {"color": "#rrggbb"}
                | {"type": "gradient", "from": "#rrggbb", "to": "#rrggbb",
                   "direction": "vertical"|"horizontal"},
    "shapes": optional [ {"kind": "circle", "center": [x, y], "radius": r,
                           "fill": "#rrggbb"}
                       | {"kind": "rect", "box": [x0, y0, x1, y1],
                           "fill": "#rrggbb"}
                       | {"kind": "polygon", "points": [[x, y], ...],
                           "fill": "#rrggbb"} ],
              (absent -> deterministic seeded-geometric default),
    "texts": optional [ {"text": str, "position": [x, y], "size": int,
                         "fill": "#rrggbb"} ],
  }
Colors are strict "#rrggbb" (or "#rgb") hex. All coordinates are ints.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple

IMAGE_SPEC_BOUNDS = [
    "spec-driven deterministic rasterization (PIL), NOT a learned "
    "text-to-image model: no diffusion network, no neural weights, no "
    "training data, no GPU",
    "CAN: solid/gradient backgrounds, circles/rects/polygons at exact "
    "spec'd positions and colors, text via DejaVuSans TTF",
    "CANNOT: depict arbitrary subjects -- the 'subject' field is a "
    "descriptive label carried as metadata, not a depiction; a spec "
    "without shapes renders a seeded geometric default, not the named "
    "subject",
    "deterministic: same (spec, seed) -> byte-identical PNG",
    "resolution cap 2048x2048 per side",
]

_MAX_SIDE = 2048
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

_FONT_PATHS = [
    "DejaVuSans.ttf",  # via PIL's font lookup
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]


def _parse_color(s: Any, where: str) -> Tuple[int, int, int]:
    if not isinstance(s, str) or not _HEX_RE.match(s):
        raise ValueError(f"{where}: color must be '#rrggbb' hex, got {s!r}")
    h = s[1:]
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _check_int(v: Any, where: str, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{where}: expected int in [{lo},{hi}], got {v!r}")
    if not (lo <= v <= hi):
        raise ValueError(f"{where}: expected int in [{lo},{hi}], got {v!r}")
    return v


def _check_point(v: Any, where: str, w: int, h: int) -> Tuple[int, int]:
    if (not isinstance(v, (list, tuple)) or len(v) != 2
            or any(isinstance(c, bool) or not isinstance(c, int) for c in v)):
        raise ValueError(f"{where}: expected [x, y] int pair, got {v!r}")
    x, y = v
    if not (0 <= x < w and 0 <= y < h):
        raise ValueError(f"{where}: point {v!r} outside {w}x{h}")
    return x, y


def _validate_shape(s: Any, w: int, h: int) -> Dict[str, Any]:
    if not isinstance(s, dict):
        raise ValueError(f"shapes: each shape must be an object, got {s!r}")
    kind = s.get("kind")
    fill = _parse_color(s.get("fill"), "shapes.fill")
    if kind == "circle":
        cx, cy = _check_point(s.get("center"), "circle.center", w, h)
        r = _check_int(s.get("radius"), "circle.radius", 1, max(w, h))
        return {"kind": "circle", "center": [cx, cy], "radius": r,
                "fill": s["fill"]}
    if kind == "rect":
        box = s.get("box")
        if (not isinstance(box, (list, tuple)) or len(box) != 4
                or any(isinstance(c, bool) or not isinstance(c, int)
                       for c in box)):
            raise ValueError(f"rect.box: expected [x0,y0,x1,y1] ints, "
                             f"got {box!r}")
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            raise ValueError(f"rect.box: invalid/outside box {box!r} "
                             f"for {w}x{h}")
        return {"kind": "rect", "box": [x0, y0, x1, y1], "fill": s["fill"]}
    if kind == "polygon":
        pts = s.get("points")
        if (not isinstance(pts, (list, tuple)) or len(pts) < 3
                or any(not isinstance(p, (list, tuple)) or len(p) != 2
                       for p in pts)):
            raise ValueError(f"polygon.points: expected >=3 [x,y] pairs, "
                             f"got {pts!r}")
        norm = [_check_point(p, "polygon.points[]", w, h) for p in pts]
        return {"kind": "polygon",
                "points": [[x, y] for x, y in norm], "fill": s["fill"]}
    raise ValueError(f"shapes: unknown kind {kind!r} "
                     f"(circle|rect|polygon), got {s!r}")


def _validate_text(t: Any, w: int, h: int) -> Dict[str, Any]:
    if not isinstance(t, dict):
        raise ValueError(f"texts: each text must be an object, got {t!r}")
    txt = t.get("text")
    if not isinstance(txt, str) or not txt.strip():
        raise ValueError(f"texts: 'text' must be a non-empty string, "
                         f"got {txt!r}")
    x, y = _check_point(t.get("position"), "texts.position", w, h)
    size = _check_int(t.get("size"), "texts.size", 8, 256)
    _parse_color(t.get("fill"), "texts.fill")
    return {"text": txt, "position": [x, y], "size": size,
            "fill": t["fill"]}


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _validate_spec(spec: Any) -> Dict[str, Any]:
    """Strict validation -> normalized spec. Raises ValueError (fail closed)."""
    if not isinstance(spec, dict):
        raise ValueError(f"spec must be an object, got {type(spec).__name__}")
    subject = spec.get("subject")
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("spec.subject: required non-empty string "
                         "(descriptive label; not depicted)")
    w = _check_int(spec.get("width"), "spec.width", 1, _MAX_SIDE)
    h = _check_int(spec.get("height"), "spec.height", 1, _MAX_SIDE)
    style = spec.get("style")
    if style not in ("flat", "gradient"):
        raise ValueError(f"spec.style: 'flat'|'gradient', got {style!r}")
    seed = spec.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(f"spec.seed: required int, got {seed!r}")

    bg = spec.get("background")
    if bg is None:
        bg = {"type": "default"}
    elif not isinstance(bg, dict):
        raise ValueError(f"spec.background: object, got {bg!r}")
    elif "color" in bg:
        _parse_color(bg["color"], "background.color")
        bg = {"color": bg["color"]}
    else:
        btype = bg.get("type", "gradient")
        if btype == "gradient":
            c0 = _parse_color(bg.get("from", "#1a2a6c"), "background.from")
            c1 = _parse_color(bg.get("to", "#fdbb2d"), "background.to")
            direction = bg.get("direction", "vertical")
            if direction not in ("vertical", "horizontal"):
                raise ValueError("background.direction: "
                                 "'vertical'|'horizontal'")
            bg = {"type": "gradient",
                  "from": "#%02x%02x%02x" % c0, "to": "#%02x%02x%02x" % c1,
                  "direction": direction}
        elif btype == "solid":
            c = _parse_color(bg.get("color", "#0b1e3a"), "background.color")
            bg = {"color": "#%02x%02x%02x" % c}
        else:
            raise ValueError(f"spec.background: unknown type {btype!r}")

    shapes = spec.get("shapes")
    if shapes is None:
        shapes = None  # resolved from seed at render time
    else:
        if not isinstance(shapes, (list, tuple)) or not shapes:
            raise ValueError("spec.shapes: non-empty list or omit")
        shapes = [_validate_shape(s, w, h) for s in shapes]

    texts = spec.get("texts", [])
    if not isinstance(texts, (list, tuple)):
        raise ValueError(f"spec.texts: list, got {texts!r}")
    texts = [_validate_text(t, w, h) for t in texts]

    return {"subject": subject.strip(), "width": w, "height": h,
            "style": style, "seed": seed, "background": bg,
            "shapes": shapes, "texts": texts}


def _seeded_default_shapes(w: int, h: int, seed: int) -> List[Dict[str, Any]]:
    """Deterministic geometric default when the spec names no shapes."""
    rng = random.Random(seed ^ 0x5EED)
    palette = ["#e63946", "#f1fa8c", "#2a9d8f", "#e9c46a", "#f4a261"]
    shapes: List[Dict[str, Any]] = []
    for i in range(4):
        kind = ["circle", "rect", "polygon", "circle"][i % 4]
        fill = palette[rng.randrange(len(palette))]
        if kind == "circle":
            r = rng.randint(min(w, h) // 12, min(w, h) // 5)
            cx = rng.randint(r, w - r - 1)
            cy = rng.randint(r, h - r - 1)
            shapes.append({"kind": "circle", "center": [cx, cy],
                           "radius": r, "fill": fill})
        elif kind == "rect":
            bw, bh = rng.randint(w // 10, w // 3), rng.randint(h // 10, h // 3)
            x0, y0 = rng.randint(0, w - bw - 1), rng.randint(0, h - bh - 1)
            shapes.append({"kind": "rect",
                           "box": [x0, y0, x0 + bw, y0 + bh], "fill": fill})
        else:
            cx, cy = rng.randint(w // 4, 3 * w // 4), rng.randint(
                h // 4, 3 * h // 4)
            r = rng.randint(min(w, h) // 10, min(w, h) // 4)
            pts = []
            for k in range(5):
                import math as _m
                a = 2 * _m.pi * k / 5 + rng.random() * 0.6
                px = min(max(int(cx + r * _m.cos(a)), 0), w - 1)
                py = min(max(int(cy + r * _m.sin(a)), 0), h - 1)
                pts.append([px, py])
            shapes.append({"kind": "polygon", "points": pts, "fill": fill})
    return shapes


def _load_font(size: int):
    from PIL import ImageFont
    last: Optional[Exception] = None
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except Exception as exc:
            last = exc
    raise RuntimeError(
        "DejaVuSans TTF not loadable (tried "
        f"{_FONT_PATHS}): {last}; text rendering refused, not degraded "
        "to a bitmap font")


def _paint_background(img, bg: Dict[str, Any], style: str, seed: int):
    from PIL import ImageDraw
    w, h = img.size
    dr = ImageDraw.Draw(img)
    if "color" in bg:
        dr.rectangle([0, 0, w, h], fill=_parse_color(bg["color"], "bg"))
        return
    if bg.get("type") == "default":
        # seeded deterministic default: vertical gradient, seeded hues
        rng = random.Random(seed ^ 0xBAC6)
        c0 = (rng.randint(10, 60), rng.randint(20, 70), rng.randint(60, 130))
        c1 = (rng.randint(180, 245), rng.randint(150, 200), rng.randint(40, 90))
    else:
        c0 = _parse_color(bg["from"], "bg.from")
        c1 = _parse_color(bg["to"], "bg.to")
    direction = bg.get("direction", "vertical")
    if style == "flat":
        # flat style + gradient spec: solid midpoint (documented)
        mid = tuple((a + b) // 2 for a, b in zip(c0, c1))
        dr.rectangle([0, 0, w, h], fill=mid)
        return
    span = (h - 1) if direction == "vertical" else (w - 1)
    span = max(span, 1)
    if direction == "vertical":
        for y in range(h):
            t = y / span
            col = tuple(int(a + (b - a) * t) for a, b in zip(c0, c1))
            dr.line([(0, y), (w, y)], fill=col)
    else:
        for x in range(w):
            t = x / span
            col = tuple(int(a + (b - a) * t) for a, b in zip(c0, c1))
            dr.line([(x, 0), (x, h)], fill=col)


def _draw_shapes(img, shapes: List[Dict[str, Any]]):
    from PIL import ImageDraw
    dr = ImageDraw.Draw(img)
    for s in shapes:
        fill = _parse_color(s["fill"], "shape.fill")
        kind = s["kind"]
        if kind == "circle":
            cx, cy = s["center"]
            r = s["radius"]
            dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
        elif kind == "rect":
            dr.rectangle(s["box"], fill=fill)
        elif kind == "polygon":
            dr.polygon([tuple(p) for p in s["points"]], fill=fill)


def _draw_texts(img, texts: List[Dict[str, Any]]):
    from PIL import ImageDraw
    dr = ImageDraw.Draw(img)
    for t in texts:
        font = _load_font(t["size"])
        dr.text(tuple(t["position"]), t["text"],
               font=font, fill=_parse_color(t["fill"], "text.fill"))


def render_spec(spec: Dict[str, Any], out_path: str) -> Dict[str, Any]:
    """Render the spec to a PNG at out_path. Returns a result dict.

    Raises ValueError on a malformed spec, RuntimeError when the TTF is
    unavailable, OSError on write failure. Deterministic: identical
    (spec, seed) -> byte-identical PNG.
    """
    from PIL import Image
    norm = _validate_spec(spec)
    w, h = norm["width"], norm["height"]
    img = Image.new("RGB", (w, h))
    _paint_background(img, norm["background"], norm["style"], norm["seed"])
    shapes = norm["shapes"]
    if shapes is None:
        shapes = _seeded_default_shapes(w, h, norm["seed"])
    _draw_shapes(img, shapes)
    _draw_texts(img, norm["texts"])
    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    tmp = out_path + ".tmp"
    img.save(tmp, format="PNG")
    os.replace(tmp, out_path)
    with open(out_path, "rb") as fh:
        blob = fh.read()
    return {
        "ok": True,
        "out_path": os.path.abspath(out_path),
        "width": w,
        "height": h,
        "subject": norm["subject"],
        "style": norm["style"],
        "seed": norm["seed"],
        "spec_digest": hashlib.sha256(
            _canonical(norm).encode()).hexdigest(),
        "png_sha256": hashlib.sha256(blob).hexdigest(),
        "size_bytes": len(blob),
        "shapes_rendered": len(shapes),
        "texts_rendered": len(norm["texts"]),
        "spec": norm,  # normalized spec echo, for pixel-level verification
        "method": "spec-driven-deterministic",
        "bounds": list(IMAGE_SPEC_BOUNDS),
    }


# ---------------------------------------------------------------------------
# Pixel-level smoke judge (registered as a bound predicate oracle at
# admission; see wiring.admit_image_spec_capability).
# ---------------------------------------------------------------------------
def _check_image_spec_pixels(value: Any) -> Tuple[bool, str]:
    """Assert pixel-level spec compliance of a render_spec result.

    Returns (passed, via/reason). Checks, all against the normalized spec
    echo in the result (not against the renderer's internals):
      1. the PNG exists and its dimensions equal the spec's;
      2. every spec'd circle: sample points well inside the disc are
         exactly the fill color; points well outside are not;
      3. determinism: re-rendering the same spec+seed yields
         byte-identical bytes (sha256 match).
    """
    from PIL import Image
    if not isinstance(value, dict) or value.get("ok") is not True:
        return False, "result is not an ok dict"
    path = value.get("out_path")
    spec = value.get("spec")
    if not isinstance(path, str) or not isinstance(spec, dict):
        return False, "result missing out_path/spec echo"
    if not os.path.exists(path):
        return False, f"output file missing: {path}"
    img = Image.open(path).convert("RGB")
    w, h = spec.get("width"), spec.get("height")
    if img.size != (w, h):
        return False, (f"dimension mismatch: file {img.size} != "
                       f"spec {(w, h)}")
    px = img.load()
    for s in spec.get("shapes") or []:
        if s.get("kind") != "circle":
            continue
        try:
            fill = _parse_color(s["fill"], "smoke.circle.fill")
        except ValueError as exc:
            return False, f"bad fill in spec echo: {exc}"
        (cx, cy), r = s["center"], s["radius"]
        inside = [(cx, cy), (cx + r // 2, cy), (cx - r // 2, cy),
                  (cx, cy + r // 2), (cx, cy - r // 2)]
        for (x, y) in inside:
            if px[x, y] != fill:
                return False, (f"circle at {(cx, cy)} r={r}: pixel "
                               f"{(x, y)} = {px[x, y]} != fill {fill}")
        d = r + 8
        outside = [(cx + d, cy + d), (cx - d, cy - d),
                   (cx + d, cy - d), (cx - d, cy + d)]
        for (x, y) in outside:
            if not (0 <= x < w and 0 <= y < h):
                continue
            if px[x, y] == fill:
                return False, (f"circle at {(cx, cy)} r={r}: outside pixel "
                               f"{(x, y)} is fill color {fill}")
    # determinism: same spec+seed -> byte-identical PNG
    with open(path, "rb") as fh:
        file_sha = hashlib.sha256(fh.read()).hexdigest()
    if value.get("png_sha256") != file_sha:
        return False, "result png_sha256 does not match file bytes"
    tmp = os.path.join(tempfile.mkdtemp(prefix="spec_smoke_"),
                       "redeterminism.png")
    try:
        rerun = render_spec(spec, tmp)
    except Exception as exc:
        return False, f"re-render raised {type(exc).__name__}: {exc}"
    if rerun["png_sha256"] != file_sha:
        return False, (f"non-deterministic: re-render sha "
                       f"{rerun['png_sha256'][:12]} != {file_sha[:12]}")
    return True, "pixel: dims + disc in/out + byte-determinism"


def judge_image_spec_pixels(value: Any) -> bool:
    """Bound predicate oracle for the image-spec admission smoke test.

    Must return a plain bool (admission's SmokeTest.judge wraps it).
    The detailed reason is available to direct callers via
    _check_image_spec_pixels.
    """
    ok, _why = _check_image_spec_pixels(value)
    return ok
