# KidTime-Preview

KidTime-Preview 是一个单机运行的 Windows 上机时间管控桌面客户端（版本 `v1.0.0`）。**不联网、不依赖任何远端服务器。**

> 本项目由早期「客户端 + 网页管理后台」架构精简而来，**已移除证书校验、设备配对向导、
> 云端管理等功能**，只保留单机本地闭环。代码中仍可能残留个别历史注释，属措辞问题、非功能。

## 目录组成

```
client-preview/
├── desktop/
│   ├── kidtime_client/               # 桌面客户端包
│   │   ├── app.py                    #   主控制器
│   │   ├── portable_launcher.py      #   打包入口（起内嵌后端 → 主流程）
│   │   ├── portable/                 #   内嵌后端管理 / 自动配对 / 数据目录
│   │   ├── core/ sync/ storage/      #   引擎 / 同步(到内嵌后端) / 本地存储
│   │   └── ui/                       #   家长面板、门禁、悬浮球、锁屏遮罩等
│   ├── kidtime_client_preview.spec   # PyInstaller 构建配方
│   ├── kidtime_client_runtime_hook.py
│   └── requirements.txt              # 桌面端运行时依赖 + PyInstaller（构建）
├── backend/
│   ├── app/                          # 内嵌 FastAPI 服务端（冻结进 exe）
│   ├── alembic/ + alembic.ini        # 建表迁移（模板库生成用）
│   └── requirements.txt              # 后端依赖
├── scripts/
│   ├── build_template_db.py          # 生成预建模板库（产物在 release/，不入库）
│   └── build_preview.py              # 打包编排：模板库 → PyInstaller → zip
└── docs/
    └── 使用说明.txt                  # 面向使用者的说明（打包时随附进 zip 根目录）
```

## 从源码构建

### 环境要求

- Windows 10 1809+（x64）
- Python 3.10 或更高版本（PySide6 6.6+、SQLAlchemy 2.0、Pydantic 2.9+ 等依赖需要较新的 Python）
- PyInstaller（构建打包用，已列入 `desktop/requirements.txt`）

### 安装依赖

```bash
pip install -r desktop/requirements.txt   # 桌面端运行时依赖 + PyInstaller 打包工具
pip install -r backend/requirements.txt   # 内嵌后端依赖
```

### 构建两步

前置：Windows + 已装上述依赖的 venv。

```bash
# 1) 生成预建模板库（含全部表 + 内置规则模板；不预置任何账号，
#    admin 由程序首启时自动创建）
#    产物：release/preview/kidtime_template.db
cd backend
python ../scripts/build_template_db.py

# 2) 打包（模板库就绪后；构建产物落本地临时目录规避同步盘锁文件）
#    产物：release/KidTimePreview-1.0.0-win64.zip
cd desktop
python ../scripts/build_preview.py
```

产物：`release/KidTimePreview-1.0.0-win64.zip`（解压即用，无需安装）。
`release/` 目录不入库——正式发布的二进制经 GitHub Releases 分发。

## 验证

- 编译：`python -m compileall -q desktop/kidtime_client backend/app`

## 版本约定

产品版本号唯一权威来源 = `desktop/kidtime_client/constants.py` 的
`CLIENT_VERSION`（当前 `1.0.0`）。改版只动这一处。

## 协议

本项目以 **GPL-3.0-or-later** 发布。任何人可自由使用、学习、修改和再分发
本项目代码，但二次分发（包括基于本项目打包后的可执行文件）必须遵守同一
协议，并提供对应的完整源码。

完整协议文本见仓库根目录的 [`LICENSE`](LICENSE) 文件
（GNU General Public License v3.0 官方全文）。

## 免责与安全声明

1. 本工具主要面向**成年家长自主对未成年家庭成员**的电脑设备使用管控。成年家长应依法合规使用本工具；本工具不构成对监护、教育责任的替代。
2. 所有数据仅保存在本机 `data/` 目录，**不上传任何服务器**。
3. 作者不对任何滥用行为负责。
4. 本程序按"原样"提供，不附带任何形式的担保——详见 GPL-3.0 第 15 条
   （No Warranty）。

## 致谢 / 第三方组件

本项目依赖以下开源组件（以 `desktop/requirements.txt` 与
`backend/requirements.txt` 实际声明为准）：

| 组件                                                                                   | 用途          | 许可证                                                  |
| ------------------------------------------------------------------------------------ | ----------- | ---------------------------------------------------- |
| [PySide6](https://pyside.org)（Qt for Python）                                         | 桌面客户端 UI    | LGPL-3.0（以动态链接方式使用）                                  |
| [FastAPI](https://fastapi.tiangolo.com)                                              | 内嵌后端框架      | MIT                                                  |
| [SQLAlchemy](https://www.sqlalchemy.org)                                             | ORM / 数据库访问 | MIT                                                  |
| [Alembic](https://alembic.sqlalchemy.org)                                            | 数据库迁移       | MIT                                                  |
| [uvicorn](https://www.uvicorn.org)                                                   | ASGI 服务器    | BSD-3-Clause                                         |
| [httpx](https://www.python-httpx.org)                                                | HTTP 客户端    | BSD-3-Clause                                         |
| [pydantic](https://docs.pydantic.dev) / pydantic-settings                            | 数据校验        | MIT                                                  |
| [python-jose](https://github.com/mpdavis/python-jose)                                | JWT / 加密    | MIT                                                  |
| [passlib](https://passlib.readthedocs.io) / [bcrypt](https://github.com/pyca/bcrypt) | 口令哈希        | BSD / Apache-2.0                                     |
| [python-multipart](https://github.com/kludex/python-multipart)                       | 表单解析        | Apache-2.0                                           |
| [certifi](https://github.com/certifi/python-certifi)                                 | CA 证书包      | MPL-2.0                                              |
| [tzdata](https://github.com/python/tzdata)                                           | 时区数据        | Apache-2.0                                           |
| [pywin32](https://github.com/mhammond/pywin32)                                       | Windows 集成  | PSF-2.0                                              |
| [pytest](https://pytest.org) / pytest-asyncio                                        | 测试          | MIT / Apache-2.0                                     |
| [PyInstaller](https://pyinstaller.org)                                               | 打包为可执行文件    | GPL-2.0-or-later（bootloader 带特殊例外，允许打包分发不适用 GPL 的应用） |

以上组件归各自权利人所有，感谢开源社区。
