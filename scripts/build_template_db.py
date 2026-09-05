"""构建单机版预建模板库 ``release/preview/kidtime_template.db``。

在「已安装 backend 依赖」的 Python venv 中运行（开发机 / CI），不在目标机运行。
产物经 PyInstaller 落位到 ``_internal/portable/``，运行时由
``db_bootstrap.ensure_database`` 复制到 ``data/server/kidtime.db``。

步骤：
1. 用 alembic 在临时库上 ``upgrade head``（建全部表 + alembic_version）。
2. 0009 迁移幂等补种内置规则模板（见 backend/alembic/versions/0009）。
3. 🔴 S2（开源整改）：**模板不再种任何内置 admin**。运行期内置 admin 由后端
   启动时按 ``KIDTIME_SEED_ADMIN_*`` 环境变量（来自桌面端运行期随机口令文件）
   创建 / 同步，固定口令绝不落盘、绝不进源码与打包产物。
4. 复制前 ``wal_checkpoint(TRUNCATE)`` + 切回 ``journal_mode=DELETE``，
   确保单文件模板自包含。
5. 复制产物到 ``release/preview/kidtime_template.db``，并写 ``db_revision.txt``。

用法：
    cd backend
    python ../scripts/build_template_db.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
OUT_DIR = ROOT / "release" / "preview"
TEMP_DB = BACKEND / "_preview_template_build.db"


def _fold_wal_to_main() -> None:
    """把 WAL 折叠回主库并切回 DELETE 模式，确保单文件模板自包含。"""
    raw = sqlite3.connect(str(TEMP_DB))
    try:
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw.execute("PRAGMA journal_mode=DELETE")
        raw.commit()
    finally:
        raw.close()
    # 清理可能残留的 -wal / -shm（已折叠为空，可安全删除）。
    for suffix in ("-wal", "-shm"):
        extra = TEMP_DB.with_name(TEMP_DB.name + suffix)
        if extra.exists():
            try:
                extra.unlink()
            except OSError:
                pass


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if TEMP_DB.exists():
        TEMP_DB.unlink()

    env = {**os.environ, "DATABASE_URL": f"sqlite:///{TEMP_DB.as_posix()}",
           "ALLOW_UNSAFE_DB": "1"}
    # 用 backend 自带 alembic 走到 head（含 0009 内置模板补种）。
    # 用 sys.executable -m alembic，避免 venv 未激活时找不到 alembic 可执行。
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        return proc.returncode

    # 释放 SQLAlchemy 连接池（否则 _fold_wal_to_main 切 journal_mode 会报
    # database is locked）。
    # 注：模板不再种 admin（S2 整改），因此这里无种子步骤。
    try:
        from app.db.session import reset_engine
        reset_engine()
    except Exception:
        pass

    # 读取当前 revision（alembic_version 单行）。
    revision = ""
    try:
        conn = sqlite3.connect(str(TEMP_DB))
        try:
            row = conn.execute(
                "SELECT version_num FROM alembic_version LIMIT 1"
            ).fetchone()
            revision = row[0] if row else ""
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - 防御
        print(f"[warn] 读取 revision 失败：{exc}")

    # 复制前把 WAL 折叠回主库并切回 DELETE 模式，确保单文件模板自包含。
    _fold_wal_to_main()

    target = OUT_DIR / "kidtime_template.db"
    if target.exists():
        target.unlink()
    shutil.copy(TEMP_DB, target)
    (OUT_DIR / "db_revision.txt").write_text(revision or "unknown", encoding="utf-8")
    TEMP_DB.unlink()
    print(f"[ok] 模板库已生成：{target}（revision={revision or '?'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
