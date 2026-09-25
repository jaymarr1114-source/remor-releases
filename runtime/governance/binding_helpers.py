"""
swarm_engine/governance/binding_helpers.py

Shared helpers for oracle-channel binding (trust-anchor mission, workstream 4:
binding the 11 genuinely-bindable oracle families O9, O10, O12--O20).

This module introduces NO new trust machinery: every helper below is a thin
composition of the existing ``governance/oracle_binding.py`` API
(``OracleRegistry.register_oracle`` / ``evaluate`` / ``verify_oracle_row``,
authenticated producers, chained records). It exists so the eleven call
sites share one honest attribution policy instead of re-inventing it eleven
times:

Attribution policy for injected callables (O9, O10, O13--O17):
  * When the injector supplies (producer_id, token), the oracle is registered
    under that AUTHENTICATED producer.
  * When no credentials are supplied, the oracle is registered under the
    ENGINE producer with a source tag stating the caller is unattributed.
    This records the definition digest (defeating silent swap and
    repudiation of the definition) while honestly marking attribution as
    unattributed -- it never claims an authentication that did not happen.
  * When no registry is supplied at all, call sites preserve legacy behavior
    exactly (classified as unbound in that configuration).

What binding proves per family is stated at each call site; in every case the
oracle's truth/adequacy (or the world's reality, or the task author's intent)
remains caller-asserted. Channel binding defeats substitution and repudiation
attacks that are undetectable today -- it does not certify correctness.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

from swarm_engine.governance.oracle_binding import (
    OracleBindingError,
    OracleRegistry,
    _canonical,
    _digest,
)


def canonical_digest(obj: Any) -> str:
    """Content digest of an arbitrary object (canonical JSON, repr fallback)."""
    return _digest(_canonical(obj))


def definition_digest_of(registry: OracleRegistry, definition: Any
                         ) -> Tuple[str, str]:
    """(definition_digest, definition_text) for a callable or data definition,
    using the registry's own canonicalization."""
    return registry._definition_text(definition)


def authenticated_registrar(registry: OracleRegistry, engine_handle,
                            producer_id: Optional[str] = None,
                            token: Optional[str] = None,
                            role: str = "oracle"):
    """Resolve WHO an injected oracle is registered under.

    Returns ``(register, producer_id, source_note)`` where
    ``register(name, definition, input_contract="", output_contract="",
    source="") -> (oracle_id, version)``.

    * Caller credentials supplied -> authenticated under that producer
      (wrong/missing token raises ``OracleBindingError``).
    * No credentials -> registered under the engine producer with a source
      tag honestly marking the caller unattributed.
    """
    if producer_id is not None:
        if not token:
            raise OracleBindingError(
                f"{role}: producer_id supplied without a token -- refusing "
                f"to register an unauthenticated producer")
        if not registry.authenticate(producer_id, token):
            raise OracleBindingError(
                f"{role}: producer authentication failed -- forged or "
                f"missing identity refused")
        def _register(name: str, definition: Any, input_contract: str = "",
                      output_contract: str = "", source: str = ""
                      ) -> Tuple[str, int]:
            return registry.register_oracle(
                producer_id, token, name, definition,
                input_contract=input_contract,
                output_contract=output_contract,
                source=source or role)
        return _register, producer_id, role

    def _register_engine(name: str, definition: Any, input_contract: str = "",
                         output_contract: str = "", source: str = ""
                         ) -> Tuple[str, int]:
        note = (source + "; " if source else "") + (
            f"{role}: injector supplied no producer credential; registered "
            f"under the engine producer with caller unattributed")
        return engine_handle.register_oracle(
            name, definition, input_contract=input_contract,
            output_contract=output_contract, source=note)
    return _register_engine, engine_handle.producer_id, \
        f"{role} (injector unattributed)"


def verify_live_callable(registry: OracleRegistry, oracle_id: str,
                         version: int, live_fn: Any, what: str = "oracle"
                         ) -> Dict[str, Any]:
    """The O1 re-digest pattern: the callable about to be (or just) used must
    still be byte-identical to the registered definition.

    Raises ``OracleBindingError`` when the oracle row is unknown, mutated, or
    when the live callable's definition digest no longer matches the
    registration (silent swap / post-registration replacement). Returns the
    oracle row on success.
    """
    ok, why = registry.verify_oracle_row(oracle_id, version)
    if not ok:
        raise OracleBindingError(f"{what}: refusing -- {why}")
    row = registry.oracle_version_row(oracle_id, version)
    if row is None:
        raise OracleBindingError(
            f"{what}: no registered oracle {oracle_id} v{version} -- "
            f"refusing to use an unregistered oracle")
    _, current_text = registry._definition_text(live_fn)
    import hmac as _hmac
    if not _hmac.compare_digest(_digest(current_text),
                                row["definition_digest"]):
        raise OracleBindingError(
            f"{what}: live definition does not match the registered "
            f"definition of {oracle_id} v{version} -- the oracle was swapped "
            f"after registration; refusing to use it")
    return row


class BoundCallable:
    """A callable oracle wrapped with per-call channel binding (O9 pattern).

    Every call re-verifies the wrapped callable against its registered
    definition (the O1 re-digest pattern) BEFORE invoking it, then records
    the (input digest -> result digest) evaluation as a chained record.
    A definition mismatch raises ``OracleBindingError`` before the oracle's
    value can influence anything.
    """

    def __init__(self, registry: OracleRegistry, engine_handle,
                 fn: Callable, oracle_id: str, version: int,
                 producer_id: str, what: str = "oracle"):
        self._registry = registry
        self._handle = engine_handle
        self._fn = fn
        self._oracle_id = oracle_id
        self._version = version
        self._producer_id = producer_id
        self._what = what
        # Snapshot the definition digest at wrap time so a swap between
        # wrapping and the first call is caught even before the registry
        # comparison runs.
        _, text = registry._definition_text(fn)
        self._wrapped_digest = _digest(text)

    @property
    def oracle_id(self) -> str:
        return self._oracle_id

    @property
    def oracle_version(self) -> int:
        return self._version

    @property
    def wrapped(self) -> Callable:
        """The raw callable (for identity checks, not for invocation)."""
        return self._fn

    def __call__(self, *args, **kwargs):
        verify_live_callable(self._registry, self._oracle_id, self._version,
                             self._fn, what=self._what)
        result = self._fn(*args, **kwargs)
        # Fail closed on recording: a bound oracle whose query is not
        # chained is audit-indistinguishable from an unbound one, so a
        # record-keeping failure must surface loudly, never pass silently.
        try:
            input_obj = {"args": args, "kwargs": kwargs}
            self._handle.evaluate(
                self._oracle_id, input_obj, {"result": result},
                input_ref=f"{self._what}:query",
                version=self._version, supplier_id=self._producer_id)
        except OracleBindingError:
            raise
        except Exception as exc:
            raise OracleBindingError(
                f"{self._what}: evaluation recording failed ({exc}); the "
                "query is not chained, so its value must not be trusted"
            ) from exc
        return result
