"""Shared preamble for PLUGIN-1 proof probes (fresh process each)."""
import json
import os
import sys

WT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(WT, "pylib"))

SCRATCH = os.environ["PLUGIN1_SCRATCH"]
REGISTRY = os.path.join(SCRATCH, "registry")
FIX = os.path.join(SCRATCH, "fixtures")


def check(cond, msg):
    if not cond:
        print(f"CHECK FAILED: {msg}")
        raise SystemExit(1)
    print(f"  ok: {msg}")


def fresh_service():
    from swarm_engine.plugin.service import PluginService
    return PluginService(REGISTRY)
