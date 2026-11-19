"""HTTPS for the harness's own calls (Bitbucket PRs, ntfy).

The python.org installer ships Python without CA certificates until someone runs its
"Install Certificates.command", and then every HTTPS call fails with CERTIFICATE_VERIFY_FAILED.
When Python has no CA certificates of its own, use the operating system's bundle instead.
Verification is never turned off: with no bundle anywhere, calls keep failing.
"""

from __future__ import annotations

import ssl
import urllib.request
from pathlib import Path

PYTHON_DEFAULT = "Python's own"
# macOS, Debian/Ubuntu, Fedora/RHEL.
SYSTEM_BUNDLES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt")


def _python_has_cas(verify_paths) -> bool:
    # cafile/capath are None when the paths do not exist; SSL_CERT_FILE/SSL_CERT_DIR count too.
    if verify_paths.cafile and Path(verify_paths.cafile).is_file():
        return True
    capath = Path(verify_paths.capath) if verify_paths.capath else None
    return bool(capath and capath.is_dir() and any(capath.iterdir()))


def ca_source(verify_paths=None, bundles=SYSTEM_BUNDLES) -> str | None:
    """PYTHON_DEFAULT, the path of a system bundle, or None if there are no certificates."""
    if _python_has_cas(verify_paths or ssl.get_default_verify_paths()):
        return PYTHON_DEFAULT
    return next((b for b in bundles if Path(b).is_file()), None)


def context(verify_paths=None, bundles=SYSTEM_BUNDLES) -> ssl.SSLContext:
    source = ca_source(verify_paths, bundles)
    if source in (None, PYTHON_DEFAULT):
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=source)


def urlopen(req: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(req, timeout=timeout, context=context())
