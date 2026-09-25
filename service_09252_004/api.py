"""HTTP 接口边界：仅依赖标准库的 JSON API。

错误约定：{"error": {"code", "message", "details"}}；
状态码映射：not_found→404，conflict→409，validation_failed→422。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import ServiceError, ValidationError
from .service import ReleaseService

_STATUS_BY_CODE = {"not_found": 404, "conflict": 409, "validation_failed": 422}

RouteHandler = Callable[..., tuple[int, Any]]


def parse_time(value: Any) -> float | None:
    """解析外部时间输入：Unix 时间戳或 ISO 8601 字符串。"""
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError("无法解析时间", {"value": value}) from exc
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    raise ValidationError("无法解析时间", {"value": value})


def _build_routes(service: ReleaseService) -> list[tuple[str, re.Pattern, RouteHandler]]:
    """路由表：(方法, 路径模式, 处理器)。处理器返回 (状态码, 响应体)。"""
    routes: list[tuple[str, re.Pattern, RouteHandler]] = []

    def route(method: str, pattern: str):
        def decorator(fn: RouteHandler) -> RouteHandler:
            routes.append((method, re.compile(pattern), fn))
            return fn

        return decorator

    @route("GET", r"/health")
    def health(body: dict) -> tuple[int, Any]:
        return 200, {"status": "ok"}

    @route("POST", r"/components")
    def create_component(body: dict) -> tuple[int, Any]:
        comp = service.create_component(
            name=body.get("name"), kind=body.get("kind"),
            author=body.get("author"), metadata=body.get("metadata"),
        )
        return 201, comp.to_dict()

    @route("GET", r"/components")
    def list_components(body: dict) -> tuple[int, Any]:
        return 200, {"components": [c.to_dict() for c in service.list_components()]}

    @route("GET", r"/components/(?P<component_id>[^/]+)")
    def get_component(body: dict, component_id: str) -> tuple[int, Any]:
        comp = service.get_component(component_id)
        data = comp.to_dict()
        data["variants"] = [v.to_dict() for v in service.list_variants(component_id)]
        data["dependencies"] = service.dependencies_of(component_id)
        return 200, data

    @route("GET", r"/components/(?P<component_id>[^/]+)/conflicts")
    def component_conflicts(body: dict, component_id: str) -> tuple[int, Any]:
        return 200, {"conflicts": service.variant_conflicts(component_id)}

    @route("POST", r"/components/(?P<component_id>[^/]+)/variants")
    def upload_variant(body: dict, component_id: str) -> tuple[int, Any]:
        variant, created = service.upload_variant(
            component_id=component_id,
            language=body.get("language"),
            content=body.get("content"),
            standards=body.get("standards"),
            metadata=body.get("metadata"),
            created_by=body.get("created_by"),
        )
        payload = variant.to_dict()
        payload["deduplicated"] = not created
        return (201 if created else 200), payload

    @route("POST", r"/components/(?P<component_id>[^/]+)/dependencies")
    def add_dependency(body: dict, component_id: str) -> tuple[int, Any]:
        result = service.add_dependency(
            component_id, depends_on=body.get("depends_on"), note=body.get("note")
        )
        return (201 if result["created"] else 200), result

    @route("POST", r"/licenses")
    def grant_license(body: dict) -> tuple[int, Any]:
        lic = service.grant_license(
            component_id=body.get("component_id"),
            licensor=body.get("licensor"),
            regions=body.get("regions"),
            standards=body.get("standards"),
            valid_from=parse_time(body.get("valid_from")),
            valid_until=parse_time(body.get("valid_until")),
        )
        return 201, lic.to_dict()

    @route("GET", r"/licenses")
    def list_licenses(body: dict) -> tuple[int, Any]:
        return 200, {"licenses": [l.to_dict() for l in service.list_licenses()]}

    @route("POST", r"/licenses/(?P<license_id>[^/]+)/withdraw")
    def withdraw_license(body: dict, license_id: str) -> tuple[int, Any]:
        result = service.withdraw_license(license_id, reason=body.get("reason"))
        return 200, {
            "license": result["license"].to_dict(),
            "affected_release_ids": result["affected_release_ids"],
        }

    @route("POST", r"/candidates")
    def create_candidate(body: dict) -> tuple[int, Any]:
        cand = service.create_candidate(
            course_id=body.get("course_id"),
            target_region=body.get("target_region"),
            target_standard=body.get("target_standard"),
            selections=body.get("selections") or {},
            title=body.get("title"),
            created_by=body.get("created_by"),
            required_approvals=body.get("required_approvals"),
        )
        return 201, cand.to_dict()

    @route("GET", r"/candidates/(?P<candidate_id>[^/]+)")
    def get_candidate(body: dict, candidate_id: str) -> tuple[int, Any]:
        cand = service.get_candidate(candidate_id)
        data = cand.to_dict()
        data["reviews"] = [r.to_dict() for r in service.list_reviews(candidate_id)]
        data["evaluation"] = service.evaluate_candidate(candidate_id)
        return 200, data

    @route("POST", r"/candidates/(?P<candidate_id>[^/]+)/reviews")
    def submit_review(body: dict, candidate_id: str) -> tuple[int, Any]:
        review = service.submit_review(
            candidate_id, reviewer=body.get("reviewer"),
            decision=body.get("decision"), comment=body.get("comment"),
        )
        return 201, review.to_dict()

    @route("POST", r"/candidates/(?P<candidate_id>[^/]+)/publish")
    def publish(body: dict, candidate_id: str) -> tuple[int, Any]:
        release, created = service.publish(candidate_id, issued_by=body.get("issued_by"))
        release = dict(release)
        release["idempotent_replay"] = not created
        return (201 if created else 200), release

    @route("GET", r"/releases")
    def list_releases(body: dict) -> tuple[int, Any]:
        return 200, {"releases": service.list_releases()}

    @route("GET", r"/releases/(?P<release_id>[^/]+)")
    def get_release(body: dict, release_id: str) -> tuple[int, Any]:
        return 200, service.get_release(release_id)

    @route("POST", r"/releases/(?P<release_id>[^/]+)/rollback")
    def rollback(body: dict, release_id: str) -> tuple[int, Any]:
        return 200, service.rollback(release_id, reason=body.get("reason"))

    return routes


def make_server(service: ReleaseService, host: str, port: int) -> ThreadingHTTPServer:
    routes = _build_routes(service)

    class Handler(BaseHTTPRequestHandler):
        server_version = "JointRelease/1.0"

        def _dispatch(self, method: str) -> None:
            try:
                body: dict = {}
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(body, dict):
                        raise ValidationError("请求体必须是 JSON 对象")
                path = self.path.split("?", 1)[0]
                for route_method, pattern, handler in routes:
                    if route_method != method:
                        continue
                    match = pattern.fullmatch(path)
                    if match:
                        status, payload = handler(body, **match.groupdict())
                        return self._send(status, payload)
                self._send(404, {"error": {"code": "not_found", "message": "路由不存在"}})
            except ServiceError as exc:
                self._send(
                    _STATUS_BY_CODE.get(exc.code, 400),
                    {"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
                )
            except json.JSONDecodeError as exc:
                self._send(400, {"error": {"code": "bad_json", "message": str(exc)}})
            except Exception as exc:  # noqa: BLE001 - 接口边界兜底
                self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

        def _send(self, status: int, payload: Any) -> None:
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, *args: Any) -> None:  # 测试与嵌入场景保持静默
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve(service: ReleaseService, host: str, port: int) -> None:
    server = make_server(service, host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
