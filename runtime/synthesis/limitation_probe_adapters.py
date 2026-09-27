"""Probe adapters for Q12's production limitation recorder.

Q10's limitation probe replays a recorded reproduction in a child process
and treats "raised an exception" as failure. The media generators report
refusal as DATA ({"ok": False, "error": ...}) rather than raising, so a
bare generator reference would make the probe misread a full-scale refusal
as success ("cleared"). These adapters are the faithful replay: they call
the REAL generator with the REAL args and convert the refusal-dict into
the exception the probe understands. No behavior is invented -- the
refusal comes from the substrate itself.

The image_spec renderer already raises on out-of-bounds, so it needs no
adapter; its real callable is referenced directly.
"""
from __future__ import annotations


def _refusal(res) -> bool:
    return isinstance(res, dict) and res.get("ok") is False


def probe_image_generate(**kwargs):
    """swarm_engine.media.image.generate, raising on its refusal-dict."""
    from swarm_engine.media.image import generate
    res = generate(**kwargs)
    if _refusal(res):
        raise RuntimeError(str(res.get("error", "refused")))
    return res


def probe_video_generate(**kwargs):
    """swarm_engine.media.video.generate, raising on its refusal-dict."""
    from swarm_engine.media.video import generate
    res = generate(**kwargs)
    if _refusal(res):
        raise RuntimeError(str(res.get("error", "refused")))
    return res
