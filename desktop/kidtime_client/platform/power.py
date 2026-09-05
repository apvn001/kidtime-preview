"""系统电源控制（1.2 增量 · 决策 1「5 分钟无操作显示关机按钮」）。

启动门禁的双入口锁屏停在那里 5 分钟没人碰，就露出一个「关机」按钮：孩子如果
不打算用电脑，可以体面地把机器关掉，而不是被一块锁屏干晾着。这里只提供
「怎么关机」这一个能力，**什么时候关**由
:class:`~kidtime_client.core.startup_gate.StartupGateController` 决定。

为什么用 ``shutdown.exe`` 而不是 ``ExitWindowsEx``：
    ``ExitWindowsEx`` 需要先给进程令牌启用 ``SE_SHUTDOWN_NAME`` 特权，一整套
    ``OpenProcessToken`` / ``LookupPrivilegeValue`` / ``AdjustTokenPrivileges``
    写下来又长又容易在受限账户上悄悄失败。系统自带的 ``shutdown.exe`` 内部
    已经处理好特权提升，普通交互式用户默认就有 ``SeShutdownPrivilege``，
    一行命令即可，且失败时有明确的退出码。

依赖：**零新增第三方依赖**，只用标准库 ``subprocess``。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Final, Protocol

logger = logging.getLogger(__name__)

#: 调用 ``shutdown.exe`` 的超时（秒）。命令本身只是投递请求，很快返回。
SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 10.0

#: Windows ``CREATE_NO_WINDOW``：别在孩子面前闪一个黑色控制台窗口。
_CREATE_NO_WINDOW: Final[int] = 0x08000000


class PowerController(Protocol):
    """关机能力的抽象接口。

    抽出接口是为了让 :class:`~kidtime_client.core.startup_gate.StartupGateController`
    的单元测试可以注入 :class:`FakePowerController`，绝不会在 CI 上真把
    构建机关掉。
    """

    def shutdown(self) -> bool:
        """请求关闭计算机。

        Returns:
            ``True`` 表示关机请求已成功投递给系统。
        """
        ...


class Win32PowerController:
    """基于 ``shutdown.exe`` 的真实关机实现。

    Args:
        delay_seconds: 关机延迟（秒）。默认 0 表示立即关机。
        force_close_apps: 是否强制关闭仍在运行的程序（``/f``）。默认 ``False``，
            让用户还有机会保存正在编辑的文件——门禁场景下孩子还没开始用机，
            通常也没有未保存的内容，但强制关闭对家长自己的窗口不友好。
    """

    def __init__(
        self, delay_seconds: int = 0, force_close_apps: bool = False
    ) -> None:
        self._delay_seconds = max(0, int(delay_seconds))
        self._force_close_apps = bool(force_close_apps)

    def shutdown(self) -> bool:
        """调用 ``shutdown /s /t <delay>`` 关闭计算机。

        本方法**从不抛异常**：门禁窗口在主线程调用它，任何未捕获的异常都会
        让锁屏窗口崩掉，反而给孩子留出一个没有防护的桌面。

        Returns:
            ``True`` 表示命令返回码为 0（请求已被系统接受）。
        """
        if sys.platform != "win32":
            logger.warning("非 Windows 平台，忽略关机请求（platform=%s）", sys.platform)
            return False

        command = ["shutdown", "/s", "/t", str(self._delay_seconds)]
        if self._force_close_apps:
            command.append("/f")

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=SHUTDOWN_TIMEOUT_SECONDS,
                creationflags=_CREATE_NO_WINDOW,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.error("关机命令超时未返回：%s", " ".join(command))
            return False
        except OSError as exc:
            logger.error("关机命令无法启动：%s（%s）", " ".join(command), exc)
            return False

        if completed.returncode != 0:
            logger.error(
                "关机命令失败，返回码 %d：%s",
                completed.returncode,
                (completed.stderr or completed.stdout or "").strip(),
            )
            return False

        logger.info("关机请求已提交（延迟 %d 秒）", self._delay_seconds)
        return True


class FakePowerController:
    """测试替身：只记录被调用了几次，不碰真实系统。

    Args:
        succeed: :meth:`shutdown` 的返回值，用来演练失败分支。
    """

    def __init__(self, succeed: bool = True) -> None:
        self._succeed = bool(succeed)
        self.calls = 0

    def shutdown(self) -> bool:
        """记录一次关机请求。

        Returns:
            构造时给定的 ``succeed`` 值。
        """
        self.calls += 1
        logger.info("FakePowerController.shutdown() 被调用第 %d 次", self.calls)
        return self._succeed


def shutdown_computer(delay_seconds: int = 0, force_close_apps: bool = False) -> bool:
    """便捷函数：立刻请求关机。

    Args:
        delay_seconds: 关机延迟（秒）。
        force_close_apps: 是否强制关闭仍在运行的程序。

    Returns:
        ``True`` 表示关机请求已成功投递。
    """
    return Win32PowerController(delay_seconds, force_close_apps).shutdown()


__all__ = [
    "SHUTDOWN_TIMEOUT_SECONDS",
    "FakePowerController",
    "PowerController",
    "Win32PowerController",
    "shutdown_computer",
]
