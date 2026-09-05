"""Process entry point: ``python -m kidtime_client``.

Responsibilities, in order:

1. Parse the (deliberately tiny) command line.
2. Load :class:`~kidtime_client.config.ClientConfig` and configure logging with
   secret redaction.
3. Take the single-instance mutex so a second copy cannot double-count time.
4. Create the ``QApplication``, bootstrap :class:`~kidtime_client.app.KidTimeApp`,
   and enter the event loop.

Exit codes: ``0`` success (including "already running"), ``1`` fatal start-up
failure, ``2`` bad command line.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from kidtime_client import __version__
from kidtime_client.config import ClientConfig
from kidtime_client.constants import APP_DISPLAY_NAME
from kidtime_client.logging_setup import setup_logging
from kidtime_client.platform.single_instance import SingleInstanceGuard

logger = logging.getLogger("kidtime_client.main")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="kidtime_client",
        description=f"{APP_DISPLAY_NAME} 客户端",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_DISPLAY_NAME} {__version__}",
    )
    parser.add_argument(
        "--no-single-instance",
        action="store_true",
        help="跳过单实例检查（仅用于调试，正常运行请勿使用）",
    )
    parser.add_argument(
        "--no-console-log",
        action="store_true",
        help="不向控制台输出日志（打包为 GUI 程序时自动生效）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the client.

    Args:
        argv: Command-line arguments without the program name (defaults to
            ``sys.argv[1:]``).

    Returns:
        The process exit code.
    """
    args = build_parser().parse_args(list(argv) if argv is not None else None)

    # 🔴 便携单机版：先启动内嵌后端，再读取连接配置。
    from kidtime_client.portable.bootstrap import PortableBootstrapper

    logger.info("启动内嵌后端…")
    PortableBootstrapper().run()

    config = ClientConfig.load()
    console = not args.no_console_log and sys.stderr is not None
    log_path = setup_logging(config.log_dir, config.log_level, console=console)
    logger.info("=== %s %s starting ===", APP_DISPLAY_NAME, __version__)
    logger.info("Data directory: %s", config.data_dir)
    logger.info("Log file: %s", log_path)

    guard: SingleInstanceGuard | None = None
    if not args.no_single_instance:
        guard = SingleInstanceGuard()
        if not guard.acquire():
            logger.warning("Another KidTime client is already running; exiting")
            _notify_already_running()
            return 0

    try:
        return _run_gui(config)
    finally:
        if guard is not None:
            guard.release()


def _run_gui(config: ClientConfig) -> int:
    """Create the Qt application and run the client.

    Args:
        config: The loaded configuration.

    Returns:
        The process exit code.
    """
    # Imported lazily so ``--version`` / ``--help`` never pay the Qt start-up
    # cost and so a missing display backend fails with a clear message.
    from PySide6.QtWidgets import QApplication

    from kidtime_client.app import KidTimeApp

    qt_app = QApplication(sys.argv)
    # 便携单机版：首启风险提示（数据在本地、勿放同步盘），仅展示一次。
    from kidtime_client.portable.risk_notice import maybe_show_risk_notice

    maybe_show_risk_notice()
    client = KidTimeApp(config, qt_app)
    try:
        if not client.bootstrap():
            logger.info("Bootstrap did not complete; exiting")
            client.shutdown()
            return 0
    except Exception:
        logger.exception("Fatal error during bootstrap")
        client.shutdown()
        return 1

    try:
        return client.run()
    except Exception:  # pragma: no cover - the event loop should not raise
        logger.exception("Fatal error in the Qt event loop")
        client.shutdown()
        return 1


def _notify_already_running() -> None:
    """Tell the user that another instance owns the mutex (best effort)."""
    try:  # pragma: no cover - requires a display
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.information(
            None,
            APP_DISPLAY_NAME,
            f"{APP_DISPLAY_NAME} 已经在运行了，请在系统托盘里查看。",
        )
        del app
    except Exception:
        logger.debug("Could not show the 'already running' dialog", exc_info=True)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
