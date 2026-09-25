"""
swarm_engine/benchmarking.py

Replaces the fabricated "Swarm AI vs GPT-4o/Claude/Llama" and "Quantum
Optimization" comparison charts. Those charts plotted hand-typed numbers
(`# Industry Averages/Approximations`) next to output from agents whose
execute_task() never did any real work — so neither side of the
comparison was measuring anything real.

This module only measures what can actually be measured here: real wall
-clock latency of the real (non-mocked) BaseAgent dispatching real tasks,
logged to the KnowledgeBase so the numbers are reproducible and inspectable
rather than typed into a list.

It deliberately does NOT produce a "Swarm vs GPT-4o" chart. Getting an
honest number for that requires actually calling those APIs with your own
keys — I don't have them, and typing in plausible-looking numbers would
just be the same fabrication with a different author. run_external_comparison()
below shows where and how to plug real API calls in if you want that chart
later; it raises rather than fake it.
"""
import statistics
import time
from typing import List

from swarm_engine.agents.base_agent import AgentRegistry
from swarm_engine.memory.knowledge_base import KnowledgeBase


async def run_benchmark(task_specs: List[dict], iterations: int = 20, db_path: str = "swarm_engine.db") -> dict:
    """Runs each task spec `iterations` times through a real agent and
    records actual elapsed time per call. task_specs is a list of
    {"type": ..., "payload": ...} dicts — the same shape execute_task expects.

    Returns per-task-type real statistics (min/median/p95/max), computed
    from actual measured latencies, not simulated. Call with `await` — in
    a Colab cell that's just `await run_benchmark([...])`.
    """
    kb = KnowledgeBase(db_path=db_path)
    registry = AgentRegistry(knowledge_base=kb)
    agent = registry.spawn(0)

    results_by_type = {}
    for spec in task_specs:
        task_type = spec["type"]
        latencies = []
        failures = 0
        for _ in range(iterations):
            start = time.perf_counter()
            result = await agent.execute_task(spec)
            latencies.append(time.perf_counter() - start)
            if not result["success"]:
                failures += 1
        latencies.sort()
        results_by_type[task_type] = {
            "n": len(latencies),
            "failures": failures,
            "min_sec": round(latencies[0], 6),
            "median_sec": round(statistics.median(latencies), 6),
            "p95_sec": round(latencies[int(len(latencies) * 0.95) - 1], 6),
            "max_sec": round(latencies[-1], 6),
        }
    return results_by_type


def run_external_comparison(*args, **kwargs):
    """Intentionally not implemented with placeholder numbers.

    To make this real: call the actual GPT-4o / Claude / Llama APIs with
    your own credentials on the same task_specs used above, time the real
    calls the same way, and merge the results with run_benchmark()'s
    output. Until real credentials + real calls are wired in here, no
    comparison chart should claim to represent those systems.
    """
    raise NotImplementedError(
        "No real external API calls are wired in. Fabricating comparison "
        "numbers here would recreate the exact problem this module was "
        "written to fix — plug in real API calls with your own keys."
    )
