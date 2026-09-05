"""客户端版本服务：发布（幂等 upsert）与查询 + SemVer 比较。

⚠️ ``compare_semver`` 的语义必须与桌面端 ``kidtime_client/update/version_util.py``
**逐字一致**（同一套解析规则、同一套比较顺序），否则会出现「服务端认为要强更、
客户端认为不用更」的分裂。任何一侧修改都必须同步另一侧。
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.constants import CHANNEL_STABLE
from app.core.errors import BadRequestError, ErrorCode
from app.core.security import new_uuid
from app.db.base import utcnow
from app.db.session import retry_on_busy
from app.models.client_version import ClientVersion
from app.models.user import User
from app.schemas.client_version import AdminVersionPublishIn

# 只取版本号的数字主体：前导 ``v`` 可选，随后 1~4 段数字；
# ``-prerelease`` / ``+buildmeta`` 后缀被整体丢弃（MVP 仅 stable，忽略 pre-release）。
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})")

_VERSION_TUPLE_LEN = 4
"""统一补齐到 (major, minor, patch, build) 四元组再比较。"""


def parse_version(text: str) -> tuple[int, int, int, int]:
    """把版本号字符串解析成 ``(major, minor, patch, build)`` 四元组。

    忽略 pre-release / build metadata 后缀（``1.2.3-rc1`` 与 ``1.2.3`` 等价），
    缺省段补 0（``1.2`` → ``(1, 2, 0, 0)``）。

    Args:
        text: 版本号字符串，允许前导 ``v``。

    Returns:
        长度恒为 4 的整数元组。

    Raises:
        ValueError: 完全无法解析出数字主体。
    """
    match = _VERSION_RE.match((text or "").strip())
    if match is None:
        raise ValueError(f"无法解析版本号：{text!r}")
    parts = [int(chunk) for chunk in match.group(1).split(".")]
    parts.extend([0] * (_VERSION_TUPLE_LEN - len(parts)))
    return (parts[0], parts[1], parts[2], parts[3])


def compare_semver(a: str, b: str) -> int:
    """比较两个版本号。

    Args:
        a: 左侧版本号。
        b: 右侧版本号。

    Returns:
        ``a < b`` 返回 ``-1``；``a == b`` 返回 ``0``；``a > b`` 返回 ``1``。

    Raises:
        ValueError: 任一侧无法解析。
    """
    left = parse_version(a)
    right = parse_version(b)
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def compare_semver_or_400(a: str, b: str) -> int:
    """:func:`compare_semver` 的 HTTP 包装：解析失败转 400 业务错误。

    Raises:
        BadRequestError: 版本号格式非法（``INVALID_VERSION_FORMAT``）。
    """
    try:
        return compare_semver(a, b)
    except ValueError as exc:
        raise BadRequestError(
            "版本号格式不正确，应为 MAJOR.MINOR.PATCH",
            code=ErrorCode.INVALID_VERSION_FORMAT,
            details={"a": a, "b": b},
        ) from exc


def get_latest(db: Session, channel: str = CHANNEL_STABLE) -> ClientVersion | None:
    """取指定通道的最新版本行（``is_latest=True``）。

    Args:
        db: 数据库会话。
        channel: 发布通道。

    Returns:
        最新版本行；该通道尚无任何发布时返回 ``None``。
    """
    stmt = (
        select(ClientVersion)
        .where(ClientVersion.channel == channel, ClientVersion.is_latest.is_(True))
        .order_by(ClientVersion.published_at.desc())
        .limit(1)
    )
    return db.execute(stmt).scalars().first()


def get_by_version(
    db: Session, channel: str, version: str
) -> ClientVersion | None:
    """按 ``(channel, version)`` 取唯一一行。

    Args:
        db: 数据库会话。
        channel: 发布通道。
        version: 版本号。

    Returns:
        命中的版本行；不存在时返回 ``None``。
    """
    stmt = select(ClientVersion).where(
        ClientVersion.channel == channel, ClientVersion.version == version
    )
    return db.execute(stmt).scalars().first()


@retry_on_busy
def publish(
    db: Session,
    payload: AdminVersionPublishIn,
    admin: User,
    *,
    now: datetime | None = None,
) -> ClientVersion:
    """发布一个版本：以 ``(channel, version)`` 为键做幂等 upsert 并置位 ``is_latest``。

    重复用同一 ``(channel, version)`` 调用是**幂等**的：不会新增行，只刷新
    下载物元信息、``min_version``、发布人与 ``published_at``。

    Args:
        db: 数据库会话。
        payload: 发布载荷（已通过 Schema 校验，含 SemVer 与 sha256 格式）。
        admin: 发布人（``require_admin`` 保证是管理员）。
        now: 注入的当前时间，便于测试；``None`` 时取 :func:`utcnow`。

    Returns:
        已写入（新建或更新）的版本行，``is_latest`` 恒为 ``True``。
    """
    moment = now or utcnow()
    row = get_by_version(db, payload.channel, payload.version)
    if row is None:
        row = ClientVersion(
            id=new_uuid(),
            version=payload.version,
            channel=payload.channel,
            created_at=moment,
        )
        db.add(row)

    row.build_number = payload.build_number
    row.min_version = payload.min_version
    row.download_url = payload.download_url
    row.sha256 = payload.sha256
    row.size_bytes = payload.size_bytes
    row.release_notes = payload.release_notes
    row.published_by = admin.id
    row.published_by_name = admin.username
    row.published_at = moment
    row.is_latest = False
    # 先落库拿到确定的主键，才能在下面的批量清位里安全地排除自己。
    db.flush()

    # 同通道内至多一行 is_latest=True：先清同通道其它行，再置位自己。
    db.execute(
        update(ClientVersion)
        .where(
            ClientVersion.channel == payload.channel,
            ClientVersion.id != row.id,
            ClientVersion.is_latest.is_(True),
        )
        .values(is_latest=False)
    )
    row.is_latest = True
    db.flush()
    return row
