"""PBKDF2-HMAC-SHA256 hashing for the local parent password and recovery code.

Hard constraint #6: the local parent password and the 20-character offline
recovery code are hashed with PBKDF2-HMAC-SHA256 using 210 000 rounds and a
16-byte random salt.  Only :mod:`hashlib` from the standard library is used --
no third-party crypto dependency is required by the client.

Stored format (a single ASCII string, safe for ``local_settings``)::

    pbkdf2_sha256$210000$<base64 salt>$<base64 derived key>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Final

#: Number of PBKDF2 iterations. Fixed by the design; do not lower.
PBKDF2_ITERATIONS: Final[int] = 210_000

#: Random salt length in bytes.
SALT_BYTES: Final[int] = 16

#: Derived key length in bytes (SHA-256 native output).
_DK_BYTES: Final[int] = 32

#: Algorithm marker written into the stored hash string.
_ALGORITHM: Final[str] = "pbkdf2_sha256"

#: Minimum accepted parent password length.
MIN_PARENT_PASSWORD_LENGTH: Final[int] = 6

#: Alphabet used for recovery codes -- digits and upper-case letters with the
#: visually ambiguous characters (0/O, 1/I/L) removed.
_RECOVERY_ALPHABET: Final[str] = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

#: Recovery codes are 4 groups of 5 characters -> 20 significant characters.
_RECOVERY_GROUPS: Final[int] = 4
_RECOVERY_GROUP_SIZE: Final[int] = 5


class WeakPasswordError(ValueError):
    """Raised when a candidate parent password does not meet the policy."""


def hash_secret(secret: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Hash ``secret`` with PBKDF2-HMAC-SHA256 and a fresh random salt.

    Args:
        secret: Plaintext secret (parent password or normalized recovery code).
        iterations: PBKDF2 round count. Defaults to :data:`PBKDF2_ITERATIONS`.

    Returns:
        An encoded hash string of the form
        ``pbkdf2_sha256$<iterations>$<salt_b64>$<dk_b64>``.

    Raises:
        ValueError: When ``secret`` is empty.
    """
    if not secret:
        raise ValueError("secret must not be empty")
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, iterations, _DK_BYTES)
    return "{algo}${iters}${salt}${dk}".format(
        algo=_ALGORITHM,
        iters=iterations,
        salt=base64.b64encode(salt).decode("ascii"),
        dk=base64.b64encode(derived).decode("ascii"),
    )


def verify_secret(secret: str, encoded: str | None) -> bool:
    """Constant-time verification of ``secret`` against a stored hash.

    Args:
        secret: Plaintext candidate.
        encoded: Stored hash string produced by :func:`hash_secret`. ``None``
            or a malformed value always returns ``False``.

    Returns:
        ``True`` when the secret matches, ``False`` otherwise. Never raises.
    """
    if not secret or not encoded:
        return False
    try:
        algorithm, iterations_text, salt_b64, dk_b64 = encoded.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
    except (ValueError, TypeError):
        return False

    candidate = hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), salt, iterations, len(expected) or _DK_BYTES
    )
    return hmac.compare_digest(candidate, expected)


def validate_parent_password(password: str) -> None:
    """Validate a candidate parent password against the local policy.

    The policy is intentionally light: this is a family-facing "speed bump",
    not an internet-facing credential.  It must be at least
    :data:`MIN_PARENT_PASSWORD_LENGTH` characters long, must not be entirely
    whitespace, and must not be a trivially guessable sequence.

    Args:
        password: Candidate password.

    Raises:
        WeakPasswordError: When the password does not satisfy the policy.
    """
    if password is None or not password.strip():
        raise WeakPasswordError("密码不能为空")
    if len(password) < MIN_PARENT_PASSWORD_LENGTH:
        raise WeakPasswordError(f"密码至少需要 {MIN_PARENT_PASSWORD_LENGTH} 位")
    if password.strip() != password:
        raise WeakPasswordError("密码首尾不能有空格")
    lowered = password.lower()
    if lowered in {"123456", "111111", "000000", "password", "abc123", "123456789"}:
        raise WeakPasswordError("密码过于简单，请换一个")
    if len(set(password)) == 1:
        raise WeakPasswordError("密码不能是同一个字符重复")
    if password.isdigit():
        # 🔴 真机第九轮：docstring 一直承诺拒绝 trivially guessable 序列，
        # 但纯数字（如 987654）从未被拦截。首启密码设置与家长改密同源校验。
        raise WeakPasswordError("密码不能是纯数字，请至少包含一个字母或符号")


def generate_recovery_code() -> tuple[str, str]:
    """Generate a fresh 20-character offline recovery code.

    Returns:
        A ``(display, normalized)`` tuple.  ``display`` is the human-friendly
        grouped form (``XXXXX-XXXXX-XXXXX-XXXXX``) that is shown once to the
        parent; ``normalized`` is the 20-character upper-case string that must
        be fed into :func:`hash_secret` for storage.
    """
    groups = [
        "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP_SIZE))
        for _ in range(_RECOVERY_GROUPS)
    ]
    display = "-".join(groups)
    return display, "".join(groups)


def normalize_recovery_code(code: str) -> str:
    """Normalize user-typed recovery code input for comparison.

    Strips separators and whitespace, upper-cases the result, and maps the
    ambiguous characters the parent may have mistyped (``O`` -> ``0`` is not
    applied because ``0`` is not in the alphabet; instead ``0`` -> ``O`` is
    rejected naturally by the hash comparison).

    Args:
        code: Raw user input, possibly containing dashes or spaces.

    Returns:
        The normalized, comparison-ready string.
    """
    if not code:
        return ""
    cleaned = [ch for ch in code.upper() if ch.isalnum()]
    return "".join(cleaned)


__all__ = [
    "MIN_PARENT_PASSWORD_LENGTH",
    "PBKDF2_ITERATIONS",
    "SALT_BYTES",
    "WeakPasswordError",
    "generate_recovery_code",
    "hash_secret",
    "normalize_recovery_code",
    "validate_parent_password",
    "verify_secret",
]
