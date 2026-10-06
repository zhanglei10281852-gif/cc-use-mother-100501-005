"""命令行接口：面向协调员、权属单位与审计人员的操作入口。

状态保存在 JSON 文件中（--state 指定，默认 ./uc_state.json），
所有输出均为 JSON，便于脚本编排与审计留痕。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional

from .api import _json_default
from .audit import participant_view, verify_audit_trail
from .models import Actor, Role, parse_dt
from .service import CoordinationService, DomainError
from .store import JsonFileStore


def _actor(args: argparse.Namespace) -> Actor:
    return Actor(actor_id=args.actor, role=Role(args.role), owner_id=args.owner)


def _path_arg(raw: str) -> list[tuple[float, float]]:
    """解析 "x1,y1;x2,y2;..." 形式的路径。"""
    points = []
    for item in raw.split(";"):
        x, y = item.split(",")
        points.append((float(x), float(y)))
    return points


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="utility-coordination", description="地下管网施工冲突协同 CLI")
    parser.add_argument("--state", default="uc_state.json", help="状态文件路径")
    parser.add_argument("--actor", required=True, help="操作人 id")
    parser.add_argument("--role", required=True, choices=[r.value for r in Role], help="操作人角色")
    parser.add_argument("--owner", default=None, help="权属单位 id（role=utility_owner 时必填）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("submit-segment", help="提交管段快照")
    p.add_argument("--segment-id", required=True)
    p.add_argument("--utility", required=True, choices=["water", "heating", "gas", "telecom"])
    p.add_argument("--confidentiality", required=True, choices=["public", "internal", "secret"])
    p.add_argument("--path", required=True, help="形如 x1,y1;x2,y2")
    p.add_argument("--depth-top", type=float, required=True)
    p.add_argument("--depth-bottom", type=float, required=True)
    p.add_argument("--effective-from", required=True)
    p.add_argument("--effective-to", default=None)
    p.add_argument("--risk", required=True, choices=["low", "medium", "high"])
    p.add_argument("--owner-id", default=None, help="协调员代提交时指定权属单位")

    p = sub.add_parser("segments", help="按角色查看可见管段")
    p.add_argument("--at", default=None, help="查看该时刻可见的版本（ISO-8601）")

    p = sub.add_parser("apply", help="申请占用")
    p.add_argument("--request-id", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--impact-radius", type=float, required=True)
    p.add_argument("--method", required=True)
    p.add_argument("--depth-top", type=float, required=True)
    p.add_argument("--depth-bottom", type=float, required=True)
    p.add_argument("--window-start", required=True)
    p.add_argument("--window-end", required=True)

    p = sub.add_parser("confirm", help="权属确认")
    p.add_argument("--application", required=True)
    p.add_argument("--segment-id", required=True)
    p.add_argument("--accept", choices=["yes", "no"], required=True)
    p.add_argument("--comment", default="")

    p = sub.add_parser("cosign", help="风险会签")
    p.add_argument("--application", required=True)
    p.add_argument("--approve", choices=["yes", "no"], required=True)
    p.add_argument("--comment", default="")

    p = sub.add_parser("issue", help="签发许可证")
    p.add_argument("--application", required=True)

    p = sub.add_parser("design-change", help="设计变更")
    p.add_argument("--application", required=True)
    p.add_argument("--path", default=None)
    p.add_argument("--impact-radius", type=float, default=None)
    p.add_argument("--method", default=None)
    p.add_argument("--depth-top", type=float, default=None)
    p.add_argument("--depth-bottom", type=float, default=None)
    p.add_argument("--window-start", default=None)
    p.add_argument("--window-end", default=None)

    p = sub.add_parser("delay", help="工程延期")
    p.add_argument("--application", required=True)
    p.add_argument("--new-window-end", required=True)

    p = sub.add_parser("suspend", help="暂停许可证")
    p.add_argument("--permit", required=True)
    p.add_argument("--reason", required=True)

    p = sub.add_parser("resume", help="恢复许可证")
    p.add_argument("--permit", required=True)

    p = sub.add_parser("complete", help="完工核销")
    p.add_argument("--permit", required=True)

    p = sub.add_parser("emergency", help="紧急抢修（临时占位）")
    p.add_argument("--request-id", required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--impact-radius", type=float, required=True)
    p.add_argument("--method", required=True)
    p.add_argument("--depth-top", type=float, required=True)
    p.add_argument("--depth-bottom", type=float, required=True)
    p.add_argument("--window-start", required=True)
    p.add_argument("--window-end", required=True)

    p = sub.add_parser("show-application", help="查看申请当前状态")
    p.add_argument("--application", required=True)

    p = sub.add_parser("audit-view", help="还原参与方在某时刻前看到的版本/警示/决定/通知")
    p.add_argument("--participant", required=True)
    p.add_argument("--at", default=None)

    sub.add_parser("audit-verify", help="校验审计哈希链")
    return parser


def run(args: argparse.Namespace) -> Any:
    store = JsonFileStore(args.state)
    service = CoordinationService(store)
    actor = _actor(args)
    cmd = args.command

    if cmd == "submit-segment":
        result = service.submit_segment_snapshot(
            actor,
            segment_id=args.segment_id,
            utility=args.utility,
            confidentiality=args.confidentiality,
            path=_path_arg(args.path),
            depth_top=args.depth_top,
            depth_bottom=args.depth_bottom,
            effective_from=parse_dt(args.effective_from),
            effective_to=parse_dt(args.effective_to) if args.effective_to else None,
            risk=args.risk,
            owner_id=args.owner_id,
        )
    elif cmd == "segments":
        result = service.list_segments(actor, at=parse_dt(args.at) if args.at else None)
    elif cmd == "apply":
        app, assessment = service.apply_occupancy(
            actor,
            request_id=args.request_id,
            project_code=args.project,
            path=_path_arg(args.path),
            impact_radius=args.impact_radius,
            method=args.method,
            depth_top=args.depth_top,
            depth_bottom=args.depth_bottom,
            window_start=parse_dt(args.window_start),
            window_end=parse_dt(args.window_end),
        )
        result = {"application": app, "assessment": assessment}
    elif cmd == "confirm":
        result = service.confirm_segment(
            actor, args.application, args.segment_id,
            accept=args.accept == "yes", comment=args.comment,
        )
    elif cmd == "cosign":
        result = service.co_sign(actor, args.application, approve=args.approve == "yes",
                                 comment=args.comment)
    elif cmd == "issue":
        result = service.issue_permit(actor, args.application)
    elif cmd == "design-change":
        app, assessment = service.design_change(
            actor,
            args.application,
            path=_path_arg(args.path) if args.path else None,
            impact_radius=args.impact_radius,
            method=args.method,
            depth_top=args.depth_top,
            depth_bottom=args.depth_bottom,
            window_start=parse_dt(args.window_start) if args.window_start else None,
            window_end=parse_dt(args.window_end) if args.window_end else None,
        )
        result = {"application": app, "assessment": assessment}
    elif cmd == "delay":
        app, assessment = service.delay_project(
            actor, args.application, new_window_end=parse_dt(args.new_window_end)
        )
        result = {"application": app, "assessment": assessment}
    elif cmd == "suspend":
        result = service.suspend_permit(actor, args.permit, reason=args.reason)
    elif cmd == "resume":
        result = service.resume_permit(actor, args.permit)
    elif cmd == "complete":
        result = service.complete_work(actor, args.permit)
    elif cmd == "emergency":
        app, permit = service.emergency_repair(
            actor,
            request_id=args.request_id,
            project_code=args.project,
            path=_path_arg(args.path),
            impact_radius=args.impact_radius,
            method=args.method,
            depth_top=args.depth_top,
            depth_bottom=args.depth_bottom,
            window_start=parse_dt(args.window_start),
            window_end=parse_dt(args.window_end),
        )
        result = {"application": app, "permit": permit}
    elif cmd == "show-application":
        result = service.get_application(args.application)
    elif cmd == "audit-view":
        result = participant_view(
            service, actor, args.participant, at=parse_dt(args.at) if args.at else None
        )
    elif cmd == "audit-verify":
        result = verify_audit_trail(service)
    else:  # pragma: no cover - argparse 已拦截
        raise DomainError(f"未知命令 {cmd}")

    store.save()
    return result


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except DomainError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=1, default=_json_default))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
