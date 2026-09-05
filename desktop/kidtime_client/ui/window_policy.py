"""统一窗口置顶策略（v1.4.0 增量 · PRD P0-1）。

家长模式交互优化：进入家长模式后，家长面板及相关窗口/弹窗**不再无条件置顶**，
便于家长自由调整参数、与其他窗口协作；但**锁屏状态下**（启动门禁或额度/宵禁/
休息锁屏可见时）仍保持置顶，避免家长控制窗口被全屏锁屏遮挡——安全门禁不能被
绕过。

置顶判据的唯一入口是 ``app.KidTimeApp._lock_active()``（门禁/锁屏可见即 True），
窗口自身不做判断。本模块只提供「改窗标志 + 让 Windows 即时生效」的机械动作。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget


def apply_always_on_top(widget: QWidget, enabled: bool) -> None:
    """Set or clear ``Qt.WindowStaysOnTopHint`` on ``widget``.

    Args:
        widget: The window whose top-most flag should change.
        enabled: ``True`` keeps the window on top; ``False`` restores the
            normal z-order.

    Notes:
        Changing window flags on Windows may not take effect while the window
        is already visible, so a visible window is briefly hidden and shown
        again to force the new flag to apply. Callers that set the flag before
        the first ``show()`` do not pay this cost. Idempotent: no-op when the
        flag already matches ``enabled``.
    """
    flag = Qt.WindowStaysOnTopHint
    if bool(widget.windowFlags() & flag) == enabled:
        return
    widget.setWindowFlag(flag, enabled)
    if widget.isVisible():
        widget.hide()
        widget.show()


def center_within_available_geometry(widget: QWidget) -> None:
    """把窗口放进「扣除任务栏后」的可见屏幕区域并居中，避免被桌面遮挡。

    窗口由 OS 默认放置时可能落在任务栏之下（尤其小屏 / 高 DPI 缩放），导致
    底部部分窗口被遮挡。这里用 ``availableGeometry()``（不含任务栏）重新夹位：

    * **等比例缩放**——取宽、高两个方向里更小的缩放比，让整窗按**原比例**缩入
      可用区，避免「只压高度不压宽度」导致的非等比压缩、文字控件被挤压变形重叠
      （旧实现把宽高各自独立 ``min`` 到可用区并 ``setMaximumSize`` 锁死，正是
      家长面板"被压扁、内容重叠"的根因）；
    * 仅在确实放不下时才**临时放开窗口最小尺寸约束**，缩到刚好容纳进可用区；
      内容由各页内部滚动区兜底，不会挤压重叠。窗口自身的最大尺寸**不**被我们
      锁死，大屏 / 分屏时用户仍可自由放大；
    * 在可用区内**居中**；
    * 用 ``setGeometry`` **原子地**设置位置与尺寸，规避 Windows 上 WM 默认放置
      覆盖窗口几何（也适用于 ``show()`` 之前调用，避免先闪一下默认位置再跳动）。

    Args:
        widget: 顶层窗口，可在 ``show()`` 之前或之后调用。
    """
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None:  # pragma: no cover - 无屏环境（offscreen 测试）
        return
    avail = screen.availableGeometry()

    # 取窗口预期尺寸；若尚未布局 / 刚 show 导致尺寸为 0，回退到 sizeHint()。
    size = widget.size()
    if size.width() <= 0 or size.height() <= 0:
        size = widget.sizeHint()

    # 等比例缩放：宽、高取同一缩放比，保证整窗按原比例缩入可用区，不变形。
    scale = min(
        1.0,
        avail.width() / max(size.width(), 1),
        avail.height() / max(size.height(), 1),
    )
    w = int(round(size.width() * scale))
    h = int(round(size.height() * scale))

    # 仅在需要比窗口自身最小尺寸更小才能放进可用区时，才临时把最小尺寸下调到
    # 缩放后尺寸（而非 0），让窗口能缩进可用区；内容靠内部滚动区兜底不重叠。
    if w < widget.minimumWidth() or h < widget.minimumHeight():
        widget.setMinimumSize(min(w, widget.minimumWidth()), min(h, widget.minimumHeight()))

    # 在可用区内居中。
    x = avail.x() + max(0, (avail.width() - w) // 2)
    y = avail.y() + max(0, (avail.height() - h) // 2)

    # 原子地设置位置与尺寸，规避 Windows 上 WM 默认放置覆盖；不调用 setMaximumSize，
    # 保留窗口自然最大约束，用户可自由缩放。
    widget.setGeometry(x, y, w, h)


__all__ = ["apply_always_on_top", "center_within_available_geometry"]
