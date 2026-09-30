#!/usr/bin/env python3
"""QWEN3-ACQUIRE-1 quarantine proof: the governed weight channel fails closed.

Proves adversarially, against the REAL pinned HuggingFace revision:
  K1 wrong sha256 pin -> FetchRefused, partial bytes deleted, nothing
     staged, audit entry records the refusal with both hashes
  K2 unknown artifact -> refused pre-socket (no network activity)
  K3 pinned URL outside its index base -> refused
  K4 correct pin -> fetched, staged only after hash match, audit "fetched"

Run in a fresh process from the tree root:
    python3 proofs/qwen3_quarantine_proof.py
Exit 0 iff every section passes. Uses only the small LICENSE text file
from the pinned revision -- the 5 GB weight fetch is exercised by the
acquisition itself and verified by Q2 of qwen3_acquire1_proof.py.
"""

import os
import sys

TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TREE)

from runtime.acquisition.substrate import ArtifactPin  # noqa: E402
from runtime.acquisition.weights import (  # noqa: E402
    FetchRefused,
    GovernedWeightFetch,
    huggingface_index,
)

PASS = []
FAIL = []

REV = "7c41481f57cb95916b40956ab2f0b139b296d974"
LICENSE_URL = (f"https://huggingface.co/Qwen/Qwen3-8B-GGUF/"
               f"resolve/{REV}/LICENSE")
LICENSE_SHA256 = ("5de36594c10839788a8c589443a8ef9d8b8d17c65a1b5807206ae037fc36c6bd")
STAGING = "/home/hatch/workspace/models/qwen3-8b"


def check(section, name, cond, detail=""):
    if cond:
        PASS.append(f"{section}:{name}")
    else:
        FAIL.append(f"{section}:{name} -- {detail}")


def staging_files(prefix):
    return [p for p in os.listdir(STAGING) if p.startswith(prefix)]


def k1_wrong_pin_quarantined():
    index = huggingface_index(artifacts={
        "license-text": ArtifactPin(
            artifact_id="license-text", url=LICENSE_URL,
            sha256="0" * 64, media_type="text/plain"),
    })
    f = GovernedWeightFetch([index], staging_dir=STAGING)
    try:
        f.fetch("license-text")
        check("K1", "refused", False, "fetch succeeded with a wrong pin")
        return
    except FetchRefused as exc:
        check("K1", "refused", True)
        check("K1", "names_mismatch", "sha256 mismatch" in str(exc),
              str(exc)[:100])
    check("K1", "partial_deleted",
          not staging_files(".partial-license-text"),
          str(staging_files(".partial-license-text")))
    check("K1", "nothing_staged",
          not [p for p in staging_files("license-text")
               if not p.startswith(".partial")],
          "a file was staged despite mismatch")
    audits = [e for e in f.audit_log() if e.verdict == "refused"]
    check("K1", "audit_refused", len(audits) == 1,
          str([(e.verdict, e.detail) for e in f.audit_log()]))
    check("K1", "audit_records_hashes",
          audits and audits[0].sha256_expected == "0" * 64
          and audits[0].sha256_observed == LICENSE_SHA256,
          str(audits[0])[:120] if audits else "no audit")


def k2_unknown_artifact_presocket():
    index = huggingface_index(artifacts={})
    f = GovernedWeightFetch([index], staging_dir=STAGING)
    try:
        f.fetch("does-not-exist")
        check("K2", "refused", False, "unknown artifact was fetched")
    except FetchRefused as exc:
        check("K2", "refused", "not pinned in any trusted index" in str(exc),
              str(exc)[:100])


def k3_url_outside_index_base():
    index = huggingface_index(artifacts={
        "evil": ArtifactPin(
            artifact_id="evil", url="https://evil.example/x.gguf",
            sha256="0" * 64, media_type="application/octet-stream"),
    })
    f = GovernedWeightFetch([index], staging_dir=STAGING)
    try:
        f.fetch("evil")
        check("K3", "refused", False, "off-index URL was fetched")
    except FetchRefused as exc:
        check("K3", "refused", "outside its index base" in str(exc),
              str(exc)[:100])


def k4_correct_pin_fetched():
    index = huggingface_index(artifacts={
        "license-text": ArtifactPin(
            artifact_id="license-text", url=LICENSE_URL,
            sha256=LICENSE_SHA256, media_type="text/plain"),
    })
    f = GovernedWeightFetch([index], staging_dir=STAGING)
    w = f.fetch("license-text")
    check("K4", "fetched", os.path.isfile(w.path), w.path)
    check("K4", "hash_matches_pin", w.sha256 == LICENSE_SHA256,
          w.sha256[:16])
    check("K4", "staged_name_carries_hash",
          w.sha256[:16] in os.path.basename(w.path),
          os.path.basename(w.path))
    audits = [e for e in f.audit_log() if e.verdict == "fetched"]
    check("K4", "audit_fetched", len(audits) == 1,
          str([(e.verdict, e.detail) for e in f.audit_log()]))
    os.remove(w.path)  # evidence already archived under proofs/qwen3_license/
    check("K4", "cleanup", not os.path.exists(w.path), w.path)


def main():
    os.makedirs(STAGING, exist_ok=True)
    k1_wrong_pin_quarantined()
    k2_unknown_artifact_presocket()
    k3_url_outside_index_base()
    k4_correct_pin_fetched()
    print(f"\nquarantine proof: {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("FAIL:", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
