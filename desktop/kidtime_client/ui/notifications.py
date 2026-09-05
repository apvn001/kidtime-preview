"""Sound and toast helpers.

``PySide6.QtMultimedia`` is not available in this deployment, so **all** audio
goes through the standard-library :mod:`winsound` module. On a non-Windows host
every call degrades to a no-op so the UI stays testable.

1.4（PRD P0-3 / 音频修复）：全部提示音改由
:mod:`kidtime_client.ui.sound_synth` 在内存合成 WAV 后播放——

* 1 分钟「最后提示音」：柔和两音电子音（~1.2s，正弦、慢起音）；
* 15/5 分钟点：原频率的干净正弦音；
* 锁屏/解锁：原频率的干净正弦音。

所有播放均在**调用方线程（Qt 主线程）同步**执行——原因见
:mod:`sound_synth` 的模块说明：在部分 Windows 上 ``PlaySound`` 在 worker 线程
里静默失效，只能主线程同步调用才出声。因此这里**不使用任何子线程**，由调用方
（``_on_reminder`` / ``_on_state_changed`` 等 Qt 槽，均运行于主线程）保证在主
线程调用。牺牲极短的同步阻塞（单次 ≤ ~440ms）换取稳定的发声。
"""

from __future__ import annotations

import logging
import sys
from typing import Final

from kidtime_client.ui.sound_synth import play_soft_reminder, synth_tone

logger = logging.getLogger(__name__)

_IS_WINDOWS: Final[bool] = sys.platform.startswith("win")

try:  # pragma: no cover - import guard
    import winsound  # type: ignore[import-not-found]

    _HAS_WINSOUND = True
except Exception:  # pragma: no cover - non-Windows
    winsound = None  # type: ignore[assignment]
    _HAS_WINSOUND = False


#: Beep patterns as ``(frequency_hz, duration_ms)`` pairs.
_PATTERN_REMINDER: Final[tuple[tuple[int, int], ...]] = ((880, 120),)
_PATTERN_URGENT: Final[tuple[tuple[int, int], ...]] = ((988, 130), (784, 200))
_PATTERN_LOCK: Final[tuple[tuple[int, int], ...]] = ((587, 180), (466, 260))
_PATTERN_UNLOCK: Final[tuple[tuple[int, int], ...]] = ((659, 120), (880, 160))


def _play(pattern: tuple[tuple[int, int], ...]) -> bool:
    """在主线程同步逐个合成并播放提示音（WAV + 同步 ``PlaySound``）。

    调用方须保证在主线程调用（客户端里所有调用点都是 Qt 槽，运行于主线程）。
    不使用任何子线程——在部分 Windows 上 ``PlaySound`` 在 worker 线程里静默
    失效，只能主线程同步调用才出声（已真机验证）。

    Args:
        pattern: ``(frequency_hz, duration_ms)`` 序列。

    Returns:
        ``True`` 当声音已同步播放；非 Windows / winsound 缺失返回 ``False``。
    """
    if not (_IS_WINDOWS and _HAS_WINSOUND):
        logger.debug("Sound suppressed (winsound unavailable): %s", pattern)
        return False

    for frequency, duration in pattern:
        wav = synth_tone(int(frequency), int(duration), amp=0.45)
        try:
            winsound.PlaySound(wav, winsound.SND_MEMORY)  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - audio device busy/absent
            logger.debug("PlaySound failed", exc_info=True)
            return False
    return True


def play_reminder(point_minutes: int) -> bool:
    """Play the reminder chime for a ``15/5/1`` minute checkpoint.

    1.4（PRD P0-3）：1 分钟「最后提示音」改走合成柔和音（~1.2s、主线程同步）；
    15/5 分钟点保持原频率的干净正弦音。

    Args:
        point_minutes: The reminder checkpoint that fired.

    Returns:
        ``True`` when a sound was played.
    """
    if point_minutes <= 1:
        return play_soft_reminder()
    pattern = _PATTERN_URGENT if point_minutes <= 5 else _PATTERN_REMINDER
    return _play(pattern)


def play_lock() -> bool:
    """Play the descending tone used when the screen locks."""
    return _play(_PATTERN_LOCK)


def play_unlock() -> bool:
    """Play the ascending tone used when the screen unlocks."""
    return _play(_PATTERN_UNLOCK)


def reminder_message(point_minutes: int, remaining_minutes: float) -> tuple[str, str]:
    """Compose the reminder toast text.

    Args:
        point_minutes: The checkpoint that fired (15/5/1).
        remaining_minutes: Actual remaining minutes.

    Returns:
        A ``(title, body)`` tuple written in a friendly, non-scolding tone.
    """
    if point_minutes <= 1:
        return (
            "还有 1 分钟",
            "马上就要到时间啦，记得先保存正在做的事情。",
        )
    if point_minutes <= 5:
        return (
            f"还有 {point_minutes} 分钟",
            "快到时间了，可以开始收尾了哦。",
        )
    return (
        f"还有 {point_minutes} 分钟",
        f"今天剩余 {int(max(0.0, remaining_minutes))} 分钟，安排好节奏吧。",
    )


__all__ = [
    "play_lock",
    "play_reminder",
    "play_unlock",
    "reminder_message",
]
