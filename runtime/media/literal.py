"""Literal image entities: frame -> spec-driven rendering (ACQ-MEDIA-1).

The one location for the product's literal-image artifact vocabulary and
for the entity -> image_spec translation.

WHY THIS MODULE EXISTS
  The NL dispatch path understood "Create a 256x256 blue square image and
  save it as blue.png" (CREATE_IMAGE @ 0.90) but the media substrate drew
  procedural art at the 512px default named image.png: the size/color/
  shape/filename the user named never became entities, so nothing could
  route on them. This module turns those frame entities into an explicit
  image_spec spec for the already-admitted, pixel-verified, deterministic
  ``media.image_render_spec`` renderer (runtime/media/image_spec.py).
  It does NOT render anything itself and it does NOT replace the
  generative fallback: when the entities are absent or incomplete, the
  caller keeps the existing procedural-art path unchanged.

LINGUISTIC HONESTY
  Every linguistic classification -- token boundaries, part of speech,
  noun-chunk structure -- comes from the acquired NLU substrate
  (runtime/synthesis/nlu_substrate.py); this module only consumes the
  substrate's lowercased content-word list. The color/shape lists below
  are the product's own artifact vocabulary (the same standing as the
  noun taxonomy in semantic_frames), not a grammar. The WxH number-shape
  parse (``256x256`` -> (256, 256)) reads an already-classified token's
  digits -- number extraction with literal provenance, the Q9 precedent --
  not a linguistic rule.

GEOMETRY CONTRACT (documented, deterministic)
  - color+shape required; anything else refuses with a named reason.
  - size explicit (a WxH token in the frame): the shape IS the size --
      square/rectangle fill the canvas; circle is inscribed
      (r = min(w,h)//2); triangle is scaled proportionally to the canvas.
  - size absent: the legacy centered geometry on 512x512 (circle r=120,
    rect 200x200, triangle as before) -- byte-identical to the previous
    shape-words path for the already-bound phrasings.
  - background: flat #0b1e3a (unchanged legacy default).
  - bounds: render_spec validates 1..2048 strictly; out-of-bounds sizes
    pass through and fail closed there (the dispatch limitation probe
    classifies them -- nothing here invents a clamp).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Canonical product vocabulary for literal images (relocated from
# nl_dispatch._SPEC_COLORS / _SPEC_SHAPES -- one location).
LITERAL_COLORS: Dict[str, str] = {
    "red": "#ff0000", "green": "#00a86b", "blue": "#2563eb",
    "yellow": "#facc15", "white": "#ffffff", "black": "#000000",
    "orange": "#f97316", "purple": "#8b5cf6", "pink": "#ec4899",
    "cyan": "#22d3ee", "gray": "#9ca3af", "grey": "#9ca3af",
}
LITERAL_SHAPES: Tuple[str, ...] = ("circle", "square", "rectangle",
                                   "triangle")

_LEGACY_BACKGROUND = "#0b1e3a"
_LEGACY_SIDE = 512

_SIZE_RE = re.compile(r"^(\d+)x(\d+)$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*\.png$")


def extract_literal_entities(mods: List[str],
                             phrase_text: str = "") -> Dict[str, Any]:
    """Pull literal-image entities from the substrate's NP content words.

    ``mods`` is the lowercased content-word list the acquired NLU
    substrate returned for the object noun chunk (e.g. ["256x256",
    "blue", "square"]). Returns a dict with any of: ``literal_color``,
    ``literal_shape``, ``literal_width``/``literal_height`` plus
    ``literal_size_explicit=True``. Color/shape are set only when exactly
    one vocabulary word each is present (two colors = not literal, same
    refusal contract as the old shape-words path). Absent = the request
    carries no literal entities and the caller falls back to generative
    art -- never a partial or fabricated entity set.
    """
    words = [str(m).lower() for m in (mods or [])]
    colors = [c for c in LITERAL_COLORS if c in words]
    shapes = [s for s in LITERAL_SHAPES if s in words]
    ent: Dict[str, Any] = {}
    if len(colors) == 1:
        ent["literal_color"] = colors[0]
    if len(shapes) == 1:
        ent["literal_shape"] = shapes[0]
    for tok in words:
        m = _SIZE_RE.match(tok)
        if m:
            ent["literal_width"] = int(m.group(1))
            ent["literal_height"] = int(m.group(2))
            ent["literal_size_explicit"] = True
            break
    return ent


def sanitize_filename(hint: Any) -> Optional[str]:
    """A filename_hint is usable only as a clean ``*.png`` basename.

    Rejects path separators, leading dots, and anything that is not
    already a safe basename -- fail closed to the server-chosen uuid
    name, never a stripped/repaired variant.
    """
    if not isinstance(hint, str):
        return None
    name = hint.strip()
    if not _SAFE_NAME_RE.match(name):
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    return name


def build_literal_spec(entities: Dict[str, Any], seed: int
                       ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Build an image_spec spec from literal entities, or (None, reason).

    Requires ``literal_color`` + ``literal_shape``; the refusal reason
    preserves the old shape-words contract verbatim. Geometry follows
    the module contract above.
    """
    entities = entities or {}
    color = entities.get("literal_color")
    shape = entities.get("literal_shape")
    if (not color or not shape or color not in LITERAL_COLORS
            or shape not in LITERAL_SHAPES):
        return None, (
            "the spec renderer draws only explicit shapes from an "
            "explicit spec (e.g. 'draw a red circle'); it cannot depict "
            "subjects it has no geometry for. Provide {'spec': ...} via "
            "dispatch_by_id, or ask for a '<color> <shape>'.")
    w = entities.get("literal_width")
    h = entities.get("literal_height")
    if not isinstance(w, int) or isinstance(w, bool):
        w = _LEGACY_SIDE
    if not isinstance(h, int) or isinstance(h, bool):
        h = _LEGACY_SIDE
    explicit = bool(entities.get("literal_size_explicit"))
    fill = LITERAL_COLORS[color]
    cx, cy = w // 2, h // 2
    if shape in ("square", "rectangle") and explicit:
        # The square IS the named size: fill the canvas.
        geo = {"kind": "rect", "box": [0, 0, w, h], "fill": fill}
    elif shape == "circle" and explicit:
        r = min(w, h) // 2
        geo = {"kind": "circle", "center": [cx, cy], "radius": r,
               "fill": fill}
    elif shape == "triangle" and explicit:
        s = min(w, h) / 512.0
        geo = {"kind": "polygon",
               "points": [[cx, int(cy - 110 * s)],
                          [int(cx - 110 * s), int(cy + 90 * s)],
                          [int(cx + 110 * s), int(cy + 90 * s)]],
               "fill": fill}
    else:
        # Legacy centered geometry (byte-identical at 512x512).
        if shape == "circle":
            r = min(120, min(w, h) // 2)
            geo = {"kind": "circle", "center": [cx, cy], "radius": r,
                   "fill": fill}
        elif shape in ("square", "rectangle"):
            half = min(100, min(w, h) // 2)
            geo = {"kind": "rect",
                   "box": [cx - half, cy - half, cx + half, cy + half],
                   "fill": fill}
        else:  # triangle
            s = min(w, h) / 512.0
            geo = {"kind": "polygon",
                   "points": [[cx, int(cy - 110 * s)],
                              [int(cx - 110 * s), int(cy + 90 * s)],
                              [int(cx + 110 * s), int(cy + 90 * s)]],
                   "fill": fill}
    spec = {"subject": "%s %s" % (color, shape),
            "width": w, "height": h, "style": "flat", "seed": seed,
            "background": {"color": _LEGACY_BACKGROUND},
            "shapes": [geo], "texts": []}
    return spec, None
