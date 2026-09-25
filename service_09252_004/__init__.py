"""数字教学资源联合发布的服务端包入口。"""
from .api import make_server
from .errors import (
    ConflictError,
    ConstraintViolation,
    NotFoundError,
    ServiceError,
    ValidationError,
)
from .ports import ManualClock, SequentialIds, SystemClock, UuidIds
from .service import CourseHubService

PROJECT_CODE = "service_09252_004"


def project_info() -> dict[str, str]:
    """返回稳定的项目标识。"""
    return {"code": PROJECT_CODE, "title": "数字教学资源联合发布"}


__all__ = [
    "PROJECT_CODE",
    "project_info",
    "CourseHubService",
    "make_server",
    "ServiceError",
    "ValidationError",
    "NotFoundError",
    "ConflictError",
    "ConstraintViolation",
    "SystemClock",
    "ManualClock",
    "UuidIds",
    "SequentialIds",
]
