"""runtime/acquisition/weights.py

QWEN3-ACQUIRE-1 — governed weight acquisition.

This module is an ADDITIVE extension of M3's governed substrate-fetch
channel (runtime/acquisition/substrate.py), not a second acquisition
pipeline. It reuses M3's trust records verbatim:

  * TrustedIndex / ArtifactPin — the allowlist + pinned hash
  * FetchRefused — fail-closed refusal
  * FetchAuditEntry — the audit trail

The trust properties are identical to the M3 channel:
  1. Trusted-index allowlist: the URL must be pinned in a configured
     trusted index AND share the index's base URL. Unknown hosts are
     refused pre-socket.
  2. Pinned hash: sha256 over the received bytes must equal the pin;
     mismatch -> bytes are discarded (never staged), fetch refused,
     audit entry written. This is the quarantine: a failed-verification
     file is never usable.
  3. Governor gate (optional): Effect.NETWORK checked pre-socket, same
     model as the M3 channel.
  4. Auditability: every attempt appends a FetchAuditEntry.

Why a separate fetch class instead of reusing GovernedFetchChannel
directly: the M3 channel is STRUCTURALLY data-only (8 MB cap, JSON/text/
markdown media types, whole body held in RAM). Those constraints are
load-bearing for M3's threat model and must not be weakened. A 5 GB
GGUF weight file cannot pass through it, and widening it would regress
M3's guarantee. Weights are still data — they are never exec'd, never
imported, never placed on sys.path; they are consumed only by an
external GGUF runtime (llama.cpp) behind the cognition teacher slot.
So this class keeps every trust property and changes only the
transport mechanics: streaming to disk with incremental hashing and a
weight-appropriate size cap.

Ownership: QWEN3-ACQUIRE-1 adds this file. M3's substrate.py is
consumed (records imported), never edited.
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from .substrate import (
    ArtifactPin,
    FetchAuditEntry,
    FetchRefused,
    TrustedIndex,
)

_WEIGHT_UA = "REMOR-WeightFetch/1 (governed weight acquisition)"


@dataclass(frozen=True)
class FetchedWeights:
    """Verified weight bytes on disk. The file is data: no exec/import
    path exists for it anywhere in this module."""

    artifact_id: str
    sha256: str
    size_bytes: int
    path: str
    provenance: dict


class GovernedWeightFetch:
    """Governed fetch for multi-GB weight files.

    Same trust contract as GovernedFetchChannel, streaming transport:
    bytes hash as they arrive; the file is staged ONLY after the
    observed sha256 matches the pin. Any mismatch deletes the partial
    file and refuses — quarantine by construction.
    """

    def __init__(self, indexes: Sequence[TrustedIndex], *,
                 governor: Any = None,
                 staging_dir: str,
                 timeout_s: float = 30.0,
                 max_bytes: int = 16 * 1024 * 1024 * 1024) -> None:
        self._indexes = list(indexes)
        self._governor = governor
        self._staging = staging_dir
        self._timeout = timeout_s
        self._max_bytes = max_bytes
        self._audit_entries: List[FetchAuditEntry] = []
        os.makedirs(self._staging, exist_ok=True)

    def audit_log(self) -> List[FetchAuditEntry]:
        return list(self._audit_entries)

    def _resolve(self, artifact_id: str) -> ArtifactPin:
        for index in self._indexes:
            pin = index.artifacts.get(artifact_id)
            if pin is not None:
                if not index.allows_url(pin.url):
                    raise FetchRefused(
                        f"artifact {artifact_id!r}: pinned URL outside its "
                        f"index base {index.base_url!r}")
                return pin
        raise FetchRefused(f"unknown artifact {artifact_id!r}: not pinned in "
                           f"any trusted index")

    def _audit(self, artifact_id: str, url: str, expected: str,
               observed: str, verdict: str, detail: str = "") -> None:
        self._audit_entries.append(FetchAuditEntry(
            at=time.time(), artifact_id=artifact_id, url=url,
            sha256_expected=expected, sha256_observed=observed,
            verdict=verdict, detail=detail))

    def _index_name_for(self, pin: ArtifactPin) -> str:
        for index in self._indexes:
            if pin.artifact_id in index.artifacts:
                return index.name
        return "?"

    def fetch(self, artifact_id: str) -> FetchedWeights:
        """Download, hash incrementally, verify, then stage.

        The partial file lives at <staging>/.partial-<artifact_id> and is
        renamed into place only on hash match. Mismatch -> partial
        deleted, refusal raised, audit written.
        """
        pin = self._resolve(artifact_id)
        if self._governor is not None:
            try:
                from swarm_engine.primitives.core import Effect
                self._governor.check(Effect.NETWORK, pin.url,
                                     primitive="weight_fetch")
            except Exception as exc:
                self._audit(artifact_id, pin.url, pin.sha256, "",
                            "refused", f"governor denied: {exc}")
                raise FetchRefused(
                    f"artifact {artifact_id!r}: governor denied network "
                    f"effect for {pin.url!r}") from exc

        partial = os.path.join(self._staging, f".partial-{artifact_id}")
        # A stale partial from a killed run is never trusted: restart.
        if os.path.exists(partial):
            os.remove(partial)

        digest = hashlib.sha256()
        received = 0
        req = urllib.request.Request(
            pin.url, headers={"User-Agent": _WEIGHT_UA})
        try:
            with urllib.request.urlopen(req,
                                        timeout=self._timeout) as resp, \
                    open(partial, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > self._max_bytes:
                        raise FetchRefused(
                            f"artifact {artifact_id!r}: body exceeds "
                            f"max_bytes ({self._max_bytes})")
                    digest.update(chunk)
                    fh.write(chunk)
        except FetchRefused:
            if os.path.exists(partial):
                os.remove(partial)
            self._audit(artifact_id, pin.url, pin.sha256, "",
                        "refused", "body exceeds max_bytes; partial deleted")
            raise
        except Exception as exc:
            if os.path.exists(partial):
                os.remove(partial)
            self._audit(artifact_id, pin.url, pin.sha256, "",
                        "refused", f"transport failed: {exc}; partial deleted")
            raise FetchRefused(
                f"artifact {artifact_id!r}: transport failed: {exc}") from exc

        observed = digest.hexdigest()
        if observed != pin.sha256.lower():
            os.remove(partial)
            self._audit(artifact_id, pin.url, pin.sha256, observed,
                        "refused",
                        "sha256 mismatch: bytes discarded, never staged")
            raise FetchRefused(
                f"artifact {artifact_id!r}: sha256 mismatch "
                f"(expected {pin.sha256[:16]}..., got {observed[:16]}...); "
                f"bytes discarded, never staged")

        staged = os.path.join(
            self._staging, f"{artifact_id}.{observed[:16]}.gguf")
        os.rename(partial, staged)
        self._audit(artifact_id, pin.url, pin.sha256, observed,
                    "fetched", f"staged {received} bytes after hash match")
        return FetchedWeights(
            artifact_id=artifact_id, sha256=observed,
            size_bytes=received, path=staged,
            provenance={"index": self._index_name_for(pin),
                         "url": pin.url,
                         "license_gate": "apache-2.0@Qwen/Qwen3-8B-GGUF"})


def huggingface_index(name: str = "huggingface",
                      artifacts: Optional[dict] = None) -> TrustedIndex:
    """A trusted index scoped to huggingface.co model file URLs."""
    return TrustedIndex(name=name,
                        base_url="https://huggingface.co/",
                        artifacts=artifacts or {})
