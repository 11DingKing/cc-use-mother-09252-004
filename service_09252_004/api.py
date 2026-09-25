"""HTTP 接口边界：基于标准库的 JSON API（无第三方依赖）。

路由 → CourseHubService 应用服务；领域错误映射为对应 HTTP 状态码：
400 入参不合法 / 404 不存在 / 409 组合冲突 / 422 目标地区约束未满足。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .errors import NotFoundError, ServiceError, ValidationError
from .service import CourseHubService

Handler = Callable[..., tuple[int, dict]]


def make_server(
    service: CourseHubService, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    """构造挂载了给定服务的 HTTP 服务（多线程，支持并行评审/并发签发）。"""

    class _Handler(CourseHubHandler):
        pass

    _Handler.service = service
    return ThreadingHTTPServer((host, port), _Handler)


class CourseHubHandler(BaseHTTPRequestHandler):
    service: CourseHubService
    server_version = "CourseHub/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------- 基础框架

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        pass

    def _dispatch(self, method: str) -> None:
        try:
            status, payload = self._route(method)
        except ServiceError as exc:
            status = exc.status
            payload = {"error": exc.code, "message": exc.message, **exc.details}
        except Exception as exc:  # 兜底，不外泄堆栈
            status = 500
            payload = {"error": "internal_error", "message": str(exc)}
        self._send_json(status, payload)

    def _route(self, method: str) -> tuple[int, dict]:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        body = self._read_body() if method in ("POST", "PUT") else {}
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(parts.path)
            if match:
                return handler(self, body=body, query=query, **match.groupdict())
        raise NotFoundError(f"route not found: {method} {parts.path}")

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValidationError("request body must be valid JSON")
        if not isinstance(data, dict):
            raise ValidationError("request body must be a JSON object")
        return data

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------- 路由处理

    def _health(self, **_: Any) -> tuple[int, dict]:
        return 200, {"status": "ok"}

    def _post_components(self, body: dict, **_: Any) -> tuple[int, dict]:
        result = self.service.upload_component(
            title=body.get("title"),
            author=body.get("author"),
            origin_region=body.get("origin_region"),
            payload=body.get("payload"),
        )
        return (201 if result["created"] else 200), result

    def _get_component(self, component_id: str, **_: Any) -> tuple[int, dict]:
        return 200, {"component": self.service.get_component(component_id)}

    def _post_variants(self, body: dict, **_: Any) -> tuple[int, dict]:
        result = self.service.upload_variant(
            component_id=body.get("component_id"),
            language=body.get("language"),
            region=body.get("region"),
            subtitles=body.get("subtitles"),
            cases=body.get("cases"),
            payload=body.get("payload"),
        )
        return (201 if result["created"] else 200), result

    def _get_variant(self, variant_id: str, **_: Any) -> tuple[int, dict]:
        return 200, {"variant": self.service.get_variant(variant_id)}

    def _post_licenses(self, body: dict, **_: Any) -> tuple[int, dict]:
        result = self.service.register_license(
            component_id=body.get("component_id"),
            licensor=body.get("licensor"),
            regions=body.get("regions"),
            standards=body.get("standards"),
            valid_from=body.get("valid_from"),
            valid_until=body.get("valid_until"),
        )
        return (201 if result["created"] else 200), result

    def _get_license(self, license_id: str, **_: Any) -> tuple[int, dict]:
        return 200, {"license": self.service.get_license(license_id)}

    def _list_licenses(self, query: dict, **_: Any) -> tuple[int, dict]:
        component_id = query.get("component_id", [None])[0]
        return 200, {"licenses": self.service.list_licenses(component_id)}

    def _withdraw_license(self, license_id: str, **_: Any) -> tuple[int, dict]:
        return 200, self.service.withdraw_license(license_id)

    def _post_dependencies(self, body: dict, **_: Any) -> tuple[int, dict]:
        result = self.service.add_dependency(
            component_id=body.get("component_id"),
            depends_on_id=body.get("depends_on_id"),
            required_fingerprint=body.get("required_fingerprint"),
        )
        return (201 if result["created"] else 200), result

    def _put_region_policy(self, region: str, body: dict, **_: Any) -> tuple[int, dict]:
        return 200, self.service.set_region_policy(
            region,
            required_standards=body.get("required_standards"),
            required_approvals=body.get("required_approvals", 1),
        )

    def _get_region_policy(self, region: str, **_: Any) -> tuple[int, dict]:
        return 200, {"policy": self.service.get_region_policy(region)}

    def _post_candidates(self, body: dict, **_: Any) -> tuple[int, dict]:
        result = self.service.create_candidate(
            region=body.get("region"), variant_ids=body.get("variant_ids")
        )
        return (201 if result["created"] else 200), result

    def _get_candidate(self, candidate_id: str, **_: Any) -> tuple[int, dict]:
        return 200, {"candidate": self.service.get_candidate(candidate_id)}

    def _check_candidate(self, candidate_id: str, **_: Any) -> tuple[int, dict]:
        return 200, self.service.check_candidate(candidate_id)

    def _post_review(self, candidate_id: str, body: dict, **_: Any) -> tuple[int, dict]:
        result = self.service.submit_review(
            candidate_id=candidate_id,
            reviewer=body.get("reviewer"),
            decision=body.get("decision"),
            scope=body.get("scope"),
            comment=body.get("comment", ""),
        )
        return (201 if result["created"] else 200), result

    def _post_release(self, body: dict, **_: Any) -> tuple[int, dict]:
        result = self.service.sign_release(candidate_id=body.get("candidate_id"))
        return (201 if result["created"] else 200), result

    def _list_releases(self, query: dict, **_: Any) -> tuple[int, dict]:
        return 200, {
            "releases": self.service.list_releases(
                status=query.get("status", [None])[0],
                region=query.get("region", [None])[0],
            )
        }

    def _get_release(self, release_id: str, **_: Any) -> tuple[int, dict]:
        return 200, {"release": self.service.get_release(release_id)}

    def _rollback_release(self, release_id: str, body: dict, **_: Any) -> tuple[int, dict]:
        return 200, self.service.rollback_release(release_id, reason=body.get("reason", ""))


ROUTES: list[tuple[str, "re.Pattern[str]", Handler]] = [
    ("GET", re.compile(r"/health"), CourseHubHandler._health),
    ("POST", re.compile(r"/components"), CourseHubHandler._post_components),
    ("GET", re.compile(r"/components/(?P<component_id>[^/]+)"), CourseHubHandler._get_component),
    ("POST", re.compile(r"/variants"), CourseHubHandler._post_variants),
    ("GET", re.compile(r"/variants/(?P<variant_id>[^/]+)"), CourseHubHandler._get_variant),
    ("POST", re.compile(r"/licenses"), CourseHubHandler._post_licenses),
    ("GET", re.compile(r"/licenses"), CourseHubHandler._list_licenses),
    ("GET", re.compile(r"/licenses/(?P<license_id>[^/]+)"), CourseHubHandler._get_license),
    ("POST", re.compile(r"/licenses/(?P<license_id>[^/]+)/withdraw"), CourseHubHandler._withdraw_license),
    ("POST", re.compile(r"/dependencies"), CourseHubHandler._post_dependencies),
    ("PUT", re.compile(r"/regions/(?P<region>[^/]+)/policy"), CourseHubHandler._put_region_policy),
    ("GET", re.compile(r"/regions/(?P<region>[^/]+)/policy"), CourseHubHandler._get_region_policy),
    ("POST", re.compile(r"/candidates"), CourseHubHandler._post_candidates),
    ("GET", re.compile(r"/candidates/(?P<candidate_id>[^/]+)"), CourseHubHandler._get_candidate),
    ("GET", re.compile(r"/candidates/(?P<candidate_id>[^/]+)/check"), CourseHubHandler._check_candidate),
    ("POST", re.compile(r"/candidates/(?P<candidate_id>[^/]+)/reviews"), CourseHubHandler._post_review),
    ("POST", re.compile(r"/releases"), CourseHubHandler._post_release),
    ("GET", re.compile(r"/releases"), CourseHubHandler._list_releases),
    ("GET", re.compile(r"/releases/(?P<release_id>[^/]+)"), CourseHubHandler._get_release),
    ("POST", re.compile(r"/releases/(?P<release_id>[^/]+)/rollback"), CourseHubHandler._rollback_release),
]
