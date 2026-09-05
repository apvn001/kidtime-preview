"""规则模板 Schema（落地架构设计 §4.1）。

模板入参复用 `RuleProfileIn` 全部校验（11 参数），追加 name/description。
出参追加模板元信息与 `applied_device_count`（服务层聚合）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import ApiModel, UtcTimestamp
from app.schemas.rule import RuleProfileIn


class RuleTemplateIn(RuleProfileIn):
    """创建/更新模板：复用 `RuleProfileIn` 全部校验，追加 name/description。"""

    name: str = Field(..., min_length=1, max_length=64, description="模板名（必填）")
    description: str | None = Field(None, max_length=255, description="模板说明（可选）")

    @model_validator(mode="after")
    def _strip_name(self) -> "RuleTemplateIn":
        """名称去首尾空白后必须非空。"""
        text = (self.name or "").strip()
        if not text:
            raise ValueError("模板名称不能为空")
        object.__setattr__(self, "name", text)
        return self


class RuleTemplateOut(ApiModel):
    """模板出参：11 参数 + 元信息 + `applied_device_count`（服务层聚合）。"""

    id: int = Field(..., description="模板 id")
    name: str = Field(..., description="模板名")
    description: str | None = Field(None, description="模板说明")
    is_builtin: bool = Field(..., description="内置模板不可删除/改名")
    weekday_quota_minutes: int = Field(..., description="工作日配额分钟")
    weekend_quota_minutes: int = Field(..., description="周末配额分钟")
    allowed_start: str = Field(..., description="允许时段开始 HH:MM")
    allowed_end: str = Field(..., description="允许时段结束 HH:MM")
    continuous_limit_minutes: int = Field(..., description="连续使用上限分钟")
    break_minutes: int = Field(..., description="强制休息分钟")
    idle_minutes: int = Field(..., description="空闲判定分钟")
    reminder_points: list[int] = Field(..., description="提醒节点，去重降序")
    parent_mode_timeout_minutes: int = Field(..., description="家长模式超时分钟")
    sync_interval_seconds: int = Field(..., description="同步间隔秒")
    enforcement_enabled: bool = Field(..., description="模板默认管控态")
    version: int = Field(..., description="模板版本，单调递增")
    created_by: int | None = Field(None, description="创建人用户 id")
    created_by_name: str | None = Field(
        None, description="创建人姓名快照（V5 §2.2）：删账号时写入，历史可追溯"
    )
    created_at: UtcTimestamp = Field(..., description="创建时间 UTC")
    updated_at: UtcTimestamp = Field(..., description="更新时间 UTC")
    updated_by: int | None = Field(None, description="更新人用户 id")
    updated_by_name: str | None = Field(
        None, description="更新人姓名快照（V5 §2.2）：删账号时写入，历史可追溯"
    )
    applied_device_count: int = Field(0, ge=0, description="已应用该模板的设备数")


class RuleTemplateApplyIn(BaseModel):
    """`POST /rule-templates/{id}/apply` 入参：`all=true` 或 `device_ids` 至少其一。"""

    device_ids: list[str] | None = Field(
        None, max_length=200, description="目标设备 UUID 列表"
    )
    all: bool = Field(False, description="true 时应用到全部 active 设备")

    @model_validator(mode="after")
    def _check_target(self) -> "RuleTemplateApplyIn":
        """`all` 与 `device_ids` 至少满足其一（空列表视为未提供）。"""
        ids = self.device_ids or []
        if not self.all and not ids:
            raise ValueError("device_ids 与 all 至少提供其一")
        return self


class RuleTemplateApplyItem(BaseModel):
    """apply 跳过清单项。"""

    device_id: str = Field(..., description="设备 id")
    reason: str = Field(..., description="跳过原因，如 device_not_found / device_retired")


class RuleTemplateApplyOut(BaseModel):
    """apply 出参：成功应用与跳过清单。"""

    applied: list[str] = Field(default_factory=list, description="成功应用的设备 id")
    skipped: list[RuleTemplateApplyItem] = Field(
        default_factory=list, description="被跳过的设备及原因"
    )


class RuleTemplateDeviceOut(BaseModel):
    """模板应用状态出参（T7）：设备 + `is_tweaked` + `applied_at` 溯源。"""

    device_id: str = Field(..., description="设备 UUID")
    device_name: str = Field(..., description="设备展示名")
    online_status: str = Field(..., description="在线状态计算值（D08）")
    is_tweaked: bool = Field(..., description="设备 11 参数与模板任一不同")
    applied_at: UtcTimestamp | None = Field(
        None, description="设备规则更新时间（仅当规则溯源到本模板时非空）"
    )
