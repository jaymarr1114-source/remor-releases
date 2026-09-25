"""Exception taxonomy for the agent organization.

Binding rule: every refusal path raises one of these specific exceptions.
Silent False returns are forbidden -- a refusal that does not raise is a
simulation of a refusal.
"""
from __future__ import annotations


class OrgError(Exception):
    """Base class for all agent-organization errors."""


class AuthorityError(OrgError):
    """Raised when an actor lacks authority for the requested action."""


class LifecycleError(OrgError):
    """Raised when a state transition is not in the allowed lifecycle map."""


class ExecutionError(OrgError):
    """Raised when substrate execution fails or returns an unusable result."""


class VerificationFailed(OrgError):
    """Raised when independent verification refuses a work product."""


class SynthesisError(OrgError):
    """Raised when synthesis gates (distinctness, sanity) refuse."""
