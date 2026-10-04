"""Append-only ledger of curiosity-initiated inquiry mintings.

The ledger is the causal anchor for C-1.3's negative: an inquiry is
curiosity-initiated if and only if its trigger_id was minted here, with
primary_request_id=None and livepath_involvement=False. A trigger that
claims PRIMARY_REQUESTED but appears in this ledger is forged; a trigger
that claims CURIOUSITY_INITIATED but has no ledger record was not minted
by this path. Both are refused by verify_origin().
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional

#: The frozen origin spelling (boundary.py ORIGINS). Used as-is; the
#: misspelling is a frozen-vocabulary fact, not ours to repair.
ORIGIN_INITIATED = "CURIOUSITY_INITIATED"
ORIGIN_PRIMARY_REQUESTED = "PRIMARY_REQUESTED"


class OriginForged(Exception):
    """A trigger's claimed origin does not match its causal record."""


@dataclass(frozen=True)
class InitiationRecord:
    """One minting event. Frozen: the causal record is not editable."""
    trigger_id: str
    minted_at: float
    boundary_class: str
    bounded_objective: str
    origin: str
    primary_request_id: Optional[str]  # always None: no Primary request exists
    livepath_involvement: bool  # always False: the LivePath was never touched


class InitiationLedger:
    """Append-only JSONL ledger. Fail-closed on malformed rows."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._records: Dict[str, InitiationRecord] = {}
        if self._path.exists():
            for line in self._path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    rec = InitiationRecord(**row)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise OriginForged(
                        f"initiation ledger {self._path} has a malformed "
                        f"row; refusing to trust it: {exc}") from exc
                if rec.trigger_id in self._records:
                    raise OriginForged(
                        f"initiation ledger {self._path} has a duplicate "
                        f"trigger_id {rec.trigger_id!r}; refusing to trust it")
                self._records[rec.trigger_id] = rec

    def mint(self, *, trigger_id: str, boundary_class: str,
             bounded_objective: str) -> InitiationRecord:
        """Record one minting. The origin is always the initiated one."""
        if not (trigger_id or "").strip():
            raise OriginForged("cannot mint an initiation record without a trigger_id")
        if not (boundary_class or "").strip():
            raise OriginForged("cannot mint an initiation record without a boundary_class")
        if not (bounded_objective or "").strip():
            raise OriginForged("cannot mint an initiation record without a bounded_objective")
        if trigger_id in self._records:
            raise OriginForged(
                f"trigger_id {trigger_id!r} already minted; refusing double-mint")
        rec = InitiationRecord(
            trigger_id=trigger_id,
            minted_at=time.time(),
            boundary_class=boundary_class,
            bounded_objective=bounded_objective,
            origin=ORIGIN_INITIATED,
            primary_request_id=None,
            livepath_involvement=False,
        )
        with self._path.open("a") as fh:
            fh.write(json.dumps(asdict(rec)) + "\n")
        self._records[trigger_id] = rec
        return rec

    def lookup(self, trigger_id: str) -> Optional[InitiationRecord]:
        return self._records.get(trigger_id)

    def verify_origin(self, trigger_id: str, claimed_origin: str) -> InitiationRecord:
        """Cross-check a claimed origin against the causal record.

        Raises OriginForged when the claim does not match reality:
        - claimed PRIMARY_REQUESTED but the trigger was minted here
          (forgery: curiosity cannot mint Primary requests);
        - claimed CURIOUSITY_INITIATED but no mint record exists
          (unminted: not from this path).
        Returns the record when the claim is honest.
        """
        rec = self._records.get(trigger_id)
        if claimed_origin == ORIGIN_PRIMARY_REQUESTED and rec is not None:
            raise OriginForged(
                f"trigger {trigger_id!r} claims {ORIGIN_PRIMARY_REQUESTED} "
                f"but was minted by the curiosity-initiated path at "
                f"{rec.minted_at}: forged origin refused")
        if claimed_origin == ORIGIN_INITIATED and rec is None:
            raise OriginForged(
                f"trigger {trigger_id!r} claims {ORIGIN_INITIATED} but has "
                f"no initiation record: unminted origin refused")
        if rec is None:
            raise OriginForged(
                f"trigger {trigger_id!r}: no causal record either way; "
                f"claimed origin {claimed_origin!r} refused")
        return rec
