"""便携模式专用异常。

所有异常都带一条**面向普通家长**的中文 `hint`，供 `startup` 层直接塞进
`QMessageBox`。工程细节（路径、状态码）放 `detail`，只进日志、不进弹窗标题。
"""

from __future__ import annotations


class PortableError(RuntimeError):
    """便携模式启动链路的基类异常。

    Attributes:
        hint: 面向用户的一句话说明（可直接展示）。
        detail: 面向工程的补充信息（进日志）。
    """

    #: 默认用户提示，子类覆盖。
    default_hint: str = "预览体验版启动失败。"

    def __init__(self, detail: str = "", hint: str | None = None) -> None:
        """初始化。

        Args:
            detail: 工程细节，例如路径或状态码。
            hint: 覆盖默认用户提示。
        """
        self.hint: str = hint or self.default_hint
        self.detail: str = detail or ""
        message = f"{self.hint} {self.detail}".strip()
        super().__init__(message)


class PortableRootNotWritableError(PortableError):
    """便携根目录不可写（只读介质、权限不足、被安全软件锁定）。"""

    default_hint = "当前目录不可写，请把整个文件夹复制到桌面或 D 盘后重新运行。"


class TemplateMissingError(PortableError):
    """随包模板库缺失（包被裁坏，或开发期未生成模板）。"""

    default_hint = "安装包内的数据模板缺失，请重新下载完整的压缩包。"


class DbRevisionMismatchError(PortableError):
    """现有数据库的迁移版本号与随包模板不一致。"""

    default_hint = "本机数据版本与当前程序不匹配，请备份 data 目录后重新解压。"


class ServerStartError(PortableError):
    """内嵌服务启动失败（导入失败、绑定失败）。"""

    default_hint = "本机服务启动失败，请重启电脑后再试。"


class ServerStartTimeoutError(ServerStartError):
    """内嵌服务在超时窗口内未就绪。"""

    default_hint = "本机服务启动超时，请关闭杀毒软件的拦截后再试。"


class PortNotAvailableError(ServerStartError):
    """连续探测若干端口全部被占用。"""

    default_hint = "本机端口被其他程序占满，请关闭部分程序后再试。"


class BootstrapError(PortableError):
    """首启自动初始化 / 自动配对失败。"""

    default_hint = "首次初始化失败，请删除 data 目录后重新运行。"


__all__ = [
    "BootstrapError",
    "DbRevisionMismatchError",
    "PortNotAvailableError",
    "PortableError",
    "PortableRootNotWritableError",
    "ServerStartError",
    "ServerStartTimeoutError",
    "TemplateMissingError",
]
