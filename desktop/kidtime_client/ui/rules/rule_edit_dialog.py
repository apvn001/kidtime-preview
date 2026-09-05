"""便携单机版规则管控面板。

把「规则管控」的 10 个字段编辑整合进家长面板设置页，作为设置页内的
**平级分组**（两列参数并排、不自带滚动区，随设置页统一滚动）。同步间隔
（sync_interval_seconds）已从 UI 移除（单机内嵌服务即本机、无外联同步
概念），后端字段仍保留，提交时由 payload 透传最近加载到的值；设备身份
来自首启引导保存的连接文件；全部写操作经 ``RulesEditorService`` 落到
内嵌后端，并在成功后把最新规则回写引擎（立即生效）。
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QTime, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from kidtime_client.portable.rules_editor import RulesEditorService
from kidtime_client.ui.theme import APP_STYLESHEET

logger = logging.getLogger(__name__)

# 字段校验范围（与后端 RuleProfileIn 一致，详见 backend/app/schemas/rule.py）。
_RANGE = {
    "weekday_quota_minutes": (0, 1440),
    "weekend_quota_minutes": (0, 1440),
    "continuous_limit_minutes": (10, 240),
    "break_minutes": (1, 60),
    "idle_minutes": (1, 60),
    "parent_mode_timeout_minutes": (5, 120),
    "sync_interval_seconds": (15, 300),
}

# 进程级共享规则服务（真机第六轮：内嵌后端 /auth/login 限流 10 次/60 秒，
# 面板每次打开都重新 login 会撞 429 →「规则服务不可用」。单例复用已登录客户端，
# 仅在首次创建时登录一次；创建失败不缓存，下次调用自动重试）。
_SHARED_SVC: RulesEditorService | None = None


def _get_shared_service() -> RulesEditorService | None:
    """返回进程级共享的规则服务；后端未就绪时返回 None（不缓存失败）。"""
    global _SHARED_SVC
    if _SHARED_SVC is None:
        try:
            _SHARED_SVC = RulesEditorService()
        except Exception as exc:
            logger.warning("规则服务不可用：%s", exc)
            return None
    return _SHARED_SVC


class RulesPanel(QWidget):
    """preview 规则管控面板（10 字段全量展示，两列排版）。

    嵌入家长面板设置页：本组件自带一个「规则管控」QGroupBox（标题即分组
    名），由设置页滚动区统一承载——**不自建滚动区、不要求外层再套壳**，
    与「本机设置」「安全」平级排版，一个界面即可看全所有要设置的内容。
    """

    #: 规则成功保存到内嵌后端并应立即回写引擎时触发，载荷为 RuleProfileOut 字典。
    rulesCommitted = Signal(dict)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setStyleSheet(APP_STYLESHEET)

        self._svc: RulesEditorService | None = None
        self._current: dict[str, Any] = {}

        self._build_ui()
        # 🔴 面板创建即建服务并加载当前规则（E2E 曾抓出漏调导致表单恒空的时序 bug）。
        self._ensure_service()
        self._update_service_state()
        if self._svc is not None:
            self._load_current()

    # ------------------------------------------------------------------
    # 服务生命周期
    # ------------------------------------------------------------------
    def _ensure_service(self) -> None:
        """惰性接入进程级共享规则服务（面板反复开关不重复登录）。"""
        self._svc = _get_shared_service()

    def _update_service_state(self) -> None:
        self._save_btn.setDisabled(self._svc is None)

    def _set_error(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.setStyleSheet("color: #c62828; font-size: 12px;")
        self._status_label.show()

    def _set_info(self, message: str) -> None:
        self._status_label.setText(message)
        self._status_label.setStyleSheet("color: #2e7d32; font-size: 12px;")
        self._status_label.show()

    def _load_current(self) -> None:
        """读取当前规则并回填表单；失败在面板内提示（不弹窗）。"""
        assert self._svc is not None
        try:
            self._current = self._svc.get_rules()
        except Exception as exc:
            logger.warning("加载规则失败：%s", exc)
            self._set_error(f"无法加载当前规则：{exc}")
            return
        self._status_label.hide()
        self._populate(self._current)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        """排版：单 QGroupBox「规则管控」，参数**两列并排**（由两个 QFormLayout
        左右拼入 QHBoxLayout）。不自带 QScrollArea——内嵌家长面板设置页时由设置页
        外层滚动区统一承载，避免「滚动套滚动」的双层观感（真机第十三轮：合并
        必须排版，而非把独立面板整块塞进去）。

        列划分：
          左列：工作日配额 / 周末配额 / 允许时段开始 / 允许时段结束 / 连续使用上限
          右列：强制休息 / 空闲判定 / 家长模式超时 / 提醒节点
          （「启用管控」已迁至「本机设置」组末位，见 parent_panel.py）
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        box = QGroupBox("规则管控")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(12, 8, 12, 8)
        box_layout.setSpacing(6)

        # --- 创建全部控件（属性名与历史 E2E/回填逻辑一致，勿改名） ---
        self._sp_weekday = self._spin(*_RANGE["weekday_quota_minutes"])
        self._sp_weekend = self._spin(*_RANGE["weekend_quota_minutes"])
        self._te_start = QTimeEdit()
        self._te_start.setDisplayFormat("HH:mm")
        self._apply_timeedit_style(self._te_start)
        self._te_end = QTimeEdit()
        self._te_end.setDisplayFormat("HH:mm")
        self._apply_timeedit_style(self._te_end)
        self._sp_continuous = self._spin(*_RANGE["continuous_limit_minutes"])
        self._sp_break = self._spin(*_RANGE["break_minutes"])
        self._sp_idle = self._spin(*_RANGE["idle_minutes"])
        self._sp_timeout = self._spin(*_RANGE["parent_mode_timeout_minutes"])
        self._le_points = QLineEdit()
        self._le_points.setFixedWidth(110)
        self._le_points.setFixedHeight(34)
        self._le_points.setAlignment(Qt.AlignCenter)
        self._le_points.setPlaceholderText("例 15,30,45")

        # --- 两列排布 ---
        columns = QHBoxLayout()
        columns.setSpacing(18)
        left_form = QFormLayout()
        left_form.setVerticalSpacing(6)
        right_form = QFormLayout()
        right_form.setVerticalSpacing(6)
        left_form.addRow("工作日配额（分钟）", self._sp_weekday)
        left_form.addRow("周末配额（分钟）", self._sp_weekend)
        left_form.addRow("允许时段开始", self._te_start)
        left_form.addRow("允许时段结束", self._te_end)
        left_form.addRow("连续使用上限（分钟）", self._sp_continuous)
        right_form.addRow("强制休息（分钟）", self._sp_break)
        right_form.addRow("空闲判定（分钟）", self._sp_idle)
        right_form.addRow("家长模式超时（分钟）", self._sp_timeout)
        right_form.addRow("提醒节点", self._le_points)
        columns.addLayout(left_form)
        columns.addLayout(right_form)
        box_layout.addLayout(columns)

        # --- 状态行 + 保存按钮 ---
        bottom = QHBoxLayout()
        self._status_label = QLabel("")
        self._status_label.setWordWrap(True)
        self._status_label.hide()
        bottom.addWidget(self._status_label, 1)
        self._save_btn = QPushButton("保存规则")
        self._save_btn.setObjectName("primary")
        self._save_btn.clicked.connect(self._on_save)
        bottom.addWidget(self._save_btn)
        box_layout.addLayout(bottom)

        root.addWidget(box)

    @staticmethod
    def _spin(minimum: int, maximum: int) -> QSpinBox:
        box = QSpinBox()
        box.setRange(minimum, maximum)
        box.setValue(minimum)
        box.setFixedWidth(110)
        # 真机第九轮：去掉上下调节按钮（用不上）。
        # 真机第十二轮：行高 26→34——上轮 26 在数字略大时有上下轻微遮挡。
        box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        box.setFixedHeight(34)
        box.setAlignment(Qt.AlignCenter)
        return box

    @staticmethod
    def _apply_timeedit_style(box: QTimeEdit) -> None:
        """时间框与数字框同规格：去上下按钮、定宽定高。"""
        box.setButtonSymbols(QAbstractSpinBox.NoButtons)
        box.setFixedWidth(110)
        # 行高 26→34（真机第十二轮）
        box.setFixedHeight(34)
        box.setAlignment(Qt.AlignCenter)

    # ------------------------------------------------------------------
    # 数据回填 / 收集
    # ------------------------------------------------------------------
    def _populate(self, rule: dict[str, Any]) -> None:
        """把 RuleProfileOut 字典回填到表单。"""
        self._sp_weekday.setValue(int(rule.get("weekday_quota_minutes", 0)))
        self._sp_weekend.setValue(int(rule.get("weekend_quota_minutes", 0)))
        self._te_start.setTime(
            QTime.fromString(str(rule.get("allowed_start", "07:00")), "HH:mm")
        )
        self._te_end.setTime(
            QTime.fromString(str(rule.get("allowed_end", "21:00")), "HH:mm")
        )
        self._sp_continuous.setValue(int(rule.get("continuous_limit_minutes", 10)))
        self._sp_break.setValue(int(rule.get("break_minutes", 1)))
        self._sp_idle.setValue(int(rule.get("idle_minutes", 1)))
        self._sp_timeout.setValue(int(rule.get("parent_mode_timeout_minutes", 30)))
        points = rule.get("reminder_points", []) or []
        self._le_points.setText(",".join(str(int(p)) for p in points))
        self.setToolTip(f"当前规则版本 v{rule.get('version', '?')}")

    def _collect_payload(self) -> dict[str, Any] | None:
        """从表单收集 RuleProfileIn 全量载荷；校验失败返回 None。"""
        points = self._parse_reminder_points()
        if points is None:
            return None
        return {
            "weekday_quota_minutes": self._sp_weekday.value(),
            "weekend_quota_minutes": self._sp_weekend.value(),
            "allowed_start": self._te_start.time().toString("HH:mm"),
            "allowed_end": self._te_end.time().toString("HH:mm"),
            "continuous_limit_minutes": self._sp_continuous.value(),
            "break_minutes": self._sp_break.value(),
            "idle_minutes": self._sp_idle.value(),
            "reminder_points": points,
            "parent_mode_timeout_minutes": self._sp_timeout.value(),
            # 真机第十二轮：同步间隔已从 UI 移除（preview 内嵌服务即本机），
            # 但后端 RuleProfileIn 字段仍必填——透传最近一次加载到的值，避免
            # 保存时因缺字段被 422。
            "sync_interval_seconds": int(self._current.get("sync_interval_seconds", 60)),
            # 「启用管控」不再由本表单提交（已迁至「本机设置」组末位）。载荷
            # 缺失该字段时，parent_panel._on_rules_committed 会注入引擎当前值，
            # 避免保存规则把已暂停状态重置为启用。
        }

    def _parse_reminder_points(self) -> list[int] | None:
        """解析「逗号分隔整数」提醒节点；校验 1-60、最多 5 个、去重降序。"""
        raw = self._le_points.text().strip()
        if not raw:
            QMessageBox.warning(self, "提醒节点有误", "请至少填写 1 个提醒节点（1-60 分钟）。")
            return None
        items: list[int] = []
        try:
            for part in raw.split(","):
                part = part.strip()
                if not part:
                    continue
                value = int(part)
                if value < 1 or value > 60:
                    raise ValueError(value)
                items.append(value)
        except ValueError:
            QMessageBox.warning(
                self, "提醒节点有误", "提醒节点必须为 1-60 的整数，逗号分隔，最多 5 个。"
            )
            return None
        if len(items) > 5:
            QMessageBox.warning(self, "提醒节点有误", "最多 5 个提醒节点。")
            return None
        return sorted(set(items), reverse=True)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_save(self) -> None:
        """保存 11 字段规则到内嵌后端（全量替换语义），成功后立即生效。"""
        self._ensure_service()
        self._update_service_state()
        if self._svc is None:
            QMessageBox.critical(
                self,
                "无法连接本机服务",
                "规则服务当前不可用，请确认已完成首启引导后重试。",
            )
            return
        payload = self._collect_payload()
        if payload is None:
            return
        try:
            resp = self._svc.save_rules(payload)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", f"无法保存规则：{exc}")
            return
        self._current = resp
        self._populate(resp)
        self.rulesCommitted.emit(resp)
        self._set_info(f"已保存，规则版本 v{resp.get('version', '?')}，立即生效。")

    def refresh(self) -> None:
        """对外刷新入口：重新读取当前规则（页签切换时可调用）。"""
        self._ensure_service()
        self._update_service_state()
        if self._svc is not None:
            self._load_current()


__all__ = ["RulesPanel"]
