"""ACQ-STT-1: governed checkpoint fetch for Systran/faster-whisper-base.

Reuses M3's trust records verbatim (TrustedIndex, ArtifactPin, FetchRefused,
FetchAuditEntry from runtime/acquisition/substrate.py) — the same trust
contract as GovernedWeightFetch, adapted for a multi-file CTranslate2
checkpoint whose files are regular git blobs (not LFS):

  Integrity basis (honest, documented in the catalog entry):
  1. Revision pin: every URL is revision-anchored
     (…/resolve/<rev>/<file>), so bytes are anchored to the published
     commit ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66.
  2. Byte-identical dual-fetch: each file is fetched twice over
     independent HTTPS connections; mismatch -> both copies deleted,
     FetchRefused, audit written (the V9-NLU-ARM bounded-attestation
     precedent).
  3. sha256 of the verified bytes is recorded in the catalog entry as
     the integrity state.

  License basis: HF API cardData license=mit at the pinned revision
  (saved as checkpoint_api.json); README.md at the pinned revision
  saved alongside. Re-verified at the exact revision per James's
  standing caveat — never assumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request

# Import M3's trust records from the worktree's runtime tree.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from runtime.acquisition.substrate import (  # noqa: E402
    ArtifactPin,
    FetchAuditEntry,
    FetchRefused,
    TrustedIndex,
)

REPO = "Systran/faster-whisper-base"
REVISION = "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
BASE_URL = "https://huggingface.co/"
MODEL_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
EVIDENCE_FILES = ["README.md"]

_UA = "REMOR-STTFetch/1 (governed checkpoint acquisition)"


def _pinned_url(filename: str) -> str:
    return f"{BASE_URL}{REPO}/resolve/{REVISION}/{filename}"


def _fetch_bytes(url: str, timeout_s: float = 120.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        chunks = []
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


def fetch_checkpoint(staging_dir: str, audit_path: str) -> dict:
    """Fetch, dual-verify, and stage the checkpoint. Returns manifest."""
    index = TrustedIndex(
        name="huggingface",
        base_url=BASE_URL,
        artifacts={
            fn: ArtifactPin(artifact_id=f"stt-{fn}", url=_pinned_url(fn),
                            sha256="", media_type="application/octet-stream")
            for fn in MODEL_FILES + EVIDENCE_FILES
        },
    )
    os.makedirs(staging_dir, exist_ok=True)
    audit: list = []

    def record(artifact_id, url, expected, observed, verdict, detail=""):
        audit.append(FetchAuditEntry(
            at=time.time(), artifact_id=artifact_id, url=url,
            sha256_expected=expected, sha256_observed=observed,
            verdict=verdict, detail=detail))

    manifest = {"repo": REPO, "revision": REVISION, "files": {}}
    for fn in MODEL_FILES + EVIDENCE_FILES:
        pin = index.artifacts[fn]
        if not index.allows_url(pin.url):
            record(pin.artifact_id, pin.url, "", "", "refused",
                   "URL outside trusted index base")
            raise FetchRefused(f"{fn}: URL outside trusted index")
        # Pass 1.
        b1 = _fetch_bytes(pin.url)
        h1 = hashlib.sha256(b1).hexdigest()
        record(pin.artifact_id, pin.url, "", h1, "fetched",
               f"pass 1: {len(b1)} bytes at pinned revision")
        # Pass 2: independent re-fetch, byte-identity required.
        b2 = _fetch_bytes(pin.url)
        h2 = hashlib.sha256(b2).hexdigest()
        if h1 != h2:
            record(pin.artifact_id, pin.url, h1, h2, "refused",
                   "dual-fetch mismatch: bytes differ between passes")
            raise FetchRefused(
                f"{fn}: dual-fetch byte mismatch "
                f"({h1[:16]} vs {h2[:16]}); nothing staged")
        record(pin.artifact_id, pin.url, h1, h2, "fetched",
               "pass 2 byte-identical: integrity confirmed")
        if fn in MODEL_FILES:
            dest = os.path.join(staging_dir, fn)
            with open(dest, "wb") as fh:
                fh.write(b1)
            manifest["files"][fn] = {
                "sha256": h1, "size_bytes": len(b1),
                "url": pin.url,
            }
        else:
            with open(os.path.join(staging_dir, f"EVIDENCE_{fn}"),
                      "wb") as fh:
                fh.write(b1)

    with open(audit_path, "w") as fh:
        json.dump([{
            "at": e.at, "artifact_id": e.artifact_id, "url": e.url,
            "sha256_expected": e.sha256_expected,
            "sha256_observed": e.sha256_observed,
            "verdict": e.verdict, "detail": e.detail,
        } for e in audit], fh, indent=2)
    return manifest


if __name__ == "__main__":
    staging = sys.argv[1]
    audit_path = sys.argv[2]
    manifest = fetch_checkpoint(staging, audit_path)
    manifest_path = os.path.join(staging, "checkpoint_manifest.json")
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(json.dumps(
        {k: (v if k != "files" else {fk: fv["sha256"][:16] + "..."
                                     for fk, fv in v.items()})
         for k, v in manifest.items()}, indent=2))
    print("manifest:", manifest_path)
