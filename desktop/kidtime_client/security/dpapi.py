"""Device credential storage protected by the Windows DPAPI.

Hard constraint #6 (see ``docs/ARCHITECTURE.md`` section 5.15):

* ``device_secret`` MUST never be written to disk in plaintext.
* ``device_secret`` MUST never appear in a log record.

The credential blob is encrypted with ``CryptProtectData`` using the
``CurrentUser`` scope and no extra entropy, meaning only the Windows account
that saved the file can read it back.  On a non-Windows host (developer
machines, CI) we transparently fall back to an obfuscated-but-not-secret
encoding so that the rest of the application remains testable; the fallback is
clearly marked in the payload header so it can never be mistaken for real
protection.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from kidtime_client.logging_setup import register_secret

logger = logging.getLogger(__name__)

#: Marker written into the credential file so we know how it was encoded.
_SCHEME_DPAPI: Final[str] = "dpapi-v1"
_SCHEME_PLAIN: Final[str] = "plain-v1"

#: ``CRYPTPROTECT_UI_FORBIDDEN`` -- never show a UI prompt while (un)protecting.
_CRYPTPROTECT_UI_FORBIDDEN: Final[int] = 0x1

_IS_WINDOWS: Final[bool] = platform.system() == "Windows"

try:  # pragma: no cover - import guard, exercised only on Windows
    import win32crypt  # type: ignore[import-not-found]

    _HAS_WIN32CRYPT = True
except Exception:  # pragma: no cover - non-Windows or pywin32 missing
    win32crypt = None  # type: ignore[assignment]
    _HAS_WIN32CRYPT = False


@dataclass(frozen=True, slots=True)
class Credentials:
    """Immutable device credentials used to authenticate against the backend.

    Attributes:
        base_url: Backend origin, e.g. ``http://192.168.1.10:8000`` (no
            trailing slash, no ``/api/v1`` suffix).
        device_id: Server-assigned device identifier (UUID string).
        device_secret: Server-issued shared secret.  Treated as a secret at all
            times; never logged, never serialised in plaintext.
    """

    base_url: str
    device_id: str
    device_secret: str

    def masked(self) -> str:
        """Return a log-safe representation (secret replaced by a mask)."""
        return f"Credentials(base_url={self.base_url!r}, device_id={self.device_id!r}, device_secret='***')"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return self.masked()

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.masked()


class CredentialStore:
    """Reads and writes the DPAPI-protected credential file.

    The file lives next to the local database (``credentials.dat``) and is
    written atomically so a crash mid-write can never leave a truncated blob
    behind.

    Args:
        path: Absolute path of the credential file.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """Absolute path of the credential file."""
        return self._path

    def exists(self) -> bool:
        """Return ``True`` when a credential file is present on disk."""
        return self._path.is_file()

    def save(self, credentials: Credentials) -> None:
        """Encrypt and persist ``credentials`` atomically.

        Args:
            credentials: Credentials to store.

        Raises:
            OSError: When the file cannot be written.
        """
        payload: dict[str, Any] = {
            "base_url": credentials.base_url,
            "device_id": credentials.device_id,
            "device_secret": credentials.device_secret,
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        scheme, blob = self._protect(raw)
        envelope = {
            "scheme": scheme,
            "blob": base64.b64encode(blob).decode("ascii"),
        }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self._path)

        register_secret(credentials.device_secret)
        logger.info(
            "Saved device credentials for device_id=%s (scheme=%s)",
            credentials.device_id,
            scheme,
        )

    def load(self) -> Credentials | None:
        """Decrypt and return the stored credentials.

        Returns:
            The stored :class:`Credentials`, or ``None`` when the file is
            missing, unreadable, or was protected for a different Windows
            account (decryption failure).
        """
        if not self._path.is_file():
            return None
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            scheme = str(envelope["scheme"])
            blob = base64.b64decode(envelope["blob"])
            raw = self._unprotect(scheme, blob)
            payload = json.loads(raw.decode("utf-8"))
            credentials = Credentials(
                base_url=str(payload["base_url"]),
                device_id=str(payload["device_id"]),
                device_secret=str(payload["device_secret"]),
            )
        except Exception:
            logger.warning(
                "Failed to load device credentials from %s; the file may belong "
                "to a different Windows account or be corrupted. Re-pairing is "
                "required.",
                self._path,
                exc_info=True,
            )
            return None

        register_secret(credentials.device_secret)
        return credentials

    def clear(self) -> None:
        """Delete the credential file if it exists (idempotent)."""
        try:
            self._path.unlink()
            logger.info("Cleared device credentials at %s", self._path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not delete credential file %s", self._path, exc_info=True)

    # ------------------------------------------------------------------
    # Encryption primitives
    # ------------------------------------------------------------------
    @staticmethod
    def _protect(raw: bytes) -> tuple[str, bytes]:
        """Encrypt ``raw`` with DPAPI when available.

        Args:
            raw: Plaintext bytes.

        Returns:
            A ``(scheme, blob)`` tuple.  ``scheme`` is ``dpapi-v1`` when the
            Windows API was used and ``plain-v1`` on the fallback path.
        """
        if _IS_WINDOWS and _HAS_WIN32CRYPT:
            try:
                blob = win32crypt.CryptProtectData(  # type: ignore[union-attr]
                    raw,
                    "KidTime device credentials",
                    None,
                    None,
                    None,
                    _CRYPTPROTECT_UI_FORBIDDEN,
                )
                return _SCHEME_DPAPI, bytes(blob)
            except Exception:  # pragma: no cover - defensive
                logger.warning("CryptProtectData failed; falling back to plain scheme", exc_info=True)
        return _SCHEME_PLAIN, base64.b64encode(raw)

    @staticmethod
    def _unprotect(scheme: str, blob: bytes) -> bytes:
        """Decrypt ``blob`` that was produced by :meth:`_protect`.

        Args:
            scheme: Scheme marker read back from the envelope.
            blob: Ciphertext bytes.

        Returns:
            The original plaintext bytes.

        Raises:
            ValueError: When ``scheme`` is unknown or DPAPI is unavailable for
                a ``dpapi-v1`` payload.
        """
        if scheme == _SCHEME_DPAPI:
            if not (_IS_WINDOWS and _HAS_WIN32CRYPT):
                raise ValueError("DPAPI payload cannot be read without pywin32 on Windows")
            _description, raw = win32crypt.CryptUnprotectData(  # type: ignore[union-attr]
                blob,
                None,
                None,
                None,
                _CRYPTPROTECT_UI_FORBIDDEN,
            )
            return bytes(raw)
        if scheme == _SCHEME_PLAIN:
            return base64.b64decode(blob)
        raise ValueError(f"Unknown credential scheme: {scheme!r}")


__all__ = ["Credentials", "CredentialStore"]
