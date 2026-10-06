"""基于标准库的 JSON HTTP API。

仅用于本地/内网部署：不引入外部依赖，认证以请求头
X-Actor-Id / X-Actor-Role / X-Owner-Id 声明身份（接入网关后可替换）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from .audit import participant_view, verify_audit_trail
from .models import Actor, Role, parse_dt
from .service import CoordinationService, DomainError, NotFound, PermissionDenied


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return str(value)


def _actor(headers: Any) -> Actor:
    actor_id = headers.get("X-Actor-Id", "").strip()
    role_raw = headers.get("X-Actor-Role", "").strip()
    owner_id = headers.get("X-Owner-Id", "").strip() or None
    if not actor_id or not role_raw:
        raise PermissionDenied("缺少 X-Actor-Id / X-Actor-Role 请求头")
    return Actor(actor_id=actor_id, role=Role(role_raw), owner_id=owner_id)


def _dt(body: dict[str, Any], key: str) -> datetime:
    return parse_dt(str(body[key]))


def _opt_dt(body: dict[str, Any], key: str) -> Optional[datetime]:
    value = body.get(key)
    return parse_dt(str(value)) if value else None


def _path(body: dict[str, Any], key: str = "path") -> list[tuple[float, float]]:
    return [(float(p[0]), float(p[1])) for p in body[key]]


def make_handler(service: CoordinationService) -> type[BaseHTTPRequestHandler]:
    """构造绑定指定服务的请求处理器。"""

    def respond(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def route(method: str, path: str) -> Optional[Callable[[Actor, dict[str, Any], dict[str, str]], Any]]:
        parts = [p for p in path.strip("/").split("/") if p]
        routes: dict[tuple[str, tuple[str, ...]], Callable[..., Any]] = {}

        routes[("POST", ("segments",))] = lambda actor, body, q: service.submit_segment_snapshot(
            actor,
            segment_id=body["segment_id"],
            utility=body["utility"],
            confidentiality=body["confidentiality"],
            path=_path(body),
            depth_top=float(body["depth_top"]),
            depth_bottom=float(body["depth_bottom"]),
            effective_from=_dt(body, "effective_from"),
            effective_to=_opt_dt(body, "effective_to"),
            risk=body["risk"],
            owner_id=body.get("owner_id"),
        )
        routes[("GET", ("segments",))] = lambda actor, body, q: service.list_segments(
            actor, at=parse_dt(q["at"]) if "at" in q else None
        )
        routes[("POST", ("applications",))] = lambda actor, body, q: _application_payload(
            *service.apply_occupancy(
                actor,
                request_id=body["request_id"],
                project_code=body["project_code"],
                path=_path(body),
                impact_radius=float(body["impact_radius"]),
                method=body["method"],
                depth_top=float(body["depth_top"]),
                depth_bottom=float(body["depth_bottom"]),
                window_start=_dt(body, "window_start"),
                window_end=_dt(body, "window_end"),
            )
        )
        routes[("POST", ("emergency-repairs",))] = lambda actor, body, q: _emergency_payload(
            *service.emergency_repair(
                actor,
                request_id=body["request_id"],
                project_code=body["project_code"],
                path=_path(body),
                impact_radius=float(body["impact_radius"]),
                method=body["method"],
                depth_top=float(body["depth_top"]),
                depth_bottom=float(body["depth_bottom"]),
                window_start=_dt(body, "window_start"),
                window_end=_dt(body, "window_end"),
            )
        )
        if len(parts) == 3 and parts[0] == "applications" and method == "POST":
            app_id, action = parts[1], parts[2]
            if action == "confirmations":
                return lambda actor, body, q: service.confirm_segment(
                    actor, app_id, body["segment_id"], accept=bool(body["accept"]),
                    comment=body.get("comment", ""),
                )
            if action == "cosign":
                return lambda actor, body, q: service.co_sign(
                    actor, app_id, approve=bool(body["approve"]), comment=body.get("comment", "")
                )
            if action == "issue":
                return lambda actor, body, q: service.issue_permit(actor, app_id)
            if action == "design-change":
                return lambda actor, body, q: _application_payload(
                    *service.design_change(
                        actor,
                        app_id,
                        path=_path(body) if "path" in body else None,
                        impact_radius=float(body["impact_radius"]) if "impact_radius" in body else None,
                        method=body.get("method"),
                        depth_top=float(body["depth_top"]) if "depth_top" in body else None,
                        depth_bottom=float(body["depth_bottom"]) if "depth_bottom" in body else None,
                        window_start=_opt_dt(body, "window_start"),
                        window_end=_opt_dt(body, "window_end"),
                    )
                )
            if action == "delay":
                return lambda actor, body, q: _application_payload(
                    *service.delay_project(actor, app_id, new_window_end=_dt(body, "new_window_end"))
                )
            if action == "cancel":
                return lambda actor, body, q: service.cancel_application(actor, app_id)
        if len(parts) == 3 and parts[0] == "permits" and method == "POST":
            permit_id, action = parts[1], parts[2]
            if action == "suspend":
                return lambda actor, body, q: service.suspend_permit(
                    actor, permit_id, reason=body.get("reason", "")
                )
            if action == "resume":
                return lambda actor, body, q: service.resume_permit(actor, permit_id)
            if action == "complete":
                return lambda actor, body, q: service.complete_work(actor, permit_id)
        if len(parts) == 2 and parts[0] == "applications" and method == "GET":
            app_id = parts[1]
            return lambda actor, body, q: service.get_application(app_id)
        if len(parts) == 2 and parts[0] == "assessments" and method == "GET":
            assessment_id = parts[1]
            return lambda actor, body, q: service.get_assessment(assessment_id)
        if len(parts) == 2 and parts[0] == "permits" and method == "GET":
            permit_id = parts[1]
            return lambda actor, body, q: service.get_permit(permit_id)
        if len(parts) == 3 and parts[0] == "audit" and parts[1] == "participants" and method == "GET":
            participant_id = parts[2]
            return lambda actor, body, q: participant_view(
                service, actor, participant_id, at=parse_dt(q["at"]) if "at" in q else None
            )
        if len(parts) == 2 and parts[0] == "audit" and parts[1] == "verify" and method == "GET":
            return lambda actor, body, q: verify_audit_trail(service)
        return routes.get((method, tuple(parts)))

    def _application_payload(app: Any, assessment: Any) -> dict[str, Any]:
        return {"application": app, "assessment": assessment}

    def _emergency_payload(app: Any, permit: Any) -> dict[str, Any]:
        return {"application": app, "permit": permit}

    class Handler(BaseHTTPRequestHandler):
        server_version = "UtilityCoordination/0.1"

        def _handle(self, method: str) -> None:
            try:
                path, _, query = self.path.partition("?")
                params = dict(
                    pair.split("=", 1) for pair in query.split("&") if "=" in pair
                )
                handler = route(method, path)
                if handler is None:
                    respond(self, 404, {"error": "not_found", "message": "路由不存在"})
                    return
                body: dict[str, Any] = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length", "0") or 0)
                    if length:
                        body = json.loads(self.rfile.read(length).decode("utf-8"))
                result = handler(_actor(self.headers), body, params)
                respond(self, 200, result)
            except PermissionDenied as exc:
                respond(self, 403, {"error": "permission_denied", "message": str(exc)})
            except NotFound as exc:
                respond(self, 404, {"error": "not_found", "message": str(exc)})
            except DomainError as exc:
                respond(self, 409, {"error": "domain_error", "message": str(exc)})
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                respond(self, 400, {"error": "bad_request", "message": str(exc)})

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def log_message(self, format: str, *args: Any) -> None:  # 静默访问日志
            return

    return Handler


def serve(service: CoordinationService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """启动 HTTP 服务（阻塞）。"""
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"地下管网施工冲突协同 API 已启动：http://{host}:{port}")
    server.serve_forever()
    return server
