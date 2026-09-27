"""swarm_engine/acquisition/substrate.py

M3 — Substrate acquisition, data-only (LEARN/ADMIT).

A capability quarantined for a missing dependency triggers a governed fetch;
the fetched DATA is distilled into knowledge and tested for applicability;
applicable -> re-verify -> re-admit -> un-quarantine through the real
lifecycle edge; not applicable -> logged as not-applicable /
needs-improvement / not-functional and the capability remains quarantined,
honestly labeled.

James's standing rules encoded here:
* Two acquisition flavors: technique (M1/M2) and substrate (this module).
* The fetch channel is a GOVERNED EFFECT under the W4-R1 sandbox: trusted
  indexes, pinned hashes, sandbox-scoped staging. Arbitrary internet installs
  are a supply-chain surface -- the channel is authorized fetch, not a bypass.
* Internet boundary is DATA-ONLY, and it is structural, not a policy comment:
  fetched bytes enter as knowledge (parsed as JSON/text only); they are never
  exec'd, never imported, never placed on sys.path. Only synthesized
  capability derived from them can be admitted, through the real admission
  path. A compromised index can at worst teach badly -- the applicability
  test + re-verification catch that before admission.
* Honest failure: UNAVAILABLE names the exact missing piece (e.g. an
  executable package the data-only channel cannot provide). "Not available"
  is an experience record, not a terminal label.

Ownership: M3 owns this file. Frozen interfaces consumed (called, never
edited): quarantine reason API shape ``{reason, since, system}`` (M5
implements; this module ships a best-effort reader over today's free-text
stores behind the same protocol), ``effect_sandbox`` (read-only),
``media/wiring`` (read-only), the capability store / admission /
``effective_status`` / lifecycle APIs.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence

# ---------------------------------------------------------------------------
# Quarantine reason — frozen shape {reason, since, system}
# ---------------------------------------------------------------------------

# Structured reason prefix written by this module when it quarantines for a
# missing dependency. Format:
#   missing_dependency:<kind>:<name>[:detail]
# kind in {"data", "package", "model", "docs"}; detail is a JSON object for
# data kinds (e.g. {"requires_keys": [...], "artifact_id": "..."}).
MISSING_DEPENDENCY_PREFIX = "missing_dependency:"


@dataclass(frozen=True)
class QuarantineReason:
    """Frozen quarantine-reason shape: {reason, since, system}."""

    capability_id: str
    reason: str
    since: str = ""
    system: str = ""  # "lifecycle" | "store" | "trust" | "epistemic"


class QuarantineReasonReader(Protocol):
    """Protocol M5's real implementation satisfies. M3 builds against this."""

    def get(self, capability_id: str) -> Optional[QuarantineReason]:
        ...


@dataclass(frozen=True)
class DependencySpec:
    """A parsed missing-dependency declaration."""

    name: str
    kind: str  # "data" | "package" | "model" | "docs"
    detail: str = ""  # JSON object for data kinds


def parse_dependency_reason(reason: str) -> Optional[DependencySpec]:
    """Parse a structured missing-dependency reason. None if not one."""
    if not reason.startswith(MISSING_DEPENDENCY_PREFIX):
        return None
    parts = reason.split(":", 3)
    if len(parts) < 3:
        return None
    kind = parts[1].strip()
    name = parts[2].strip()
    detail = parts[3] if len(parts) > 3 else ""
    if kind not in ("data", "package", "model", "docs") or not name:
        return None
    return DependencySpec(name=name, kind=kind, detail=detail)


class StoreScanningReasonReader:
    """Best-effort reader over today's free-text stores.

    Reads the lifecycle transition log (latest hop TO quarantined carries the
    quarantining reason) and falls back to capability-store events. This is
    the adapter M5's structured implementation will replace; it satisfies
    the same QuarantineReasonReader protocol so the driver never changes.
    """

    def __init__(self, engine: Any):
        self._engine = engine

    def get(self, capability_id: str) -> Optional[QuarantineReason]:
        engine = self._engine
        # A quarantine reason is actionable only while the quarantine is in
        # force. The lifecycle log retains the historical quarantine hop after
        # a restore, so check the live effective status first; otherwise a
        # re-run after re-admission would resurrect a stale reason.
        try:
            from swarm_engine.synthesis.integrity import effective_status
            if effective_status(engine, capability_id)["effective"] != "quarantined":
                return None
        except Exception:
            pass
        # 1. Lifecycle log: the latest transition INTO quarantined.
        try:
            history = engine.lifecycle.history(capability_id)
            for tr in reversed(history):
                to_state = getattr(tr.to_state, "value", str(tr.to_state))
                if to_state == "quarantined":
                    return QuarantineReason(
                        capability_id=capability_id,
                        reason=tr.reason or "",
                        since=str(getattr(tr, "at", "")),
                        system="lifecycle",
                    )
        except Exception:
            pass
        # 2. Capability-store events: latest quarantine-ish event.
        try:
            events = engine.capabilities.events(capability_id, limit=50)
            for ev in reversed(events or []):
                if isinstance(ev, dict):
                    name, detail, at = (ev.get("event", ""), ev.get("detail", ""),
                                        ev.get("at", ""))
                else:
                    name, detail, at = (getattr(ev, "event", ""),
                                        getattr(ev, "detail", ""),
                                        getattr(ev, "at", ""))
                if "quarantin" in str(name).lower():
                    return QuarantineReason(
                        capability_id=capability_id,
                        reason=str(detail),
                        since=str(at),
                        system="store",
                    )
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Trusted index + pinned artifact
# ---------------------------------------------------------------------------

#: Media types the data-only channel will parse. Anything else is refused
#: structurally -- fetched bytes can never reach execution through this path.
DATA_MEDIA_TYPES = frozenset({"application/json", "text/plain", "text/markdown"})


@dataclass(frozen=True)
class ArtifactPin:
    """One fetchable DATA artifact: pinned hash, allowlisted media type."""

    artifact_id: str
    url: str
    sha256: str
    media_type: str = "application/json"


@dataclass(frozen=True)
class TrustedIndex:
    """A trusted index: name + base URL (host allowlist) + pinned artifacts."""

    name: str
    base_url: str
    artifacts: Dict[str, ArtifactPin] = field(default_factory=dict)

    def allows_url(self, url: str) -> bool:
        return url.startswith(self.base_url)


# ---------------------------------------------------------------------------
# Governed fetch channel
# ---------------------------------------------------------------------------

class FetchRefused(RuntimeError):
    """The governed channel refused a fetch. Fail closed, audited."""


@dataclass(frozen=True)
class FetchedArtifact:
    """Fetched DATA bytes. Structural data-only: no exec/import path exists.

    The staging directory is never added to sys.path anywhere in this
    module; bytes are exposed only via read_bytes()/read_text() for parsing
    as data.
    """

    artifact_id: str
    sha256: str
    media_type: str
    path: str
    provenance: Dict[str, str] = field(default_factory=dict)

    def read_bytes(self) -> bytes:
        with open(self.path, "rb") as fh:
            return fh.read()

    def read_text(self) -> str:
        return self.read_bytes().decode("utf-8")


@dataclass
class FetchAuditEntry:
    at: float
    artifact_id: str
    url: str
    sha256_expected: str
    sha256_observed: str
    verdict: str  # "fetched" | "refused"
    detail: str = ""


_FETCH_UA = "REMOR-SubstrateFetch/1 (governed data-only channel)"


class GovernedFetchChannel:
    """Authorized fetch, not a bypass. Trust properties (each tested):

    1. Trusted-index allowlist: the URL must be pinned in a configured
       trusted index AND share the index's base URL. Unknown hosts are
       refused pre-socket -- no network activity happens for them.
    2. Governor gate: when a governor is supplied, Effect.NETWORK is checked
       against the concrete URL before any socket activity (same model as
       NetworkSource: deny-by-default, audited).
    3. Pinned hash: sha256 over the received bytes must equal the pin;
       mismatch -> bytes discarded, fetch refused, audit entry written.
    4. Data-only structural: media type must be in DATA_MEDIA_TYPES; bytes
       land in a staging dir that is never on sys.path; this module has no
       exec()/import path for fetched bytes.
    5. Auditability: every attempt (fetch or refusal) appends a FetchAuditEntry.
    """

    def __init__(self, indexes: Sequence[TrustedIndex], *,
                 governor: Any = None,
                 staging_dir: str,
                 timeout_s: float = 8.0,
                 max_bytes: int = 8 * 1024 * 1024):
        self._indexes = list(indexes)
        self._governor = governor
        self._staging = staging_dir
        self._timeout = timeout_s
        self._max_bytes = max_bytes
        self._audit_entries: List[FetchAuditEntry] = []
        os.makedirs(self._staging, exist_ok=True)
        # Marker: this directory holds data only. Informational; the real
        # guarantee is that nothing here ever execs/imports these bytes.
        marker = os.path.join(self._staging, ".data_only")
        if not os.path.exists(marker):
            with open(marker, "w") as fh:
                fh.write("data-only staging: fetched bytes are never executed\n")

    def audit_log(self) -> List[FetchAuditEntry]:
        return list(self._audit_entries)

    def _index_name_for(self, pin: ArtifactPin) -> str:
        for index in self._indexes:
            if pin.artifact_id in index.artifacts:
                return index.name
        return "?"

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

    def fetch(self, artifact_id: str) -> FetchedArtifact:
        pin = self._resolve(artifact_id)
        # Pre-socket gates: host allowlist is structural (resolve already
        # enforced it); governor gate is the W4-R1 governed effect.
        if self._governor is not None:
            try:
                from swarm_engine.primitives.core import Effect
                self._governor.check(Effect.NETWORK, pin.url,
                                     primitive="substrate_fetch")
            except Exception as exc:  # PermissionError_ -> refused
                self._audit(artifact_id, pin.url, pin.sha256, "",
                            "refused", f"governor denied: {exc}")
                raise FetchRefused(
                    f"artifact {artifact_id!r}: governor denied network "
                    f"effect for {pin.url!r}") from exc
        if pin.media_type not in DATA_MEDIA_TYPES:
            self._audit(artifact_id, pin.url, pin.sha256, "",
                        "refused",
                        f"media type {pin.media_type!r} not data-only")
            raise FetchRefused(
                f"artifact {artifact_id!r}: media type {pin.media_type!r} "
                f"is not data-only; refusing structurally")
        req = urllib.request.Request(
            pin.url,
            headers={"User-Agent": _FETCH_UA, "Accept": pin.media_type})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                chunks: List[bytes] = []
                remaining = self._max_bytes + 1
                while remaining > 0:
                    chunk = resp.read(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                body = b"".join(chunks)
        except Exception as exc:
            self._audit(artifact_id, pin.url, pin.sha256, "",
                        "refused", f"transport failed: {exc}")
            raise FetchRefused(
                f"artifact {artifact_id!r}: transport failed: {exc}") from exc
        if len(body) > self._max_bytes:
            self._audit(artifact_id, pin.url, pin.sha256, "",
                        "refused", "body exceeds max_bytes")
            raise FetchRefused(f"artifact {artifact_id!r}: body too large")
        observed = hashlib.sha256(body).hexdigest()
        if observed != pin.sha256.lower():
            # Bytes are discarded (never staged) on hash mismatch.
            self._audit(artifact_id, pin.url, pin.sha256, observed,
                        "refused", "sha256 mismatch: bytes discarded")
            raise FetchRefused(
                f"artifact {artifact_id!r}: sha256 mismatch "
                f"(expected {pin.sha256[:16]}..., got {observed[:16]}...); "
                f"bytes discarded, never staged")
        # Stage as data. The staging dir is never on sys.path.
        staging_abs = os.path.abspath(self._staging)
        if any(os.path.abspath(p) == staging_abs for p in sys.path):
            raise FetchRefused("staging dir is on sys.path: refusing")
        fname = f"{artifact_id}.{observed[:16]}.data"
        path = os.path.join(self._staging, fname)
        with open(path, "wb") as fh:
            fh.write(body)
        self._audit(artifact_id, pin.url, pin.sha256, observed,
                    "fetched", f"staged {len(body)} bytes as data")
        return FetchedArtifact(
            artifact_id=artifact_id, sha256=observed,
            media_type=pin.media_type, path=path,
            provenance={"index": self._index_name_for(pin),
                         "url": pin.url})


# ---------------------------------------------------------------------------
# Knowledge distillation (data in, data out)
# ---------------------------------------------------------------------------

class DistillRefused(RuntimeError):
    """Fetched bytes could not be distilled into knowledge as data."""


@dataclass(frozen=True)
class KnowledgeRecord:
    """Distilled knowledge: parsed data + provenance. Data only."""

    artifact_id: str
    sha256: str
    media_type: str
    data: Any
    provenance: Dict[str, str] = field(default_factory=dict)


class KnowledgeDistiller:
    """Parse fetched bytes into knowledge. JSON/text only -- structural.

    Anything that is not parseable as data is refused; there is no code
    path for fetched bytes anywhere in this module.
    """

    def distill(self, fetched: FetchedArtifact) -> KnowledgeRecord:
        if fetched.media_type not in DATA_MEDIA_TYPES:
            raise DistillRefused(
                f"media type {fetched.media_type!r} is not data-only")
        raw = fetched.read_text()
        if fetched.media_type == "application/json":
            try:
                data = json.loads(raw)
            except Exception as exc:
                raise DistillRefused(
                    f"artifact {fetched.artifact_id!r} is not valid JSON: "
                    f"{exc}") from exc
        else:
            data = raw
        return KnowledgeRecord(
            artifact_id=fetched.artifact_id, sha256=fetched.sha256,
            media_type=fetched.media_type, data=data,
            provenance=dict(fetched.provenance))


# ---------------------------------------------------------------------------
# Applicability test
# ---------------------------------------------------------------------------

APPLICABILITY_LABELS = ("applicable", "not-applicable",
                        "needs-improvement", "not-functional")


@dataclass(frozen=True)
class ApplicabilityVerdict:
    applicable: bool
    label: str  # one of APPLICABILITY_LABELS
    detail: str = ""


class ApplicabilityTest(Protocol):
    def test(self, knowledge: KnowledgeRecord,
             dependency: DependencySpec) -> ApplicabilityVerdict:
        ...


class SchemaApplicabilityTest:
    """The dependency declares what the data must contain; the knowledge
    must actually contain it. dependency.detail is JSON, e.g.::

        {"requires_keys": ["hello", "bye"],
         "requires_types": {"hello": "str"}}

    Labels: missing keys -> "not-applicable" (wrong data for this
    dependency); present-but-wrong-typed -> "needs-improvement"; parseable
    but empty/degenerate -> "not-functional".
    """

    def test(self, knowledge: KnowledgeRecord,
             dependency: DependencySpec) -> ApplicabilityVerdict:
        try:
            spec = json.loads(dependency.detail) if dependency.detail else {}
        except Exception:
            spec = {}
        requires_keys = spec.get("requires_keys", [])
        requires_types = spec.get("requires_types", {})
        data = knowledge.data
        if not isinstance(data, dict):
            return ApplicabilityVerdict(
                False, "not-applicable",
                f"knowledge data is {type(data).__name__}, need an object "
                f"with keys {requires_keys}")
        missing = [k for k in requires_keys if k not in data]
        if missing:
            return ApplicabilityVerdict(
                False, "not-applicable",
                f"missing required keys: {missing}")
        _TYPEMAP = {"str": str, "int": int, "float": float,
                    "list": list, "dict": dict, "bool": bool}
        mistyped = {k: type(data[k]).__name__
                    for k, t in requires_types.items()
                    if k in data and not isinstance(data[k], _TYPEMAP.get(t, object))}
        if mistyped:
            return ApplicabilityVerdict(
                False, "needs-improvement",
                f"keys present but wrongly typed: {mistyped}")
        if not data:
            return ApplicabilityVerdict(
                False, "not-functional", "knowledge data is empty")
        return ApplicabilityVerdict(
            True, "applicable",
            f"all {len(requires_keys)} required keys present with "
            f"correct types")


# ---------------------------------------------------------------------------
# The driver: quarantine reason -> governed fetch -> distill -> applicability
#               -> re-verify -> re-admit -> un-quarantine (or honest label)
# ---------------------------------------------------------------------------

ATTEMPT_OUTCOMES = ("readmitted", "still_quarantined", "unavailable",
                    "not_applicable")


@dataclass
class AttemptResult:
    capability_id: str
    outcome: str  # one of ATTEMPT_OUTCOMES
    label: str = ""     # applicability label when relevant
    detail: str = ""
    audit: List[Dict[str, Any]] = field(default_factory=list)


class SubstrateAcquisitionDriver:
    """The M3 loop driver.

    Consumes the frozen quarantine-reason shape, fetches through the
    governed channel, distills to knowledge, tests applicability, and on
    success re-verifies and walks the real QUARANTINED -> ... -> DEPLOYED
    lifecycle edge via restore_everywhere. On failure the capability stays
    quarantined with an honest, logged label.
    """

    def __init__(self, engine: Any, *,
                 channel: GovernedFetchChannel,
                 reason_reader: Optional[QuarantineReasonReader] = None,
                 applicability: Optional[ApplicabilityTest] = None,
                 caller: Any = None):
        self._engine = engine
        self._channel = channel
        self._reader = reason_reader or StoreScanningReasonReader(engine)
        self._applicability = applicability or SchemaApplicabilityTest()
        self._caller = caller if caller is not None else getattr(
            engine, "oracle", None)

    def _log_attempt(self, capability_id: str, event: str,
                     detail: str) -> None:
        try:
            self._engine.capabilities.log(capability_id, event, detail)
        except Exception:
            pass

    def attempt(self, capability_id: str, *,
                artifact_id: Optional[str] = None) -> AttemptResult:
        engine = self._engine
        # 1. Quarantine reason (frozen shape).
        qreason = self._reader.get(capability_id)
        if qreason is None:
            return AttemptResult(capability_id, "not_applicable", "",
                                 "no quarantine reason found; nothing to acquire")
        dependency = parse_dependency_reason(qreason.reason)
        if dependency is None:
            return AttemptResult(
                capability_id, "not_applicable", "",
                f"quarantine reason is not a missing-dependency reason: "
                f"{qreason.reason[:120]!r}")
        # 2. Data-only boundary: only DATA kinds can cross. Packages, models
        #    (weights are data only if served as data AND applicable -- model
        #    *code*/runners are not), and docs-as-code are UNAVAILABLE with
        #    the exact missing piece named. This is honest, not a terminal
        #    label: it names what would be needed.
        if dependency.kind != "data":
            detail = (
                f"dependency {dependency.name!r} is kind {dependency.kind!r}: "
                f"the data-only channel cannot provide executable substrate. "
                f"Missing piece: an installable {dependency.kind} artifact "
                f"for {dependency.name!r} from a trusted source")
            self._log_attempt(capability_id, "substrate_attempt_unavailable",
                              detail)
            return AttemptResult(capability_id, "unavailable", "", detail)
        # 3. Governed fetch.
        aid = artifact_id or dependency.name
        try:
            fetched = self._channel.fetch(aid)
        except FetchRefused as exc:
            detail = f"governed fetch refused: {exc}"
            self._log_attempt(capability_id, "substrate_fetch_refused", detail)
            return AttemptResult(capability_id, "still_quarantined", "", detail)
        # 4. Distill to knowledge (data in, data out).
        try:
            knowledge = KnowledgeDistiller().distill(fetched)
        except DistillRefused as exc:
            detail = f"distillation refused: {exc}"
            self._log_attempt(capability_id, "substrate_distill_refused", detail)
            return AttemptResult(capability_id, "still_quarantined",
                                 "not-functional", detail)
        # 5. Applicability test.
        verdict = self._applicability.test(knowledge, dependency)
        if not verdict.applicable:
            detail = (f"applicability {verdict.label}: {verdict.detail}; "
                      f"capability remains quarantined")
            self._log_attempt(capability_id, "substrate_not_applicable", detail)
            return AttemptResult(capability_id, "still_quarantined",
                                 verdict.label, detail)
        # 6. Re-verify + re-admit through the real lifecycle edge.
        from swarm_engine.synthesis.integrity import (
            restore_everywhere, RestoreRefused)
        try:
            restored = restore_everywhere(
                engine, capability_id, caller=self._caller,
                reason=(f"substrate acquired: {dependency.name!r} fetched "
                        f"from trusted index "
                        f"{fetched.provenance.get('index', '?')} "
                        f"(sha256 {fetched.sha256[:16]}...), applicability "
                        f"{verdict.label}"))
        except (RestoreRefused, Exception) as exc:
            detail = (f"applicability passed but re-admission refused: "
                      f"{type(exc).__name__}: {exc}")
            self._log_attempt(capability_id, "substrate_readmit_refused", detail)
            return AttemptResult(capability_id, "still_quarantined",
                                 verdict.label, detail)
        detail = (f"readmitted: {dependency.name!r} acquired as data "
                  f"(sha256 {fetched.sha256[:16]}...), applicability "
                  f"{verdict.label}, lifecycle walk: "
                  f"{restored.get('lifecycle', '?')}")
        self._log_attempt(capability_id, "substrate_readmitted", detail)
        return AttemptResult(capability_id, "readmitted", verdict.label,
                             detail,
                             audit=[{"artifact_id": fetched.artifact_id,
                                     "sha256": fetched.sha256,
                                     "provenance": fetched.provenance}])
