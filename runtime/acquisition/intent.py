"""
Intent / effect-demand / postcondition helpers.

Generalized (not filesystem-hardcoded) mapping from natural-language goals
to required effects and optional world postconditions. Used by gap analysis
and verification so effectful goals cannot be certified by return value alone.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


# Declarative effect lexicon: each entry is
# (effect_value, cue_patterns)
# Cues are matched as whole-word / phrase patterns against lowercased goal text.
# This is intentionally shallow and extensible — not a full NLU model.
_EFFECT_LEXICON: List[Tuple[str, Tuple[str, ...]]] = [
    ("write_fs", (
        r"\bwrite\b", r"\bsave\b", r"\bcreate file\b", r"\bwrite the text\b",
        r"\bwrite text\b", r"\bpersist to\b", r"\bappend to file\b",
        r"\boverwrite\b", r"\binto the file\b", r"\binto /\b",
        # Generic create-file / create-directory cues (effect family, not task-specific)
        r"\bcreate\b.{0,60}\bfile\b",
        r"\bcreate\b.{0,40}\bdirectory\b",
        r"\bcreate\b.{0,40}\bdir\b",
        r"\bmkdir\b", r"\bmake.?dir\b",
    )),
    ("read_fs", (
        r"\bread file\b", r"\bload file\b", r"\bopen file\b",
        r"\bread the (?:contents|content|file)\b", r"\bfrom disk\b",
    )),
    ("network", (
        r"\bhttp\b", r"\bhttps\b", r"\bfetch\b", r"\bdownload\b",
        r"\bscrape\b", r"\burl\b", r"\bweb\b", r"\bapi request\b",
    )),
    ("process", (
        r"\bsubprocess\b", r"\bshell out\b", r"\brun command\b",
        r"\bexecute command\b", r"\bspawn process\b",
        r"\brun test\b", r"\brun the test\b", r"\bexecute test\b",
        r"\brun python\b", r"\bpytest\b", r"\bpython -m\b",
    )),
    # Media-generation intents (worker E, 2026-09-26). Cue -> effect tokens
    # only; routing of concrete phrasings goes through the capability
    # store's exact goal bindings. Effect values are plain strings (like
    # "write_fs") and flow through acquisition as string sets -- they are
    # never converted to the Effect enum, so new values are safe.
    ("media_voice", (
        r"\bsynthesize speech\b", r"\btext.to.speech\b",
        r"\btext (?:to|into) speech\b", r"\bspeak the text\b",
        r"\bread aloud\b", r"\bread (?:this|the )?text aloud\b",
        r"\bvoiceover\b", r"\bvoice-over\b", r"\bnarrat\w+\b",
    )),
    ("media_song", (
        r"\bmake (?:me )?a song\b", r"\bcreate (?:me )?a song\b",
        r"\bcompose (?:me )?a song\b", r"\bwrite (?:me )?a song\b",
        r"\bsong about\b", r"\bsong for\b",
    )),
    ("media_image", (
        r"\bgenerate (?:an? )?(?:\w+ )?image\b",
        r"\bcreate (?:an? )?(?:\w+ )?image\b",
        r"\bmake (?:an? )?(?:\w+ )?image\b",
        r"\bgenerate (?:a )?(?:\w+ )?picture\b",
        r"\bcreate (?:a )?(?:\w+ )?picture\b",
        r"\bdraw (?:a )?picture\b", r"\btext.to.image\b",
        r"\bimage of\b", r"\bpicture of\b",
    )),
    ("media_video", (
        r"\bgenerate (?:a )?(?:\w+ )?video\b",
        r"\bcreate (?:a )?(?:\w+ )?video\b",
        r"\bmake (?:a )?(?:\w+ )?video\b",
        r"\btext.to.video\b", r"\bvideo of\b", r"\bshort video\b",
        r"\banimat\w*\b",
    )),
]


@dataclass
class Postcondition:
    """Generic external/world predicate. Not filesystem-specific."""
    kind: str
    target: str = ""
    expect: Any = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "target": self.target,
                "expect": self.expect, "meta": dict(self.meta)}


def infer_required_effects(goal: str) -> List[str]:
    """Derive required effect tokens from NL goal using the effect lexicon."""
    low = (goal or "").lower()
    found: List[str] = []
    for effect, patterns in _EFFECT_LEXICON:
        for pat in patterns:
            if re.search(pat, low):
                if effect not in found:
                    found.append(effect)
                break
    return found


def _extract_quoted_or_path(goal: str) -> List[str]:
    """Best-effort extraction of path-like or quoted literals from goal text."""
    hits: List[str] = []
    for m in re.finditer(r"['\"]([^'\"]+)['\"]", goal or ""):
        hits.append(m.group(1))
    for m in re.finditer(r"(/(?:tmp|var|home|Users|usr)[^\s'\"]+)", goal or ""):
        if m.group(1) not in hits:
            hits.append(m.group(1))
    return hits


def infer_postconditions(goal: str, examples: Optional[Sequence[Any]] = None
                         ) -> List[Postcondition]:
    """Infer minimal world postconditions from goal (+ optional examples).

    Currently implements observers needed for write_fs acceptance tests:
    path existence and content equality when content can be recovered from
    the goal text or examples. Other effect kinds return empty lists until
    observers are added — fail-closed on value-only success is handled by
    required_effects coverage, not by inventing fake postconditions.
    """
    effects = infer_required_effects(goal)
    posts: List[Postcondition] = []
    if "write_fs" not in effects:
        return posts

    paths = _extract_quoted_or_path(goal)
    content: Optional[str] = None
    # Prefer explicit quoted non-path string as content
    for h in paths:
        if not h.startswith("/") and not re.match(r"^[A-Za-z]:\\", h):
            content = h
            break
    # Path: first absolute-looking token
    path = None
    for h in paths:
        if h.startswith("/") or re.match(r"^[A-Za-z]:\\", h):
            path = h
            break
    # Unquoted pattern: "write the text <content> into <path>"
    if content is None:
        m = re.search(
            r"\bwrite(?:\s+the)?\s+text\s+(\S+?)\s+into\b",
            (goal or "").lower(),
        )
        if m:
            content = m.group(1).strip("'\"")
    if path is None:
        m = re.search(
            r"\binto\s+(/[^\s'\"]+|[A-Za-z]:\\[^\s'\"]+)",
            goal or "",
        )
        if m:
            path = m.group(1)
    # Recover from examples if needed
    if examples:
        first = examples[0]
        args = None
        expected = None
        if isinstance(first, (list, tuple)) and len(first) >= 2 and isinstance(first[0], dict):
            args, expected = first[0], first[1]
        elif isinstance(first, dict) and "input" in first:
            args, expected = first.get("input"), first.get("output", first.get("expected"))
        if isinstance(args, dict):
            path = path or args.get("path") or args.get("file") or args.get("target")
            for k in ("text", "content", "data", "body"):
                if k in args and content is None:
                    content = args[k]
                    break

    if path:
        posts.append(Postcondition(kind="path_exists", target=str(path)))
        if content is not None:
            posts.append(Postcondition(
                kind="file_content_equals", target=str(path), expect=content))
    return posts


# --- observers (minimal set for acceptance) --------------------------------

def _obs_path_exists(pc: Postcondition) -> Tuple[bool, str]:
    ok = os.path.exists(pc.target)
    return ok, "exists" if ok else f"missing path {pc.target!r}"


def _obs_file_content_equals(pc: Postcondition) -> Tuple[bool, str]:
    if not os.path.exists(pc.target):
        return False, f"missing path {pc.target!r}"
    try:
        with open(pc.target, "r", encoding="utf-8") as f:
            data = f.read()
    except Exception as ex:
        return False, f"read error: {ex}"
    if data == pc.expect:
        return True, "content match"
    return False, f"content {data!r} != expected {pc.expect!r}"


_OBSERVERS: Dict[str, Callable[[Postcondition], Tuple[bool, str]]] = {
    "path_exists": _obs_path_exists,
    "file_content_equals": _obs_file_content_equals,
}


def evaluate_postconditions(postconditions: Sequence[Postcondition]
                            ) -> Tuple[bool, str]:
    """Return (all_passed, detail). Empty list → vacuously True."""
    if not postconditions:
        return True, "no postconditions"
    details = []
    for pc in postconditions:
        obs = _OBSERVERS.get(pc.kind)
        if obs is None:
            return False, f"no observer for postcondition kind {pc.kind!r}"
        ok, msg = obs(pc)
        details.append(f"{pc.kind}:{msg}")
        if not ok:
            return False, "; ".join(details)
    return True, "; ".join(details)


