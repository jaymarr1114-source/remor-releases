"""Typed honest errors for the plugin subsystem.

Every refusal carries a machine-readable code, a human reason, and the
evidence that caused it. Nothing here ever reports fake success.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# -- codes ---------------------------------------------------------------
PLUGIN_UNKNOWN = "PLUGIN_UNKNOWN"                      # no such plugin registered
PLUGIN_MANIFEST_INVALID = "PLUGIN_MANIFEST_INVALID"    # bad manifest / hash mismatch / unsigned
PLUGIN_PROTOCOL_VIOLATION = "PLUGIN_PROTOCOL_VIOLATION"  # plugin broke the bot protocol
PLUGIN_POLICY_VIOLATION = "PLUGIN_POLICY_VIOLATION"    # sandbox policy breach detected
PLUGIN_EXECUTION_FAILED = "PLUGIN_EXECUTION_FAILED"    # plugin crashed / non-zero exit
PLUGIN_TIMEOUT = "PLUGIN_TIMEOUT"                      # plugin exceeded its time budget
PLUGIN_INVALID_TASK = "PLUGIN_INVALID_TASK"            # task envelope not a JSON dict
PLUGIN_REPORTED_FAILURE = "PLUGIN_REPORTED_FAILURE"  # plugin ran but reported its own failure
PLUGIN_REGISTRY_ERROR = "PLUGIN_REGISTRY_ERROR"        # registry I/O failure


class PluginError(Exception):
    """Raised for every honest refusal inside the plugin subsystem."""

    def __init__(self, code: str, reason: str,
                 detail: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.detail = detail or {}

    def as_result(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "reason": self.reason},
        }
        if self.detail:
            result["error"]["detail"] = self.detail
        return result


def plugin_error(code: str, reason: str,
                 detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the wire shape directly (for handlers that don't raise)."""
    return PluginError(code, reason, detail).as_result()


__all__ = [
    "PLUGIN_UNKNOWN",
    "PLUGIN_MANIFEST_INVALID",
    "PLUGIN_PROTOCOL_VIOLATION",
    "PLUGIN_POLICY_VIOLATION",
    "PLUGIN_EXECUTION_FAILED",
    "PLUGIN_TIMEOUT",
    "PLUGIN_INVALID_TASK",
    "PLUGIN_REPORTED_FAILURE",
    "PLUGIN_REGISTRY_ERROR",
    "PluginError",
    "plugin_error",
]
