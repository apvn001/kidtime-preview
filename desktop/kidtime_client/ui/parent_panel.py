"""Parent panel: status, history and local settings (PRD §4.5 / ARCH §5.20).

Everything shown here is read from the **local** database, so the panel works
exactly the same when the backend is unreachable (D45). Rule *editing* stays in
the web console -- the client only offers a local "pause enforcement" escape
hatch, which is itself audited as an event.

All widgets live on the GUI main thread, so every repository call made here is
compliant with hard constraint #2 (Single-Writer).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from kidtime_client.config import default_data_dir
from kidtime_client.constants import (
    APP_DISPLAY_NAME,
    CLIENT_VERSION,
    LOCK_STYLE_EYECARE,
    SETTING_AUTOSTART,
    SETTING_FLOATING_VISIBLE,
    SETTING_LOCK_STYLE,
    normalize_lock_style,
)
from kidtime_client.core.rules import RuleSnapshot, base_quota_for
from kidtime_client.core.state_machine import State
from kidtime_client.security.passwords import (
    WeakPasswordError,
    validate_parent_password,
)
from kidtime_client.ui.parent_mode import EXIT_MANUAL
from kidtime_client.ui.theme import (
    APP_STYLESHEET,
    format_minutes,
)

logger = logging.getLogger(__name__)

_STATE_TEXT: dict[State, str] = {
    State.DISABLED: "管控已暂停",
    State.GRACE: "临时放行中",
    State.PARENT: "家长模式",
    State.LOCKED_CURFEW: "非可用时段（已锁屏）",
    State.LOCKED_QUOTA: "今日额度用完（已锁屏）",
    State.BREAK: "强制休息中",
    State.IDLE_PAUSED: "长时间无操作，已暂停计时",
    State.ACTIVE: "正在使用",
}

#: 功能说明（「关于」页展示，与交付包「使用说明.txt」保持一致）。
_ABOUT_FEATURES: Final[str] = (
    "· 上机时长配额：按工作日 / 周末分别限制每日可用分钟数，额度用完立即锁屏。\n"
    "· 时段管控：限定每天允许使用的时间段（开始 / 结束），非时段内自动锁屏。\n"
    "· 连续使用休息：连续使用达到上限后强制休息，保护视力与作息。\n"
    "· 护眼锁屏：锁屏采用低蓝光护眼配色，柔和提示而非惩罚式界面。\n"
    "· 本地单机运行：全部数据仅存于本机，不联网、不上传任何信息到远端服务器。"
)

_EVENT_TEXT: dict[str, str] = {
    "STATE_CHANGED": "状态变化",
    "DAILY_USAGE": "每日用量",
    "RULE_UPDATED": "规则更新",
    "PARENT_MODE_ENTER": "进入家长模式",
    "PARENT_MODE_EXIT": "退出家长模式",
    "PASSWORD_FAILED": "密码输错",
    "EMERGENCY_GRACE": "应急放行",
    "RECOVERY_CODE_USED": "使用恢复码",
    "TIME_TAMPER": "系统时间异常",
    "ABNORMAL_EXIT": "异常退出",
    "SYNC_FAILED": "同步失败",
    "EXTENSION_SUBMITTED": "提交延时申请",
    "EXTENSION_CREDITED": "延时已入账",
    "COMMAND_EXECUTED": "执行远程指令",
    "DEVICE_PAIRED": "设备配对",
    "CREDENTIAL_REVOKED": "凭据被吊销",
    "TOKEN_REUSE_DETECTED": "凭据疑似泄露",
}

_HISTORY_DAYS = 7
_EVENT_LIMIT = 100


class ParentPanel(QDialog):
    """Read-only status plus a handful of local, audited settings.

    Args:
        engine: The running :class:`~kidtime_client.core.engine.RuntimeEngine`.
        repos: Aggregated repositories.
        parent_mode: The parent-mode controller (for password management).
        device_id: Paired device id, shown for support purposes.
        parent: Optional Qt parent.

    Attributes:
        floatingToggled: The floating badge visibility changed.
        autostartToggled: The autostart preference changed.
        quitRequested: The parent asked to quit the guard.
    """

    floatingToggled = Signal(bool)
    autostartToggled = Signal(bool)
    quitRequested = Signal()

    def __init__(
        self,
        engine: Any,
        repos: Any,
        parent_mode: Any,
        device_id: str = "",
        parent: QWidget | None = None,
        data_dir: Path | None = None,
    ) -> None:
        super().__init__(parent)
        # v1.4（PRD P0-1）：家长面板不再无条件置顶。是否置顶由 app 层在每次
        # show 前按「锁屏是否可见」动态决定（apply_always_on_top + _lock_active），
        # 家长模式下为普通层级便于自由调整参数，锁屏状态下保持置顶不被遮挡。
        self._engine = engine
        self._repos = repos
        self._parent_mode = parent_mode
        self._device_id = device_id
        # 1.3：证书来源只读行需要数据目录（P1-3/P1-4）。测试注入 tmp_path，
        # 生产由 app 层传 ``config.data_dir``；缺省时退回默认数据目录。
        self._data_dir = Path(data_dir) if data_dir is not None else default_data_dir()

        self.setWindowTitle(f"{APP_DISPLAY_NAME} · 家长面板")
        self.setModal(False)
        # 1.4.2 家长面板显示优化：把最小尺寸抬高到较舒适的默认，避免内容一上来就
        # 被压窄（配合 center_within_available_geometry 的等比例缩放，正常屏幕不会
        # 触发缩放；小屏 / 高 DPI 下缩放后仍由各页滚动区兜底，不挤压重叠）。
        self.setMinimumSize(720, 560)
        self.setStyleSheet(APP_STYLESHEET)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_overview_tab(), "概览")
        # preview：规则管控合并到设置页（第十二轮），tabs 固定为「概览/记录/设置」。
        self._tabs.addTab(self._build_history_tab(), "记录")
        self._tabs.addTab(self._build_settings_tab(), "设置")
        self._about_tab = self._build_about_tab()
        self._tabs.addTab(self._about_tab, "关于")
        layout.addWidget(self._tabs, 1)

        footer = QHBoxLayout()
        # 1.3（P0-2 / 约束 18）：「切换到孩子模式」与「退出家长模式」行为等价，
        # 合并为单一主按钮 —— 退出家长会话后状态机按规则自然流转。
        # 用 addStretch 顶住右侧，保持两个按钮仍靠右排列（布局不变）。
        footer.addStretch(1)
        close_button = QPushButton("切换到孩子模式")
        close_button.setObjectName("primary")
        close_button.clicked.connect(self._on_exit_parent_clicked)
        footer.addWidget(close_button)
        # 1.4.2 UI 优化：退出 KidTime 从设置页移出，固定在本按钮右侧，
        # 让家长一眼找到退出入口、且不与设置项混在一起。
        quit_button = QPushButton("退出 KidTime")
        quit_button.clicked.connect(self._on_quit_clicked)
        footer.addWidget(quit_button)
        layout.addLayout(footer)

        engine.tickCompleted.connect(self._on_tick)
        self.refresh()

    # ------------------------------------------------------------------
    # Tab construction
    # ------------------------------------------------------------------
    def _build_overview_tab(self) -> QWidget:
        """Build the "概览" tab.

        外层包 ``QScrollArea``：窗口较小时内容滚动而非挤压重叠。
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page = QWidget()
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        status_box = QGroupBox("当前状态")
        status_form = QFormLayout(status_box)
        self._lbl_state = QLabel("—")
        self._lbl_remaining = QLabel("—")
        self._lbl_used = QLabel("—")
        self._lbl_quota = QLabel("—")
        self._lbl_break = QLabel("—")
        status_form.addRow("状态", self._lbl_state)
        status_form.addRow("今日剩余", self._lbl_remaining)
        status_form.addRow("今日已用", self._lbl_used)
        status_form.addRow("今日额度", self._lbl_quota)
        status_form.addRow("休息次数", self._lbl_break)
        layout.addWidget(status_box)

        rules_box = QGroupBox("规则管控")
        rules_form = QFormLayout(rules_box)
        self._lbl_window = QLabel("—")
        self._lbl_quotas = QLabel("—")
        self._lbl_continuous = QLabel("—")
        self._lbl_rule_version = QLabel("—")
        rules_form.addRow("允许时段", self._lbl_window)
        rules_form.addRow("每日额度", self._lbl_quotas)
        rules_form.addRow("连续使用/休息", self._lbl_continuous)
        rules_form.addRow("规则版本", self._lbl_rule_version)
        # preview：规则编辑入口已并入「设置」页置顶的「规则管控」分组（第十三轮
        # 排版；详见 _build_settings_tab 顶部嵌入块），概览页仅保留只读摘要。
        layout.addWidget(rules_box)

        layout.addStretch(1)
        return scroll

    def _build_history_tab(self) -> QWidget:
        """Build the "记录" tab.

        外层包 ``QScrollArea``：窗口较小时内容滚动而非挤压重叠（用量表 / 事件表
        自身也可滚动，二者叠加保证任意尺寸都完整清晰）。
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page = QWidget()
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 1.4.2 UI 优化：用量与最近事件交换位置——最近事件上移、用量下移；
        # 两表可见行数由 _apply_history_heights 控制（事件 5 行、用量 2 行），
        # 超出部分在页内滚动区滚动，不挤压重叠。
        layout.addWidget(QLabel("最近事件"))
        self._event_table = QTableWidget(0, 3)
        self._event_table.setHorizontalHeaderLabels(["时间", "类型", "详情"])
        self._prepare_table(self._event_table)
        layout.addWidget(self._event_table)

        layout.addWidget(QLabel(f"最近 {_HISTORY_DAYS} 天用量"))
        self._usage_table = QTableWidget(0, 5)
        self._usage_table.setHorizontalHeaderLabels(
            ["日期", "已用", "额度", "加时", "休息"]
        )
        self._prepare_table(self._usage_table)
        layout.addWidget(self._usage_table)

        refresh_row = QHBoxLayout()
        refresh_row.addStretch(1)
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self._reload_tables)
        refresh_row.addWidget(refresh_button)
        layout.addLayout(refresh_row)
        return scroll

    def _build_settings_tab(self) -> QWidget:
        """Build the "设置" tab.

        外层包一层 ``QScrollArea``：窗口较小时内容滚动而非被挤压重叠（配合
        ``center_within_available_geometry`` 的等比例缩放，不同窗口尺寸下都能
        完整、清晰地显示）。
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page = QWidget()
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # 规则管控合并到设置页：作为设置页**第一个平级主体分组**置顶排版——
        # RulesPanel 自带「规则管控」分组标题、参数两列并排、无内嵌滚动区，
        # 与下方「本机设置」「安全」同处一个滚动页，一屏即可看全所有设置。
        from kidtime_client.ui.rules.rule_edit_dialog import RulesPanel

        self._rules_panel = RulesPanel()
        self._rules_panel.rulesCommitted.connect(self._on_rules_committed)
        layout.addWidget(self._rules_panel)

        local_box = QGroupBox("本机设置")
        local_layout = QVBoxLayout(local_box)
        self._chk_autostart = QCheckBox("开机自动启动")
        self._chk_autostart.toggled.connect(self._on_autostart_toggled)
        local_layout.addWidget(self._chk_autostart)
        self._chk_floating = QCheckBox("显示悬浮时间提示")
        self._chk_floating.toggled.connect(self._on_floating_toggled)
        local_layout.addWidget(self._chk_floating)
        # 「启用管控」由本机设置组统一控制，置于本组列表末位；规则管控的
        # 参数表单里不再出现该开关，避免两个入口语义冲突。
        self._chk_enforce = QCheckBox("启用管控")
        self._chk_enforce.toggled.connect(self._on_enforce_toggled)
        local_layout.addWidget(self._chk_enforce)

        layout.addWidget(local_box)

        security_box = QGroupBox("安全")
        security_layout = QHBoxLayout(security_box)
        change_button = QPushButton("修改家长密码")
        change_button.clicked.connect(self._on_change_password)
        security_layout.addWidget(change_button)
        # 恢复码是「忘记家长密码」的云端找回机制，单机闭环无意义，不提供。
        security_layout.addStretch(1)
        layout.addWidget(security_box)

        layout.addStretch(1)
        return scroll

    def _build_about_tab(self) -> QWidget:
        """Build the "关于" tab (与概览/记录/设置并列独立展示).

        页面风格与「概览」一致—— ``QScrollArea`` 容器 + 多个 ``QGroupBox``
        分组展示（版本信息 / 功能说明）。
        使用指引与免责条款不在此处展示（前者在交付包「使用说明.txt」，
        后者在首启弹窗 ``portable/risk_notice.py``）。
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page = QWidget()
        scroll.setWidget(page)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # --- 版本信息（QGroupBox + QFormLayout，与概览「当前状态」一致） ---
        version_box = QGroupBox("版本信息")
        version_form = QFormLayout(version_box)
        version_form.addRow("产品名称", QLabel("KidTime"))
        version_form.addRow("客户端版本", QLabel(f"v{CLIENT_VERSION}"))
        layout.addWidget(version_box)

        # --- 功能说明（每条独立一行，与概览 QGroupBox+逐项 Label 视觉一致） ---
        features_box = QGroupBox("功能说明")
        features_layout = QVBoxLayout(features_box)
        features_layout.setContentsMargins(12, 8, 12, 8)
        features_layout.setSpacing(4)
        for line in _ABOUT_FEATURES.split("\n"):
            if not line.strip():
                continue
            label = QLabel(line)
            label.setWordWrap(True)
            features_layout.addWidget(label)
        layout.addWidget(features_box)

        layout.addStretch(1)
        return scroll

    @staticmethod
    def _prepare_table(table: QTableWidget) -> None:
        """Apply the shared read-only table styling.

        Args:
            table: The table to configure.
        """
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeToContents)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """Reload everything from the local database."""
        self._reload_status()
        self._reload_settings()
        self._reload_tables()

    def _reload_status(self) -> None:
        """Refresh the overview labels from the engine and repositories."""
        rules = self._engine.rules
        record = self._repos.usage.get(self._engine.effective_date)
        base_quota = base_quota_for(rules, self._engine.effective_date)
        bonus = int(record.bonus_minutes) if record is not None else 0
        used = float(record.used_seconds) / 60.0 if record is not None else 0.0
        breaks = int(record.break_count) if record is not None else 0
        effective = base_quota + bonus

        state = self._engine.state
        self._lbl_state.setText(_STATE_TEXT.get(state, state.value))
        self._lbl_remaining.setText(format_minutes(max(0.0, effective - used)))
        self._lbl_used.setText(format_minutes(used))
        self._lbl_quota.setText(
            f"{format_minutes(effective)}（基础 {base_quota} 分钟 + 加时 {bonus} 分钟）"
        )
        self._lbl_break.setText(f"{breaks} 次")

        self._lbl_window.setText(f"{rules.allowed_start} - {rules.allowed_end}")
        self._lbl_quotas.setText(
            f"工作日 {rules.weekday_quota_minutes} 分钟 / "
            f"周末 {rules.weekend_quota_minutes} 分钟"
        )
        self._lbl_continuous.setText(
            f"连续 {rules.continuous_limit_minutes} 分钟 → 休息 {rules.break_minutes} 分钟"
        )
        self._lbl_rule_version.setText(f"v{rules.version}")
        # 单机形态：内嵌服务即本机，无「同步到家长后台」概念，概览不展示同步行。

    def _reload_settings(self) -> None:
        """Refresh the settings tab widgets without re-emitting signals."""
        settings = self._repos.settings
        # 单机形态：锁屏外观固定为米黄淡绿护眼样式（LOCK_STYLE_EYECARE）。
        # 存量数据若被写成其它值（历史版本残留），这里一次性纠正，避免 UI
        # 之外残留非护眼外观。引擎构造前（app._build_core）也会先写 eyecare。
        current_style = normalize_lock_style(settings.get(SETTING_LOCK_STYLE))
        if current_style != LOCK_STYLE_EYECARE:
            settings.set(SETTING_LOCK_STYLE, LOCK_STYLE_EYECARE)
        for widget, value in (
            (self._chk_autostart, settings.get_bool(SETTING_AUTOSTART, False)),
            (self._chk_floating, settings.get_bool(SETTING_FLOATING_VISIBLE, True)),
            (self._chk_enforce, self._engine.rules.enforcement_enabled),
        ):
            widget.blockSignals(True)
            widget.setChecked(bool(value))
            widget.blockSignals(False)

    def _reload_tables(self) -> None:
        """Refresh the usage and event tables."""
        records = self._repos.usage.recent(_HISTORY_DAYS)
        self._usage_table.setRowCount(len(records))
        for row, record in enumerate(records):
            cells = (
                record.usage_date.isoformat(),
                format_minutes(record.used_seconds / 60.0),
                f"{record.base_quota_minutes} 分钟",
                f"{record.bonus_minutes} 分钟",
                f"{record.break_count} 次",
            )
            for column, text in enumerate(cells):
                self._usage_table.setItem(row, column, QTableWidgetItem(text))

        events = self._repos.events.recent(_EVENT_LIMIT)
        self._event_table.setRowCount(len(events))
        for row, event in enumerate(events):
            local = self._to_local(event.occurred_at)
            cells = (
                f"{local:%m-%d %H:%M:%S}",
                _EVENT_TEXT.get(event.event_type, event.event_type),
                self._summarize(event.payload),
            )
            for column, text in enumerate(cells):
                self._event_table.setItem(row, column, QTableWidgetItem(text))
        # 数据刷新后按「可见行数」限制两表高度（事件 5 行 / 用量 2 行）。
        self._apply_history_heights()

    def _apply_history_heights(self) -> None:
        """限制记录页两表的可见行数：最近事件 5 行、用量 2 行。

        表内数据仍保留全部历史（事件 100 条 / 用量 7 天），仅把**可见高度**限制为
        指定行数，超出部分在本页滚动区内滚动，避免一次塞太多行被挤压重叠。
        """
        for table, visible_rows in ((self._event_table, 5), (self._usage_table, 2)):
            if table.rowCount() == 0:
                # 暂无数据时给一个最小高度，避免出现 0 高的怪异空白。
                table.setMaximumHeight(0)
                continue
            header_h = table.horizontalHeader().height()
            row_h = table.rowHeight(0) or 30
            table.setMaximumHeight(header_h + row_h * visible_rows)

    @staticmethod
    def _summarize(payload: dict) -> str:
        """Render an event payload as a compact one-liner.

        Args:
            payload: The stored JSON payload.

        Returns:
            A short human-readable summary.
        """
        if not payload:
            return "—"
        parts = [f"{key}={value}" for key, value in list(payload.items())[:4]]
        return " ".join(parts)[:180]

    def _to_local(self, moment: datetime) -> datetime:
        """Convert a UTC datetime into the engine's local timezone.

        Args:
            moment: A timezone-aware (or naive UTC) datetime.

        Returns:
            The same instant in local time.
        """
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        try:
            return moment.astimezone(ZoneInfo(self._engine.timezone_name))
        except Exception:  # pragma: no cover - tzdata missing
            return moment.astimezone()

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------
    @Slot(object)
    def _on_tick(self, _output: object) -> None:
        """Refresh the overview once per second while the panel is open."""
        if self.isVisible() and self._tabs.currentIndex() == 0:
            self._reload_status()

    def _on_autostart_toggled(self, checked: bool) -> None:
        """Persist and broadcast the autostart preference.

        Args:
            checked: New checkbox state.
        """
        self._repos.settings.set_bool(SETTING_AUTOSTART, checked)
        self.autostartToggled.emit(bool(checked))

    def _on_floating_toggled(self, checked: bool) -> None:
        """Persist and broadcast the floating-badge preference.

        Args:
            checked: New checkbox state.
        """
        self._repos.settings.set_bool(SETTING_FLOATING_VISIBLE, checked)
        self.floatingToggled.emit(bool(checked))

    @Slot(bool)
    def _on_enforce_toggled(self, checked: bool) -> None:
        """Enable or disable rule enforcement on this machine.

        与「启用管控」复选框（本机设置组末位）一一对应；引擎会同时回写规则
        快照的 ``enforcement_enabled`` 与 ``enforcement_paused`` 设置。

        Args:
            checked: New checkbox state.
        """
        self._engine.apply_enforcement(bool(checked))

    def _on_change_password(self) -> None:
        """Prompt twice for a new parent password and store its hash."""
        first, ok = QInputDialog.getText(
            self, "修改家长密码", "请输入新密码（至少 6 位）：", QLineEdit.Password
        )
        if not ok:
            return
        try:
            validate_parent_password(first)
        except WeakPasswordError as exc:
            QMessageBox.warning(self, "密码不符合要求", str(exc))
            return
        second, ok = QInputDialog.getText(
            self, "修改家长密码", "请再次输入新密码：", QLineEdit.Password
        )
        if not ok:
            return
        if first != second:
            QMessageBox.warning(self, "两次输入不一致", "两次输入的密码不一样，请重试。")
            return
        self._parent_mode.set_parent_password(first)
        QMessageBox.information(self, "已更新", "家长密码已更新。")

    def _on_exit_parent_clicked(self) -> None:
        """「切换到孩子模式」：退出家长模式并关闭本面板。

        1.3（P0-2 / 约束 18）：「切换到孩子模式」与「退出家长模式」行为等价
        （退出家长会话后状态机按规则自然流转到 ACTIVE / 锁屏），合并为单一
        按钮。``manual`` exits leave closing the panel to the dialog itself
        (see ``KidTimeApp._on_parent_exited``), so the controller is told
        first and then the window is accepted -- this guarantees the
        parent-mode session is torn down even though the window disappears.
        """
        self._parent_mode.exit(EXIT_MANUAL)
        self.accept()

    def _on_quit_clicked(self) -> None:
        """Ask for confirmation before quitting the guard."""
        confirm = QMessageBox.question(
            self,
            "退出 KidTime",
            "退出后本机将不再统计和限制上机时间，确定退出吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm == QMessageBox.Yes:
            self.quitRequested.emit()
            self.accept()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Disconnect engine signals so the panel can be garbage collected.

        Args:
            event: The Qt close event.
        """
        for signal, slot in (
            (self._engine.tickCompleted, self._on_tick),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):  # pragma: no cover - already gone
                pass
        super().closeEvent(event)

    # ------------------------------------------------------------------
    def _on_rules_committed(self, rule_dict: dict) -> None:
        """规则已保存到内嵌后端：立即回写引擎并刷新概览（立即生效）。

        规则保存载荷不再携带 ``enforcement_enabled``，故 ``from_dict`` 会回落默认
        ``True`` 而覆盖用户已暂停的状态。这里在重建快照前注入引擎当前的暂停值，
        保证「启用管控」开关状态不被规则保存意外翻转。
        """
        try:
            rule_dict = dict(rule_dict)
            rule_dict.setdefault(
                "enforcement_enabled", self._engine.rules.enforcement_enabled
            )
            snapshot = RuleSnapshot.from_dict(rule_dict)
            self._engine.apply_rules(snapshot)
            self.refresh()
        except Exception as exc:  # pragma: no cover - 防御
            logger.warning("应用新规则到引擎失败：%s", exc)


__all__ = ["ParentPanel"]
