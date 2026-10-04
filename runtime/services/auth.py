"""Contract — bearer-token gate for the REMOR HTTP adoption layer.

There is no user/credential substrate anywhere in the runtime (no user
accounts, no password store, no SSO), so per-user authorization cannot be
built honestly. What CAN be built honestly is a bearer-token gate:

* Token source: ``REMOR_API_TOKEN`` env var wins; else
  ``<base_dir>/api_token`` (created 0600 on first boot if absent; the path
  is logged, the value NEVER is).
* Comparison is ``hmac.compare_digest`` (timing-safe).
* When no token is configured anywhere (no env var, no base_dir to
  provision from, no explicit token), the gate is PERMISSIVE -- the
  local-first default for a 127.0.0.1-bound server. ``is_gated()`` tells
  callers whether the gate is actually enforcing, so sensitive routes
  (e.g. capability install) can fail closed when it is not.

``check(method, path, headers)`` returns None (allow) or
``(401, typed_body)``. The handler calls it at the top of every do_*
before any routing.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
import stat
import sys
from typing import Dict, Optional, Tuple

log = logging.getLogger(__name__)

TOKEN_ENV_VAR = "REMOR_API_TOKEN"
TOKEN_FILE_NAME = "api_token"

AUTH_REQUIRED_CODE = "auth_required"
AUTH_INVALID_CODE = "auth_invalid"


class AuthGate:
    """Bearer-token gate. See module docstring for the trust model."""

    def __init__(self, base_dir: Optional[str] = None,
                 token: Optional[str] = None,
                 provision: bool = True) -> None:
        self.base_dir = os.path.abspath(base_dir) if base_dir else None
        self.token_path: Optional[str] = None
        self._token: Optional[str] = None

        explicit = token if token else os.environ.get(TOKEN_ENV_VAR)
        if explicit:
            # Explicit token or env var wins; never written anywhere.
            self._token = explicit
            return
        if self.base_dir is not None and provision:
            self.token_path = os.path.join(self.base_dir, TOKEN_FILE_NAME)
            self._token = self._load_or_provision(self.token_path)
        # else: no token anywhere -> permissive (local-first default).

    # -- provisioning ---------------------------------------------------
    @staticmethod
    def _load_or_provision(path: str) -> str:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                return value
            # Empty file: fall through and replace it (a zero-length token
            # file would otherwise leave the gate permissive-but-gated).
        value = secrets.token_hex(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(value + "\n")
        except BaseException:
            os.close(fd)
            raise
        # Harden permissions even if the umask/open mode was overridden.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # Path is logged; the value NEVER is.
        msg = f"[auth] provisioned new API token file at {path} (0600)"
        log.warning(msg)
        print(msg, file=sys.stderr)
        return value

    # -- introspection ---------------------------------------------------
    def is_gated(self) -> bool:
        """True when a token is configured and check() actually enforces."""
        return self._token is not None

    # -- enforcement -----------------------------------------------------
    def check(self, method: str, path: str,
              headers: Dict[str, str]) -> Optional[Tuple[int, Dict]]:
        """None = allow; (401, typed body) = refuse.

        ``headers`` is the request's header mapping (case-insensitive in
        BaseHTTPRequestHandler; plain dicts in tests).
        """
        if self._token is None:
            # Fail closed: no token configured anywhere. The local-first
            # default is preserved by provisioning a token when base_dir is
            # set; reaching here means the gate was constructed without any
            # token source, which must not silently allow access.
            return 401, {
                "ok": False,
                "error": "unauthorized: no API token configured "
                         "(set REMOR_API_TOKEN or provide a base_dir)",
                "code": AUTH_REQUIRED_CODE,
            }
        presented = self._bearer(headers)
        if presented is None:
            return 401, {
                "ok": False,
                "error": "unauthorized: this endpoint requires a bearer "
                         "token (Authorization: Bearer <token>)",
                "code": AUTH_REQUIRED_CODE,
            }
        # Timing-safe comparison: never short-circuit on the secret.
        if not hmac.compare_digest(presented, self._token):
            return 401, {
                "ok": False,
                "error": "unauthorized: invalid bearer token",
                "code": AUTH_INVALID_CODE,
            }
        return None

    @staticmethod
    def _bearer(headers: Dict[str, str]) -> Optional[str]:
        raw = None
        try:
            raw = headers.get("Authorization")
        except AttributeError:
            raw = None
        if raw is None:
            # Plain-dict callers in tests may use lowercase keys.
            try:
                raw = headers.get("authorization")
            except AttributeError:
                raw = None
        if not raw:
            return None
        scheme, _, credentials = raw.partition(" ")
        if scheme.lower() != "bearer" or not credentials.strip():
            return None
        return credentials.strip()
