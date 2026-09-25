"""应用服务错误类型。接口边界据此映射 HTTP 状态码。"""
from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """业务错误基类。details 携带结构化上下文（如未满足的约束清单）。"""

    code = "service_error"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(ServiceError):
    code = "not_found"


class ConflictError(ServiceError):
    code = "conflict"


class ValidationError(ServiceError):
    code = "validation_failed"
