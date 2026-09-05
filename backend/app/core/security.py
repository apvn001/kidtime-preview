"""密码、JWT、配对码、设备密钥（ARCHITECTURE.md §5.1）。

🔴 密码学参数三档，不可混用：
  * Web 账号密码 —— bcrypt cost **12**（D38）
  * 设备密钥     —— bcrypt cost **10**（S5，同步高频，cost 12 会成为 CPU 瓶颈）
  * 客户端本地家长密码 —— PBKDF2-HMAC-SHA256 210000 轮（D39，属批次 B，后端不实现）
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import get_settings
from app.core.errors import InvalidPairingCodeError, InvalidTokenError, WeakPasswordError

_WEB_PASSWORD_CONTEXT: Final[CryptContext] = CryptContext(
    schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12
)
_DEVICE_SECRET_CONTEXT: Final[CryptContext] = CryptContext(
    schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=10
)

_USERNAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.\-]{3,64}$")

PAIRING_ALPHABET: Final[str] = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""配对码字母表，去掉易混淆的 O/0/I/1（D01）。"""

RECOVERY_ALPHABET: Final[str] = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""恢复码字母表（D40）。"""

PAIRING_CODE_LENGTH: Final[int] = 8


def sha256_hex(raw: str) -> str:
    """返回字符串的 sha256 十六进制摘要。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- Web 密码

def hash_password(plain: str) -> str:
    """用 bcrypt cost 12 哈希 Web 账号密码（D38）。"""
    return _WEB_PASSWORD_CONTEXT.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """校验 Web 账号密码。哈希串损坏时返回 ``False`` 而非抛异常。"""
    try:
        return _WEB_PASSWORD_CONTEXT.verify(plain, hashed)
    except (ValueError, TypeError):
        return False


def validate_password_policy(plain: str) -> None:
    """校验密码策略：≥8 字符且同时含字母与数字。

    Raises:
        WeakPasswordError: 不满足策略。
    """
    if not isinstance(plain, str) or len(plain) < 8:
        raise WeakPasswordError()
    if not re.search(r"[A-Za-z]", plain) or not re.search(r"\d", plain):
        raise WeakPasswordError()


def validate_username(username: str) -> str:
    """校验用户名格式：3–64 字符，仅 `[A-Za-z0-9_.-]`。

    Returns:
        去首尾空白后的用户名。

    Raises:
        WeakPasswordError: 不在此校验范围内；本函数只抛 ValueError 由上层转换。
    """
    value = (username or "").strip()
    if not _USERNAME_RE.match(value):
        raise ValueError("用户名需为 3-64 个字符，且只能包含字母、数字、下划线、点或连字符")
    return value


# ---------------------------------------------------------------- JWT

@dataclass(frozen=True)
class TokenPayload:
    """已解码的访问令牌载荷。"""

    sub: str
    role: str
    typ: str
    jti: str
    exp: datetime
    iat: datetime


def create_access_token(
    user_id: int, role: str, expires_minutes: int | None = None
) -> tuple[str, int]:
    """签发访问令牌。

    Args:
        user_id: 用户主键。
        role: ``admin`` 或 ``parent``。
        expires_minutes: 覆盖默认 TTL（分钟）。

    Returns:
        ``(jwt 字符串, 有效秒数)``。
    """
    settings = get_settings()
    ttl_minutes = expires_minutes if expires_minutes is not None else settings.ACCESS_TOKEN_TTL_MINUTES
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=ttl_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "typ": "access",
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, int(ttl_minutes * 60)


def decode_access_token(token: str) -> TokenPayload:
    """解码并校验访问令牌。

    Raises:
        InvalidTokenError: 签名错误、已过期或 `typ` 不是 ``access``。
    """
    settings = get_settings()
    try:
        raw = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise InvalidTokenError() from exc
    if raw.get("typ") != "access":
        raise InvalidTokenError()
    try:
        return TokenPayload(
            sub=str(raw["sub"]),
            role=str(raw.get("role", "parent")),
            typ=str(raw["typ"]),
            jti=str(raw.get("jti", "")),
            exp=datetime.fromtimestamp(int(raw["exp"]), tz=timezone.utc),
            iat=datetime.fromtimestamp(int(raw.get("iat", 0)), tz=timezone.utc),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidTokenError() from exc


def create_refresh_token(
    expires_days: int | None = None, now: datetime | None = None
) -> tuple[str, str, datetime]:
    """生成刷新令牌。

    Args:
        expires_days: 覆盖默认 TTL（天）。
        now: 注入的当前时间（G8）。

    Returns:
        ``(明文 token, sha256 哈希, 过期时间)``。
    """
    settings = get_settings()
    moment = now or datetime.now(timezone.utc)
    days = expires_days if expires_days is not None else settings.REFRESH_TOKEN_TTL_DAYS
    raw = secrets.token_urlsafe(48)
    return raw, sha256_hex(raw), moment + timedelta(days=days)


def new_family_id() -> str:
    """生成刷新令牌家族 id。"""
    return str(uuid.uuid4())


# ---------------------------------------------------------------- 配对码

def generate_pairing_code() -> tuple[str, str, str]:
    """生成配对码（D01/D02）。

    Returns:
        ``(展示码 'XXXX-XXXX', 归一化码 'XXXXXXXX', sha256 哈希)``。
    """
    normalized = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
    display = f"{normalized[:4]}-{normalized[4:]}"
    return display, normalized, sha256_hex(normalized)


def normalize_pairing_code(user_input: str) -> str:
    """归一化用户输入的配对码。

    去除连字符与空白 → 转大写 → 校验长度与字符集。

    Raises:
        InvalidPairingCodeError: 长度或字符集不合法。
    """
    if not isinstance(user_input, str):
        raise InvalidPairingCodeError()
    value = re.sub(r"[\s\-_]", "", user_input).upper()
    if len(value) != PAIRING_CODE_LENGTH:
        raise InvalidPairingCodeError()
    if any(char not in PAIRING_ALPHABET for char in value):
        raise InvalidPairingCodeError()
    return value


# ---------------------------------------------------------------- 设备密钥

def generate_device_secret() -> str:
    """生成设备密钥明文（48 字节 urlsafe）。"""
    return secrets.token_urlsafe(48)


def hash_device_secret(secret: str) -> str:
    """用 bcrypt cost 10 哈希设备密钥（S5）。"""
    return _DEVICE_SECRET_CONTEXT.hash(secret)


def verify_device_secret(secret: str, hashed: str) -> bool:
    """校验设备密钥。"""
    try:
        return _DEVICE_SECRET_CONTEXT.verify(secret, hashed)
    except (ValueError, TypeError):
        return False


def device_secret_lookup(secret: str) -> str:
    """设备密钥的 sha256 索引值，用于 O(1) 定位后再做 bcrypt 校验。"""
    return sha256_hex(secret)


# ---------------------------------------------------------------- 恢复码

def generate_recovery_code() -> tuple[str, str]:
    """生成 20 位恢复码（D40）。

    Returns:
        ``(展示码 'XXXXX-XXXXX-XXXXX-XXXXX', 归一化 20 位)``。
    """
    normalized = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(20))
    groups = [normalized[i : i + 5] for i in range(0, 20, 5)]
    return "-".join(groups), normalized


def is_uuid(value: str | None) -> bool:
    """判断字符串是否为合法 UUID。"""
    if not value:
        return False
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def new_uuid() -> str:
    """生成新的 UUIDv4 字符串。"""
    return str(uuid.uuid4())
