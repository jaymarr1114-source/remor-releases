"""swarm_engine/plugin — real plugin / external-bot execution substrate.

Replaces the PLUGIN_REGISTRY_ABSENT unavailability with working machinery:

  * registry.py — disk-persisted plugin registry (register/list/get/remove)
  * protocol.py  — the versioned bot protocol + hash-pinned manifest validation
  * loader.py    — loading with re-verified hashes (no unsigned code, ever)
  * sandbox.py   — sandboxed execution reusing services.sandbox.SandboxContext,
                   plus a real jail: fresh mount/network namespaces and
                   pivot_root into the per-run dir (PLUGIN-FIX-1); a
                   plugin never runs uncontained
  * service.py   — PluginService: the orchestration behind POST /api/execute/plugin
  * errors.py    — typed honest error codes

Bot-protocol shape (James, 2026-09-27): the user states the outcome, the
plugin does the work, it reports back. Users add their own bots but do not
control bot behavior or governance.
"""
from swarm_engine.plugin.service import PluginService

__all__ = ["PluginService"]
