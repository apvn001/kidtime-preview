"""后端通用小工具（无领域依赖，可安全被任意层 import）。"""

from __future__ import annotations


def parse_csv_filter(value: str | None) -> list[str]:
    """把逗号分隔的多值过滤参数拆成去空白、去空项的列表。

    供各 list 端点的多值筛选（status / type / severity / device_ids …）复用，
    消除散落在 services 与 endpoints 的 ``X.split(',')`` 样板（B4）。
    """
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]
