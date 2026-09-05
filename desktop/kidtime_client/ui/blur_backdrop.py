"""统一毛玻璃底座（1.2 增量 · 架构文档 §D.3）。

1.2 把「启动门禁双入口锁屏」和「倒计时结束锁屏」统一到同一张视觉基线上：
一层自绘的品牌渐变 → 高斯模糊 → 上面压 scrim 和居中卡片。这个模块只负责
最底下那层**模糊后的品牌背景**，卡片和文字由各自的窗口去画。

🔴 隐私红线（PRD 硬约束 / 架构 §D.3）
    毛玻璃**只模糊我们自己画出来的渐变**。严禁以任何方式抓取 Windows 桌面
    内容——``QScreen.grabWindow`` / ``BitBlt`` / ``PrintWindow`` / DWM 共享
    表面全部不许出现。理由有两条：
      1. 锁屏一旦糊上桌面截图，孩子刚才在看什么就被留在了画面里；
      2. 家长模式下的锁屏可能被拍照外传，截图会连带泄露文件名和窗口标题。
    ``tests/test_blur_backdrop.py`` 会 grep 本文件源码来守这条线。

渲染管线（架构 §D.3.2）::

    画渐变(降采样尺寸)  ->  模糊(半径/BLUR_DOWNSCALE)  ->  平滑放大  ->  缓存
         ~0.3ms                    ~2ms                     ~4ms

性能预算：1920×1080 首帧 ≤20ms，4K@2x ≤35ms，倒计时每秒刷新 ≤2ms。
倒计时刷新走的是缓存命中路径（**不重算模糊**），所以第三项天然满足。

缓存失效只在三种情况发生，由 :class:`~kidtime_client.ui.overlay_manager.OverlayManager`
负责调用 :meth:`BrandBlurBackdrop.invalidate`：

* ``QGuiApplication.screenAdded`` / ``screenRemoved``
* ``QScreen.geometryChanged``
* DPR 变化（Windows 上体现为 ``QScreen.logicalDotsPerInchChanged``）
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict
from typing import Final

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPixmap

from kidtime_client.constants import LOCK_STYLE_DEFAULT, normalize_lock_style
from kidtime_client.ui.theme import (
    BLUR_DOWNSCALE,
    BLUR_RADIUS_BACKDROP,
    BRAND_GRADIENT_ANGLE_DEG,
    BRAND_GRADIENT_COLORS,
    BRAND_GRADIENT_STOPS,
    LOCK_STYLE_DEFAULT,
    LOCK_STYLE_GRADIENTS,
    SCRIM_WEAK,
)

logger = logging.getLogger(__name__)

#: 常规缓存上限（张）。双屏 + 一次 DPR 抖动也就 3~4 个键。
BACKDROP_CACHE_LIMIT: Final[int] = 4

#: 4K 及以上单张位图约 32MB，缓存降到 2 张避免常驻内存爆掉。
BACKDROP_CACHE_LIMIT_LARGE: Final[int] = 2

#: 判定「大屏」的物理像素阈值（3840×2160）。
LARGE_SURFACE_PIXELS: Final[int] = 3840 * 2160

#: 降级模糊的重采样轮数。每轮「缩小再放大」等价于一次盒式模糊，
#: 3 轮叠加已经非常接近高斯（中心极限定理）。
_RESAMPLE_PASSES: Final[int] = 3


def _gradient_endpoints(
    width: int, height: int, angle_deg: float
) -> tuple[float, float, float, float]:
    """按 CSS ``linear-gradient`` 语义算出渐变线的起止点。

    CSS 约定：0° 指向正上方，角度顺时针增大，所以 135° = 左上 → 右下。
    渐变线长度取 ``|w·sin a| + |h·cos a|``，保证渐变覆盖整个矩形的对角投影，
    四个角都不会出现「颜色没铺满」的死角。

    Args:
        width: 目标矩形宽度（像素）。
        height: 目标矩形高度（像素）。
        angle_deg: CSS 语义的角度（度）。

    Returns:
        ``(x1, y1, x2, y2)`` 起点与终点坐标。
    """
    radians = math.radians(angle_deg)
    # 屏幕坐标系 y 轴向下，故 dy 取 -cos。
    dir_x = math.sin(radians)
    dir_y = -math.cos(radians)
    length = abs(width * math.sin(radians)) + abs(height * math.cos(radians))
    center_x = width / 2.0
    center_y = height / 2.0
    half = length / 2.0
    return (
        center_x - dir_x * half,
        center_y - dir_y * half,
        center_x + dir_x * half,
        center_y + dir_y * half,
    )


def render_brand_gradient(
    width: int, height: int, colors: "tuple[str, str, str] | None" = None
) -> QImage:
    """把品牌渐变画到一张 ``width × height`` 的 ARGB 位图上。

    这是整条管线里唯一产生像素的地方，且像素全部来自
    :mod:`kidtime_client.ui.theme` 的令牌——不读屏、不读文件。

    Args:
        width: 位图宽度（像素，至少 1）。
        height: 位图高度（像素，至少 1）。
        colors: 三停止色 ``(start, mid, end)``。``None`` 时用默认蓝黑品牌
            渐变（``BRAND_GRADIENT_COLORS``）；调用方（如 ``BrandBlurBackdrop``）
            传入 ``LOCK_STYLE_GRADIENTS`` 以切换浅色低蓝光调色板（1.4.2 FR-1）。

    Returns:
        画好渐变的 :class:`QImage`（``Format_ARGB32_Premultiplied``）。
    """
    safe_width = max(1, int(width))
    safe_height = max(1, int(height))
    if colors is None:
        colors = BRAND_GRADIENT_COLORS

    image = QImage(safe_width, safe_height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(colors[-1]))

    x1, y1, x2, y2 = _gradient_endpoints(
        safe_width, safe_height, BRAND_GRADIENT_ANGLE_DEG
    )
    gradient = QLinearGradient(x1, y1, x2, y2)
    for stop, color in zip(BRAND_GRADIENT_STOPS, colors):
        gradient.setColorAt(float(stop), QColor(color))

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(0, 0, safe_width, safe_height, gradient)
    finally:
        # 必须显式 end()：QImage 上还挂着 painter 时任何拷贝都会触发 Qt 警告。
        painter.end()
    return image


def _blur_with_graphics_effect(image: QImage, radius: float) -> QImage | None:
    """首选路径：借 ``QGraphicsBlurEffect`` 做一次真正的高斯模糊。

    Args:
        image: 待模糊的位图。
        radius: 模糊半径（作用于 ``image`` 自身的像素尺度）。

    Returns:
        模糊后的位图；当运行环境不支持（例如没有 QApplication、
        或平台插件缺少必要能力）时返回 ``None``，由调用方降级。
    """
    try:
        from PySide6.QtWidgets import (
            QGraphicsBlurEffect,
            QGraphicsPixmapItem,
            QGraphicsScene,
        )

        source = QPixmap.fromImage(image)
        if source.isNull():
            return None

        effect = QGraphicsBlurEffect()
        effect.setBlurRadius(float(radius))
        effect.setBlurHints(QGraphicsBlurEffect.QualityHint)

        item = QGraphicsPixmapItem(source)
        item.setGraphicsEffect(effect)

        scene = QGraphicsScene()
        scene.addItem(item)

        output = QImage(image.size(), QImage.Format_ARGB32_Premultiplied)
        output.fill(Qt.transparent)
        painter = QPainter(output)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            scene.render(painter, target=output.rect(), source=item.boundingRect())
        finally:
            painter.end()
        # 显式断开：scene 析构时会连带删掉 item 和 effect，避免悬挂引用。
        scene.removeItem(item)
        item.setGraphicsEffect(None)
        return output
    except Exception:  # pragma: no cover - 取决于运行时平台插件
        logger.debug("QGraphicsBlurEffect 不可用，降级到重采样模糊", exc_info=True)
        return None


def _blur_by_resampling(image: QImage, radius: float) -> QImage:
    """降级路径：用「平滑缩小 + 平滑放大」逼近盒式模糊。

    为什么不写逐像素的 Python 盒式模糊：一张 480×270 的图有 12.9 万个像素，
    纯 Python 三趟分离卷积要几百毫秒，直接击穿 20ms 的首帧预算。Qt 的
    ``QImage.scaled(..., SmoothTransformation)`` 是 C++ 侧的双线性重采样，
    一次「缩小 N 倍再放大回来」在数学上就等价于一次半径约 N 的盒式模糊，
    叠 3 轮即接近高斯，而耗时仍在亚毫秒级。

    Args:
        image: 待模糊的位图。
        radius: 期望的模糊半径（作用于 ``image`` 自身的像素尺度）。

    Returns:
        模糊后的位图，尺寸与输入一致。
    """
    width = max(1, image.width())
    height = max(1, image.height())
    # 单轮盒式半径 ≈ radius / 轮数；再换算成缩放因子。
    per_pass = max(2, int(round(max(1.0, float(radius)) / _RESAMPLE_PASSES)) + 1)

    result = image
    for _ in range(_RESAMPLE_PASSES):
        small = result.scaled(
            max(1, width // per_pass),
            max(1, height // per_pass),
            Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation,
        )
        result = small.scaled(
            width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
        )
    return result


def blur_image(image: QImage, radius: float) -> QImage:
    """对位图做一次模糊，自动在首选/降级两条路径之间选择。

    Args:
        image: 待模糊的位图。
        radius: 模糊半径（作用于 ``image`` 自身的像素尺度）。

    Returns:
        模糊后的位图。
    """
    blurred = _blur_with_graphics_effect(image, radius)
    if blurred is not None and not blurred.isNull():
        return blurred
    return _blur_by_resampling(image, radius)


def paint_backdrop(
    painter: QPainter,
    size: QSize,
    device_pixel_ratio: float,
    backdrop: "BrandBlurBackdrop",
    scrim_alpha: float = SCRIM_WEAK,
    style: "str | None" = None,
) -> None:
    """把毛玻璃背景 + 一层轻遮罩画到 ``painter`` 上。

    门禁窗口和倒计时锁屏的 ``paintEvent`` 都走这一个入口，保证两块锁屏的底层
    观感逐像素一致。刻意接收 ``size``/``dpr`` 而不是 QWidget，这样本模块不必
    在顶层导入 QtWidgets。

    Args:
        painter: 已经 begin 在目标控件上的画笔。
        size: 目标控件的逻辑尺寸。
        device_pixel_ratio: 目标屏幕的设备像素比。
        backdrop: 共享的毛玻璃底座。
        scrim_alpha: 压在毛玻璃之上的黑色遮罩不透明度，``0`` 表示不压。
    """
    pixmap = backdrop.pixmap_for(size, device_pixel_ratio, style)
    painter.drawPixmap(0, 0, pixmap)
    if scrim_alpha > 0.0:
        alpha = int(round(min(1.0, max(0.0, float(scrim_alpha))) * 255))
        painter.fillRect(0, 0, size.width(), size.height(), QColor(0, 0, 0, alpha))


class BrandBlurBackdrop:
    """按尺寸缓存的品牌毛玻璃背景。

    所有锁屏窗口（门禁窗口与倒计时锁屏）共用**同一个实例**，这样双屏同分辨率
    时只渲染一次；不同分辨率则各占一个缓存键，互不干扰。

    Args:
        cache_limit: 常规缓存上限（张）。大表面会自动降到
            :data:`BACKDROP_CACHE_LIMIT_LARGE`。
        style: 初始锁屏外观（1.4.2）。由 ``app.py`` 在引擎加载完
            ``SETTING_LOCK_STYLE`` 后通过 :meth:`set_style` 同步。

    Attributes:
        render_count: 真正跑过多少次模糊管线。测试用它证明「倒计时每秒刷新
            不会重算模糊」。
    """

    def __init__(
        self,
        cache_limit: int = BACKDROP_CACHE_LIMIT,
        style: str = LOCK_STYLE_DEFAULT,
    ) -> None:
        self._limit = max(1, int(cache_limit))
        self._style = normalize_lock_style(style)
        # 键 = (逻辑宽, 逻辑高, DPR×100, style)，值 = 已设好 DPR 的 QPixmap。
        # 🔴 style 必须进键：三套样式共用一个实例，不带 style 会让护眼态
        #    直接命中默认蓝黑的缓存（技术方案 R-5）。
        self._cache: "OrderedDict[tuple[int, int, int, str], QPixmap]" = OrderedDict()
        self._render_count = 0

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def render_count(self) -> int:
        """累计执行模糊管线的次数（缓存命中不计）。"""
        return self._render_count

    @property
    def cache_size(self) -> int:
        """当前缓存中的位图张数。"""
        return len(self._cache)

    @property
    def style(self) -> str:
        """当前渲染所用的锁屏外观标识。"""
        return self._style

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def pixmap_for(
        self, size: QSize, device_pixel_ratio: float = 1.0, style: "str | None" = None
    ) -> QPixmap:
        """取一张覆盖 ``size`` 的毛玻璃背景。

        命中缓存时直接返回既有位图（这是倒计时每秒刷新走的路径，开销 ≈0）。

        Args:
            size: 目标**逻辑**尺寸，通常是 ``widget.size()``。
            device_pixel_ratio: 目标屏幕的设备像素比，高 DPI 下需按此倍数
                渲染物理像素，否则放大后会糊。
            style: 锁屏外观（1.4.2）。``None`` 时沿用实例当前样式
                （``self._style``，由 ``set_style`` 同步）；传入则按该样式
                取缓存键与渲染色。

        Returns:
            已设置好 ``devicePixelRatio`` 的 :class:`QPixmap`，可直接
            ``painter.drawPixmap(0, 0, pixmap)``。
        """
        width = max(1, int(size.width()))
        height = max(1, int(size.height()))
        dpr = max(1.0, float(device_pixel_ratio))
        effective_style = normalize_lock_style(style) if style else self._style
        key = (width, height, int(round(dpr * 100)), effective_style)

        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        pixmap = self._render(width, height, dpr, effective_style)
        self._cache[key] = pixmap
        self._cache.move_to_end(key)
        self._evict(width, height, dpr)
        return pixmap

    def invalidate(self) -> None:
        """丢弃全部缓存。

        只在屏幕拓扑真正变化时调用：``screenAdded`` / ``screenRemoved`` /
        ``geometryChanged`` / DPR 变化。**倒计时刷新绝不调用这里**，否则每秒
        重算一次模糊会把 CPU 吃满。
        """
        if not self._cache:
            return
        logger.debug("毛玻璃缓存失效，丢弃 %d 张", len(self._cache))
        self._cache.clear()

    def set_style(self, style: str) -> None:
        """切换锁屏外观并作废缓存（1.4.2 FR-1）。

        缓存键里已经带了 style，理论上不换键就不会串色；这里仍然显式
        ``invalidate()``，是为了不让「上一套样式」的位图常驻内存——三套样式
        × 双屏 × DPR 抖动很容易顶爆 4 张的上限（技术方案 R-5）。

        Args:
            style: 目标样式（``"default"|"eyecare"|"eyecare2"``）。
        """
        normalized = normalize_lock_style(style)
        if normalized == self._style:
            return
        self._style = normalized
        logger.info("毛玻璃底座切换外观：%s", normalized)
        self.invalidate()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _render(
        self, width: int, height: int, dpr: float, style: "str | None" = None
    ) -> QPixmap:
        """执行一次完整的渲染管线。

        Args:
            width: 逻辑宽度。
            height: 逻辑高度。
            dpr: 设备像素比。
            style: 锁屏外观（1.4.2），``None`` 时沿用实例当前样式。

        Returns:
            渲染好的位图。
        """
        physical_width = max(1, int(round(width * dpr)))
        physical_height = max(1, int(round(height * dpr)))

        small_width = max(1, physical_width // BLUR_DOWNSCALE)
        small_height = max(1, physical_height // BLUR_DOWNSCALE)

        effective_style = normalize_lock_style(style) if style else self._style
        colors = LOCK_STYLE_GRADIENTS.get(
            effective_style, LOCK_STYLE_GRADIENTS[LOCK_STYLE_DEFAULT]
        )
        # 直接在降采样尺寸上作画。线性渐变是尺度无关的，「全尺寸画完再缩小」
        # 与「直接小尺寸画」结果等价，但后者省掉一次全分辨率填充和一次
        # 平滑缩小——这正是 4K@2x 能压进 35ms 预算的关键。
        small = render_brand_gradient(small_width, small_height, colors)

        blurred = blur_image(small, BLUR_RADIUS_BACKDROP / BLUR_DOWNSCALE)

        full = blurred.scaled(
            physical_width,
            physical_height,
            Qt.IgnoreAspectRatio,
            Qt.SmoothTransformation,
        )

        pixmap = QPixmap.fromImage(full)
        pixmap.setDevicePixelRatio(dpr)
        self._render_count += 1
        logger.debug(
            "毛玻璃渲染 %dx%d @%.2fx（降采样 %dx%d），累计 %d 次",
            width,
            height,
            dpr,
            small_width,
            small_height,
            self._render_count,
        )
        return pixmap

    def _evict(self, width: int, height: int, dpr: float) -> None:
        """按 LRU 淘汰超出上限的缓存项。

        Args:
            width: 刚插入项的逻辑宽度。
            height: 刚插入项的逻辑高度。
            dpr: 刚插入项的设备像素比。
        """
        physical_pixels = int(round(width * dpr)) * int(round(height * dpr))
        limit = self._limit
        if physical_pixels >= LARGE_SURFACE_PIXELS:
            limit = min(limit, BACKDROP_CACHE_LIMIT_LARGE)
        while len(self._cache) > limit:
            evicted_key, _ = self._cache.popitem(last=False)
            logger.debug("毛玻璃缓存淘汰 %r（上限 %d）", evicted_key, limit)


__all__ = [
    "BACKDROP_CACHE_LIMIT",
    "BACKDROP_CACHE_LIMIT_LARGE",
    "LARGE_SURFACE_PIXELS",
    "BrandBlurBackdrop",
    "blur_image",
    "paint_backdrop",
    "render_brand_gradient",
]
