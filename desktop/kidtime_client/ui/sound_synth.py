"""柔和电子提示音合成（v1.4.0 增量 · PRD P0-3）。

背景：v1.3 及之前全部提示音走 ``winsound.Beep``（方波、无包络、同步阻塞主
线程）。用户要求「倒数结束前的最后提示音」更柔和、时长更长（约 1.2s）且不突兀
刺耳——Beep 无法表达正弦/三角波与慢起音，故本模块用**纯标准库**合成一段两音
正弦 WAV，再以同步 ``PlaySound(SND_MEMORY)`` 播放。

.. note::
   CPython 的 ``winsound.PlaySound`` 禁止 ``SND_MEMORY | SND_ASYNC`` 组合
   （会抛 ``RuntimeError: Cannot play asynchronously from memory``）。

   更关键的是：在**部分 Windows（如用户 Win11 家庭版）**上，``PlaySound`` 在
   **worker 线程**里会静默失效（完全不发声），只能在**主线程同步调用**才出声
   （已用独立脚本在真机验证）。因此本模块**刻意不在任何子线程里播放**，所有
   发声函数都设计为在调用方线程同步执行——客户端中调用方即 Qt 主线程，由
   :mod:`kidtime_client.ui.notifications` 保证在主线程调用。

设计要点：

* 两音轻柔过渡：523.25Hz(C5) → 659.25Hz(E5)，音程温和，避免高频刺耳；
* 包络：attack 150ms 慢起 + release 350ms 渐弱，首尾零样本、无爆音；
* 峰值幅度 ~0.32（柔和），带少量二次谐波增加「暖」感；
* 合成结果模块级缓存（首次调用后零重复计算）；
* 非 Windows 环境一律返回 ``False``，保持 UI 可测试（与
  ``notifications._play`` 同风格）。
"""

from __future__ import annotations

import io
import logging
import math
import struct
import sys
import wave
from typing import Final

logger = logging.getLogger(__name__)

_IS_WINDOWS: Final[bool] = sys.platform.startswith("win")

try:  # pragma: no cover - import guard
    import winsound  # type: ignore[import-not-found]

    _HAS_WINSOUND = True
except Exception:  # pragma: no cover - non-Windows
    winsound = None  # type: ignore[assignment]
    _HAS_WINSOUND = False

#: 最后提示音总时长（毫秒）。用户拍板 1.2s（PRD Q4 倾向值）。
SOFT_REMINDER_DURATION_MS: Final[int] = 1200
#: 采样率：22050Hz 足够表达 523/659Hz 且体积小、合成快。
_SAMPLE_RATE: Final[int] = 22050
#: 两个音的音高：C5 → E5（大三度，柔和不刺耳）。
_NOTE_1_HZ: Final[float] = 523.25
_NOTE_2_HZ: Final[float] = 659.25
#: 第一个音占总时长比例（剩余为第二个音）。
_NOTE_1_RATIO: Final[float] = 0.55
#: 两音之间的过渡时间（毫秒），做平滑渐变避免切换爆音。
_NOTE_CROSSFADE_MS: Final[int] = 80
#: 起音（attack）与收尾（release）时长（毫秒）。
_ATTACK_MS: Final[int] = 150
_RELEASE_MS: Final[int] = 350
#: 峰值幅度（0~1 比例），明显低于 0.5 保证不突兀。
_PEAK_AMPLITUDE: Final[float] = 0.32

_cache: bytes | None = None


def _synthesize() -> bytes:
    """合成两音正弦 WAV（mono / 16bit / 22050Hz）并返回完整文件字节。"""
    n_total = int(_SAMPLE_RATE * SOFT_REMINDER_DURATION_MS / 1000)
    note1_end = int(n_total * _NOTE_1_RATIO)
    attack_n = int(_SAMPLE_RATE * _ATTACK_MS / 1000)
    release_start = n_total - int(_SAMPLE_RATE * _RELEASE_MS / 1000)
    crossfade_n = max(1, int(_SAMPLE_RATE * _NOTE_CROSSFADE_MS / 1000))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_SAMPLE_RATE)
        frames = bytearray()
        for i in range(n_total):
            t = i / _SAMPLE_RATE
            # 两音平滑过渡：note1 → note2（以过渡窗中点对齐切换点）。
            k = min(1.0, max(0.0, (i - (note1_end - crossfade_n // 2)) / crossfade_n))
            freq = _NOTE_1_HZ + (_NOTE_2_HZ - _NOTE_1_HZ) * k
            # 包络：慢起 + 渐弱，保证首尾零样本、无爆音。
            if i < attack_n:
                env = i / attack_n
            elif i > release_start:
                env = max(0.0, 1.0 - (i - release_start) / (n_total - release_start))
            else:
                env = 1.0
            # 柔和：基频为主 + 少量二次谐波（暖感），低幅度。
            phase = 2.0 * math.pi * freq * t
            sample = math.sin(phase) + 0.15 * math.sin(2.0 * phase)
            value = int(_PEAK_AMPLITUDE * env * sample * 32767)
            value = max(-32768, min(32767, value))
            frames += struct.pack("<h", value)
        wav.writeframes(bytes(frames))
    return buf.getvalue()


def get_soft_reminder_wav() -> bytes:
    """返回缓存的柔和提示音 WAV 字节（首次调用时合成）。

    Returns:
        完整 WAV 文件字节（``RIFF/WAVE`` 头 + 16bit PCM）。
    """
    global _cache
    if _cache is None:
        _cache = _synthesize()
    return _cache


def synth_tone(freq: float, ms: int, amp: float = 0.45) -> bytes:
    """合成单音 WAV 字节（mono / 16bit / 22050Hz），带轻包络避免爆音。

    Args:
        freq: 频率（Hz），如 880.0。
        ms: 时长（毫秒）。
        amp: 峰值幅度（0~1），默认 0.45，明显低于 1.0 以保证不刺耳。

    Returns:
        完整 WAV 文件字节（``RIFF/WAVE`` 头 + 16bit PCM）。
    """
    n = max(1, int(_SAMPLE_RATE * ms / 1000))
    atk = max(1, int(_SAMPLE_RATE * 0.01))
    rel = max(1, int(_SAMPLE_RATE * 0.02))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_SAMPLE_RATE)
        frames = bytearray()
        for i in range(n):
            t = i / _SAMPLE_RATE
            if i < atk:
                env = i / atk
            elif i > n - rel:
                env = max(0.0, (n - i) / rel)
            else:
                env = 1.0
            sample = math.sin(2.0 * math.pi * freq * t)
            value = int(amp * env * sample * 32767)
            value = max(-32768, min(32767, value))
            frames += struct.pack("<h", value)
        wav.writeframes(bytes(frames))
    return buf.getvalue()


def _play_wav_sync(wav_bytes: bytes) -> bool:
    """同步播放内存 WAV（不使用 ``SND_ASYNC``，规避 CPython 限制）。

    必须在**调用方线程（客户端即 Qt 主线程）**调用。原因见模块 docstring：
    在部分 Windows 上，``PlaySound`` 在 worker 线程里静默失效，只能在主线程
    同步调用才出声。

    CPython 禁止 ``SND_MEMORY | SND_ASYNC`` 组合（抛
    ``RuntimeError: Cannot play asynchronously from memory``），因此统一用
    同步 ``SND_MEMORY`` 播放——调用期间字节缓冲始终有效。
    """
    if not (_IS_WINDOWS and _HAS_WINSOUND):
        logger.debug("WAV playback suppressed (winsound unavailable)")
        return False
    try:
        winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)  # type: ignore[union-attr]
        return True
    except Exception:  # pragma: no cover - audio device busy/absent
        logger.debug("winsound.PlaySound failed", exc_info=True)
        return False


def play_soft_reminder() -> bool:
    """播放柔和最后提示音（内存合成 WAV，在主线程同步播放）。

    调用方须保证在主线程调用（客户端由 :mod:`notifications` 保证）。

    Returns:
        ``True`` 当声音已在主线程同步播放；非 Windows / winsound 缺失返回 ``False``。
    """
    return _play_wav_sync(get_soft_reminder_wav())


__all__ = [
    "SOFT_REMINDER_DURATION_MS",
    "get_soft_reminder_wav",
    "play_soft_reminder",
    "synth_tone",
]
