#!/usr/bin/env python3
"""Generate Buzz self-signed TLS certificate material."""

from __future__ import annotations

import argparse
from pathlib import Path

from buzz.core.tls import (
    DEFAULT_CERT_PATH,
    DEFAULT_KEY_PATH,
    ensure_tls_certificate,
)


def main() -> None:
    """Generate or renew the configured certificate pair."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--cert-path", default=str(DEFAULT_CERT_PATH))
    parser.add_argument("--key-path", default=str(DEFAULT_KEY_PATH))
    args = parser.parse_args()

    result = ensure_tls_certificate(
        cert_path=Path(args.cert_path),
        key_path=Path(args.key_path),
    )
    action = "Generated" if result.generated else "Certificate already valid"
    print(f"{action}: {result.cert_path}")
    print(f"Key: {result.key_path}")
    print(f"SHA-256 fingerprint: {result.fingerprint}")


if __name__ == "__main__":
    main()
