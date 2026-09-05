"""统一错误体系（API.md §10 全量错误码表）。

所有非 2xx 响应体格式一致：``{code, message, details, request_id}``。
"""

from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import request_id_var

logger = logging.getLogger(__name__)


class ErrorCode:
    """机器可读错误码常量（与 API.md §10 一一对应）。"""

    IDEMPOTENCY_KEY_REQUIRED: Final[str] = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_KEY_INVALID: Final[str] = "IDEMPOTENCY_KEY_INVALID"
    IDEMPOTENCY_KEY_REUSED: Final[str] = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_IN_PROGRESS: Final[str] = "IDEMPOTENCY_IN_PROGRESS"
    BAD_REQUEST: Final[str] = "BAD_REQUEST"

    INVALID_CREDENTIALS: Final[str] = "INVALID_CREDENTIALS"
    INVALID_TOKEN: Final[str] = "INVALID_TOKEN"
    INVALID_REFRESH_TOKEN: Final[str] = "INVALID_REFRESH_TOKEN"
    REFRESH_TOKEN_REUSED: Final[str] = "REFRESH_TOKEN_REUSED"
    INVALID_DEVICE_CREDENTIAL: Final[str] = "INVALID_DEVICE_CREDENTIAL"

    REGISTRATION_CLOSED: Final[str] = "REGISTRATION_CLOSED"
    ADMIN_REQUIRED: Final[str] = "ADMIN_REQUIRED"
    USER_DISABLED: Final[str] = "USER_DISABLED"
    DEVICE_RETIRED: Final[str] = "DEVICE_RETIRED"

    USER_NOT_FOUND: Final[str] = "USER_NOT_FOUND"
    DEVICE_NOT_FOUND: Final[str] = "DEVICE_NOT_FOUND"
    RULE_NOT_FOUND: Final[str] = "RULE_NOT_FOUND"
    RULE_TEMPLATE_NOT_FOUND: Final[str] = "RULE_TEMPLATE_NOT_FOUND"
    BUILTIN_TEMPLATE_PROTECTED: Final[str] = "BUILTIN_TEMPLATE_PROTECTED"
    RULE_TEMPLATE_APPLY_EMPTY: Final[str] = "RULE_TEMPLATE_APPLY_EMPTY"
    EXTENSION_NOT_FOUND: Final[str] = "EXTENSION_NOT_FOUND"
    COMMAND_NOT_FOUND: Final[str] = "COMMAND_NOT_FOUND"
    EVENT_NOT_FOUND: Final[str] = "EVENT_NOT_FOUND"
    PAIRING_CODE_NOT_FOUND: Final[str] = "PAIRING_CODE_NOT_FOUND"

    ALREADY_INITIALIZED: Final[str] = "ALREADY_INITIALIZED"
    USERNAME_TAKEN: Final[str] = "USERNAME_TAKEN"
    LAST_ADMIN_PROTECTED: Final[str] = "LAST_ADMIN_PROTECTED"
    CANNOT_DISABLE_SELF: Final[str] = "CANNOT_DISABLE_SELF"
    PAIRING_CODE_USED: Final[str] = "PAIRING_CODE_USED"
    DEVICE_ALREADY_RETIRED: Final[str] = "DEVICE_ALREADY_RETIRED"
    EXTENSION_NOT_PENDING: Final[str] = "EXTENSION_NOT_PENDING"
    EXTENSION_DATE_PASSED: Final[str] = "EXTENSION_DATE_PASSED"

    # --- V7 增量（重置用量 / 下发时长）---
    USER_SCOPE_NOT_SUPPORTED: Final[str] = "USER_SCOPE_NOT_SUPPORTED"
    """422：下发时长请求了不支持的 target_type（如 'user'，本版仅支持 'device'）。"""

    GRANT_DAILY_LIMIT_EXCEEDED: Final[str] = "GRANT_DAILY_LIMIT_EXCEEDED"
    """409：单设备单日下发次数或总额超过上限（R4 频控）。"""

    GRANT_NOT_FOUND: Final[str] = "GRANT_NOT_FOUND"
    """404：下发时长记录不存在。"""

    RESET_SCOPE_REQUIRED: Final[str] = "RESET_SCOPE_REQUIRED"
    """422：重置用量缺少 scope 或 device_id。"""

    RESET_CONFIRM_REQUIRED: Final[str] = "RESET_CONFIRM_REQUIRED"
    """422：全局重置（L3 高危）未在 confirm_text 中逐字输入 RESET。"""

    # --- V5 增量（增量架构设计 §2.4）---
    DEVICE_NOT_RETIRED: Final[str] = "DEVICE_NOT_RETIRED"
    """409：对 status=active 的设备调用硬删除。提示「请先停用该设备后再删除」。"""

    DEVICE_NOT_RETIRED_FOR_UNRETIRE: Final[str] = "DEVICE_NOT_RETIRED_FOR_UNRETIRE"
    """409：对 status=active 的设备调用 unretire。提示「该设备当前不是已停用状态」。"""

    CANNOT_DELETE_SELF: Final[str] = "CANNOT_DELETE_SELF"
    """409：删除当前登录账号（PRD D1）。"""

    DEVICE_NAME_MISMATCH: Final[str] = "DEVICE_NAME_MISMATCH"
    """400：L3 逐字确认的设备名与实际不符（服务端二次校验，防绕过前端）。"""

    # --- PC 客户端版本更新（MVP 方案 A）---
    VERSION_NOT_FOUND: Final[str] = "VERSION_NOT_FOUND"
    """404：该通道还没有任何已发布版本（`GET /client/version/latest`）。"""

    ASSET_NOT_FOUND: Final[str] = "ASSET_NOT_FOUND"
    """404：请求的版本没有对应的下载物记录（`GET /client/update/asset`）。"""

    INVALID_VERSION_FORMAT: Final[str] = "INVALID_VERSION_FORMAT"
    """400：版本号不是 `MAJOR.MINOR.PATCH` 形式，无法参与比较。"""

    PAIRING_CODE_EXPIRED: Final[str] = "PAIRING_CODE_EXPIRED"
    PAYLOAD_TOO_LARGE: Final[str] = "PAYLOAD_TOO_LARGE"

    VALIDATION_ERROR: Final[str] = "VALIDATION_ERROR"
    WEAK_PASSWORD: Final[str] = "WEAK_PASSWORD"
    INVALID_PAIRING_CODE_FORMAT: Final[str] = "INVALID_PAIRING_CODE_FORMAT"
    INVALID_COMMAND_PAYLOAD: Final[str] = "INVALID_COMMAND_PAYLOAD"
    RANGE_TOO_LARGE: Final[str] = "RANGE_TOO_LARGE"

    PAIRING_CODE_LIMIT: Final[str] = "PAIRING_CODE_LIMIT"
    EXTENSION_PENDING_LIMIT: Final[str] = "EXTENSION_PENDING_LIMIT"
    # P1-1 降级版：进程内轻量限流命中阈值（app/middleware/rate_limit.py）
    RATE_LIMITED: Final[str] = "RATE_LIMITED"

    INTERNAL_ERROR: Final[str] = "INTERNAL_ERROR"
    DB_BUSY: Final[str] = "DB_BUSY"


class AppError(Exception):
    """业务异常基类。

    Attributes:
        status_code: HTTP 状态码。
        code: 机器可读错误码。
        message: 中文人类可读描述。
        details: 附加信息，可为 ``None``。
    """

    status_code: int = 500
    code: str = ErrorCode.INTERNAL_ERROR
    message: str = "服务器内部错误"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
    ) -> None:
        self.message = message or self.__class__.message
        self.code = code or self.__class__.code
        self.status_code = status_code or self.__class__.status_code
        self.details = details
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        """渲染成统一错误响应体。"""
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "request_id": request_id_var.get(),
        }


class BadRequestError(AppError):
    """400 请求参数错误。"""

    status_code = 400
    code = ErrorCode.BAD_REQUEST
    message = "请求参数错误"


class UnauthorizedError(AppError):
    """401 未授权。"""

    status_code = 401
    code = ErrorCode.INVALID_TOKEN
    message = "访问令牌无效或已过期"


class InvalidTokenError(UnauthorizedError):
    """401 访问令牌无效（供 `decode_access_token` 抛出）。"""


class ForbiddenError(AppError):
    """403 权限不足。"""

    status_code = 403
    code = ErrorCode.ADMIN_REQUIRED
    message = "该操作需要管理员权限"


class NotFoundError(AppError):
    """404 资源不存在。"""

    status_code = 404
    code = ErrorCode.DEVICE_NOT_FOUND
    message = "资源不存在"


class ConflictError(AppError):
    """409 状态冲突。"""

    status_code = 409
    code = ErrorCode.EXTENSION_NOT_PENDING
    message = "资源状态冲突"


class GoneError(AppError):
    """410 资源已失效。"""

    status_code = 410
    code = ErrorCode.PAIRING_CODE_EXPIRED
    message = "配对码已过期"


class PayloadTooLargeError(AppError):
    """413 请求体过大。"""

    status_code = 413
    code = ErrorCode.PAYLOAD_TOO_LARGE
    message = "请求体超出大小限制"


class UnprocessableEntityError(AppError):
    """422 语义校验失败。"""

    status_code = 422
    code = ErrorCode.VALIDATION_ERROR
    message = "请求参数校验失败"


class WeakPasswordError(UnprocessableEntityError):
    """422 密码强度不足（D38）。"""

    code = ErrorCode.WEAK_PASSWORD
    message = "密码需至少 8 位且同时包含字母和数字"


class InvalidPairingCodeError(UnprocessableEntityError):
    """422 配对码格式错误（D01）。"""

    code = ErrorCode.INVALID_PAIRING_CODE_FORMAT
    message = "配对码格式不正确"


class TooManyRequestsError(AppError):
    """429 触发限流。"""

    status_code = 429
    code = ErrorCode.RATE_LIMITED
    message = "请求过于频繁"


class ServiceUnavailableError(AppError):
    """503 服务暂时不可用（SQLite 重试耗尽）。"""

    status_code = 503
    code = ErrorCode.DB_BUSY
    message = "数据库繁忙，请稍后重试"


class UnsafeDatabasePathError(RuntimeError):
    """数据库路径位于云同步目录或网络路径，拒绝启动（S1）。"""


_STATUS_CODE_MAP: Final[dict[int, str]] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.INVALID_TOKEN,
    403: ErrorCode.ADMIN_REQUIRED,
    404: ErrorCode.DEVICE_NOT_FOUND,
    405: ErrorCode.BAD_REQUEST,
    409: ErrorCode.IDEMPOTENCY_IN_PROGRESS,
    410: ErrorCode.PAIRING_CODE_EXPIRED,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.VALIDATION_ERROR,
    429: ErrorCode.PAIRING_CODE_LIMIT,
    503: ErrorCode.DB_BUSY,
}

_STATUS_MESSAGE_MAP: Final[dict[int, str]] = {
    400: "请求参数错误",
    401: "访问令牌无效或已过期",
    403: "该操作需要管理员权限",
    404: "资源不存在",
    405: "请求方法不被允许",
    409: "资源状态冲突",
    410: "资源已失效",
    413: "请求体超出大小限制",
    422: "请求参数校验失败",
    429: "请求过于频繁",
    503: "服务暂时不可用",
}


def error_payload(code: str, message: str, details: Any = None) -> dict[str, Any]:
    """构造统一错误响应体（供中间件直接使用）。"""
    return {
        "code": code,
        "message": message,
        "details": details,
        "request_id": request_id_var.get(),
    }


def json_error(status_code: int, code: str, message: str, details: Any = None) -> JSONResponse:
    """构造统一错误 `JSONResponse`。"""
    return JSONResponse(status_code=status_code, content=error_payload(code, message, details))


def _format_validation_details(exc: RequestValidationError) -> list[dict[str, str]]:
    """把 Pydantic 校验错误转成 `[{field, message}]` 数组（API.md §5.2）。"""
    details: list[dict[str, str]] = []
    for err in exc.errors():
        loc = [str(part) for part in err.get("loc", ()) if part not in ("body", "query", "path")]
        details.append(
            {
                "field": ".".join(loc) or "body",
                "message": str(err.get("msg", "参数不合法")),
            }
        )
    return details


def register_exception_handlers(app: FastAPI) -> None:
    """把 `AppError` / 校验错误 / HTTPException / 未捕获异常统一渲染。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("业务异常 %s: %s", exc.code, exc.message)
        else:
            logger.info("业务异常 %s: %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return json_error(
            422,
            ErrorCode.VALIDATION_ERROR,
            "请求参数校验失败",
            _format_validation_details(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _STATUS_CODE_MAP.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else None
        message = message or _STATUS_MESSAGE_MAP.get(exc.status_code, "服务器内部错误")
        return json_error(exc.status_code, code, message)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未捕获异常：%s", exc)
        return json_error(500, ErrorCode.INTERNAL_ERROR, "服务器内部错误")
