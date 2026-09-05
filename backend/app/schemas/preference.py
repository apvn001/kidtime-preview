"""偏好 Schema（API.md §9.3 / §9.4）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.core.constants import KpiId
from app.schemas.common import ApiModel, UtcTimestamp

_VALID_KPI_IDS = {item.value for item in KpiId}

# 主题合法取值：旧值 light/dark/system 保持兼容，新增值 default/eyecare/eyecare2
ThemeName = Literal["light", "dark", "system", "default", "eyecare", "eyecare2"]


class PreferenceOut(ApiModel):
    """偏好出参。"""

    dashboard_layout: list[str] = Field(..., description="仪表盘 KPI 卡片顺序")
    theme: ThemeName = Field(..., description="主题")
    updated_at: UtcTimestamp = Field(..., description="更新时间 UTC")


class PreferenceIn(BaseModel):
    """偏好入参，全量替换。"""

    dashboard_layout: list[str] = Field(
        ..., min_length=1, max_length=5, description="仪表盘 KPI 卡片顺序，1-5 项"
    )
    theme: ThemeName = Field(..., description="主题")

    @field_validator("dashboard_layout")
    @classmethod
    def _check_layout(cls, value: list[str]) -> list[str]:
        """校验 KPI id 合法且去重（保持首次出现顺序）。"""
        invalid = [item for item in value if item not in _VALID_KPI_IDS]
        if invalid:
            raise ValueError(
                "存在非法的 KPI id：" + "、".join(invalid) + "；合法取值为 "
                + "、".join(sorted(_VALID_KPI_IDS))
            )
        seen: set[str] = set()
        deduped: list[str] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped
