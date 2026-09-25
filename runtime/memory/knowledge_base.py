"""
swarm_engine/memory/knowledge_base.py

Persistent memory for the engine. Two things live here that matter for
self-improvement:

1. `capabilities` — every piece of code the CapabilitySynthesizer has ever
   written, keyed by a deterministic spec hash. If the same *structural*
   request comes in again, the engine reuses the stored capability instead
   of re-synthesizing it from scratch. This is the "doesn't outsource,
   doesn't redo work" part.

2. `generation_log` — every attempt (success or failure) tagged with the
   body_plan/features that were used, so the synthesizer can check
   "how has this pattern performed historically" and bias future synthesis
   (e.g. away from a body plan that keeps failing watertightness).
"""
import sqlite3
import json
import time


class KnowledgeBase:
    def __init__(self, db_path="swarm_engine.db"):
        self.db_path = db_path
        self._init_schema()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS capabilities (
                    capability_id TEXT PRIMARY KEY,
                    spec_json TEXT NOT NULL,
                    code TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    use_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS generation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capability_id TEXT,
                    prompt TEXT,
                    body_plan TEXT,
                    passed INTEGER,
                    feedback TEXT,
                    timestamp REAL
                )
            """)

    # --- capability storage / reuse -------------------------------------

    def get_capability(self, capability_id: str):
        with self._conn() as c:
            row = c.execute(
                "SELECT spec_json, code, use_count, success_count, fail_count "
                "FROM capabilities WHERE capability_id = ?",
                (capability_id,),
            ).fetchone()
        if not row:
            return None
        spec_json, code, use_count, success_count, fail_count = row
        return {
            "capability_id": capability_id,
            "spec": json.loads(spec_json),
            "code": code,
            "use_count": use_count,
            "success_count": success_count,
            "fail_count": fail_count,
        }

    def store_capability(self, capability_id: str, spec: dict, code: str):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO capabilities "
                "(capability_id, spec_json, code, created_at) VALUES (?, ?, ?, ?)",
                (capability_id, json.dumps(spec), code, time.time()),
            )

    def record_use(self, capability_id: str, success: bool):
        with self._conn() as c:
            if success:
                c.execute(
                    "UPDATE capabilities SET use_count = use_count + 1, "
                    "success_count = success_count + 1 WHERE capability_id = ?",
                    (capability_id,),
                )
            else:
                c.execute(
                    "UPDATE capabilities SET use_count = use_count + 1, "
                    "fail_count = fail_count + 1 WHERE capability_id = ?",
                    (capability_id,),
                )

    def log_generation(self, capability_id, prompt, body_plan, passed, feedback):
        with self._conn() as c:
            c.execute(
                "INSERT INTO generation_log "
                "(capability_id, prompt, body_plan, passed, feedback, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (capability_id, prompt, body_plan, int(passed), json.dumps(feedback), time.time()),
            )

    def body_plan_success_rate(self, body_plan: str):
        """Historical pass rate for a given body plan, used by the
        synthesizer to bias generation (e.g. add reinforcement geometry
        to body plans that have a track record of failing critique)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*), SUM(passed) FROM generation_log WHERE body_plan = ?",
                (body_plan,),
            ).fetchone()
        total, passed = row
        if not total:
            return None
        return (passed or 0) / total

    def stats(self):
        with self._conn() as c:
            n_caps = c.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0]
            n_logs = c.execute("SELECT COUNT(*) FROM generation_log").fetchone()[0]
        return {"capabilities": n_caps, "generations_logged": n_logs}
