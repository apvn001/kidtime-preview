"""KidTime 后端的轻量 HTTP 客户端。

**只能**在 :class:`~kidtime_client.sync.sync_engine.SyncWorker` 内部运行
（架构硬约束 #2）：本模块绝不 import 任何 repository、绝不打开 SQLite 连接、
绝不触碰 Qt 控件。

每个写请求都带 ``Idempotency-Key``（全新 UUIDv4），网络超时后的重试不可能在
服务端重复生效。

🔴 1.1.0 TLS 说明：``verify_tls`` 的类型是 ``bool | str``。传字符串时表示
**自定义 CA bundle 文件路径**，``httpx`` 原生支持 ``verify="/path/ca.pem"``，
这里不需要任何额外处理。自签证书部署必须走这条路径 —— ``verify=True`` 用的是
httpx 自带的 certifi bundle，**不读 Windows 证书存储**，导入系统根证书对本
客户端无效。
"""

from __future__ import annotations

import logging
import platform
import uuid
from typing import Any, Final, Mapping, Sequence

import httpx

from kidtime_client.constants import CLIENT_VERSION
from kidtime_client.sync.payloads import CommandAck, PairResponse, SyncRequest, SyncResponse

logger = logging.getLogger(__name__)

#: 拼接在配置的 origin 之后的 API 前缀。
API_PREFIX: Final[str] = "/api/v1"

#: 默认单请求超时（秒）。
DEFAULT_TIMEOUT: Final[float] = 15.0

#: 传输层失败的错误码：普通网络故障。
CODE_NETWORK: Final[str] = "NETWORK"

#: 传输层失败的错误码：**TLS 证书校验失败**。单独拆出来是因为它和「网络不通」
#: 的处置方式完全不同 —— 重试一万次也不会成功，必须改配置。
CODE_TLS_CERT: Final[str] = "TLS_CERT"

#: 证书校验失败时附加的排查指引（面向现场运维，不要精简）。
#: 🔴 1.3.0 起不再引导 ``install.ps1`` 与环境变量：证书配置已并入配对流程，
#: 引导家长在「配对向导 / 重新配对」中重新选择服务器证书（P0-7）。
TLS_HINT: Final[str] = (
    "服务端证书未通过校验。若后端使用自签证书，请打开「家长面板 → 设置 → "
    "设备信息 → 重新配对服务器…」，在配对流程中选择随部署分发的 kidtime-ca.crt；"
    "或在新装电脑上通过首次设置向导的「服务器证书」步骤完成配置。"
    "注意：把自签根证书导入 Windows「受信任的根证书颁发机构」对本客户端无效。"
)

#: 用于识别证书校验失败的特征串（httpx 会把 ssl 的异常文本原样带出来）。
_TLS_ERROR_MARKERS: Final[tuple[str, ...]] = (
    "CERTIFICATE_VERIFY_FAILED",
    "certificate verify failed",
    "SSLCertVerificationError",
    "self signed certificate",
    "self-signed certificate",
    "unable to get local issuer certificate",
    "Hostname mismatch",
    "IP address mismatch",
)


class ApiError(Exception):
    """后端返回非 2xx 或传输层失败时抛出。

    Attributes:
        status: HTTP 状态码；传输层失败时为 ``0``。
        code: 机器可读的错误码（离线为 ``NETWORK``，证书校验失败为 ``TLS_CERT``）。
        message: 可直接展示给用户的中文消息。
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"[{status}/{code}] {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def is_auth_error(self) -> bool:
        """设备凭据被拒绝时返回 ``True``。"""
        return self.status in (401, 403)

    @property
    def is_tls_error(self) -> bool:
        """TLS 证书校验失败时返回 ``True``。"""
        return self.code == CODE_TLS_CERT

    @property
    def is_retryable(self) -> bool:
        """稍后重试有合理成功可能时返回 ``True``。

        🔴 证书校验失败**不可重试**：它是配置问题，不改配置永远不会成功。
        把它排除在重试之外，可以避免客户端对着服务器无意义地空转，也让
        ``SYNC_FAILED`` 事件里的原因一眼可辨。
        """
        if self.is_tls_error:
            return False
        if self.status == 0:
            return True
        if self.status == 409 and self.code == "IDEMPOTENCY_IN_PROGRESS":
            return True
        return self.status == 429 or 500 <= self.status < 600


def _os_info() -> str:
    """返回用于 ``os_info`` 字段的简短系统描述。"""
    try:
        return f"{platform.system()} {platform.release()} ({platform.machine()})"[:128]
    except Exception:  # pragma: no cover - defensive
        return "Windows"


def _is_tls_error(exc: BaseException) -> bool:
    """判断一个传输层异常是否由 TLS 证书校验失败引起。

    同时检查异常链（``__cause__`` / ``__context__``）：httpx 会把
    ``ssl.SSLCertVerificationError`` 包装进 ``httpx.ConnectError``。

    Args:
        exc: 捕获到的异常。

    Returns:
        判定为证书问题返回 ``True``。
    """
    import ssl

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        text = str(current)
        if any(marker in text for marker in _TLS_ERROR_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def _transport_error(exc: BaseException, prefix: str) -> ApiError:
    """把传输层异常转换成带排查指引的 :class:`ApiError`。

    Args:
        exc: httpx 抛出的异常。
        prefix: 中文消息前缀，例如 ``"无法连接服务器"``。

    Returns:
        证书问题返回 ``TLS_CERT``，其余返回 ``NETWORK``。
    """
    if _is_tls_error(exc):
        return ApiError(0, CODE_TLS_CERT, f"{prefix}：{TLS_HINT}（原始错误：{exc}）")
    return ApiError(0, CODE_NETWORK, f"{prefix}：{exc}")


def _error_from_response(response: httpx.Response) -> ApiError:
    """把非 2xx 的 :class:`httpx.Response` 转成 :class:`ApiError`。"""
    code = "HTTP_ERROR"
    message = f"服务端返回 {response.status_code}"
    try:
        body: Any = response.json()
        if isinstance(body, Mapping):
            detail = body.get("detail")
            if isinstance(detail, Mapping):
                code = str(detail.get("code") or code)
                message = str(detail.get("message") or message)
            elif isinstance(detail, str) and detail:
                message = detail
            elif body.get("code"):
                code = str(body.get("code"))
                message = str(body.get("message") or message)
    except Exception:  # pragma: no cover - non-JSON error body
        pass
    return ApiError(response.status_code, code, message)


class ApiClient:
    """绑定一对设备凭据的同步式后端客户端。

    Args:
        base_url: 后端 origin，不含 ``/api/v1`` 后缀。``http://`` 与
            ``https://`` 均可，仅做去尾斜杠处理，不对协议前缀做任何特殊判断。
        device_id: ``X-Device-Id`` 头使用的设备标识。
        device_secret: ``X-Device-Secret`` 头使用的设备密钥。
        timeout: 单请求超时（秒）。
        verify_tls: TLS 证书校验设置。``True`` 使用 httpx 自带的 certifi CA
            bundle；**传字符串则表示自定义 CA bundle 文件路径**（自签证书部署
            必须用这种方式，值来自 ``KIDTIME_CA_BUNDLE``）；``False`` 关闭校验，
            仅限本地调试，打包产物中不可能出现。
        client_version: ``X-Client-Version`` 头的取值。
    """

    def __init__(
        self,
        base_url: str,
        device_id: str,
        device_secret: str,
        timeout: float = DEFAULT_TIMEOUT,
        verify_tls: bool | str = True,
        client_version: str = CLIENT_VERSION,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._device_id = device_id
        self._device_secret = device_secret
        self._client_version = client_version
        # httpx 原生接受 bool 或 CA bundle 路径字符串，此处直接透传即可。
        self._client = httpx.Client(
            base_url=f"{self._base_url}{API_PREFIX}",
            timeout=timeout,
            http2=False,
            verify=verify_tls,
            follow_redirects=False,
        )

    @property
    def device_id(self) -> str:
        """本客户端使用的设备标识。"""
        return self._device_id

    def close(self) -> None:
        """关闭底层连接池（幂等）。"""
        try:
            self._client.close()
        except Exception:  # pragma: no cover - defensive
            logger.debug("关闭 httpx 客户端时出错，已忽略", exc_info=True)

    def __enter__(self) -> "ApiClient":  # pragma: no cover - convenience
        return self

    def __exit__(self, *_exc: object) -> None:  # pragma: no cover - convenience
        self.close()

    # ------------------------------------------------------------------
    # Pairing (no credentials yet)
    # ------------------------------------------------------------------
    @staticmethod
    def pair(
        base_url: str,
        code: str,
        name: str,
        timezone: str,
        client_version: str,
        os_info: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        verify_tls: bool | str = True,
    ) -> PairResponse:
        """用配对码换取永久设备凭据。

        Args:
            base_url: 后端 origin，不含 ``/api/v1`` 后缀；支持 ``https://``。
            code: 家长端后台显示的配对码（带不带连字符都可以）。
            name: 本设备的显示名称。
            timezone: IANA 时区名，例如 ``Asia/Shanghai``。
            client_version: 客户端版本号字符串。
            os_info: 可选的系统描述；省略时自动探测。
            timeout: 请求超时（秒）。
            verify_tls: TLS 证书校验设置。同 :class:`ApiClient`：``True`` 用
                certifi 内置 bundle，**字符串表示自定义 CA bundle 文件路径**，
                ``False`` 关闭校验（仅本地调试）。

        Returns:
            :class:`~kidtime_client.sync.payloads.PairResponse`，内含一次性的
            ``device_secret`` 与初始规则快照。

        Raises:
            ApiError: 配对码无效/过期，或网络、证书校验失败。
        """
        origin = base_url.rstrip("/")
        payload = {
            "code": code.strip(),
            "device_name": name.strip(),
            "timezone": timezone,
            "client_version": client_version,
            "os_info": os_info or _os_info(),
        }
        headers = {
            "Content-Type": "application/json",
            "X-Client-Version": client_version,
            "Idempotency-Key": str(uuid.uuid4()),
        }
        try:
            # verify 同样直接透传，字符串即 CA bundle 路径。
            with httpx.Client(
                base_url=f"{origin}{API_PREFIX}",
                timeout=timeout,
                http2=False,
                verify=verify_tls,
            ) as client:
                response = client.post("/client/pair", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise _transport_error(exc, "无法连接服务器") from exc

        if response.status_code >= 400:
            raise _error_from_response(response)
        return PairResponse.from_json(response.json())

    # ------------------------------------------------------------------
    # Authenticated endpoints
    # ------------------------------------------------------------------
    def sync(self, req: SyncRequest) -> SyncResponse:
        """Perform one full uplink + downlink round trip.

        Args:
            req: The request built on the GUI thread.

        Returns:
            The parsed :class:`~kidtime_client.sync.payloads.SyncResponse`, with
            ``request`` attached for main-thread bookkeeping.

        Raises:
            ApiError: On any transport or backend failure.
        """
        data = self._post("/client/sync", req.to_json())
        return SyncResponse.from_json(data, request=req)

    def confirm_credit(self, request_ids: Sequence[str]) -> dict[str, Any]:
        """Confirm that approved extension minutes were credited locally.

        Args:
            request_ids: Extension request ids that were credited (1-50).

        Returns:
            The decoded response body. An empty input short-circuits to an
            all-empty result without hitting the network.

        Raises:
            ApiError: On any transport or backend failure.
        """
        ids = [str(item) for item in request_ids if item]
        if not ids:
            return {"confirmed": [], "already_credited": [], "not_found": []}
        return self._post("/client/confirm-credit", {"request_ids": ids[:50]})

    def command_ack(self, acks: Sequence[CommandAck]) -> dict[str, Any]:
        """Acknowledge executed remote commands out-of-band.

        Args:
            acks: Acknowledgements to send (1-50).

        Returns:
            The decoded response body, or an empty result for an empty input.

        Raises:
            ApiError: On any transport or backend failure.
        """
        items = [ack.to_json() for ack in acks][:50]
        if not items:
            return {"acked": [], "ignored": []}
        return self._post("/client/command-ack", {"acks": items})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """POST ``payload`` to ``path`` and return the decoded JSON body.

        Args:
            path: Path relative to ``/api/v1``.
            payload: JSON-serialisable request body.

        Returns:
            The decoded JSON object.

        Raises:
            ApiError: On transport failure, non-2xx status, or invalid JSON.
        """
        try:
            response = self._client.post(path, json=payload, headers=self._headers(True))
        except httpx.HTTPError as exc:
            raise _transport_error(exc, "网络请求失败") from exc

        if response.status_code >= 400:
            raise _error_from_response(response)
        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(response.status_code, "BAD_JSON", "服务端返回了非法的 JSON") from exc
        if not isinstance(body, dict):
            raise ApiError(response.status_code, "BAD_JSON", "服务端返回的 JSON 不是对象")
        return body

    def _headers(self, idempotent: bool) -> dict[str, str]:
        """Build the request headers.

        Args:
            idempotent: When ``True`` a fresh ``Idempotency-Key`` is generated.
                Every write request must set this.

        Returns:
            The header mapping. The device secret is included here and must
            never be echoed into a log record.
        """
        headers = {
            "Content-Type": "application/json",
            "X-Device-Id": self._device_id,
            "X-Device-Secret": self._device_secret,
            "X-Client-Version": self._client_version,
        }
        if idempotent:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        return headers


__all__ = [
    "API_PREFIX",
    "CODE_NETWORK",
    "CODE_TLS_CERT",
    "TLS_HINT",
    "ApiClient",
    "ApiError",
    "DEFAULT_TIMEOUT",
]
