"""领域错误类型：携带 HTTP 状态码与机器可读代码，供接口边界直接映射。"""
from __future__ import annotations

from typing import Any, Optional


class ServiceError(Exception):
    """业务错误基类。"""

    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(ServiceError):
    """入参不合法。"""

    status = 400
    code = "validation_error"


class NotFoundError(ServiceError):
    """引用的资源不存在。"""

    status = 404
    code = "not_found"


class ConflictError(ServiceError):
    """组合冲突（如候选中同一组件出现两个变体、依赖成环）。"""

    status = 409
    code = "conflict"


class ConstraintViolation(ServiceError):
    """目标地区约束未满足，禁止签发；violations 列出全部未满足项。"""

    status = 422
    code = "constraints_unsatisfied"

    def __init__(self, message: str, *, violations: list[dict[str, Any]]):
        super().__init__(message, details={"violations": violations})
        self.violations = violations
