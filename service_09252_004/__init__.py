"""数字教学资源联合发布的服务端包入口。"""
from service_09252_004.service import ReleaseService

PROJECT_CODE = "service_09252_004"


def project_info() -> dict[str, str]:
    """返回稳定的项目标识。"""
    return {"code": PROJECT_CODE, "title": "数字教学资源联合发布"}


__all__ = ["PROJECT_CODE", "ReleaseService", "project_info"]
