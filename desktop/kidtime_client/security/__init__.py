"""Security helpers for the KidTime desktop client.

This package holds two independent concerns:

* :mod:`kidtime_client.security.dpapi` -- device credential storage backed by
  the Windows Data Protection API.  The device secret is never written to disk
  in plaintext and never emitted to the log files.
* :mod:`kidtime_client.security.passwords` -- PBKDF2-HMAC-SHA256 hashing for
  the local parent password and the offline recovery code.
"""

from __future__ import annotations

from kidtime_client.security.dpapi import Credentials, CredentialStore
from kidtime_client.security.passwords import (
    PBKDF2_ITERATIONS,
    SALT_BYTES,
    WeakPasswordError,
    generate_recovery_code,
    hash_secret,
    normalize_recovery_code,
    validate_parent_password,
    verify_secret,
)

__all__ = [
    "Credentials",
    "CredentialStore",
    "PBKDF2_ITERATIONS",
    "SALT_BYTES",
    "WeakPasswordError",
    "generate_recovery_code",
    "hash_secret",
    "normalize_recovery_code",
    "validate_parent_password",
    "verify_secret",
]
