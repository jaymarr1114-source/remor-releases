"""TLS with pinned target identity for the remote-dispatch channel.

The transport is TLS 1.2+ in both directions. There is no CA: the
controller pins the target's certificate fingerprint (SHA-256 of the
DER certificate), bound to the paired-device record at pairing time.
A mismatched or missing pin fails the handshake closed -- a network
attacker cannot impersonate the target even with a valid CA-signed
certificate, and passive eavesdropping sees only ciphertext.

Certificates are self-signed, generated once per target via the
openssl CLI, stored in the target's cert dir (0600 key).
"""
from __future__ import annotations

import hashlib
import os
import ssl
import subprocess
from typing import Optional, Tuple


class TLSError(RuntimeError):
    pass


def ensure_cert(cert_dir: str,
                common_name: str = "remor-rd-target"
                ) -> Tuple[str, str]:
    """Generate (once) a self-signed cert+key for the target.

    Returns (cert_path, key_path). The key is written mode 0600.
    """
    os.makedirs(cert_dir, exist_ok=True)
    cert_path = os.path.join(cert_dir, "target.crt")
    key_path = os.path.join(cert_dir, "target.key")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048",
             "-keyout", key_path, "-out", cert_path,
             "-days", "3650", "-nodes",
             "-subj", f"/CN={common_name}"],
            check=True, capture_output=True, timeout=60)
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired) as e:
        raise TLSError(f"certificate generation failed: {e}")
    os.chmod(key_path, 0o600)
    return cert_path, key_path


def fingerprint(cert_path: str) -> str:
    """SHA-256 fingerprint (hex) of the DER certificate."""
    der = subprocess.run(
        ["openssl", "x509", "-in", cert_path, "-outform", "DER"],
        check=True, capture_output=True, timeout=30).stdout
    return hashlib.sha256(der).hexdigest()


def _der_fingerprint(der_bytes: bytes) -> str:
    return hashlib.sha256(der_bytes).hexdigest()


def server_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """TLS server context for the target listener."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def client_context(pinned_fingerprint: Optional[str]) -> ssl.SSLContext:
    """TLS client context for pinning.

    pinned_fingerprint=None fails closed: without a pin there is
    nothing to authenticate against. There is no CA -- the pin IS the
    authentication, checked post-handshake in wrap_client.
    """
    if not pinned_fingerprint:
        raise TLSError("no pinned certificate fingerprint: refusing"
                       " to connect without target identity")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def wrap_client(sock, pinned_fingerprint: str,
                server_hostname: str = "remor-rd-target") -> ssl.SSLSocket:
    """TLS-wrap a connected socket, enforcing the certificate pin."""
    ctx = client_context(pinned_fingerprint)
    tls = ctx.wrap_socket(sock, server_hostname=server_hostname)
    der = tls.getpeercert(binary_form=True)
    if not der:
        tls.close()
        raise TLSError("target presented no certificate")
    got = _der_fingerprint(der)
    if got != pinned_fingerprint:
        tls.close()
        raise TLSError(
            "target certificate fingerprint mismatch: possible "
            "impersonation (spoofed target refused)")
    return tls
