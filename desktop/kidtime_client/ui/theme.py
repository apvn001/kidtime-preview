"""Shared visual language for the client UI.

The tone is deliberately warm and non-punitive: KidTime is a family time
contract, not surveillance software. Lock screens explain *why* and *when the
child can come back*, never scold.
"""

from __future__ import annotations

from typing import Final

from kidtime_client.constants import (
    LOCK_STYLE_DEFAULT,
    LOCK_STYLE_EYECARE,
    LOCK_STYLE_EYECARE2,
    LOCK_STYLE_ORDER,
)

# --- Palette -----------------------------------------------------------
COLOR_BG: Final[str] = "#FFFFFF"
COLOR_SURFACE: Final[str] = "#F7F8FA"
COLOR_BORDER: Final[str] = "#E4E7ED"
COLOR_TEXT: Final[str] = "#1F2329"
COLOR_TEXT_WEAK: Final[str] = "#646A73"
COLOR_PRIMARY: Final[str] = "#3370FF"
COLOR_PRIMARY_DARK: Final[str] = "#245BDB"
COLOR_SUCCESS: Final[str] = "#34C724"
COLOR_WARNING: Final[str] = "#FF8800"
COLOR_DANGER: Final[str] = "#F54A45"

#: Overlay background (deep navy, softened so the desktop stays faintly visible).
COLOR_OVERLAY_BG: Final[str] = "#0F1B33"
COLOR_OVERLAY_TEXT: Final[str] = "#FFFFFF"
COLOR_OVERLAY_WEAK: Final[str] = "#A9B4C8"

#: Remaining-time thresholds used to colour the floating widget.
REMAINING_OK_MINUTES: Final[int] = 15
REMAINING_WARN_MINUTES: Final[int] = 5

FONT_FAMILY: Final[str] = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'

# =======================================================================
# 1.2 增量：统一毛玻璃锁屏基线（架构文档 §D.2）
# -----------------------------------------------------------------------
# 🔴 硬约束：以下所有令牌都是「自绘品牌背景」的描述，毛玻璃**只模糊我们自己
#    画出来的这张渐变图**。任何形式的 Windows 桌面截屏（QScreen.grabWindow /
#    BitBlt / PrintWindow / DWM 共享表面）都是明令禁止的——那既是隐私红线，
#    也会在锁屏上泄露孩子刚才在看什么。
# 🔴 既有令牌一个都没有改名，1.1 的调用点全部原样可用。
# =======================================================================

# --- 品牌渐变（三停止色 + 角度） ---------------------------------------
#: 渐变起点：偏亮的深靛蓝，落在卡片左上方，负责「有光源」的观感。
COLOR_BRAND_GRADIENT_START: Final[str] = "#1B2A4A"
#: 渐变中停：整张背景里最亮的一处，也是对比度校验最吃紧的一处（见下方断言）。
COLOR_BRAND_GRADIENT_MID: Final[str] = "#26406E"
#: 渐变终点：直接复用 1.1 的 overlay 底色，保证新旧锁屏在视觉上同源。
COLOR_BRAND_GRADIENT_END: Final[str] = COLOR_OVERLAY_BG

#: 渐变角度，采用 CSS ``linear-gradient`` 语义：0° 朝上、顺时针增大，
#: 135° = 左上 → 右下。
BRAND_GRADIENT_ANGLE_DEG: Final[int] = 135

#: 三个停止点在渐变线上的位置（0.0 ~ 1.0）。中停放在 0.52 而非 0.5，
#: 让亮部略微偏向右下，避免和居中卡片的高光叠在一起显脏。
BRAND_GRADIENT_STOPS: Final[tuple[float, float, float]] = (0.0, 0.52, 1.0)

#: 按顺序排列的三个停止色，供渲染与对比度断言统一遍历。
BRAND_GRADIENT_COLORS: Final[tuple[str, str, str]] = (
    COLOR_BRAND_GRADIENT_START,
    COLOR_BRAND_GRADIENT_MID,
    COLOR_BRAND_GRADIENT_END,
)

# --- 模糊参数 -----------------------------------------------------------
#: 逻辑模糊半径（相对最终全尺寸画面而言）。
BLUR_RADIUS_BACKDROP: Final[int] = 48

#: 降采样倍数。先缩到 1/4 再用 ``BLUR_RADIUS_BACKDROP / BLUR_DOWNSCALE``
#: 的半径模糊，最后平滑放大——像素量降到 1/16，视觉几乎无损。
BLUR_DOWNSCALE: Final[int] = 4

# --- 遮罩（scrim）三档 --------------------------------------------------
# 压在毛玻璃之上的黑色半透明层，用来把局部背景压暗以托住文字。
# 数值是 alpha（0.0 ~ 1.0）。
#: 轻档：大面积背景压暗，几乎不改变观感。
SCRIM_WEAK: Final[float] = 0.18
#: 中档：卡片本体的底色，保证卡片边界可辨。
SCRIM_MEDIUM: Final[float] = 0.32
#: 重档：倒计时等超大字号区域，需要最强的托底。
SCRIM_STRONG: Final[float] = 0.48

# --- 居中卡片（锁屏方案 A） ---------------------------------------------
#: 卡片逻辑宽度（px）。窄屏时由渲染层按可用宽度收缩。
CARD_WIDTH: Final[int] = 560
#: 卡片最小逻辑高度（px），保证内容少时也不会塌成一条。
CARD_MIN_HEIGHT: Final[int] = 320
#: 卡片圆角半径（px）。
CARD_RADIUS: Final[int] = 24
#: 卡片内边距（px）。
CARD_PADDING: Final[int] = 40

#: 主要按钮的最小尺寸 ``(宽, 高)``。48px 高度是触屏/一体机上的可点击下限。
BUTTON_MIN_SIZE: Final[tuple[int, int]] = (168, 48)

# --- 四档字号 -----------------------------------------------------------
#: 展示级：倒计时数字。
FONT_SIZE_DISPLAY: Final[int] = 56
#: 标题级：锁屏主标题。
FONT_SIZE_TITLE: Final[int] = 34
#: 正文级：解释性句子。
FONT_SIZE_BODY: Final[int] = 17
#: 辅助级：下次可用时间等次要信息。
FONT_SIZE_CAPTION: Final[int] = 14

#: WCAG AA 正文对比度门槛（4.5:1）。
WCAG_AA_CONTRAST: Final[float] = 4.5

#: 品牌渐变任一停止色允许的相对亮度上限。
#:
#: 推导：最弱的前景色是 ``COLOR_OVERLAY_WEAK`` (#A9B4C8)，其相对亮度
#: ``L_fg ≈ 0.4525``。要满足 ``(L_fg + 0.05) / (L_bg + 0.05) >= 4.5``，
#: 即 ``L_bg <= (0.4525 + 0.05) / 4.5 - 0.05 ≈ 0.0617``。向下取整留出余量后
#: 定为 0.0611——只要三个停止色都在这条线以下，弱文本在渐变的**任意**位置
#: 都稳过 AA。``tests/test_blur_backdrop.py`` 用纯计算断言守着这条线。
MAX_BRAND_GRADIENT_LUMINANCE: Final[float] = 0.0611

# =======================================================================
# 1.4.2 锁屏外观三样式（默认蓝黑 / 护眼浅色 / 护眼2 暖橙米色）
# -----------------------------------------------------------------------
# 默认样式**逐字节复用**既有 ``BRAND_GRADIENT_*``（1.2 起的观感零回归）；
# 下面新增的是两套浅色低蓝光调色板 + 浅色下的深墨文字 / 卡片 / 浮窗底色。
#
# 🔴 浅色方案的文字必须用 ``COLOR_EYECARE_TEXT`` / ``COLOR_EYECARE_WEAK``
#    这类深墨色，绝不能沿用深底上的 ``COLOR_OVERLAY_TEXT``(#FFFFFF) /
#    ``COLOR_OVERLAY_WEAK``(#A9B4C8) —— 那会把正文对比度打到 1.1:1 左右，
#    直接跌破 WCAG AA（FR-11 / NFR-1）。``tests/test_blur_backdrop.py`` 用
#    ``contrast_ratio`` 纯计算守着这条线。
# =======================================================================

#: 护眼（浅色低蓝光：柔和米黄 → 淡绿），色值来自已验收 HTML 原型（FR-1）。
COLOR_EYECARE_GRADIENT_START: Final[str] = "#F8F3E3"
COLOR_EYECARE_GRADIENT_MID: Final[str] = "#EAF1DD"
COLOR_EYECARE_GRADIENT_END: Final[str] = "#DCE9CE"

#: 护眼 2（暖橙米色低蓝光），色值来自已验收 HTML 原型（FR-1）。
COLOR_EYECARE2_GRADIENT_START: Final[str] = "#FBEFD9"
COLOR_EYECARE2_GRADIENT_MID: Final[str] = "#F6E2C0"
COLOR_EYECARE2_GRADIENT_END: Final[str] = "#EECBA0"

#: 浅色方案主文字（深墨色）。对 ``#F8F3E3`` ≈11.5:1，对最暗的 ``#EECBA0``
#: 仍 ≥8:1，远高于 4.5 的门槛。
COLOR_EYECARE_TEXT: Final[str] = COLOR_TEXT
#: 浅色方案弱文字（解释性正文）。对最暗停止色 ≈8.4:1。
COLOR_EYECARE_WEAK: Final[str] = "#3A3F47"
#: 浅色方案辅助文字（下次可用时间等）。
COLOR_EYECARE_CAPTION: Final[str] = "#4A4F58"

#: 浅色方案卡片底色（近不透明的暖白，托住深墨文字）。
COLOR_EYECARE_CARD_BG: Final[str] = "#FBFCF8"
#: 浅色方案卡片描边（淡绿灰，避免纯白卡片糊在浅底上）。
COLOR_EYECARE_CARD_BORDER: Final[str] = "#D9E2CC"

#: 护眼态浮窗底色（FR-2 降蓝光）：替代默认的深蓝 ``rgba(15, 27, 51, 232)``。
COLOR_FLOATING_BG_EYECARE: Final[str] = "rgba(251, 239, 217, 232)"
#: 护眼 2 态浮窗底色（更暖一档的米橙）。
COLOR_FLOATING_BG_EYECARE2: Final[str] = "rgba(246, 226, 192, 232)"
#: 默认样式的浮窗底色（1.4 既有深蓝，原样保留）。
COLOR_FLOATING_BG_DEFAULT: Final[str] = "rgba(15, 27, 51, 232)"

#: 按样式取浮窗底色（FR-2）。
LOCK_STYLE_FLOATING_BG: Final[dict[str, str]] = {
    LOCK_STYLE_DEFAULT: COLOR_FLOATING_BG_DEFAULT,
    LOCK_STYLE_EYECARE: COLOR_FLOATING_BG_EYECARE,
    LOCK_STYLE_EYECARE2: COLOR_FLOATING_BG_EYECARE2,
}

#: 家长面板 / 网页后台展示用的中文名。
LOCK_STYLE_LABELS: Final[dict[str, str]] = {
    LOCK_STYLE_DEFAULT: "默认（蓝黑渐变）",
    LOCK_STYLE_EYECARE: "护眼（米黄淡绿·低蓝光）",
    LOCK_STYLE_EYECARE2: "护眼 2（暖橙米色·低蓝光）",
}


def _srgb_to_linear(channel: float) -> float:
    """把单个 sRGB 通道值线性化（WCAG 2.1 relative luminance 第一步）。

    Args:
        channel: 归一化后的通道值，取值范围 ``[0.0, 1.0]``。

    Returns:
        线性光强度。
    """
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def parse_hex_color(hex_color: str) -> tuple[int, int, int]:
    """把 ``#RRGGBB`` 解析成 ``(r, g, b)`` 整数三元组。

    Args:
        hex_color: 形如 ``"#1B2A4A"`` 的十六进制颜色（``#`` 可省略，
            大小写不敏感，也接受 ``#RGB`` 短写）。

    Returns:
        ``(r, g, b)``，每个分量取值 ``0..255``。

    Raises:
        ValueError: 当字符串不是合法的 3 位或 6 位十六进制颜色时。
    """
    text = hex_color.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"不是合法的十六进制颜色：{hex_color!r}")
    try:
        value = int(text, 16)
    except ValueError as exc:  # pragma: no cover - 由上面的长度校验兜住大部分
        raise ValueError(f"不是合法的十六进制颜色：{hex_color!r}") from exc
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def relative_luminance(hex_color: str) -> float:
    """计算颜色的 WCAG 2.1 相对亮度。

    Args:
        hex_color: 形如 ``"#0F1B33"`` 的十六进制颜色。

    Returns:
        相对亮度 ``L``，取值 ``[0.0, 1.0]``；纯黑为 0.0，纯白为 1.0。
    """
    red, green, blue = parse_hex_color(hex_color)
    r_lin = _srgb_to_linear(red / 255.0)
    g_lin = _srgb_to_linear(green / 255.0)
    b_lin = _srgb_to_linear(blue / 255.0)
    return 0.2126 * r_lin + 0.7152 * g_lin + 0.0722 * b_lin


def contrast_ratio(foreground: str, background: str) -> float:
    """计算两色之间的 WCAG 对比度。

    Args:
        foreground: 前景（文字）颜色。
        background: 背景颜色。

    Returns:
        对比度比值，范围 ``[1.0, 21.0]``。AA 级正文要求 ``>= 4.5``。
    """
    lum_a = relative_luminance(foreground)
    lum_b = relative_luminance(background)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


# =======================================================================
# 1.4.2 锁屏外观三样式（契约令牌）
# -----------------------------------------------------------------------
# 下面这组常量由视觉核心契约（team-lead 下发）严格规定名称，另一名工程师的
# engine / overlay / command_handler 等模块依赖这些**确切名称**，请勿改名。
# =======================================================================
#: 每套样式的品牌渐变三停止色（default 复用 ``BRAND_GRADIENT_COLORS``；
#: eyecare / eyecare2 直接引用 ``COLOR_EYECARE*_GRADIENT_*`` 常量，不再硬编码十六进制，
#: 与上方 ``COLOR_*`` 单一来源一致，消除分叉——blur_backdrop 消费此字典）。
LOCK_STYLE_GRADIENTS: Final[dict[str, tuple[str, str, str]]] = {
    LOCK_STYLE_DEFAULT: (
        COLOR_BRAND_GRADIENT_START, COLOR_BRAND_GRADIENT_MID, COLOR_BRAND_GRADIENT_END,
    ),
    LOCK_STYLE_EYECARE: (
        COLOR_EYECARE_GRADIENT_START, COLOR_EYECARE_GRADIENT_MID, COLOR_EYECARE_GRADIENT_END,
    ),
    LOCK_STYLE_EYECARE2: (
        COLOR_EYECARE2_GRADIENT_START, COLOR_EYECARE2_GRADIENT_MID, COLOR_EYECARE2_GRADIENT_END,
    ),
}

#: 每套样式的正文/标题主色（深底白字、浅底深墨字）。
LOCK_STYLE_TEXT: Final[dict[str, str]] = {
    LOCK_STYLE_DEFAULT: COLOR_OVERLAY_TEXT,
    LOCK_STYLE_EYECARE: "#26331F",
    LOCK_STYLE_EYECARE2: "#3A2E1E",
}

#: 每套样式的弱文字色（解释性正文 / 卡片描边 / 危险按钮文字）。
#: 🔴 浅色方案（eyecare / eyecare2）的弱文字统一引用 ``COLOR_EYECARE_WEAK``
#: （#3A3F47，深墨色），与上方红线约束「浅色方案文字必须用 COLOR_EYECARE_WEAK」
#: 一致，消除 LOCK_STYLE_WEAK 与 COLOR_EYECARE_WEAK 两处取值分叉（D3）。
LOCK_STYLE_WEAK: Final[dict[str, str]] = {
    LOCK_STYLE_DEFAULT: COLOR_OVERLAY_WEAK,
    LOCK_STYLE_EYECARE: COLOR_EYECARE_WEAK,
    LOCK_STYLE_EYECARE2: COLOR_EYECARE_WEAK,
}

#: 每套样式的主行动按钮色（``lockPrimary`` 背景）。
LOCK_STYLE_ACCENT: Final[dict[str, str]] = {
    LOCK_STYLE_DEFAULT: COLOR_PRIMARY,
    LOCK_STYLE_EYECARE: "#5B8A3A",
    LOCK_STYLE_EYECARE2: "#D08A2E",
}

#: 每套样式的主行动按钮 hover 色（``lockPrimary:hover`` 背景）。
LOCK_STYLE_ACCENT_DARK: Final[dict[str, str]] = {
    LOCK_STYLE_DEFAULT: COLOR_PRIMARY_DARK,
    LOCK_STYLE_EYECARE: "#4A7230",
    LOCK_STYLE_EYECARE2: "#B5751F",
}


def lock_surface_stylesheet(style: str = LOCK_STYLE_DEFAULT) -> str:
    """统一锁屏表面的样式表（门禁窗口与倒计时锁屏共用）。

    1.2 把两块锁屏收敛到同一套 objectName 约定上，避免两边各写一份几乎相同
    却又微妙不一致的内联样式：

    ==================  ==================================================
    objectName          用途
    ==================  ==================================================
    ``lockCard``        居中卡片容器（方案 A 的主体）
    ``lockTitle``       卡片主标题
    ``lockDetail``      解释性正文
    ``lockCountdown``   超大号倒计时数字
    ``lockCaption``     下次可用时间等辅助信息
    ``lockPrimary``     主行动按钮（实心）
    ``lockGhost``       次要按钮（描边）
    ``lockDanger``      危险操作按钮（关机）
    ==================  ==================================================

    Args:
        style: 锁屏外观（``"default"|"eyecare"|"eyecare2"``）。默认样式的
            输出与 1.4.1 逐字节一致，保证既有渲染零回归；护眼两套换成
            浅色卡片 + 深墨文字（FR-11 对比度 ≥ 4.5:1）。

    Returns:
        可直接 ``setStyleSheet()`` 的 QSS 字符串。
    """
    button_width, button_height = BUTTON_MIN_SIZE
    is_light = style in (LOCK_STYLE_EYECARE, LOCK_STYLE_EYECARE2)
    if is_light:
        card_bg = rgba_css("#FFFFFF", 0.62)
        card_border = rgba_css("#000000", 0.06)
        title_color = LOCK_STYLE_TEXT[style]
        detail_color = LOCK_STYLE_WEAK[style]
        caption_color = LOCK_STYLE_WEAK[style]
        ghost_text = LOCK_STYLE_TEXT[style]
        ghost_border = LOCK_STYLE_WEAK[style]
        ghost_hover_bg = rgba_css("#000000", 0.04)
        danger_text = LOCK_STYLE_WEAK[style]
        danger_border = rgba_css("#000000", 0.18)
        # 浅底上把 hover 文字压成深墨，纯白会在浅底上糊掉。
        danger_hover_text = LOCK_STYLE_TEXT[style]
    else:
        card_bg = rgba_css("#000000", SCRIM_MEDIUM)
        card_border = rgba_css("#FFFFFF", 0.10)
        title_color = LOCK_STYLE_TEXT[style]
        detail_color = LOCK_STYLE_WEAK[style]
        caption_color = LOCK_STYLE_WEAK[style]
        ghost_text = LOCK_STYLE_TEXT[style]
        ghost_border = LOCK_STYLE_WEAK[style]
        ghost_hover_bg = rgba_css("#FFFFFF", 0.08)
        danger_text = LOCK_STYLE_WEAK[style]
        danger_border = rgba_css("#FFFFFF", 0.24)
        danger_hover_text = "#FFFFFF"
    return f"""
QFrame#lockCard {{
    background: {card_bg};
    border: 1px solid {card_border};
    border-radius: {CARD_RADIUS}px;
}}
QLabel#lockTitle {{
    color: {title_color};
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_TITLE}px;
    font-weight: 700;
    background: transparent;
}}
QLabel#lockDetail {{
    color: {detail_color};
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}px;
    background: transparent;
}}
QLabel#lockCountdown {{
    color: {title_color};
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_DISPLAY}px;
    font-weight: 300;
    background: transparent;
}}
QLabel#lockCaption {{
    color: {caption_color};
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}px;
    background: transparent;
}}
QPushButton#lockPrimary {{
    background: {LOCK_STYLE_ACCENT[style]};
    color: #FFFFFF;
    border: none;
    border-radius: 10px;
    padding: 12px 24px;
    min-width: {button_width}px;
    min-height: {button_height}px;
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}px;
    font-weight: 600;
}}
QPushButton#lockPrimary:hover {{
    background: {LOCK_STYLE_ACCENT_DARK[style]};
}}
QPushButton#lockGhost {{
    background: transparent;
    color: {ghost_text};
    border: 1px solid {ghost_border};
    border-radius: 10px;
    padding: 12px 24px;
    min-width: {button_width}px;
    min-height: {button_height}px;
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_BODY}px;
}}
QPushButton#lockGhost:hover {{
    border-color: {ghost_text};
    background: {ghost_hover_bg};
}}
QPushButton#lockDanger {{
    background: transparent;
    color: {danger_text};
    border: 1px solid {danger_border};
    border-radius: 10px;
    padding: 10px 20px;
    min-height: {button_height}px;
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE_CAPTION}px;
}}
QPushButton#lockDanger:hover {{
    color: {danger_hover_text};
    border-color: {COLOR_DANGER};
    background: {rgba_css("#F54A45", 0.18)};
}}
"""


def rgba_css(hex_color: str, alpha: float) -> str:
    """把十六进制颜色 + alpha 拼成 Qt 样式表可用的 ``rgba(...)``。

    Args:
        hex_color: 形如 ``"#000000"`` 的十六进制颜色。
        alpha: 不透明度，``0.0`` 全透明 ~ ``1.0`` 全不透明（自动夹取）。

    Returns:
        形如 ``"rgba(0, 0, 0, 0.32)"`` 的字符串。
    """
    red, green, blue = parse_hex_color(hex_color)
    clamped = min(1.0, max(0.0, float(alpha)))
    return f"rgba({red}, {green}, {blue}, {clamped:.3f})"


def remaining_color(remaining_minutes: float) -> str:
    """Pick the colour that represents ``remaining_minutes``.

    Args:
        remaining_minutes: Minutes of quota left today.

    Returns:
        A hex colour string: green above 15 minutes, orange from 15 down to 5,
        red below 5.
    """
    if remaining_minutes > REMAINING_OK_MINUTES:
        return COLOR_SUCCESS
    if remaining_minutes > REMAINING_WARN_MINUTES:
        return COLOR_WARNING
    return COLOR_DANGER


def format_minutes(minutes: float) -> str:
    """Render a minute count as a friendly Chinese string.

    Args:
        minutes: Minute count (may be fractional).

    Returns:
        ``"1 小时 25 分钟"`` / ``"25 分钟"`` / ``"不到 1 分钟"``.
    """
    total = int(max(0.0, minutes))
    if total <= 0:
        return "不到 1 分钟"
    hours, rest = divmod(total, 60)
    if hours and rest:
        return f"{hours} 小时 {rest} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{rest} 分钟"


def format_countdown(seconds: int | None) -> str:
    """Render a countdown as ``MM:SS``.

    Args:
        seconds: Remaining seconds, or ``None``.

    Returns:
        ``"--:--"`` when ``seconds`` is ``None``, otherwise ``"09:58"``.
    """
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


#: Stylesheet applied to the whole application.
APP_STYLESHEET: Final[str] = f"""
QWidget {{
    font-family: {FONT_FAMILY};
    font-size: 14px;
    color: {COLOR_TEXT};
}}
QDialog, QMainWindow {{
    background: {COLOR_BG};
}}
QLabel#Title {{
    font-size: 20px;
    font-weight: 600;
}}
QLabel#Subtitle {{
    font-size: 13px;
    color: {COLOR_TEXT_WEAK};
}}
QPushButton {{
    background: {COLOR_PRIMARY};
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 8px 18px;
    font-size: 14px;
}}
QPushButton:hover {{
    background: {COLOR_PRIMARY_DARK};
}}
QPushButton:disabled {{
    background: #C9CDD4;
    color: #FFFFFF;
}}
QPushButton[variant="ghost"] {{
    background: transparent;
    color: {COLOR_TEXT_WEAK};
    border: 1px solid {COLOR_BORDER};
}}
QPushButton[variant="ghost"]:hover {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
}}
QLineEdit, QSpinBox, QTimeEdit, QComboBox, QPlainTextEdit, QTextEdit {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px 10px;
    background: {COLOR_BG};
    selection-background-color: {COLOR_PRIMARY};
}}
QLineEdit:focus, QSpinBox:focus, QTimeEdit:focus, QComboBox:focus {{
    border-color: {COLOR_PRIMARY};
}}
QGroupBox {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: {COLOR_TEXT_WEAK};
}}
QTableWidget {{
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    gridline-color: {COLOR_BORDER};
}}
QHeaderView::section {{
    background: {COLOR_SURFACE};
    border: none;
    border-bottom: 1px solid {COLOR_BORDER};
    padding: 8px;
    font-weight: 600;
}}
QTabBar::tab {{
    padding: 8px 18px;
    border: none;
    color: {COLOR_TEXT_WEAK};
}}
QTabBar::tab:selected {{
    color: {COLOR_PRIMARY};
    border-bottom: 2px solid {COLOR_PRIMARY};
}}
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {COLOR_BORDER};
}}
"""


__all__ = [
    "APP_STYLESHEET",
    "BLUR_DOWNSCALE",
    "BLUR_RADIUS_BACKDROP",
    "BRAND_GRADIENT_ANGLE_DEG",
    "BRAND_GRADIENT_COLORS",
    "BRAND_GRADIENT_STOPS",
    "BUTTON_MIN_SIZE",
    "CARD_MIN_HEIGHT",
    "CARD_PADDING",
    "CARD_RADIUS",
    "CARD_WIDTH",
    "COLOR_BG",
    "COLOR_BORDER",
    "COLOR_BRAND_GRADIENT_END",
    "COLOR_BRAND_GRADIENT_MID",
    "COLOR_BRAND_GRADIENT_START",
    "COLOR_DANGER",
    "COLOR_EYECARE2_GRADIENT_END",
    "COLOR_EYECARE2_GRADIENT_MID",
    "COLOR_EYECARE2_GRADIENT_START",
    "COLOR_EYECARE_CAPTION",
    "COLOR_EYECARE_CARD_BG",
    "COLOR_EYECARE_CARD_BORDER",
    "COLOR_EYECARE_GRADIENT_END",
    "COLOR_EYECARE_GRADIENT_MID",
    "COLOR_EYECARE_GRADIENT_START",
    "COLOR_EYECARE_TEXT",
    "COLOR_EYECARE_WEAK",
    "COLOR_FLOATING_BG_DEFAULT",
    "COLOR_FLOATING_BG_EYECARE",
    "COLOR_FLOATING_BG_EYECARE2",
    "COLOR_OVERLAY_BG",
    "COLOR_OVERLAY_TEXT",
    "COLOR_OVERLAY_WEAK",
    "COLOR_PRIMARY",
    "COLOR_PRIMARY_DARK",
    "COLOR_SUCCESS",
    "COLOR_SURFACE",
    "COLOR_TEXT",
    "COLOR_TEXT_WEAK",
    "COLOR_WARNING",
    "FONT_FAMILY",
    "FONT_SIZE_BODY",
    "FONT_SIZE_CAPTION",
    "FONT_SIZE_DISPLAY",
    "FONT_SIZE_TITLE",
    "LOCK_STYLE_DEFAULT",
    "LOCK_STYLE_EYECARE",
    "LOCK_STYLE_EYECARE2",
    "LOCK_STYLE_ACCENT",
    "LOCK_STYLE_ACCENT_DARK",
    "LOCK_STYLE_FLOATING_BG",
    "LOCK_STYLE_GRADIENTS",
    "LOCK_STYLE_LABELS",
    "LOCK_STYLE_ORDER",
    "LOCK_STYLE_TEXT",
    "LOCK_STYLE_WEAK",
    "LOCK_STYLE_LABELS",
    "LOCK_STYLE_ORDER",
    "MAX_BRAND_GRADIENT_LUMINANCE",
    "SCRIM_MEDIUM",
    "SCRIM_STRONG",
    "SCRIM_WEAK",
    "WCAG_AA_CONTRAST",
    "contrast_ratio",
    "format_countdown",
    "format_minutes",
    "lock_surface_stylesheet",
    "parse_hex_color",
    "relative_luminance",
    "remaining_color",
    "rgba_css",
]
