"""地下管网施工协同核心服务。

职责：
- 管段快照版本链（带生效期与保密等级）；
- 占用申请的空间/时间冲突评估（管段冲突 + 相邻工程封路/缓冲冲突）；
- 唯一合法时空占位（并发申请只有一个能占位）；
- 权属确认与高风险会签，**未回复的高风险管段绝不默认安全**；
- 设计变更 / 紧急抢修 / 许可证暂停 / 工程延期触发重新评估，
  已完成审核引用原始快照，可沿用的意见显式记为 carried；
- 按角色最小披露生成通知；所有动作写入只追加哈希链审计日志。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from itertools import count
from pathlib import Path
from typing import Any, Callable, Iterable

from . import geometry
from .disclosure import assessment_view, snapshot_view
from .events import EventLog
from .models import (
    Actor,
    AppStatus,
    Classification,
    ConfirmationRecord,
    ConfirmationState,
    ConflictAssessment,
    Decision,
    Notification,
    OccupancyApplication,
    OccupancyConflict,
    Permit,
    PipeConflict,
    PipeSnapshot,
    PLACEHOLDER_STATUSES,
    Priority,
    ReviewRequirement,
    RiskLevel,
    Role,
    UtilityType,
    WorkMethod,
    entity_to_dict,
    fingerprint_of,
)
from .timeutil import format_ts, parse_ts, utc_now, windows_overlap

# 未回复超时：普通 48 小时，紧急 4 小时。超时后低风险可留痕放行，高风险永不默认安全。
RESPONSE_WINDOW = {Priority.NORMAL: timedelta(hours=48), Priority.EMERGENCY: timedelta(hours=4)}


class ServiceError(Exception):
    """业务规则冲突。"""


class AuthzError(ServiceError):
    """角色权限不足或越权操作。"""


class CoordinationService:
    def __init__(
        self,
        event_log: EventLog | None = None,
        clock: Callable[[], Any] = utc_now,
        audit_path: str | Path | None = None,
    ) -> None:
        self.log = event_log or EventLog(audit_path)
        self.clock = clock
        self._actors: dict[str, Actor] = {}
        self._snapshots: dict[str, PipeSnapshot] = {}
        self._applications: dict[str, OccupancyApplication] = {}
        self._assessments: dict[str, ConflictAssessment] = {}
        # application_id -> 最新评估 id（历史评估全部保留）
        self._latest_assessment: dict[str, str] = {}
        self._confirmations: list[ConfirmationRecord] = []
        self._permits: dict[str, Permit] = {}
        self._notifications: list[Notification] = []
        self._seq = count(1)
        # 系统自动动作（超时收口、占位释放后重评估）的审计身份
        self._system_actor = Actor("system", "系统", Role.COORDINATOR)
        self._actors.setdefault("system", self._system_actor)
        # 紧急抢修批量抢占期间抑制占位释放钩子，避免等待方抢先占位
        self._release_hook_suppressed = 0

    # ------------------------------------------------------------------ 基础

    def _now(self):
        return self.clock()

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._seq):06d}"

    def register_actor(self, actor: Actor) -> Actor:
        self._actors[actor.actor_id] = actor
        self._append(actor.actor_id, "actor.registered", entity_to_dict(actor))
        return actor

    def _require_actor(self, actor_id: str) -> Actor:
        actor = self._actors.get(actor_id)
        if actor is None:
            raise AuthzError(f"未知参与方: {actor_id}")
        return actor

    def _require_role(self, actor_id: str, *roles: Role) -> Actor:
        actor = self._require_actor(actor_id)
        if actor.role not in roles:
            raise AuthzError(f"{actor_id} 角色 {actor.role.value} 无权执行该操作")
        return actor

    def _append(self, actor_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.log.append(self._next_id("evt"), format_ts(self._now()), actor_id, event_type, payload)

    def _notify(
        self,
        actor: Actor,
        subject: str,
        body: str,
        related_type: str,
        related_id: str,
        sensitive: bool = False,
    ) -> Notification:
        note = Notification(
            notification_id=self._next_id("ntf"),
            created_at=format_ts(self._now()),
            recipient_actor_id=actor.actor_id,
            subject=subject,
            body=body,
            related_type=related_type,
            related_id=related_id,
            redacted=sensitive,
            sensitive=sensitive,
        )
        self._notifications.append(note)
        # 通知按接收方视角固化，事故复盘时可直接重放“该方看到了什么”
        self._append(
            "system",
            "notification.dispatched",
            {
                "notification": entity_to_dict(note),
                "viewer": {
                    "actor_id": actor.actor_id,
                    "role": actor.role,
                    "owner_id": actor.owner_id,
                    "clearance": int(actor.clearance),
                },
            },
        )
        return note

    # ----------------------------------------------------------- 管段快照

    def submit_pipe_snapshot(
        self,
        actor_id: str,
        *,
        pipe_code: str,
        utility_type: UtilityType,
        classification: int,
        risk_level: RiskLevel,
        geometry: list[tuple[float, float]],
        diameter_mm: int,
        pressure: str,
        effective_from: str,
        effective_to: str | None = None,
    ) -> PipeSnapshot:
        actor = self._require_role(actor_id, Role.OWNER)
        assert actor.owner_id is not None
        version = 1 + sum(
            1 for s in self._snapshots.values() if s.pipe_code == pipe_code and s.owner_id == actor.owner_id
        )
        previous = self._latest_snapshot_of(pipe_code, actor.owner_id)
        snapshot = PipeSnapshot(
            snapshot_id=self._next_id("snap"),
            pipe_code=pipe_code,
            owner_id=actor.owner_id,
            utility_type=utility_type,
            classification=Classification(int(classification)),
            risk_level=risk_level,
            geometry=tuple(geometry),
            diameter_mm=diameter_mm,
            pressure=pressure,
            effective_from=effective_from,
            effective_to=effective_to,
            submitted_by=actor_id,
            submitted_at=format_ts(self._now()),
            version=version,
            supersedes=previous.snapshot_id if previous else None,
        )
        self._snapshots[snapshot.snapshot_id] = snapshot
        self._append(actor_id, "snapshot.submitted", entity_to_dict(snapshot))
        # 新快照发布：触发所有仍在占位中的相关申请重新评估
        affected = [
            app
            for app in self._applications.values()
            if app.status in PLACEHOLDER_STATUSES
            and self._snapshot_touches_application(snapshot, app)
        ]
        for app in affected:
            self._notify(
                self._require_actor(app.submitted_by),
                f"管段 {snapshot.pipe_code} 资料已更新，申请 {app.application_id} 将重新评估",
                "snapshot_update",
                "snapshot",
                snapshot.snapshot_id,
            )
            self.evaluate(app.submitted_by, app.application_id, cause="snapshot_update")
        return snapshot

    def _latest_snapshot_of(self, pipe_code: str, owner_id: str) -> PipeSnapshot | None:
        candidates = [
            s
            for s in self._snapshots.values()
            if s.pipe_code == pipe_code and s.owner_id == owner_id
        ]
        return max(candidates, key=lambda s: s.version, default=None)

    def _visible_snapshots(self, as_of: str, viewer: Actor | None = None) -> list[PipeSnapshot]:
        """某时刻“可见且生效”的快照：effective_from <= as_of，且未被截止。"""
        moment = parse_ts(as_of)
        result: list[PipeSnapshot] = []
        latest: dict[tuple[str, str], PipeSnapshot] = {}
        for snap in self._snapshots.values():
            if parse_ts(snap.submitted_at) > moment:
                continue
            if parse_ts(snap.effective_from) > moment:
                continue
            if snap.effective_to is not None and parse_ts(snap.effective_to) <= moment:
                continue
            key = (snap.owner_id, snap.pipe_code)
            incumbent = latest.get(key)
            if incumbent is None or snap.version > incumbent.version:
                latest[key] = snap
        result = list(latest.values())
        if viewer is not None:
            result = [s for s in result if self._actor_may_know_snapshot_exists(viewer, s)]
        return result

    def _actor_may_know_snapshot_exists(self, viewer: Actor, snapshot: PipeSnapshot) -> bool:
        # 协调方/审计方可见全部；权属单位可见自己的；工程方只能知道“命中其范围”的存在
        if viewer.role in (Role.COORDINATOR, Role.AUDITOR):
            return True
        if viewer.role == Role.OWNER:
            return viewer.owner_id == snapshot.owner_id
        return True  # 存在性由评估结果控制，提交阶段不直接列出快照

    def _snapshot_touches_application(self, snapshot: PipeSnapshot, app: OccupancyApplication) -> bool:
        app_start = parse_ts(app.window_start)
        app_end = parse_ts(app.window_end)
        if not windows_overlap(
            app_start,
            app_end,
            parse_ts(snapshot.effective_from),
            parse_ts(snapshot.effective_to) if snapshot.effective_to else parse_ts("9999-12-31T00:00:00Z"),
        ):
            return False
        dist = geometry.linestring_polygon_distance(snapshot.geometry, app.work_area)
        return dist <= max(app.impact_radius_m, 5.0)

    # ----------------------------------------------------------- 占用申请

    def apply_for_occupancy(
        self,
        actor_id: str,
        *,
        project_code: str,
        work_area: list[tuple[float, float]],
        road_closure: list[tuple[float, float]] | None = None,
        method: WorkMethod,
        window_start: str,
        window_end: str,
        impact_radius_m: float,
        priority: Priority = Priority.NORMAL,
    ) -> OccupancyApplication:
        actor = self._require_role(actor_id, Role.CONTRACTOR)
        application = OccupancyApplication(
            application_id=self._next_id("app"),
            project_code=project_code,
            contractor_id=actor.actor_id,
            work_area=tuple(work_area),
            road_closure=tuple(road_closure or work_area),
            method=method,
            window_start=window_start,
            window_end=window_end,
            impact_radius_m=impact_radius_m,
            submitted_by=actor_id,
            submitted_at=format_ts(self._now()),
            priority=priority,
        )
        self._applications[application.application_id] = application
        self._append(actor_id, "application.submitted", entity_to_dict(application))
        self.evaluate(actor_id, application.application_id, cause="initial")
        return application

    def _holder_applications(self) -> list[OccupancyApplication]:
        return [a for a in self._applications.values() if a.status in PLACEHOLDER_STATUSES]

    def _release_placeholder_and_reevaluate(self) -> None:
        """占位释放后，按提交顺序让被阻塞的申请重新竞争（仍冲突的保持阻塞）。"""
        waiting = sorted(
            (a for a in self._applications.values() if a.status == AppStatus.OCCUPANCY_BLOCKED),
            key=lambda a: a.submitted_at,
        )
        for app in waiting:
            if self._applications[app.application_id].status != AppStatus.OCCUPANCY_BLOCKED:
                continue
            if not self._has_placeholder_conflict(self._applications[app.application_id]):
                self.evaluate("system", app.application_id, cause="occupancy_released")

    def _has_placeholder_conflict(self, candidate: OccupancyApplication) -> list[OccupancyConflict]:
        conflicts: list[OccupancyConflict] = []
        c_start, c_end = parse_ts(candidate.window_start), parse_ts(candidate.window_end)
        for other in self._holder_applications():
            if other.application_id == candidate.application_id:
                continue
            o_start, o_end = parse_ts(other.window_start), parse_ts(other.window_end)
            time_overlap = windows_overlap(c_start, c_end, o_start, o_end)
            if not time_overlap:
                continue
            distance = geometry.polygons_distance(candidate.work_area, other.work_area)
            combined_buffer = candidate.impact_radius_m + other.impact_radius_m
            road_overlap = False
            try:
                road_overlap = geometry.polygons_distance(candidate.road_closure, other.road_closure) == 0.0
            except ValueError:
                road_overlap = False
            if road_overlap or distance <= combined_buffer:
                conflicts.append(
                    OccupancyConflict(
                        other_application_id=other.application_id,
                        other_project_code=other.project_code,
                        other_contractor_id=other.contractor_id,
                        road_closures_overlap=road_overlap,
                        buffer_distance_m=round(distance, 2),
                        combined_buffer_m=combined_buffer,
                        time_overlap=True,
                        other_priority=other.priority,
                    )
                )
        return conflicts

    # ----------------------------------------------------------- 评估

    def evaluate(
        self,
        actor_id: str,
        application_id: str,
        *,
        cause: str = "initial",
    ) -> ConflictAssessment:
        actor = self._require_actor(actor_id)
        app = self._applications.get(application_id)
        if app is None:
            raise ServiceError(f"未知申请: {application_id}")
        if actor.role == Role.CONTRACTOR and actor.actor_id != app.submitted_by:
            raise AuthzError("只能评估自己提交的申请")
        now = self._now()
        as_of = format_ts(now)

        # 已签发的申请因资料更新/恢复而重新评估时，原许可证立即失效，必须重新走签发
        if cause in ("snapshot_update", "resume") and app.status == AppStatus.PERMIT_ISSUED:
            self._invalidate_permit(application_id, "system", f"重新评估触发：{cause}")

        # 1) 唯一合法占位：与现有占位冲突则本次申请不得占位
        occupancy_conflicts = self._has_placeholder_conflict(app)
        holds_placeholder = not occupancy_conflicts

        # 2) 管段冲突：只引用评估时刻可见的快照版本（历史评估永不改写）
        snapshots = self._visible_snapshots(as_of)
        app_start, app_end = parse_ts(app.window_start), parse_ts(app.window_end)
        pipe_conflicts: list[PipeConflict] = []
        warnings: list[str] = []
        for snap in snapshots:
            s_start = parse_ts(snap.effective_from)
            s_end = parse_ts(snap.effective_to) if snap.effective_to else parse_ts("9999-12-31T00:00:00Z")
            if not windows_overlap(app_start, app_end, s_start, s_end):
                continue
            distance = geometry.linestring_polygon_distance(snap.geometry, app.work_area)
            # 工法放大影响：明挖按影响半径探测；非开挖只在更近距离命中
            reach = app.impact_radius_m if app.method == WorkMethod.OPEN_CUT else max(app.impact_radius_m * 0.5, 1.0)
            if distance > reach:
                continue
            requirement = (
                ReviewRequirement.COSIGN
                if snap.risk_level == RiskLevel.HIGH
                else ReviewRequirement.CONFIRM
            )
            pipe_conflicts.append(
                PipeConflict(
                    snapshot_id=snap.snapshot_id,
                    pipe_code=snap.pipe_code,
                    owner_id=snap.owner_id,
                    utility_type=snap.utility_type,
                    classification=snap.classification,
                    risk_level=snap.risk_level,
                    distance_m=round(distance, 2),
                    requirement=requirement,
                )
            )

        # 3) 历史意见沿用：同一快照版本在旧评估中已有结论的，显式 carried
        prior_latest_id = self._latest_assessment.get(application_id)
        carried_snapshots: dict[str, ConfirmationRecord] = {}
        if prior_latest_id is not None and cause in ("revision", "snapshot_update", "resume"):
            prior = self._assessments[prior_latest_id]
            for rec in self._confirmations_for(application_id, prior.revision):
                if rec.state == ConfirmationState.APPROVED and rec.decision == Decision.APPROVE:
                    carried_snapshots[rec.snapshot_id] = rec
            new_pipe_conflicts: list[PipeConflict] = []
            for pc in pipe_conflicts:
                old = carried_snapshots.get(pc.snapshot_id)
                if old is not None and old.decided_by is not None:
                    new_pipe_conflicts.append(
                        pc.with_state(
                            confirmation_state=ConfirmationState.CARRIED,
                            decided_by=old.decided_by,
                            decided_at=old.decided_at,
                            decision=old.decision,
                            comment=f"沿用 rev.{rec.revision} 对同一快照 {pc.snapshot_id} 的意见",
                        )
                    )
                else:
                    new_pipe_conflicts.append(pc)
            pipe_conflicts = new_pipe_conflicts

        # 4) 警示（已沿用历史意见的管段不再重复要求会签）
        pending_pipe_conflicts = [
            c for c in pipe_conflicts if c.confirmation_state != ConfirmationState.CARRIED
        ]
        if occupancy_conflicts:
            warnings.append("存在相邻工程的封路或安全缓冲区时空冲突，本申请不持有合法占位")
        high = [c for c in pending_pipe_conflicts if c.requirement == ReviewRequirement.COSIGN]
        if high:
            warnings.append(
                f"命中 {len(high)} 个高风险管段，必须完成风险会签；逾期未回复不得视为安全"
            )
        carried_count = len(pipe_conflicts) - len(pending_pipe_conflicts)
        if carried_count:
            warnings.append(f"{carried_count} 个管段沿用同一快照版本的历史意见")
        if not pipe_conflicts and holds_placeholder:
            warnings.append("当前可见资料范围内未命中管段")

        deadline = now + RESPONSE_WINDOW[app.priority]
        assessment = ConflictAssessment(
            assessment_id=self._next_id("asm"),
            application_id=application_id,
            revision=app.revision,
            created_at=as_of,
            visible_as_of=as_of,
            review_deadline=format_ts(deadline),
            pipe_conflicts=tuple(pipe_conflicts),
            occupancy_conflicts=tuple(occupancy_conflicts),
            warnings=tuple(warnings),
            supersedes_assessment=prior_latest_id,
            cause=cause,
        )
        self._assessments[assessment.assessment_id] = assessment
        self._latest_assessment[application_id] = assessment.assessment_id

        # 5) 申请状态流转 + 唯一占位
        new_status: AppStatus
        if not holds_placeholder:
            new_status = AppStatus.OCCUPANCY_BLOCKED
            status_reason = "存在合法占位冲突"
        elif pipe_conflicts:
            new_status = AppStatus.IN_REVIEW
            status_reason = None
        else:
            # 无管段命中：审核直接收口，协调员可立即签发
            new_status = AppStatus.REVIEW_CLOSED
            status_reason = "当前可见资料无管段冲突"
        self._update_application_status(app, new_status, status_reason)

        self._append(
            actor_id,
            "assessment.created",
            {
                "assessment": entity_to_dict(assessment),
                "holds_placeholder": holds_placeholder,
                "snapshot_versions": [c.snapshot_id for c in pipe_conflicts],
            },
        )

        # 6) 通知各权属单位（按其权限渲染；敏感坐标不进入外发通知）
        if holds_placeholder:
            self._dispatch_review_requests(actor, assessment, pipe_conflicts)
        # 通知工程方冲突结果（工程方视角，敏感坐标自动抹除）
        contractor = self._require_actor(app.submitted_by)
        self._notify(
            contractor,
            f"申请 {application_id}（rev.{app.revision}）冲突评估完成",
            self._render_assessment_text(contractor, assessment),
            "assessment",
            assessment.assessment_id,
        )
        return assessment

    def _update_application_status(
        self, app: OccupancyApplication, status: AppStatus, reason: str | None
    ) -> None:
        old_status = app.status
        updated = replace(app, status=status, status_reason=reason)
        self._applications[app.application_id] = updated
        self._append(
            app.submitted_by,
            "application.status_changed",
            {
                "application_id": app.application_id,
                "revision": app.revision,
                "from": old_status.value,
                "to": status.value,
                "reason": reason,
            },
        )
        if (
            self._release_hook_suppressed == 0
            and old_status in PLACEHOLDER_STATUSES
            and status not in PLACEHOLDER_STATUSES
        ):
            self._release_placeholder_and_reevaluate()

    def _dispatch_review_requests(
        self,
        trigger_actor: Actor,
        assessment: ConflictAssessment,
        pipe_conflicts: list[PipeConflict],
    ) -> None:
        by_owner: dict[str, list[PipeConflict]] = {}
        for pc in pipe_conflicts:
            if pc.confirmation_state == ConfirmationState.CARRIED:
                continue
            by_owner.setdefault(pc.owner_id, []).append(pc)
        for owner_id, items in by_owner.items():
            recipients = [
                a
                for a in self._actors.values()
                if a.role == Role.OWNER and a.owner_id == owner_id
            ]
            for recipient in recipients:
                high = [c for c in items if c.requirement == ReviewRequirement.COSIGN]
                kind = "风险会签" if high else "权属确认"
                self._notify(
                    recipient,
                    f"{kind}请求：{len(items)} 个管段与申请 {assessment.application_id} 冲突",
                    self._render_owner_review_text(recipient, assessment, items),
                    "assessment",
                    assessment.assessment_id,
                    sensitive=any(c.classification.value >= 3 for c in items),
                )

    def _render_assessment_text(self, viewer: Actor, assessment: ConflictAssessment) -> str:
        view = assessment_view(viewer, assessment)
        lines = list(view["warnings"])
        for pc in view["pipe_conflicts"]:
            lines.append(
                f"- {pc['utility_type']} 管段 {pc['pipe_code']}：风险 {pc['risk_level']}，"
                f"距离约 {pc['distance_m']}m，要求 {pc['requirement']}，状态 {pc['confirmation_state']}"
            )
        for oc in view["occupancy_conflicts"]:
            lines.append(
                f"- 相邻工程 {oc['other_application_id']}（{oc['other_project_code']}）："
                f"封路重叠={oc['road_closures_overlap']}，缓冲距离 {oc['buffer_distance_m']}m"
            )
        return "\n".join(lines)

    def _render_owner_review_text(
        self, viewer: Actor, assessment: ConflictAssessment, items: list[PipeConflict]
    ) -> str:
        lines = [
            f"截止回复时间：{assessment.review_deadline}",
            "高风险管段逾期未回复将阻断许可证签发，不会被默认安全。",
        ]
        for pc in items:
            lines.append(
                f"- {pc.pipe_code}（{pc.utility_type.value}）风险={pc.risk_level.value} "
                f"距离={pc.distance_m}m 要求={pc.requirement.value} 快照={pc.snapshot_id}"
            )
        return "\n".join(lines)

    # ----------------------------------------------------------- 会签回复

    def _confirmations_for(self, application_id: str, revision: int) -> list[ConfirmationRecord]:
        return [
            r
            for r in self._confirmations
            if r.application_id == application_id and r.revision == revision
        ]

    def respond_pipe(
        self,
        actor_id: str,
        application_id: str,
        snapshot_id: str,
        decision: Decision,
        comment: str | None = None,
    ) -> ConfirmationRecord:
        actor = self._require_role(actor_id, Role.OWNER)
        app = self._applications.get(application_id)
        if app is None:
            raise ServiceError("未知申请")
        assessment_id = self._latest_assessment.get(application_id)
        if assessment_id is None:
            raise ServiceError("申请尚未评估")
        assessment = self._assessments[assessment_id]
        target = next(
            (c for c in assessment.pipe_conflicts if c.snapshot_id == snapshot_id), None
        )
        if target is None:
            raise ServiceError("该快照不在当前评估范围内（可能已发布新版本，请查看新评估）")
        if actor.owner_id != target.owner_id:
            raise AuthzError("只能回复本权属单位的管段")
        if app.status not in (AppStatus.IN_REVIEW, AppStatus.REVIEW_CLOSED):
            raise ServiceError(f"申请当前状态 {app.status.value} 不接受回复")

        state = (
            ConfirmationState.APPROVED if decision == Decision.APPROVE else ConfirmationState.REJECTED
        )
        record = ConfirmationRecord(
            application_id=application_id,
            revision=app.revision,
            snapshot_id=snapshot_id,
            state=state,
            decided_by=actor_id,
            decided_at=format_ts(self._now()),
            decision=decision,
            comment=comment,
        )
        self._confirmations.append(record)
        self._append(
            actor_id,
            "pipe.responded",
            {
                "record": entity_to_dict(record),
                "assessment_id": assessment_id,
                "snapshot_fingerprint": fingerprint_of(entity_to_dict(self._snapshots[snapshot_id])),
            },
        )
        if decision == Decision.REJECT:
            self._update_application_status(app, AppStatus.DENIED, f"权属单位 {actor.owner_id} 驳回")
            self._notify(
                self._require_actor(app.submitted_by),
                f"申请 {application_id} 被权属单位驳回",
                comment or "管段冲突未达成一致",
                "application",
                application_id,
            )
        else:
            self._notify(
                self._require_actor(app.submitted_by),
                f"管段 {target.pipe_code} 已完成权属意见",
                comment or "同意",
                "application",
                application_id,
            )
            # 全部命中管段均已解决（含沿用与逾期留痕）则审核自动收口
            if not self._pending_conflicts(assessment):
                self._update_application_status(app, AppStatus.REVIEW_CLOSED, "所有管段意见已齐")
        return record

    def _records_for(self, application_id: str, revision: int) -> dict[str, ConfirmationRecord]:
        return {
            r.snapshot_id: r
            for r in self._confirmations
            if r.application_id == application_id and r.revision == revision
        }

    def _pending_conflicts(self, assessment: ConflictAssessment) -> list[PipeConflict]:
        records = self._records_for(assessment.application_id, assessment.revision)
        pending: list[PipeConflict] = []
        for pc in assessment.pipe_conflicts:
            if pc.confirmation_state == ConfirmationState.CARRIED:
                continue
            rec = records.get(pc.snapshot_id)
            if rec is None:
                pending.append(pc)
            elif rec.state not in (
                ConfirmationState.APPROVED,
                ConfirmationState.NO_RESPONSE_LOW_RISK,
            ):
                pending.append(pc)
        return pending

    def expire_pending_responses(self, actor_id: str = "system") -> list[str]:
        """显式收口：低风险逾期未复留痕为 no_response_low；高风险保持 pending 并阻断签发。

        返回仍被高风险未决项阻断的申请 id 列表。
        """
        now = self._now()
        blocked: list[str] = []
        for app in [a for a in self._applications.values() if a.status == AppStatus.IN_REVIEW]:
            assessment_id = self._latest_assessment[app.application_id]
            assessment = self._assessments[assessment_id]
            if parse_ts(assessment.review_deadline) > now:
                continue
            pending = self._pending_conflicts(assessment)
            for pc in pending:
                if pc.risk_level != RiskLevel.HIGH:
                    rec = ConfirmationRecord(
                        application_id=app.application_id,
                        revision=app.revision,
                        snapshot_id=pc.snapshot_id,
                        state=ConfirmationState.NO_RESPONSE_LOW_RISK,
                        decided_by=None,
                        decided_at=format_ts(now),
                        decision=None,
                        comment="低风险管段逾期未回复，记录留痕但不视为权属背书",
                    )
                    self._confirmations.append(rec)
                    self._append(
                        actor_id,
                        "pipe.no_response_timeout",
                        {"record": entity_to_dict(rec), "assessment_id": assessment_id},
                    )
            high_pending = [c for c in self._pending_conflicts(assessment) if c.risk_level == RiskLevel.HIGH]
            if high_pending:
                blocked.append(app.application_id)
                self._append(
                    actor_id,
                    "review.blocked_high_risk_pending",
                    {
                        "application_id": app.application_id,
                        "assessment_id": assessment_id,
                        "pending_snapshots": [c.snapshot_id for c in high_pending],
                    },
                )
            else:
                self._update_application_status(app, AppStatus.REVIEW_CLOSED, "回复期截止，无高风险未决项")
        return blocked

    # ----------------------------------------------------------- 许可证

    def issue_permit(
        self,
        actor_id: str,
        application_id: str,
        emergency_acknowledgement: str | None = None,
    ) -> Permit:
        actor = self._require_role(actor_id, Role.COORDINATOR)
        app = self._applications.get(application_id)
        if app is None:
            raise ServiceError("未知申请")
        assessment = self._assessments[self._latest_assessment[application_id]]
        if app.status not in (AppStatus.IN_REVIEW, AppStatus.REVIEW_CLOSED):
            raise ServiceError(f"申请状态 {app.status.value} 不可签发许可证")
        if assessment.occupancy_conflicts:
            raise ServiceError("仍存在相邻工程占位冲突，不可签发")

        resolved = self._records_for(assessment.application_id, assessment.revision)
        rejected: list[PipeConflict] = []
        for pc in assessment.pipe_conflicts:
            if pc.confirmation_state == ConfirmationState.CARRIED:
                continue
            rec = resolved.get(pc.snapshot_id)
            if rec is not None and rec.state == ConfirmationState.REJECTED:
                rejected.append(pc)
        pending = self._pending_conflicts(assessment)
        high_unresolved = [c for c in pending if c.risk_level == RiskLevel.HIGH]
        if high_unresolved:
            # 红线：高风险管段未回复永远不默认安全，紧急抢修也不例外，
            # 任何“口头/书面担责”都不能替代权属会签。
            raise ServiceError(
                "高风险管段未完成会签，不得签发许可证："
                + ",".join(c.snapshot_id for c in high_unresolved)
            )
        if rejected:
            raise ServiceError("存在被权属单位驳回的管段冲突")
        # 剩余未决项只可能是低风险但尚未到期：普通流程要求先收口；
        # 紧急抢修允许协调员出具书面风险确认后签发，确认文本固化到许可证与审计链。
        low_unresolved = [c for c in pending if c.risk_level != RiskLevel.HIGH]
        if low_unresolved and not (app.priority == Priority.EMERGENCY and emergency_acknowledgement):
            raise ServiceError("仍有低风险管段未回复，请先执行回复期收口后再签发")

        override = None
        if pending:
            override = emergency_acknowledgement
        permit = Permit(
            permit_code=self._next_id("permit"),
            application_id=application_id,
            revision=app.revision,
            issued_by=actor_id,
            issued_at=format_ts(self._now()),
            window_start=app.window_start,
            window_end=app.window_end,
            emergency_override=override,
        )
        self._permits[permit.permit_code] = permit
        self._update_application_status(app, AppStatus.PERMIT_ISSUED, f"许可证 {permit.permit_code}")
        self._append(
            actor_id,
            "permit.issued",
            {
                "permit": entity_to_dict(permit),
                "assessment_id": assessment.assessment_id,
                "resolved_snapshots": list(resolved),
            },
        )
        self._notify(
            self._require_actor(app.submitted_by),
            f"许可证已签发：{permit.permit_code}",
            f"有效期 {permit.window_start} 至 {permit.window_end}",
            "permit",
            permit.permit_code,
        )
        return permit

    def suspend_permit(
        self, actor_id: str, application_id: str, reason: str
    ) -> OccupancyApplication:
        """许可证暂停：占位释放，其他申请可竞争；恢复时重新评估。"""
        actor = self._require_role(actor_id, Role.COORDINATOR, Role.CONTRACTOR)
        app = self._applications.get(application_id)
        if app is None or app.status != AppStatus.PERMIT_ISSUED:
            raise ServiceError("仅已签发的许可证可暂停")
        if actor.role == Role.CONTRACTOR and actor.actor_id != app.submitted_by:
            raise AuthzError("只能暂停自己的许可证")
        self._invalidate_permit(application_id, actor_id, reason)
        self._update_application_status(app, AppStatus.SUSPENDED, reason)
        self._notify(
            self._require_actor(app.submitted_by),
            f"申请 {application_id} 的许可证已暂停：{reason}",
            "暂停期间时空占位释放；恢复时将基于最新资料重新评估",
            "application",
            application_id,
        )
        return self._applications[application_id]

    def resume_after_suspension(self, actor_id: str, application_id: str) -> ConflictAssessment:
        actor = self._require_role(actor_id, Role.COORDINATOR)
        app = self._applications.get(application_id)
        if app is None or app.status != AppStatus.SUSPENDED:
            raise ServiceError("仅暂停状态的申请可恢复")
        return self.evaluate(actor_id, application_id, cause="resume")

    def _invalidate_permit(self, application_id: str, actor_id: str, reason: str) -> None:
        for code, permit in list(self._permits.items()):
            if permit.application_id == application_id and permit.valid:
                updated = replace(permit, valid=False)
                self._permits[code] = updated
                self._append(
                    actor_id,
                    "permit.invalidated",
                    {"permit_code": code, "reason": reason},
                )

    # --------------------------------------- 设计变更 / 延期 / 紧急抢修

    def revise_application(
        self,
        actor_id: str,
        application_id: str,
        *,
        work_area: list[tuple[float, float]] | None = None,
        road_closure: list[tuple[float, float]] | None = None,
        method: WorkMethod | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        impact_radius_m: float | None = None,
        change_note: str = "",
    ) -> ConflictAssessment:
        """设计变更或工程延期：新版本 + 旧版本 SUPERSEDED + 重新评估。

        注意：原占位在新版本重新评估成功后才延续；若新版本与他人冲突则进入 blocked。
        """
        actor = self._require_role(actor_id, Role.CONTRACTOR, Role.COORDINATOR)
        old = self._applications.get(application_id)
        if old is None:
            raise ServiceError("未知申请")
        if actor.role == Role.CONTRACTOR and actor.actor_id != old.submitted_by:
            raise AuthzError("只能变更自己的申请")
        if old.status not in (AppStatus.IN_REVIEW, AppStatus.REVIEW_CLOSED, AppStatus.PERMIT_ISSUED, AppStatus.SUSPENDED):
            raise ServiceError(f"状态 {old.status.value} 不允许变更")
        if old.status == AppStatus.PERMIT_ISSUED:
            self._invalidate_permit(application_id, actor_id, f"设计变更/延期：{change_note}")
        revised = replace(
            old,
            work_area=tuple(work_area) if work_area is not None else old.work_area,
            road_closure=tuple(road_closure) if road_closure is not None else old.road_closure,
            method=method or old.method,
            window_start=window_start or old.window_start,
            window_end=window_end or old.window_end,
            impact_radius_m=impact_radius_m if impact_radius_m is not None else old.impact_radius_m,
            revision=old.revision + 1,
            supersedes=old.application_id,
            status=AppStatus.SUBMITTED,
            status_reason=None,
            change_note=change_note,
            submitted_at=format_ts(self._now()),
        )
        self._applications[application_id] = revised
        self._append(
            actor_id,
            "application.revised",
            {
                "application_id": application_id,
                "from_revision": old.revision,
                "to_revision": revised.revision,
                "change_note": change_note,
                "window": [revised.window_start, revised.window_end],
            },
        )
        # 旧版本状态在审计中体现为被取代（保留其评估链）
        return self.evaluate(actor_id, application_id, cause="revision")

    def emergency_repair(
        self,
        actor_id: str,
        *,
        project_code: str,
        work_area: list[tuple[float, float]],
        window_start: str,
        window_end: str,
        impact_radius_m: float,
        description: str,
    ) -> OccupancyApplication:
        """紧急抢修：优先级抢占——暂停时间窗重叠的已签发许可证，释放占位后再评估。"""
        actor = self._require_role(actor_id, Role.CONTRACTOR, Role.COORDINATOR)
        e_start, e_end = parse_ts(window_start), parse_ts(window_end)
        victims: list[OccupancyApplication] = []
        self._release_hook_suppressed += 1
        try:
            for other in self._holder_applications():
                o_start, o_end = parse_ts(other.window_start), parse_ts(other.window_end)
                if windows_overlap(e_start, e_end, o_start, o_end) and self._spatial_overlap(
                    OccupancyApplication(
                        application_id="__probe__",
                        project_code=project_code,
                        contractor_id=actor_id,
                        work_area=tuple(work_area),
                        road_closure=tuple(work_area),
                        method=WorkMethod.EMERGENCY_REPAIR,
                        window_start=window_start,
                        window_end=window_end,
                        impact_radius_m=impact_radius_m,
                        submitted_by=actor_id,
                        submitted_at=format_ts(self._now()),
                    ),
                    other,
                ):
                    victims.append(other)
            for victim in victims:
                self._invalidate_permit(victim.application_id, actor_id, f"紧急抢修抢占：{description}")
                self._update_application_status(victim, AppStatus.SUSPENDED, f"紧急抢修 {project_code} 抢占")
                self._notify(
                    self._require_actor(victim.submitted_by),
                    f"许可证因紧急抢修 {project_code} 暂停",
                    description,
                    "application",
                    victim.application_id,
                )
        finally:
            self._release_hook_suppressed -= 1
        self._append(
            actor_id,
            "emergency.declared",
            {
                "project_code": project_code,
                "description": description,
                "preempted": [v.application_id for v in victims],
            },
        )
        # 抢修方需要是 CONTRACTOR 角色才能走申请
        if actor.role != Role.CONTRACTOR:
            # 被抢占者已暂停、占位已释放；等待方此时可竞争
            self._release_placeholder_and_reevaluate()
            raise ServiceError("紧急抢修仍需由具备 contractor 角色的参与方提交申请")
        emergency_app = self.apply_for_occupancy(
            actor_id,
            project_code=project_code,
            work_area=work_area,
            road_closure=work_area,
            method=WorkMethod.EMERGENCY_REPAIR,
            window_start=window_start,
            window_end=window_end,
            impact_radius_m=impact_radius_m,
            priority=Priority.EMERGENCY,
        )
        # 抢修申请占位后，其余等待方按顺序竞争剩余空间
        self._release_placeholder_and_reevaluate()
        return emergency_app

    @staticmethod
    def _spatial_overlap(candidate: OccupancyApplication, other: OccupancyApplication) -> bool:
        distance = geometry.polygons_distance(candidate.work_area, other.work_area)
        return distance <= candidate.impact_radius_m + other.impact_radius_m

    # ----------------------------------------------------------- 查询/视图

    def get_application(self, application_id: str) -> OccupancyApplication:
        app = self._applications.get(application_id)
        if app is None:
            raise ServiceError("未知申请")
        return app

    def latest_assessment(self, application_id: str) -> ConflictAssessment:
        return self._assessments[self._latest_assessment[application_id]]

    def assessment_history(self, application_id: str) -> list[ConflictAssessment]:
        return [
            a
            for a in self._assessments.values()
            if a.application_id == application_id
        ]

    def view_assessment(self, actor_id: str, assessment_id: str) -> dict[str, Any]:
        viewer = self._require_actor(actor_id)
        return assessment_view(viewer, self._assessments[assessment_id])

    def view_snapshot(self, actor_id: str, snapshot_id: str) -> dict[str, Any]:
        viewer = self._require_actor(actor_id)
        return snapshot_view(viewer, self._snapshots[snapshot_id])

    def notifications_for(self, actor_id: str) -> list[dict[str, Any]]:
        self._require_actor(actor_id)
        return [
            entity_to_dict(n)
            for n in self._notifications
            if n.recipient_actor_id == actor_id
        ]

    def permits_for(self, application_id: str) -> list[Permit]:
        return [p for p in self._permits.values() if p.application_id == application_id]

    def complete_project(self, actor_id: str, application_id: str) -> None:
        actor = self._require_role(actor_id, Role.COORDINATOR, Role.CONTRACTOR)
        app = self._applications.get(application_id)
        if app is None:
            raise ServiceError("未知申请")
        if actor.role == Role.CONTRACTOR and actor.actor_id != app.submitted_by:
            raise AuthzError("只能完结自己的申请")
        self._update_application_status(app, AppStatus.COMPLETED, "工程完成，占位释放")
