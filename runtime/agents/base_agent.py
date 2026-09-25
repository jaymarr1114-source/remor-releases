"""
swarm_engine/agents/base_agent.py

Replaces the old MinimalAgent/FeatureAgent stubs, whose execute_task()
never inspected the task at all:

    async def _execute_simple(self, parsed):
        return {"completed": True, "agent": self.agent_id}   # old code

    async def execute_task(self, task):                       # old FeatureAgent
        await asyncio.sleep(0.1)
        return {"status": "Optimal Path Calculated", ...}      # ignores task

This BaseAgent actually looks at task["type"] and dispatches to a real
handler that does real work on task["payload"]. If a task type has no
registered handler, it reports failure honestly instead of returning a
canned success — a no-op pretending to be a result is worse than an
explicit "I can't do this yet."

Built-in capabilities:
  math            -> safe arithmetic evaluation (math_ops.safe_eval)
  text_analysis   -> real text statistics (text_ops.analyze)
  forge           -> routes to the real ForgeModule (synthesized 3D assets)
  knowledge_stats -> real read from the KnowledgeBase

Agents remain extensible: register_capability() lets you add real handlers
without touching the dispatch loop.
"""
import time
from typing import Any, Callable, Dict

from swarm_engine.agents.capabilities import math_ops, text_ops
from swarm_engine.memory.knowledge_base import KnowledgeBase


class TaskExecutionError(Exception):
    pass


class BaseAgent:
    def __init__(self, agent_id: int, knowledge_base: KnowledgeBase = None, forge_module=None):
        self.agent_id = agent_id
        self.status = "ready"
        self.kb = knowledge_base or KnowledgeBase()
        self._forge_module = forge_module  # lazy — only imports trimesh if actually used
        self.capabilities: Dict[str, Callable[[dict], Any]] = {
            "math": self._handle_math,
            "text_analysis": self._handle_text,
            "forge": self._handle_forge,
            "knowledge_stats": self._handle_knowledge_stats,
        }

    def register_capability(self, task_type: str, handler: Callable[[dict], Any]):
        self.capabilities[task_type] = handler

    async def execute_task(self, task: dict) -> dict:
        self.status = "working"
        task_type = task.get("type")
        started = time.time()

        handler = self.capabilities.get(task_type)
        if handler is None:
            self.status = "ready"
            return {
                "success": False,
                "agent": self.agent_id,
                "error": f"No capability registered for task type {task_type!r}. "
                         f"Available: {sorted(self.capabilities)}",
                "elapsed_sec": round(time.time() - started, 4),
            }

        try:
            output = handler(task.get("payload", {}))
            self.kb.log_generation(
                capability_id=f"agent_{task_type}",
                prompt=str(task.get("payload", "")),
                body_plan=task_type,
                passed=True,
                feedback=["ok"],
            )
            return {
                "success": True,
                "agent": self.agent_id,
                "result": output,
                "elapsed_sec": round(time.time() - started, 4),
            }
        except Exception as e:
            self.kb.log_generation(
                capability_id=f"agent_{task_type}",
                prompt=str(task.get("payload", "")),
                body_plan=task_type,
                passed=False,
                feedback=[str(e)],
            )
            return {
                "success": False,
                "agent": self.agent_id,
                "error": str(e),
                "elapsed_sec": round(time.time() - started, 4),
            }
        finally:
            self.status = "ready"

    # --- real handlers -----------------------------------------------

    def _handle_math(self, payload: dict):
        expression = payload.get("expression")
        if not expression:
            raise TaskExecutionError("math task requires payload['expression']")
        return {"expression": expression, "value": math_ops.safe_eval(expression)}

    def _handle_text(self, payload: dict):
        text = payload.get("text")
        if not text:
            raise TaskExecutionError("text_analysis task requires payload['text']")
        return text_ops.analyze(text)

    def _handle_forge(self, payload: dict):
        prompt = payload.get("prompt")
        if not prompt:
            raise TaskExecutionError("forge task requires payload['prompt']")
        if self._forge_module is None:
            from swarm_engine.forge.module import ForgeModule
            self._forge_module = ForgeModule(db_path=self.kb.db_path)
        output_file = payload.get("output_file", f"agent{self.agent_id}_forge.obj")
        return self._forge_module.generate_3d_asset(prompt, output_file=output_file)

    def _handle_knowledge_stats(self, payload: dict):
        return self.kb.stats()


class AgentRegistry:
    """Real registry — tracks which agents exist and what they've actually
    handled, backed by the same KnowledgeBase every agent logs to."""

    def __init__(self, knowledge_base: KnowledgeBase = None):
        self.kb = knowledge_base or KnowledgeBase()
        self.agents: Dict[int, BaseAgent] = {}

    def spawn(self, agent_id: int) -> BaseAgent:
        agent = BaseAgent(agent_id=agent_id, knowledge_base=self.kb)
        self.agents[agent_id] = agent
        return agent

    def get(self, agent_id: int) -> BaseAgent:
        return self.agents.get(agent_id) or self.spawn(agent_id)

    def find_by_capability(self, task_type: str):
        return [a for a in self.agents.values() if task_type in a.capabilities]
