"""命令行入口：

- demo：运行端到端演示并打印各步骤与事故还原摘要；
- serve：启动 HTTP API；
- audit verify：校验审计哈希链；
- audit incident：还原事故前全局时间线与各方视图；
- audit actor：还原某参与方事故前看到的版本/警示/决定/通知；
- audit application：还原某申请的完整审核版本链。

审计读取只依赖 JSONL 日志文件，可离线执行。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from utility_coordination.demo import build_demo
from utility_coordination.events import EventLog
from utility_coordination.replay import IncidentReplay
from utility_coordination.service import CoordinationService
from utility_coordination.timeutil import MutableClock


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=lambda o: getattr(o, "value", str(o))))


def _load_log(path: str) -> EventLog:
    log_path = Path(path)
    if not log_path.exists():
        raise SystemExit(f"审计日志不存在: {path}")
    return EventLog(log_path)


def cmd_demo(args: argparse.Namespace) -> None:
    clock = MutableClock("2026-09-01T00:00:00Z")
    service = CoordinationService(EventLog(args.audit_file), clock=clock.now)
    ids = build_demo(service, clock)
    print("=== 演示步骤 ===")
    for step in ids["steps"]:
        print(f"[{step['at']}] {step['step']}")
        print("   " + json.dumps(step["result"], ensure_ascii=False, default=str))
    print("\n=== 事故时刻 ===", ids["incident_moment"])
    # 在事故时刻“之前”的视角还原
    report = IncidentReplay(service.log).application_timeline(ids["application_a"], ids["incident_moment"])
    print("\n=== 申请 A 的评估版本链（事故前视角）===")
    for asm in report["assessments"]:
        print(
            f"- {asm['assessment_id']} rev.{asm['revision']} cause={asm['cause']} "
            f"引用快照 {[s['snapshot_id'] for s in asm['referenced_snapshots']]} 警示={asm['warnings']}"
        )
    print("\n=== 燃气权属单位看到的内容（事故前）===")
    gas_view = IncidentReplay(service.log).actor_view(ids["gas"], ids["incident_moment"])
    print(f"通知 {len(gas_view['notifications_received'])} 条，决定 {len(gas_view['decisions'])} 条，"
          f"提交快照版本 {[s['version'] for s in gas_view['snapshots_submitted']]}")
    print("\n审计链校验：", end="")
    service.log.verify_chain()
    print("OK")


def cmd_verify(args: argparse.Namespace) -> None:
    log = _load_log(args.audit_file)
    log.verify_chain()
    _print({"ok": True, "events": len(log.events), "head": log.head_hash()})


def cmd_incident(args: argparse.Namespace) -> None:
    log = _load_log(args.audit_file)
    replay = IncidentReplay(log)
    replay.verify()
    _print(replay.incident_report(args.before))


def cmd_actor(args: argparse.Namespace) -> None:
    log = _load_log(args.audit_file)
    IncidentReplay(log).verify()
    _print(IncidentReplay(log).actor_view(args.actor_id, args.before))


def cmd_application(args: argparse.Namespace) -> None:
    log = _load_log(args.audit_file)
    IncidentReplay(log).verify()
    _print(IncidentReplay(log).application_timeline(args.application_id, args.before))


def cmd_serve(args: argparse.Namespace) -> None:
    from utility_coordination.api import serve

    clock = MutableClock(args.clock_start)
    service = CoordinationService(EventLog(args.audit_file), clock=clock.now)
    serve(service, host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run_cli.py", description="地下管网施工协同命令行")
    parser.add_argument("--audit-file", default="data/audit.jsonl", help="审计日志 JSONL 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_demo = sub.add_parser("demo", help="运行端到端演示场景")
    p_demo.set_defaults(func=cmd_demo)

    p_verify = sub.add_parser("verify", help="校验审计哈希链")
    p_verify.set_defaults(func=cmd_verify)

    p_incident = sub.add_parser("incident", help="还原事故前全局协同时间线")
    p_incident.add_argument("--before", help="事故时刻 ISO8601，仅还原该时刻之前的事件")
    p_incident.set_defaults(func=cmd_incident)

    p_actor = sub.add_parser("actor", help="还原某参与方事故前看到的内容")
    p_actor.add_argument("actor_id")
    p_actor.add_argument("--before")
    p_actor.set_defaults(func=cmd_actor)

    p_app = sub.add_parser("application", help="还原某申请的评估/会签/签发版本链")
    p_app.add_argument("application_id")
    p_app.add_argument("--before")
    p_app.set_defaults(func=cmd_application)

    p_serve = sub.add_parser("serve", help="启动 HTTP API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--clock-start", default="2026-09-01T00:00:00Z")
    p_serve.set_defaults(func=cmd_serve)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
