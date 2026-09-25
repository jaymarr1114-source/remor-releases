"""Media generation front (§6): image and video synthesis entry points.

Classification: UNAVAILABLE. There is no generative image or video
substrate in this environment and no provider credentials (audited
2026-09-25: no torch, diffusers, transformers, OpenCV, model weights,
or GPU; no OpenAI/Anthropic/Replicate/Stability/HuggingFace/Azure
credentials). Pillow/NumPy/matplotlib can manipulate rasters and ffmpeg
can encode/mux -- those are NOT generative models and are never
presented as such.

Like the voice front, these entry points exist so callers have one
governed place to ask -- and get an honest, specific refusal instead of
a procedurally-generated bitmap mislabeled as "AI generation". The
substrate probe runs at call time.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from swarm_engine.services.unavailable import CapabilityUnavailable


def _probe_image_substrate() -> List[str]:
    missing: List[str] = []
    for mod in ("torch", "diffusers", "transformers", "cv2"):
        try:
            __import__(mod)
        except Exception:
            missing.append(f"{mod} (generative model stack)")
    import os
    if not any(os.environ.get(k) for k in (
            "OPENAI_API_KEY", "STABILITY_API_KEY", "REPLICATE_API_TOKEN",
            "HUGGINGFACE_API_KEY", "AZURE_OPENAI_API_KEY")):
        missing.append("image-provider API credentials "
                       "(OpenAI/Stability/Replicate/HuggingFace/Azure)")
    if not os.path.exists("/dev/nvidia0"):
        missing.append("GPU (local model inference)")
    return missing


def _probe_video_substrate() -> List[str]:
    missing = _probe_image_substrate()
    for mod in ("moviepy", "imageio"):
        try:
            __import__(mod)
        except Exception:
            missing.append(f"{mod} (video assembly)")
    return missing


def generate_image(prompt: str, *, width: int = 512, height: int = 512,
                   provider: Optional[str] = None) -> Dict[str, Any]:
    """Text-to-image. Always raises CapabilityUnavailable: no generative
    substrate and no provider credentials."""
    missing = _probe_image_substrate()
    raise CapabilityUnavailable(
        capability="media.generate_image",
        reason=("no text-to-image substrate (no local generative model, "
                "no provider credentials); refusing to fabricate imagery"),
        missing=missing)


def generate_video(prompt: str, *, seconds: int = 5, fps: int = 24,
                   provider: Optional[str] = None) -> Dict[str, Any]:
    """Text-to-video. Always raises CapabilityUnavailable: no generative
    substrate and no provider credentials."""
    missing = _probe_video_substrate()
    raise CapabilityUnavailable(
        capability="media.generate_video",
        reason=("no text-to-video substrate (no local generative model, "
                "no provider credentials); refusing to fabricate video"),
        missing=missing)


def status() -> Dict[str, Any]:
    """Machine-readable media-front status: unavailable + exact gaps."""
    return {
        "generate_image": {
            "available": False, "classification": "UNAVAILABLE",
            "missing": _probe_image_substrate()},
        "generate_video": {
            "available": False, "classification": "UNAVAILABLE",
            "missing": _probe_video_substrate()},
    }
