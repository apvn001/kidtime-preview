"""便携单机版管理端 HTTP 客户端。

复用后端既有 REST 端点，仅用内置 admin 身份操作本机服务。所有写操作
自带幂等键（由调用方传入），保证重试安全。

🔴 S2 整改：admin 口令改为运行期生成并落盘（``portable/runtime_secrets``），
构造时由调用方显式传入（用户名固定为 ``admin``）。本文件不引用任何固定口令。
"""
from __future__ import annotations

import logging
import uuid

import httpx

from .constants import PREVIEW_ADMIN_USERNAME
from .runtime_secrets import load_admin_credentials

logger = logging.getLogger(__name__)


class AdminApiClient:
    """对内嵌后端的的管理调用封装。"""

    def __init__(
        self,
        base_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """初始化。

        Args:
            base_url: 内嵌服务 origin（不含 ``/api/v1``）。
            username: 管理员用户名；缺省读运行期密钥（固定 ``admin``）。
            password: 管理员口令；缺省读运行期密钥文件
                （``data/server/runtime_secrets.json``）。

        Raises:
            RuntimeError: 未传口令且运行期密钥文件缺失（应先由
                ``server_manager.start()`` 生成）。
        """
        self.base_url = base_url.rstrip("/")
        if password is None or username is None:
            username, password = load_admin_credentials()
        self._username = username
        self._password = password
        self._token: str | None = None
        # 🔴 trust_env=False：内嵌服务恒为 127.0.0.1，绝不走系统代理。
        self._client = httpx.Client(base_url=self.base_url, timeout=15, trust_env=False)

    # ------------------------------------------------------------------
    # 鉴权
    # ------------------------------------------------------------------
    def login(self) -> None:
        resp = self._client.post(
            "/api/v1/auth/login",
            json={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        self._client.headers["Authorization"] = f"Bearer {self._token}"

    def adopt_token(self, access_token: str) -> None:
        """直接采用已签发的 access token（例如 init 返回的 TokenPair）。

        供首启链路复用 init 的令牌、避免无谓重复登录。

        Args:
            access_token: access token 明文。
        """
        self._token = access_token
        self._client.headers["Authorization"] = f"Bearer {access_token}"

    # ------------------------------------------------------------------
    # 初始化 / 设备
    # ------------------------------------------------------------------
    def setup_status(self) -> dict:
        resp = self._client.get("/api/v1/setup/status")
        resp.raise_for_status()
        return resp.json()

    def init(self) -> dict:
        # SetupInitIn 强制要求 username/password（≥8 位含字母数字）；用内置 admin
        # 凭据初始化，保证后续 login() 可用。
        resp = self._client.post(
            "/api/v1/setup/init",
            json={
                "username": self._username,
                "password": self._password,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        resp.raise_for_status()
        return resp.json()

    def list_devices(self) -> list[dict]:
        resp = self._client.get("/api/v1/devices")
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # 设备自配对（preview 单机闭环，零输入）
    # ------------------------------------------------------------------
    def create_pairing_code(self, note: str = "preview") -> str:
        """以内置 admin 生成一个配对码，返回展示用 code。"""
        # 端点带 IdempotencyKeyDep：缺失该头会 400（IDEMPOTENCY_KEY_REQUIRED）。
        resp = self._client.post(
            "/api/v1/devices/pairing-codes",
            json={"note": note},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        resp.raise_for_status()
        return resp.json()["code"]

    def pair_device(
        self,
        code: str,
        device_name: str,
        timezone: str,
        client_version: str,
        os_info: str | None = None,
    ) -> dict:
        """凭配对码完成设备配对，返回含 device_id / device_secret 的字典。"""
        # 端点带 IdempotencyKeyDep；同 key 重试由后端幂等中间件去重，避免超时重试
        # 造出重复设备。bootstrap 每次自配对只调一次，fresh UUID 即可。
        resp = self._client.post(
            "/api/v1/client/pair",
            json={
                "code": code,
                "device_name": device_name,
                "timezone": timezone,
                "client_version": client_version,
                "os_info": os_info,
            },
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # 规则（管控）
    # ------------------------------------------------------------------
    def get_rules(self, device_id: str) -> dict:
        resp = self._client.get(f"/api/v1/devices/{device_id}/rules")
        resp.raise_for_status()
        return resp.json()

    def put_rules(self, device_id: str, payload: dict, idempotency_key: str) -> dict:
        resp = self._client.put(
            f"/api/v1/devices/{device_id}/rules",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        resp.raise_for_status()
        return resp.json()

    def list_templates(self) -> list[dict]:
        # 🔴 GET /rule-templates 返回 Page[RuleTemplateOut] 分页信封
        # （{"items": [...], "page", "size", "total", "pages"}），必须取 items；
        # 直接当裸列表迭代会迭代到字典键（字符串）→ 'str' object has no attribute 'get'。
        resp = self._client.get("/api/v1/rule-templates")
        resp.raise_for_status()
        return resp.json()["items"]

    def apply_template(self, template_id: str, device_id: str, idempotency_key: str) -> dict:
        resp = self._client.post(
            f"/api/v1/rule-templates/{template_id}/apply",
            json={"device_ids": [device_id]},
            headers={"Idempotency-Key": idempotency_key},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # 用量（概览只读）
    # ------------------------------------------------------------------
    def usage_summary(self) -> dict:
        resp = self._client.get("/api/v1/usage/summary")
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()
