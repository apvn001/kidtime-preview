"""preview 打包编排：构建模板库 → PyInstaller → 压缩 zip。

在「已装 PySide6 + backend 依赖 + PyInstaller」的 Windows venv 中运行。
需要先有 ``release/preview/kidtime_template.db``（由 build_template_db.py 生成）。

⚠️ 关键：构建产物（dist/work）默认落在**本地临时目录**，而非项目同步盘目录。
百度同步盘会锁住新写入的文件，导致 spec 末尾的瘦身 ``os.remove`` 抛
``WinError 32`` 而中断打包。用 ``--distpath/--workpath`` 指向本地盘，并透传
``KIDTIME_DISTPATH`` 让 spec 的 slim 阶段定位到同一目录。

用法：
    cd desktop
    python ../scripts/build_preview.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESKTOP = ROOT / "desktop"
RELEASE_ZIP = ROOT / "release" / "KidTimePreview-1.0.0-win64.zip"
TEMPLATE_DB = ROOT / "release" / "preview" / "kidtime_template.db"

# 🔴 构建产物落在非同步盘本地临时目录，规避同步盘锁文件（WinError 32）。
BUILD_ROOT = Path(
    os.environ.get("KIDTIME_BUILD_ROOT")
    or os.path.join(tempfile.gettempdir(), "kidtime_preview_build")
)
DIST = BUILD_ROOT / "KidTimePreview"


def main() -> int:
    if not TEMPLATE_DB.exists():
        sys.stderr.write(
            f"[fatal] 模板库缺失：{TEMPLATE_DB}\n请先运行 scripts/build_template_db.py\n"
        )
        return 1

    DIST.parent.mkdir(parents=True, exist_ok=True)
    (BUILD_ROOT / "build").mkdir(parents=True, exist_ok=True)

    # 透传 KIDTIME_DISTPATH 让 spec 的 slim 阶段定位到本地 dist。
    env = os.environ.copy()
    env["KIDTIME_DISTPATH"] = str(DIST)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "kidtime_client_preview.spec",
            "--noconfirm",
            "--distpath",
            str(BUILD_ROOT),
            "--workpath",
            str(BUILD_ROOT / "build"),
        ],
        cwd=str(DESKTOP),
        env=env,
    )
    if proc.returncode != 0:
        return proc.returncode

    if not DIST.exists():
        sys.stderr.write(f"[fatal] 构建产物缺失：{DIST}\n")
        return 1

    # 附带「使用说明.txt」（源文件在 docs/ 下，UTF-8 BOM）到包根目录。
    manual = ROOT / "docs" / "使用说明.txt"
    if manual.exists():
        shutil.copy2(manual, DIST / manual.name)
        print(f"[ok] 已附带：{manual.name}")
    else:
        sys.stderr.write(f"[warn] 未找到使用说明：{manual}\n")

    # 2) 压缩整目录为交付 zip（双击 dist/KidTimePreview/KidTimePreview.exe 启动）。
    if RELEASE_ZIP.exists():
        RELEASE_ZIP.unlink()
    RELEASE_ZIP.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(RELEASE_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(DIST.rglob("*")):
            zf.write(path, path.relative_to(DIST))
    size_mb = RELEASE_ZIP.stat().st_size / 1024 / 1024
    print(f"[ok] 交付包：{RELEASE_ZIP}（{size_mb:.1f} MB）")
    print(f"[note] 体积目标 ≤80MB；当前 {size_mb:.1f}MB，超限请复查 excludes。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
