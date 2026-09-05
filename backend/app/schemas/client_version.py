"""客户端版本更新 Schema（PC 客户端更新 MVP 方案 A）。

契约要点（前后端务必一致）：

* ``VersionInfoOut`` 的字段名与客户端 ``update/update_client.py`` 解析的键**逐字对应**。
* 版本号统一 SemVer ``MAJOR.MINOR.PATCH``；``build_number`` 单独承载 CI 递增号，
  不参与「是否需要更新」的判定（判定只看三段式 version 与 min_version）。
* ``sha256`` 由发布方计算传入，后端不回源校验（已批准决策）。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from app.core.constants import CHANNEL_STABLE, CLIENT_CHANNELS
from app.schemas.common import ApiModel, UtcTimestamp

# SemVer 主体：三段式数字，允许可选的 ``-prerelease`` / ``+build`` 后缀（MVP 只用 stable，
# 后缀在比较时被忽略）。用于入参格式校验，防止把随意字符串写进库。
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


def _validate_semver(value: str, *, field_name: str) -> str:
    """校验并规范化 SemVer 字符串（去首尾空白）。

    Args:
        value: 待校验版本号。
        field_name: 出错提示里显示的字段名。

    Returns:
        去空白后的版本号。

    Raises:
        ValueError: 不符合 ``MAJOR.MINOR.PATCH`` 形式。
    """
    text = (value or "").strip()
    if not _SEMVER_RE.match(text):
        raise ValueError(f"{field_name} 必须是 MAJOR.MINOR.PATCH 形式的版本号")
    return text


class AdminVersionPublishIn(BaseModel):
    """管理端发布入参（幂等 upsert 的载荷）。"""

    version: str = Field(..., max_length=32, description="发布版本号 MAJOR.MINOR.PATCH")
    build_number: int = Field(..., ge=0, description="CI 构建号，单调递增")
    channel: str = Field(CHANNEL_STABLE, max_length=16, description="发布通道，MVP 仅 stable")
    min_version: str = Field(
        ..., max_length=32, description="强制更新下限，低于它必须更新"
    )
    download_url: str = Field(
        ..., min_length=1, max_length=2048, description="长期/公开下载直链"
    )
    sha256: str = Field(..., description="安装包 SHA-256（发布方计算，后端不回源校验）")
    size_bytes: int | None = Field(None, ge=0, description="安装包字节数，可选")
    release_notes: str | None = Field(None, max_length=8192, description="更新说明，可选")

    @field_validator("version", "min_version")
    @classmethod
    def _check_semver(cls, value: str) -> str:
        """version / min_version 必须是合法 SemVer。"""
        return _validate_semver(value, field_name="版本号")

    @field_validator("channel")
    @classmethod
    def _check_channel(cls, value: str) -> str:
        """通道必须在白名单内（与库表 CHECK 一致）。"""
        text = (value or "").strip() or CHANNEL_STABLE
        if text not in CLIENT_CHANNELS:
            raise ValueError(f"channel 仅支持：{', '.join(CLIENT_CHANNELS)}")
        return text

    @field_validator("sha256")
    @classmethod
    def _check_sha256(cls, value: str) -> str:
        """SHA-256 必须是 64 位十六进制。"""
        text = (value or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", text):
            raise ValueError("sha256 必须是 64 位十六进制字符串")
        return text


class VersionInfoOut(ApiModel):
    """版本详情出参（``GET /client/version/latest`` 与 check 内嵌的 latest）。"""

    version: str = Field(..., description="版本号 MAJOR.MINOR.PATCH")
    build_number: int = Field(..., description="CI 构建号")
    channel: str = Field(..., description="发布通道")
    min_version: str = Field(..., description="强制更新下限")
    download_url: str = Field(..., description="下载直链")
    sha256: str = Field(..., description="安装包 SHA-256")
    size_bytes: int | None = Field(None, description="安装包字节数")
    release_notes: str | None = Field(None, description="更新说明")
    published_at: UtcTimestamp = Field(..., description="发布时间 UTC")


class VersionCheckIn(BaseModel):
    """客户端版本检查入参。"""

    device_id: str | None = Field(
        None, max_length=64, description="设备标识，仅记录不校验"
    )
    current_version: str = Field(..., max_length=32, description="客户端当前版本")
    channel: str = Field(CHANNEL_STABLE, max_length=16, description="发布通道")

    @field_validator("current_version")
    @classmethod
    def _check_current(cls, value: str) -> str:
        """当前版本必须是合法 SemVer。"""
        return _validate_semver(value, field_name="current_version")

    @field_validator("channel")
    @classmethod
    def _check_channel(cls, value: str) -> str:
        """通道回退到 stable 并校验白名单。"""
        text = (value or "").strip() or CHANNEL_STABLE
        if text not in CLIENT_CHANNELS:
            raise ValueError(f"channel 仅支持：{', '.join(CLIENT_CHANNELS)}")
        return text


class VersionCheckOut(BaseModel):
    """客户端版本检查出参。"""

    update_required: bool = Field(
        ..., description="强制更新：current < min_version"
    )
    optional_available: bool = Field(
        ..., description="可选更新：有更新版但非强制"
    )
    min_version: str | None = Field(
        None, description="当前通道强制更新下限；无发布时为 null"
    )
    latest: VersionInfoOut | None = Field(
        None, description="当前通道最新版本；无发布时为 null"
    )


class AssetOut(BaseModel):
    """下载物出参（``GET /client/update/asset``）。"""

    version: str = Field(..., description="版本号")
    download_url: str = Field(..., description="下载直链")
    sha256: str = Field(..., description="安装包 SHA-256")
    size_bytes: int | None = Field(None, description="安装包字节数")


class AdminVersionPublishOut(ApiModel):
    """管理端发布出参。"""

    id: str = Field(..., description="记录 id")
    version: str = Field(..., description="版本号")
    build_number: int = Field(..., description="CI 构建号")
    channel: str = Field(..., description="发布通道")
    is_latest: bool = Field(..., description="是否为该通道最新版")
    published_by_name: str | None = Field(None, description="发布人姓名快照")
    published_at: UtcTimestamp = Field(..., description="发布时间 UTC")
