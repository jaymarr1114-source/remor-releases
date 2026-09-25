"""Performance log: evidence for assignment, never a ranking score.

Records raw counts per (template, problem_class): attempted, completed,
verified. This is evidence the organization may consult when assigning --
it is deliberately NOT a score, NOT a ranking, and NOT an admission input.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from swarm_engine.agent_org.store import OrgStore, now


class PerformanceLog:
    def __init__(self, store: OrgStore):
        self.store = store

    def record(self, agent_id: str, template_id: str, problem_class: str,
               outcome: str, verified: bool,
               resource_usage: Dict[str, Any]) -> None:
        """outcome: one of completed|failed|rejected|interrupted..."""
        self.store.insert("ao_performance", {
            "agent_id": agent_id, "template_id": template_id,
            "problem_class": problem_class, "outcome": str(outcome),
            "verified": "1" if verified else "0",
            "resource_usage_json": json.dumps(resource_usage, sort_keys=True,
                                              default=str),
            "created_at": now()})

    def template_stats(self, template_id: str,
                       problem_class: str) -> Dict[str, int]:
        rows = [r for r in self.store.rows("ao_performance")
                if r["template_id"] == template_id
                and r["problem_class"] == problem_class]
        attempted = len(rows)
        completed = sum(1 for r in rows if r["outcome"] == "completed")
        verified = sum(1 for r in rows if r["verified"] == "1")
        return {"attempted": attempted, "completed": completed,
                "verified": verified}

    def agent_history(self, agent_id: str) -> List[Dict[str, Any]]:
        rows = self.store.rows("ao_performance", "agent_id", agent_id)
        return [{
            "agent_id": r["agent_id"], "template_id": r["template_id"],
            "problem_class": r["problem_class"], "outcome": r["outcome"],
            "verified": r["verified"] == "1",
            "resource_usage": json.loads(r["resource_usage_json"]),
            "created_at": r["created_at"],
        } for r in rows]
