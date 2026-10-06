"""HTTP API：仅依赖标准库 http.server，便于在无外部服务的环境中运行。

鉴权采用 X-Actor-Id 请求头（演示/内网部署假设）；所有动作仍在服务层做角色校验，
并完整进入哈希链审计日志。生产部署应将其替换为带签名的身份令牌。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .models import (
    Actor,
    AppStatus,
    Classification,
    Decision,
    Priority,
    RiskLevel,
    Role,
    UtilityType,
    WorkMethod,
    entity_to_dict,
)
from .replay import IncidentReplay
from .service import AuthzError, CoordinationService, ServiceError
from .events import EventLog
from .timeutil import MutableClock


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "value"):
        return obj.value
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(type(obj).__name__)


def create_service(audit_path: str | None = None, clock: Callable[[], Any] | None = None) -> CoordinationService:
    service = CoordinationService(EventLog(audit_path), clock=clock or MutableClock().now)
    return service


def make_handler(service: CoordinationService) -> type[BaseHTTPRequestHandler]:
    service_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "UtilityCoordination/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
            pass

        # ---------------------------------------------------------------- 工具

        def _send(self, code: int, data: Any) -> None:
            body = json.dumps(data, ensure_ascii=False, default=_json_default).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ServiceError(f"请求体不是合法 JSON: {exc}")
            if not isinstance(data, dict):
                raise ServiceError("请求体必须是 JSON 对象")
            return data

        def _actor(self) -> str:
            actor = self.headers.get("X-Actor-Id")
            if not actor:
                raise AuthzError("缺少 X-Actor-Id 请求头")
            return actor

        def _query(self) -> dict[str, str]:
            qs = parse_qs(urlparse(self.path).query)
            return {k: v[0] for k, v in qs.items()}

        # ---------------------------------------------------------------- 路由

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            # 串行化所有写操作，保证并发申请场景下占位判定的原子性
            with service_lock:
                try:
                    path = urlparse(self.path).path.rstrip("/") or "/"
                    for pattern, verbs, handler in ROUTES:
                        if method in verbs:
                            params = self._match(pattern, path)
                            if params is not None:
                                handler(self, **params)
                                return
                    self._send(404, {"error": "未找到路由", "path": path})
                except AuthzError as exc:
                    self._send(403, {"error": str(exc)})
                except ServiceError as exc:
                    self._send(422, {"error": str(exc)})
                except KeyError as exc:
                    self._send(400, {"error": f"缺少字段: {exc.args[0]}"})
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})

        @staticmethod
        def _match(pattern: str, path: str) -> dict[str, str] | None:
            p_parts, x_parts = pattern.split("/"), path.split("/")
            if len(p_parts) != len(x_parts):
                return None
            params: dict[str, str] = {}
            for pp, xp in zip(p_parts, x_parts):
                if pp.startswith("{") and pp.endswith("}"):
                    params[pp[1:-1]] = xp
                elif pp != xp:
                    return None
            return params

        # ---------------------------------------------------------------- 端点

        def h_register_actor(self) -> None:
            body = self._read_json()
            actor = Actor(
                actor_id=body["actor_id"],
                name=body["name"],
                role=Role(body["role"]),
                owner_id=body.get("owner_id"),
                clearance=int(body.get("clearance", Classification.PUBLIC)),
            )
            service.register_actor(actor)
            self._send(201, entity_to_dict(actor))

        def h_submit_snapshot(self) -> None:
            body = self._read_json()
            snap = service.submit_pipe_snapshot(
                self._actor(),
                pipe_code=body["pipe_code"],
                utility_type=UtilityType(body["utility_type"]),
                classification=int(body["classification"]),
                risk_level=RiskLevel(body["risk_level"]),
                geometry=[tuple(p) for p in body["geometry"]],
                diameter_mm=int(body["diameter_mm"]),
                pressure=str(body.get("pressure", "")),
                effective_from=body["effective_from"],
                effective_to=body.get("effective_to"),
            )
            self._send(201, entity_to_dict(snap))

        def h_apply(self) -> None:
            body = self._read_json()
            app = service.apply_for_occupancy(
                self._actor(),
                project_code=body["project_code"],
                work_area=[tuple(p) for p in body["work_area"]],
                road_closure=[tuple(p) for p in body["road_closure"]] if body.get("road_closure") else None,
                method=WorkMethod(body["method"]),
                window_start=body["window_start"],
                window_end=body["window_end"],
                impact_radius_m=float(body["impact_radius_m"]),
                priority=Priority(body.get("priority", "normal")),
            )
            assessment = service.latest_assessment(app.application_id)
            current = service.get_application(app.application_id)
            self._send(201, {"application": entity_to_dict(current), "assessment": service.view_assessment(self._actor(), assessment.assessment_id)})

        def h_get_application(self, application_id: str) -> None:
            self._send(200, entity_to_dict(service.get_application(application_id)))

        def h_respond(self, application_id: str) -> None:
            body = self._read_json()
            rec = service.respond_pipe(
                self._actor(),
                application_id,
                snapshot_id=body["snapshot_id"],
                decision=Decision(body["decision"]),
                comment=body.get("comment"),
            )
            self._send(200, entity_to_dict(rec))

        def h_revise(self, application_id: str) -> None:
            body = self._read_json()
            asm = service.revise_application(
                self._actor(),
                application_id,
                work_area=[tuple(p) for p in body["work_area"]] if body.get("work_area") else None,
                road_closure=[tuple(p) for p in body["road_closure"]] if body.get("road_closure") else None,
                method=WorkMethod(body["method"]) if body.get("method") else None,
                window_start=body.get("window_start"),
                window_end=body.get("window_end"),
                impact_radius_m=float(body["impact_radius_m"]) if body.get("impact_radius_m") is not None else None,
                change_note=body.get("change_note", ""),
            )
            self._send(200, service.view_assessment(self._actor(), asm.assessment_id))

        def h_suspend(self, application_id: str) -> None:
            body = self._read_json()
            app = service.suspend_permit(self._actor(), application_id, body.get("reason", "许可证暂停"))
            self._send(200, entity_to_dict(app))

        def h_resume(self, application_id: str) -> None:
            asm = service.resume_after_suspension(self._actor(), application_id)
            self._send(200, service.view_assessment(self._actor(), asm.assessment_id))

        def h_issue(self, application_id: str) -> None:
            body = self._read_json()
            permit = service.issue_permit(
                self._actor(),
                application_id,
                emergency_acknowledgement=body.get("emergency_acknowledgement"),
            )
            self._send(201, entity_to_dict(permit))

        def h_complete(self, application_id: str) -> None:
            service.complete_project(self._actor(), application_id)
            self._send(200, {"application_id": application_id, "status": AppStatus.COMPLETED.value})

        def h_emergency(self) -> None:
            body = self._read_json()
            app = service.emergency_repair(
                self._actor(),
                project_code=body["project_code"],
                work_area=[tuple(p) for p in body["work_area"]],
                window_start=body["window_start"],
                window_end=body["window_end"],
                impact_radius_m=float(body["impact_radius_m"]),
                description=body.get("description", "紧急抢修"),
            )
            self._send(201, entity_to_dict(app))

        def h_expire(self) -> None:
            blocked = service.expire_pending_responses(self._actor())
            self._send(200, {"blocked_applications": blocked})

        def h_assessment(self, assessment_id: str) -> None:
            self._send(200, service.view_assessment(self._actor(), assessment_id))

        def h_assessment_history(self, application_id: str) -> None:
            viewer = self._actor()
            history = service.assessment_history(application_id)
            self._send(200, [service.view_assessment(viewer, a.assessment_id) for a in history])

        def h_snapshot(self, snapshot_id: str) -> None:
            self._send(200, service.view_snapshot(self._actor(), snapshot_id))

        def h_notifications(self) -> None:
            self._send(200, service.notifications_for(self._actor()))

        def h_permits(self, application_id: str) -> None:
            self._send(200, [entity_to_dict(p) for p in service.permits_for(application_id)])

        def h_audit_verify(self) -> None:
            service.log.verify_chain()
            self._send(200, {"ok": True, "events": len(service.log.events), "head": service.log.head_hash()})

        def h_replay_incident(self) -> None:
            before = self._query().get("before")
            replay = IncidentReplay(service.log)
            replay.verify()
            self._send(200, replay.incident_report(before))

        def h_replay_actor(self, actor_id: str) -> None:
            before = self._query().get("before")
            replay = IncidentReplay(service.log)
            replay.verify()
            self._send(200, replay.actor_view(actor_id, before))

        def h_replay_application(self, application_id: str) -> None:
            before = self._query().get("before")
            replay = IncidentReplay(service.log)
            replay.verify()
            self._send(200, replay.application_timeline(application_id, before))

    # (模式, 方法集合, 处理函数名)
    ROUTES = [
        ("/actors", {"POST"}, Handler.h_register_actor),
        ("/snapshots", {"POST"}, Handler.h_submit_snapshot),
        ("/snapshots/{snapshot_id}", {"GET"}, Handler.h_snapshot),
        ("/applications", {"POST"}, Handler.h_apply),
        ("/applications/{application_id}", {"GET"}, Handler.h_get_application),
        ("/applications/{application_id}/respond", {"POST"}, Handler.h_respond),
        ("/applications/{application_id}/revise", {"POST"}, Handler.h_revise),
        ("/applications/{application_id}/suspend", {"POST"}, Handler.h_suspend),
        ("/applications/{application_id}/resume", {"POST"}, Handler.h_resume),
        ("/applications/{application_id}/issue-permit", {"POST"}, Handler.h_issue),
        ("/applications/{application_id}/complete", {"POST"}, Handler.h_complete),
        ("/applications/{application_id}/assessments", {"GET"}, Handler.h_assessment_history),
        ("/applications/{application_id}/permits", {"GET"}, Handler.h_permits),
        ("/assessments/{assessment_id}", {"GET"}, Handler.h_assessment),
        ("/emergency-repairs", {"POST"}, Handler.h_emergency),
        ("/system/expire-responses", {"POST"}, Handler.h_expire),
        ("/notifications", {"GET"}, Handler.h_notifications),
        ("/audit/verify", {"GET"}, Handler.h_audit_verify),
        ("/replay/incident", {"GET"}, Handler.h_replay_incident),
        ("/replay/actors/{actor_id}", {"GET"}, Handler.h_replay_actor),
        ("/replay/applications/{application_id}", {"GET"}, Handler.h_replay_application),
    ]
    return Handler


def serve(service: CoordinationService, host: str = "127.0.0.1", port: int = 8080) -> None:
    handler = make_handler(service)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"地下管网施工协同服务监听 http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
