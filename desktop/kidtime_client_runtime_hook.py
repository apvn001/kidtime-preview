"""PyInstaller runtime hook：把 _internal 目录加入 Windows DLL 搜索路径。

问题：PyInstaller onedir 模式下，libssl-3-x64.dll / libcrypto-3-x64.dll 收在
``_internal/`` 目录里，Python 的 ``_ssl.pyd`` 也加载自 ``_internal/``。
但 ``_ssl.pyd`` 内部用 ``LoadLibrary("libssl-3-x64.dll")`` 加载依赖 DLL 时，
Windows 默认搜索路径（exe 所在目录 / 当前工作目录 / 系统目录 / PATH）
不包含 ``_internal/``，导致 ``DLL load failed while importing _ssl``。

修法：启动时把 ``_internal/``（即 ``sys._MEIPASS``）加进 DLL 搜索目录。
``os.add_dll_directory``（Python 3.8+）是 Windows 官方推荐的进程级 API，
比改 PATH 更稳（影响范围小）。
"""

import os
import sys

if os.name == "nt":
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and os.path.isdir(meipass):
        try:
            os.add_dll_directory(meipass)
        except (AttributeError, OSError):  # pragma: no cover - 旧版 Python 兜底
            # Python < 3.8 退路：把 _internal 加到 PATH（影响进程内）
            sep = ";" if os.sep == "\\" else ":"
            os.environ["PATH"] = meipass + sep + os.environ.get("PATH", "")
