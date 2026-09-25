"""
swarm_engine/capability/information_requirement.py

InformationRequirement: the same honest, generic pattern as
CapabilityRequirement, applied to "I need to know something" rather than
"I need to be able to do something."

Formulated FROM a detected insufficiency — never authored by a human ahead
of time. The `query` field is mechanically derived from the requirement's
own description and target_domain, not hand-written per task.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class InformationRequirement:
    query: str
    reason: str
    target_domain: Optional[str] = None
    origin: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    attempts: int = 0
    requirement_id: str = field(default="")
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.requirement_id:
            basis = f"{self.query}|{self.target_domain}|{time.time()}"
            self.requirement_id = "info_" + hashlib.sha256(
                basis.encode()).hexdigest()[:16]

    def as_dict(self) -> Dict[str, Any]:
        return {"requirement_id": self.requirement_id, "query": self.query,
                "reason": self.reason, "target_domain": self.target_domain,
                "origin": self.origin, "status": self.status,
                "attempts": self.attempts, "created_at": self.created_at}
