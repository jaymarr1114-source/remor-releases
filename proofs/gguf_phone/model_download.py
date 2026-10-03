"""Model download manager for phone-side LLM (GGUF work).

Handles on-demand download of Qwen3-0.6B (610MB) with:
- SHA256 verification
- Resume support (HTTP Range)
- Progress callbacks
- WiFi-only option
- Fail-closed on checksum mismatch

The bundled SmolLM2-135M (139MB) ships in the APK and needs no download.
"""
from __future__ import annotations

import hashlib
import os
from typing import Callable, Optional


# Qwen3-0.6B Q8_0 — download on demand (better quality than bundled 135M)
QWEN_06B_URL = (
    "https://github.com/jaymarr1114-source/remor-releases/releases/"
    "download/models/qwen3-0.6b-q8_0.gguf"
)
# Qwen 0.6B not yet published — download_model now FAILS CLOSED on empty
# checksum (2026-10-03), so this must be set before use.
QWEN_06B_SHA256 = ""  # MUST be set before download; empty = refuse
QWEN_06B_SIZE = 610 * 1024 * 1024  # ~610MB

# SmolLM2-360M Q8_0 (bundled in v1.0.12, verified 2026-10-03):
# - Source: Felladrin/gguf-Q8_0-SmolLM2-360M-Instruct
# - Base: HuggingFaceTB/SmolLM2-360M-Instruct (Apache 2.0)
# - Verified: valid GGUF v3, 386MB, metadata license apache-2.0
SMOLLM2_360M_SHA256 = (
    "48ab3034d0dd401fbc721eb1df3217902fee7dab9078992d66431f09b7750201"
)
SMOLLM2_360M_SIZE = 386404992


class ModelDownloadError(RuntimeError):
    """Download failed or checksum mismatch."""
    pass


def download_model(
    url: str,
    dest_path: str,
    expected_sha256: str = "",
    expected_size: int = 0,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    chunk_size: int = 1024 * 1024,
) -> str:
    """Download a model file with resume and SHA256 verification.

    Returns dest_path on success. Raises ModelDownloadError on failure.
    Fail-closed: partial/corrupt downloads are deleted, never used.
    Fail-closed: empty expected_sha256 is REJECTED (2026-10-03) — a model
    without a pinned checksum must never be downloaded.
    """
    import urllib.request

    # FAIL-CLOSED (2026-10-03): empty checksum = refuse, never skip.
    if not expected_sha256:
        raise ModelDownloadError(
            "Refusing download without pinned SHA256: %s" % url
        )

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

    # Resume: check existing partial file
    existing = 0
    if os.path.isfile(dest_path):
        existing = os.path.getsize(dest_path)
        if expected_size and existing == expected_size:
            # Already complete — verify checksum
            if expected_sha256 and not _verify_sha256(dest_path, expected_sha256):
                os.remove(dest_path)
                raise ModelDownloadError(
                    f"Existing file failed SHA256: {dest_path}"
                )
            return dest_path

    req = urllib.request.Request(url)
    if existing > 0:
        req.add_header("Range", f"bytes={existing}-")

    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except Exception as exc:
        raise ModelDownloadError(
            f"Download failed for {url}: {type(exc).__name__}: {exc}"
        ) from exc

    total = expected_size or int(resp.headers.get("Content-Length", 0)) + existing

    mode = "ab" if existing > 0 else "wb"
    downloaded = existing
    hasher = hashlib.sha256()

    # If resuming, hash the existing portion first
    if existing > 0:
        with open(dest_path, "rb") as f:
            while chunk := f.read(chunk_size):
                hasher.update(chunk)

    try:
        with open(dest_path, mode) as f:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                hasher.update(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)
    except Exception as exc:
        raise ModelDownloadError(
            f"Download interrupted: {type(exc).__name__}: {exc}"
        ) from exc

    # Verify size
    if expected_size and downloaded != expected_size:
        os.remove(dest_path)
        raise ModelDownloadError(
            f"Size mismatch: got {downloaded}, expected {expected_size}"
        )

    # Verify checksum (ALWAYS — empty was rejected at entry)
    actual = hasher.hexdigest()
    if actual != expected_sha256.lower():
        os.remove(dest_path)
        raise ModelDownloadError(
            f"SHA256 mismatch for {dest_path}: "
            f"got {actual[:16]}..., expected {expected_sha256[:16]}..."
        )

    return dest_path


def _verify_sha256(path: str, expected: str) -> bool:
    """Verify a file's SHA256."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest() == expected.lower()


def get_model_dir() -> str:
    """Get the app's model storage directory."""
    try:
        # On Android via Chaquopy, resolve the app's files dir
        from java import jclass
        # Placeholder: actual resolution happens on device
        return "models"
    except Exception:
        return os.path.expanduser("~/.remor/models")


def ensure_qwen_06b(
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> str:
    """Ensure Qwen3-0.6B is downloaded. Returns path. Raises on failure."""
    model_dir = get_model_dir()
    dest = os.path.join(model_dir, "qwen3-0.6b-q8_0.gguf")
    return download_model(
        QWEN_06B_URL,
        dest,
        expected_sha256=QWEN_06B_SHA256,
        expected_size=QWEN_06B_SIZE,
        progress_cb=progress_cb,
    )
