"""TLS certificate generation and renewal helpers."""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

DEFAULT_CERT_PATH = Path("data/tls/buzz.crt")
DEFAULT_KEY_PATH = Path("data/tls/buzz.key")
DEFAULT_VALID_DAYS = 3650
DEFAULT_RENEWAL_WINDOW_DAYS = 30


@dataclass(frozen=True)
class TlsCertificateResult:
    """Result of checking or generating TLS certificate material."""

    cert_path: Path
    key_path: Path
    generated: bool
    fingerprint: str


def resolve_tls_path(path: str | Path, cwd: Path | None = None) -> Path:
    """Resolve *path* relative to the current working directory."""
    raw_path = Path(path)
    if raw_path.is_absolute():
        return raw_path
    return (cwd or Path.cwd()) / raw_path


def ensure_tls_certificate(
    cert_path: str | Path = DEFAULT_CERT_PATH,
    key_path: str | Path = DEFAULT_KEY_PATH,
    *,
    valid_days: int = DEFAULT_VALID_DAYS,
    renewal_window_days: int = DEFAULT_RENEWAL_WINDOW_DAYS,
    cwd: Path | None = None,
) -> TlsCertificateResult:
    """Ensure a self-signed cert/key pair exists and is not near expiry."""
    resolved_cert_path = resolve_tls_path(cert_path, cwd)
    resolved_key_path = resolve_tls_path(key_path, cwd)

    cert = _load_valid_cert(resolved_cert_path, resolved_key_path)
    if cert is not None and not _is_near_expiry(cert, renewal_window_days):
        return TlsCertificateResult(
            cert_path=resolved_cert_path,
            key_path=resolved_key_path,
            generated=False,
            fingerprint=_fingerprint(cert),
        )

    cert = _generate_cert_pair(
        resolved_cert_path,
        resolved_key_path,
        valid_days=valid_days,
    )
    return TlsCertificateResult(
        cert_path=resolved_cert_path,
        key_path=resolved_key_path,
        generated=True,
        fingerprint=_fingerprint(cert),
    )


def _load_valid_cert(
    cert_path: Path,
    key_path: Path,
) -> x509.Certificate | None:
    if not cert_path.exists() or not key_path.exists():
        return None
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        serialization.load_pem_private_key(
            key_path.read_bytes(),
            password=None,
        )
    except Exception:
        return None
    return cert


def _is_near_expiry(
    cert: x509.Certificate,
    renewal_window_days: int,
) -> bool:
    expires_at = cert.not_valid_after_utc
    renewal_window = timedelta(days=renewal_window_days)
    return expires_at <= datetime.now(timezone.utc) + renewal_window


def _generate_cert_pair(
    cert_path: Path,
    key_path: Path,
    *,
    valid_days: int,
) -> x509.Certificate:
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Buzz"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_private(
        key_path,
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ),
    )
    _atomic_write_private(
        cert_path,
        cert.public_bytes(serialization.Encoding.PEM),
    )
    return cert


def _atomic_write_private(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _fingerprint(cert: x509.Certificate) -> str:
    raw = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(raw[index : index + 2] for index in range(0, len(raw), 2))
