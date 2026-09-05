# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 构建配方：KidTime 便携单机版（client-preview, 1.0.0）。

独立单机形态（不联网、不外联服务器）的桌面客户端打包配方：
* 入口为 ``kidtime_client/portable_launcher.py``（先起内嵌后端再进主流程）。
* 额外把 FastAPI ``backend`` 整套打包进 exe —— 目标机没有 Python，
  内嵌后端必须随 exe 一起冻结。
* 预建模板库 ``kidtime_template.db`` 作为 data 落位到 ``_internal/portable/``，
  运行时由 ``db_bootstrap.ensure_database`` 复制到 ``data/server/kidtime.db``。

构建（在 Windows + 已装 PySide6 / backend 依赖的 venv 中）：:

    cd desktop
    pyinstaller kidtime_client_preview.spec --noconfirm

产物：``dist/KidTimePreview/``（onedir, windowed）。整目录压缩为
``release/KidTimePreview-1.0.0-win64.zip`` 交付。
模板库由 ``../scripts/build_template_db.py``（client-preview 根）先生成。

⚠️ 本 spec 在真实 Windows 构建环境里可能需要迭代（尤其 backend 依赖收敛 /
alembic 数据文件）。首次构建后请用控件级冒烟 + 真机验收。
"""

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

APP_NAME = "KidTimePreview"

datas = [
    ("kidtime_client/storage/schema.sql", "kidtime_client/storage"),
    # 预建模板库（已含全部表 + 内置规则模板 + alembic_version）。
    ("../release/preview/kidtime_template.db", "portable"),
]
datas += collect_data_files("tzdata")

# OpenSSL DLL（与 _ssl.pyd 同版本，详见正式 spec 注释）。
# 优先从「当前 Python 解释器所在 DLLs 目录」拷贝，通用且无需硬编码机器路径；
# 找不到时回退到 collect_dynamic_libs 兜底（不致命）。
binaries = []
import os as _os
import sys as _sys
_OFFICIAL_PY = _os.environ.get("KIDTIME_OFFICIAL_PY")
if not _OFFICIAL_PY:
    _base = getattr(_sys, "base_prefix", None) or _sys.prefix
    _cand = _os.path.join(_base, "DLLs")
    if _os.path.isdir(_cand):
        _OFFICIAL_PY = _cand
for _dll in ("libssl-3-x64.dll", "libcrypto-3-x64.dll"):
    if _OFFICIAL_PY:
        _src = _os.path.join(_OFFICIAL_PY, _dll)
        if _os.path.exists(_src):
            binaries.append((_src, "."))
binaries += collect_dynamic_libs("ssl")
binaries += collect_dynamic_libs("cryptography")

hiddenimports = [
    "kidtime_client.platform.autostart",
    "kidtime_client.platform.screens",
    "kidtime_client.platform.session_events",
    "kidtime_client.platform.single_instance",
    "kidtime_client.portable.bootstrap",
    "kidtime_client.portable.server_manager",
    "kidtime_client.portable.admin_client",
    "kidtime_client.portable.rules_editor",
    "kidtime_client.portable.risk_notice",
    "kidtime_client.ui.rules.rule_edit_dialog",
    "zoneinfo",
    "tzdata",
    "winsound",
    "win32crypt",
    # 🔴 内嵌后端：server_manager.start() 运行时才 import app.main（真实包名是
    # ``app``，位于 backend/app/，backend/ 本身不是包），静态分析看不到，必须
    # 显式 hiddenimport，否则冻结后报 ModuleNotFoundError。
    "app",
    "app.main",
    # 🔴 passlib 动态导入其 handler 子模块（CryptContext(schemes=["bcrypt"]) 触发），
    # PyInstaller 静态分析抓不到，必须显式列出，否则冻结后报
    # ModuleNotFoundError: No module named 'passlib.handlers.bcrypt'。
    "passlib.handlers.bcrypt",
    "passlib.handlers.pbkdf2",
    "passlib.handlers.sha2_crypt",
    # 🔴 h11 是普通包，正常冻结即可（uvicorn 直接 import h11）。
    "h11",
    # 🔴 内嵌后端运行时依赖（server_manager 函数内 import uvicorn / admin_client
    # import httpx，静态分析能抓到，但显式列出更稳；uvicorn 触发其 hook 的
    # collect_submodules 把子模块全部打进 PYZ，避免 'No module named uvicorn.xxx'）。
    "uvicorn",
    "fastapi",
    "starlette",
    "httpx",
    "jose",
    "email_validator",
    "multipart",
    # 🔴 click 绝不能进 hiddenimports：click 8.2+ 用惰性 __getattr__ 暴露子模块，
    # 冻结进 PYZ 的会是残缺版（缺 Choice 等），运行时 `import click` 会优先命中
    # 它而报 AttributeError。正确做法见下方 datas：整包以 data 形式复制到
    # _internal/click，并从 excludes 阻止 PYZ 冻结，使其回退到磁盘真实完整包。
]

# 🔴 click 惰性子模块无法被静态收集，整包拷贝为 data 文件。
# ⚠️ 必须 include_py_files=True：PyInstaller 6.x 的 collect_data_files 默认
# include_py_files=False，只拷非 .py 的"数据文件"（click 目录里仅 py.typed），
# 导致 .py 源码既没以 data 形式落地、又被 excludes 挡住冻结，click 变空壳、
# `click.Choice` 缺失。显式 True 才能把完整 click 拷到 _internal/click。
datas += collect_data_files("click", include_py_files=True)
# h11 是普通包，hiddenimports 已正常冻结；此处 data 拷贝为冗余保险，同样含 .py。
datas += collect_data_files("h11", include_py_files=True)

excludes = [
    # 与正式版相同的 Qt 瘦身清单（保留 QtCore/Gui/Widgets）。
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
    "PySide6.QtStateMachine", "PySide6.QtSvgWidgets", "PySide6.QtTest",
    "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets", "PySide6.QtXml",
    "matplotlib", "numpy", "pandas", "scipy", "IPython", "jupyter",
    "notebook", "tkinter", "test", "unittest", "pytest", "setuptools", "pip",
    "pygments", "click",
    "PySide6.QtNetwork", "PySide6.QtSvg",
]

a = Analysis(
    ["kidtime_client/portable_launcher.py"],
    pathex=[".", "..", "../backend"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)

# ---- 复用正式版的瘦身逻辑（孤儿 Qt DLL / 翻译 / 插件白名单） ----
import shutil as _shutil
import glob as _glob

_SLIM_COL_ROOT = _os.environ.get("KIDTIME_DISTPATH") or _os.path.abspath(_os.path.join("dist", APP_NAME))
_SLIM_PYSIDE = _os.path.join(_SLIM_COL_ROOT, "_internal", "PySide6")
_SLIM_ORPHAN_QT_DLL = {
    "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlMeta.dll", "Qt6QmlModels.dll",
    "Qt6QmlWorkerScript.dll", "Qt6Pdf.dll", "Qt6PdfQuick.dll",
    "Qt6OpenGL.dll", "Qt6OpenGLWidgets.dll", "Qt6VirtualKeyboard.dll",
    "Qt6Svg.dll", "Qt6Network.dll",
}
for _dll in _SLIM_ORPHAN_QT_DLL:
    for _cand in (
        _os.path.join(_SLIM_PYSIDE, _dll),
        _os.path.join(_SLIM_COL_ROOT, "_internal", _dll),
    ):
        if _os.path.exists(_cand):
            try:
                _os.remove(_cand)
                print(f"[slim] removed orphan Qt DLL: {_dll}")
            except OSError as _e:  # 同步盘锁文件等，跳过不致命
                print(f"[slim][warn] 跳过（删除失败）：{_dll} —— {_e}")

for _qm in _glob.glob(_os.path.join(_SLIM_COL_ROOT, "**", "*.qm"), recursive=True):
    _base = _os.path.basename(_qm)
    if "zh_CN" not in _base and "_zh" not in _base:
        try:
            _os.remove(_qm)
            print(f"[slim] removed translation: {_base}")
        except OSError as _e:  # 同步盘锁文件等，跳过不致命
            print(f"[slim][warn] 跳过（删除失败）：{_base} —— {_e}")

_SLIM_KEEP_PLUGINS = {
    "platforms/qwindows.dll",
    "platforms/qoffscreen.dll",
    "styles/qmodernwindowsstyle.dll",
}
_SLIM_PLUGIN_ROOT = _os.path.join(_SLIM_PYSIDE, "plugins")
if _os.path.isdir(_SLIM_PLUGIN_ROOT):
    for _root, _dirs, _files in _os.walk(_SLIM_PLUGIN_ROOT):
        for _f in _files:
            _abs = _os.path.join(_root, _f)
            _rel = _os.path.relpath(_abs, _SLIM_PLUGIN_ROOT).replace("\\", "/")
            if _rel not in _SLIM_KEEP_PLUGINS:
                try:
                    _os.remove(_abs)
                    print(f"[slim] removed plugin: {_rel}")
                except OSError as _e:  # 同步盘锁文件等，跳过不致命
                    print(f"[slim][warn] 跳过（删除失败）：{_rel} —— {_e}")
    for _root, _dirs, _files in _os.walk(_SLIM_PLUGIN_ROOT, topdown=False):
        if _root == _SLIM_PLUGIN_ROOT:
            continue
        try:
            if not _os.listdir(_root):
                _os.rmdir(_root)
        except OSError:
            pass

for _keep in sorted(_SLIM_KEEP_PLUGINS):
    _kp = _os.path.join(_SLIM_PLUGIN_ROOT, *_keep.split("/"))
    if not _os.path.exists(_kp):
        raise SystemExit(f"[slim][FATAL] 必须保留的 Qt 插件缺失: {_keep} —— 构建中止。")
print("[slim] plugin whitelist verified: " + ", ".join(sorted(_SLIM_KEEP_PLUGINS)))
