"""Domain-separation guard for the curiosity enforcement mechanism.

Enforcement mutation is unreachable from the curiosity domain by construction:
every mutating module calls :func:`ensure_external_caller` at import time, and
every mutating write function calls it at call time. The guard walks the live
call stack and refuses when any caller frame belongs to the curiosity domain
package (``swarm_engine.curiosity.*``). Read-only access is intentionally
unguarded -- the pull-only exposure surface must be reachable from anywhere.
"""

import inspect

CURIOSITY_PACKAGE_PREFIX = "swarm_engine.curiosity"
_ENFORCEMENT_PACKAGE_PREFIX = __name__.rsplit(".", 1)[0]


class DomainSeparationError(ImportError):
    """Raised when the curiosity domain attempts to reach enforcement mutation."""


def ensure_external_caller() -> None:
    """Raise DomainSeparationError if a curiosity-domain frame is on the stack."""
    for frame_info in inspect.stack():
        module_name = frame_info.frame.f_globals.get("__name__", "")
        if not module_name:
            continue
        if module_name == __name__ or module_name.startswith(
            _ENFORCEMENT_PACKAGE_PREFIX
        ):
            continue
        if module_name.startswith(CURIOSITY_PACKAGE_PREFIX):
            raise DomainSeparationError(
                "enforcement mutation is not importable/callable from the "
                f"curiosity domain (blocked caller module {module_name!r})"
            )
