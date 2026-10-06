"""地下管网施工冲突协同的核心服务。

职责：
- 接收权属单位带生效期与保密等级的管段快照；
- 接收工程方占用申请并按"当时可见资料"做空间+时间冲突筛查；
- 组织权属确认、风险会签与许可证签发（高风险未确认不得放行）；
- 设计变更、紧急抢修、许可证暂停、工程延期时重新评估后续占用；
- 保证并发申请只有一个合法占位；
- 按角色对敏感坐标做最小披露；
- 将全部决定写入哈希链审计日志。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Callable, Optional, Sequence

from .geometry import (
    Point,
    depth_ranges_overlap,
    fuzz_depth,
    fuzz_path,
    polyline_distance,
    windows_overlap,
)
from .models import (
    Actor,
    ApplicationState,
    Assessment,
    AssessmentTrigger,
    ConflictHit,
    Confidentiality,
    Notification,
    OccupancyApplication,
    OwnerConfirmation,
    Permit,
    PermitState,
    RiskEndorsement,
    RiskLevel,
    Role,
    SegmentSnapshot,
    UtilityType,
    METHOD_MULTIPLIER,
    RISK_MULTIPLIER,
    SAFETY_BUFFER,
    VERTICAL_CLEARANCE,
    utcnow,
)
from .store import Store

# 重新评估时只考虑时间窗相交的未结申请
_REEVAL_STATES = {
    ApplicationState.SUBMITTED,
    ApplicationState.CONFIRMING,
    ApplicationState.CO_SIGNING,
    ApplicationState.APPROVED,
    ApplicationState.SUSPENDED,
}
_TERMINAL_STATES = {
    ApplicationState.REJECTED,
    ApplicationState.CANCELLED,
    ApplicationState.COMPLETED,
}


class DomainError(Exception):
    """领域规则被违反。"""


class PermissionDenied(DomainError):
    """角色无权执行该操作。"""


class NotFound(DomainError):
    """目标对象不存在。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


class OccupancyConflictError(DomainError):
    """占位冲突：同一时空范围已存在合法占用。"""


def _require(condition: bool, error: DomainError) -> None:
    if not condition:
        raise error


class CoordinationService:
    """协同服务入口；clock 可注入以便测试与审计重放。"""

    def __init__(self, store: Optional[Store] = None, clock: Callable[[], datetime] = utcnow) -> None:
        self.store = store or Store()
        self.clock = clock

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _register(self, actor: Actor) -> None:
        existing = self.store.participants.get(actor.actor_id)
        if existing is None:
            self.store.participants[actor.actor_id] = actor
        elif existing != actor:
            raise PermissionDenied(f"参与方 {actor.actor_id} 的角色/归属与注册信息不一致")

    def _notify(self, recipient: str, kind: str, subject_id: str, message: str) -> None:
        self.store.notifications.append(
            Notification(
                notification_id=self.store.next_id("ntf"),
                recipient=recipient,
                kind=kind,
                subject_id=subject_id,
                message=message,
                created_at=self.clock(),
            )
        )

    def _event(self, kind: str, actor: Actor, subject_id: str, payload: Optional[dict] = None) -> None:
        self.store.append_event(kind, actor.actor_id, actor.role, subject_id, payload, at=self.clock())

    def _current_application(self, application_id: str) -> OccupancyApplication:
        versions = self.store.applications.get(application_id)
        _require(bool(versions), NotFound(f"申请 {application_id} 不存在"))
        return versions[-1]

    def _current_permit(self, permit_id: str) -> Permit:
        versions = self.store.permits.get(permit_id)
        _require(bool(versions), NotFound(f"许可证 {permit_id} 不存在"))
        return versions[-1]

    def _latest_assessment(self, application_id: str) -> Assessment:
        candidates = [a for a in self.store.assessments.values() if a.application_id == application_id]
        _require(bool(candidates), NotFound(f"申请 {application_id} 尚无评估"))
        # 同一时刻可能产生多份评估（注入时钟场景），以单调递增的评估号决胜
        return max(candidates, key=lambda a: (a.created_at, a.assessment_id))

    def _visible_snapshots(self, as_of: datetime) -> list[SegmentSnapshot]:
        """as_of 时刻各管段可见的最新快照（按提交时间，而非生效期）。"""
        visible: list[SegmentSnapshot] = []
        for versions in self.store.segments.values():
            eligible = [s for s in versions if s.submitted_at <= as_of]
            if eligible:
                visible.append(max(eligible, key=lambda s: s.revision))
        return visible

    def _active_permits(self, exclude_application: Optional[str] = None) -> list[Permit]:
        active: list[Permit] = []
        for versions in self.store.permits.values():
            permit = versions[-1]
            if permit.state in (PermitState.ACTIVE, PermitState.PROVISIONAL):
                if exclude_application is None or permit.application_id != exclude_application:
                    active.append(permit)
        return active

    @staticmethod
    def _required_separation(app: OccupancyApplication, snap: SegmentSnapshot) -> float:
        method_factor = METHOD_MULTIPLIER.get(app.method, 1.0)
        buffer = SAFETY_BUFFER[snap.utility] * RISK_MULTIPLIER[snap.risk]
        return app.impact_radius + buffer * method_factor

    # ------------------------------------------------------------------
    # 管段快照
    # ------------------------------------------------------------------

    def submit_segment_snapshot(
        self,
        actor: Actor,
        *,
        segment_id: str,
        utility: UtilityType | str,
        confidentiality: Confidentiality | str,
        path: Sequence[Point],
        depth_top: float,
        depth_bottom: float,
        effective_from: datetime,
        risk: RiskLevel | str,
        owner_id: Optional[str] = None,
        effective_to: Optional[datetime] = None,
    ) -> SegmentSnapshot:
        """权属单位提交管段快照；同一管段形成递增的不可变版本链。"""
        self._register(actor)
        utility = UtilityType(utility)
        confidentiality = Confidentiality(confidentiality)
        risk = RiskLevel(risk)
        owner = owner_id or actor.owner_id
        _require(bool(owner), PermissionDenied("必须指定管段权属单位"))
        if actor.role is Role.UTILITY_OWNER:
            _require(actor.owner_id == owner, PermissionDenied("只能提交本单位的管段"))
        elif actor.role is not Role.COORDINATOR:
            raise PermissionDenied("只有权属单位或协调员可以提交管段快照")
        _require(len(path) >= 2, DomainError("管段路径至少需要两个坐标点"))
        _require(depth_bottom >= depth_top >= 0, DomainError("深度区间不合法"))
        if effective_to is not None:
            _require(effective_to >= effective_from, DomainError("生效期结束不得早于开始"))

        with self.store.lock:
            versions = self.store.segments.setdefault(segment_id, [])
            if versions:
                _require(
                    versions[-1].owner_id == owner,
                    PermissionDenied(f"管段 {segment_id} 的权属单位为 {versions[-1].owner_id}"),
                )
            snapshot = SegmentSnapshot(
                segment_id=segment_id,
                revision=len(versions) + 1,
                owner_id=owner,
                utility=utility,
                confidentiality=confidentiality,
                path=tuple((float(x), float(y)) for x, y in path),
                depth_top=float(depth_top),
                depth_bottom=float(depth_bottom),
                effective_from=effective_from,
                effective_to=effective_to,
                risk=risk,
                submitted_at=self.clock(),
            )
            versions.append(snapshot)
            self._event(
                "segment_snapshot_submitted",
                actor,
                segment_id,
                {"revision": snapshot.revision, "owner_id": owner, "utility": utility.value},
            )
        return snapshot

    # ------------------------------------------------------------------
    # 占用申请与冲突筛查
    # ------------------------------------------------------------------

    def apply_occupancy(
        self,
        actor: Actor,
        *,
        request_id: str,
        project_code: str,
        path: Sequence[Point],
        impact_radius: float,
        method: str,
        depth_top: float,
        depth_bottom: float,
        window_start: datetime,
        window_end: datetime,
        emergency: bool = False,
    ) -> tuple[OccupancyApplication, Assessment]:
        """工程方申请占用；request_id 保证重复提交幂等。"""
        self._register(actor)
        _require(
            actor.role in (Role.CONTRACTOR, Role.COORDINATOR, Role.UTILITY_OWNER),
            PermissionDenied("该角色不能申请占用"),
        )
        with self.store.lock:
            existing = self.store.request_index.get(request_id)
            if existing is not None:
                return self._current_application(existing), self._latest_assessment(existing)

            _require(len(path) >= 2, DomainError("施工范围至少需要两个坐标点"))
            _require(impact_radius > 0, DomainError("影响面半宽必须为正"))
            _require(depth_bottom >= depth_top >= 0, DomainError("深度区间不合法"))
            _require(window_end > window_start, DomainError("时间窗结束必须晚于开始"))

            application = OccupancyApplication(
                application_id=self.store.next_id("app"),
                revision=1,
                request_id=request_id,
                project_code=project_code,
                applicant_id=actor.actor_id,
                path=tuple((float(x), float(y)) for x, y in path),
                impact_radius=float(impact_radius),
                method=method,
                depth_top=float(depth_top),
                depth_bottom=float(depth_bottom),
                window_start=window_start,
                window_end=window_end,
                state=ApplicationState.SUBMITTED,
                emergency=emergency,
                submitted_at=self.clock(),
            )
            self.store.applications[application.application_id] = [application]
            self.store.request_index[request_id] = application.application_id
            self._event(
                "occupancy_applied",
                actor,
                application.application_id,
                {"project_code": project_code, "request_id": request_id, "emergency": emergency},
            )
            application, assessment = self._screen_and_route(
                application, AssessmentTrigger.EMERGENCY if emergency else AssessmentTrigger.INITIAL
            )
        return application, assessment

    def _screen(self, app: OccupancyApplication, trigger: AssessmentTrigger) -> Assessment:
        """按 as_of 时刻可见资料计算空间与时间冲突；评估完成后不可变。"""
        as_of = self.clock()
        hits: list[ConflictHit] = []
        refs: list[tuple[str, int]] = []
        for snap in self._visible_snapshots(as_of):
            if not snap.covers_window(app.window_start, app.window_end):
                continue
            refs.append((snap.segment_id, snap.revision))
            distance = polyline_distance(app.path, snap.path)
            required = self._required_separation(app, snap)
            if distance >= required:
                continue
            if not depth_ranges_overlap(
                app.depth_top,
                app.depth_bottom,
                snap.depth_top - VERTICAL_CLEARANCE,
                snap.depth_bottom + VERTICAL_CLEARANCE,
            ):
                continue
            hits.append(
                ConflictHit(
                    kind="utility",
                    ref_id=snap.segment_id,
                    ref_revision=snap.revision,
                    owner_id=snap.owner_id,
                    utility=snap.utility,
                    risk=snap.risk,
                    min_distance=round(distance, 3),
                    required_separation=round(required, 3),
                )
            )
        for permit in self._active_permits(exclude_application=app.application_id):
            other = self._current_application(permit.application_id)
            if not windows_overlap(app.window_start, app.window_end, permit.window_start, permit.window_end):
                continue
            distance = polyline_distance(app.path, other.path)
            required = app.impact_radius + other.impact_radius
            if distance >= required:
                continue
            hits.append(
                ConflictHit(
                    kind="occupancy",
                    ref_id=permit.permit_id,
                    ref_revision=permit.revision,
                    owner_id=other.applicant_id,
                    utility=None,
                    risk=RiskLevel.MEDIUM,
                    min_distance=round(distance, 3),
                    required_separation=round(required, 3),
                )
            )
        assessment = Assessment(
            assessment_id=self.store.next_id("asm"),
            application_id=app.application_id,
            application_revision=app.revision,
            trigger=trigger,
            as_of=as_of,
            created_at=as_of,
            snapshot_refs=tuple(sorted(refs)),
            hits=tuple(hits),
        )
        self.store.assessments[assessment.assessment_id] = assessment
        return assessment

    def _route_after_screen(self, app: OccupancyApplication, assessment: Assessment) -> OccupancyApplication:
        if assessment.utility_hits:
            app = replace(app, state=ApplicationState.CONFIRMING)
            for hit in assessment.utility_hits:
                self._notify(
                    hit.owner_id,
                    "confirmation_requested",
                    app.application_id,
                    f"申请 {app.application_id} 与管段 {hit.ref_id}（风险 {hit.risk.value}）冲突，请权属确认",
                )
            self._notify(
                app.applicant_id,
                "conflicts_found",
                app.application_id,
                f"发现 {len(assessment.utility_hits)} 处管线冲突，等待权属确认",
            )
        else:
            app = replace(app, state=ApplicationState.CO_SIGNING)
            self._notify(
                "role:coordinator",
                "cosign_requested",
                app.application_id,
                f"申请 {app.application_id} 无管线冲突，请风险会签",
            )
        return app

    def _screen_and_route(
        self, app: OccupancyApplication, trigger: AssessmentTrigger
    ) -> tuple[OccupancyApplication, Assessment]:
        assessment = self._screen(app, trigger)
        app = self._route_after_screen(app, assessment)
        versions = self.store.applications[app.application_id]
        versions[-1] = app
        return app, assessment

    # ------------------------------------------------------------------
    # 权属确认与风险会签
    # ------------------------------------------------------------------

    def confirm_segment(
        self,
        actor: Actor,
        application_id: str,
        segment_id: str,
        *,
        accept: bool,
        comment: str = "",
    ) -> OwnerConfirmation:
        """权属单位确认冲突管段；拒绝将导致申请被驳回。"""
        self._register(actor)
        _require(actor.role is Role.UTILITY_OWNER, PermissionDenied("只有权属单位可以确认管段"))
        with self.store.lock:
            app = self._current_application(application_id)
            _require(
                app.state in (ApplicationState.CONFIRMING, ApplicationState.CO_SIGNING, ApplicationState.APPROVED),
                StateError(f"申请当前状态 {app.state.value} 不接受权属确认"),
            )
            assessment = self._latest_assessment(application_id)
            hit = next((h for h in assessment.utility_hits if h.ref_id == segment_id), None)
            _require(hit is not None, NotFound(f"评估 {assessment.assessment_id} 中不存在管段 {segment_id} 的冲突"))
            _require(
                hit.owner_id == actor.owner_id,
                PermissionDenied(f"管段 {segment_id} 属于 {hit.owner_id}，本单位无权确认"),
            )
            _require(
                not any(
                    c.assessment_id == assessment.assessment_id and c.segment_id == segment_id
                    for c in self.store.confirmations
                ),
                StateError("该管段在本轮评估中已确认"),
            )
            confirmation = OwnerConfirmation(
                application_id=application_id,
                assessment_id=assessment.assessment_id,
                segment_id=segment_id,
                owner_id=actor.owner_id or "",
                accepted=accept,
                comment=comment,
                decided_by=actor.actor_id,
                decided_at=self.clock(),
            )
            self.store.confirmations.append(confirmation)
            self._event(
                "segment_confirmed" if accept else "segment_rejected",
                actor,
                application_id,
                {"assessment_id": assessment.assessment_id, "segment_id": segment_id, "comment": comment},
            )
            if not accept:
                app = replace(app, state=ApplicationState.REJECTED)
                self.store.applications[application_id][-1] = app
                self._notify(app.applicant_id, "application_rejected", application_id,
                             f"权属单位 {actor.owner_id} 拒绝管段 {segment_id} 的占用：{comment}")
                self._event("application_rejected", actor, application_id, {"reason": "owner_rejected"})
            elif self._all_hits_confirmed(assessment) and app.state is ApplicationState.CONFIRMING:
                app = replace(app, state=ApplicationState.CO_SIGNING)
                self.store.applications[application_id][-1] = app
                self._notify("role:coordinator", "cosign_requested", application_id,
                             f"申请 {application_id} 权属确认齐全，请风险会签")
        return confirmation

    def _all_hits_confirmed(self, assessment: Assessment) -> bool:
        confirmed = {
            c.segment_id
            for c in self.store.confirmations
            if c.assessment_id == assessment.assessment_id and c.accepted
        }
        return all(h.ref_id in confirmed for h in assessment.utility_hits)

    def co_sign(
        self,
        actor: Actor,
        application_id: str,
        *,
        approve: bool,
        comment: str = "",
    ) -> RiskEndorsement:
        """协调员组织风险会签；以当前评估版本为准。"""
        self._register(actor)
        _require(actor.role is Role.COORDINATOR, PermissionDenied("只有协调员可以组织风险会签"))
        with self.store.lock:
            app = self._current_application(application_id)
            _require(
                app.state is ApplicationState.CO_SIGNING,
                StateError(
                    "权属确认未齐全，不能会签"
                    if app.state is ApplicationState.CONFIRMING
                    else f"申请当前状态 {app.state.value} 不能会签"
                ),
            )
            assessment = self._latest_assessment(application_id)
            _require(
                self._all_hits_confirmed(assessment),
                StateError("权属确认未齐全，不能会签"),
            )
            endorsement = RiskEndorsement(
                application_id=application_id,
                assessment_id=assessment.assessment_id,
                approved=approve,
                comment=comment,
                signed_by=actor.actor_id,
                signed_at=self.clock(),
            )
            self.store.endorsements.append(endorsement)
            self._event(
                "risk_cosigned",
                actor,
                application_id,
                {"assessment_id": assessment.assessment_id, "approved": approve, "comment": comment},
            )
            if not approve:
                app = replace(app, state=ApplicationState.REJECTED)
                self.store.applications[application_id][-1] = app
                self._notify(app.applicant_id, "application_rejected", application_id,
                             f"风险会签未通过：{comment}")
                self._event("application_rejected", actor, application_id, {"reason": "cosign_rejected"})
        return endorsement

    # ------------------------------------------------------------------
    # 许可证签发（唯一合法占位）
    # ------------------------------------------------------------------

    def _issuance_blockers(self, app: OccupancyApplication, assessment: Assessment) -> list[str]:
        blockers: list[str] = []
        confirmed = {
            c.segment_id
            for c in self.store.confirmations
            if c.assessment_id == assessment.assessment_id and c.accepted
        }
        for hit in assessment.utility_hits:
            if hit.ref_id not in confirmed:
                if hit.risk is RiskLevel.HIGH:
                    blockers.append(f"高风险管段 {hit.ref_id} 未获权属确认，不得默认为安全")
                else:
                    blockers.append(f"管段 {hit.ref_id} 未获权属确认")
        if not any(
            e.assessment_id == assessment.assessment_id and e.approved for e in self.store.endorsements
        ):
            blockers.append("风险会签未通过")
        return blockers

    def _occupancy_conflicts(self, app: OccupancyApplication) -> list[Permit]:
        conflicts: list[Permit] = []
        for permit in self._active_permits(exclude_application=app.application_id):
            other = self._current_application(permit.application_id)
            if not windows_overlap(app.window_start, app.window_end, permit.window_start, permit.window_end):
                continue
            if polyline_distance(app.path, other.path) < app.impact_radius + other.impact_radius:
                conflicts.append(permit)
        return conflicts

    def issue_permit(self, actor: Actor, application_id: str) -> Permit:
        """签发许可证；同一时空范围只允许一个合法占位（原子检查）。"""
        self._register(actor)
        _require(actor.role is Role.COORDINATOR, PermissionDenied("只有协调员可以签发许可证"))
        with self.store.lock:
            app = self._current_application(application_id)
            if app.state is ApplicationState.APPROVED:
                active = [p for p in self._active_permits() if p.application_id == application_id]
                if active:
                    return active[0]  # 幂等：已签发则返回现有许可证
            _require(
                app.state in (ApplicationState.CONFIRMING, ApplicationState.CO_SIGNING),
                StateError(f"申请当前状态 {app.state.value} 不能签发许可证"),
            )
            assessment = self._latest_assessment(application_id)
            blockers = self._issuance_blockers(app, assessment)
            _require(not blockers, StateError("；".join(blockers)))
            _require(
                app.state is ApplicationState.CO_SIGNING,
                StateError(f"申请当前状态 {app.state.value} 不能签发许可证"),
            )
            conflicts = self._occupancy_conflicts(app)
            if conflicts:
                raise OccupancyConflictError(
                    "与已签发许可证占位冲突：" + "、".join(p.permit_id for p in conflicts)
                )
            permit = Permit(
                permit_id=self.store.next_id("permit"),
                revision=1,
                application_id=application_id,
                assessment_id=assessment.assessment_id,
                window_start=app.window_start,
                window_end=app.window_end,
                state=PermitState.ACTIVE,
                provisional=False,
                issued_at=self.clock(),
                updated_at=self.clock(),
                snapshot_refs=assessment.snapshot_refs,
            )
            self.store.permits[permit.permit_id] = [permit]
            app = replace(app, state=ApplicationState.APPROVED)
            self.store.applications[application_id][-1] = app
            self._event(
                "permit_issued",
                actor,
                permit.permit_id,
                {"application_id": application_id, "assessment_id": assessment.assessment_id},
            )
            self._notify(app.applicant_id, "permit_issued", permit.permit_id,
                         f"许可证 {permit.permit_id} 已签发")
            for hit in assessment.utility_hits:
                self._notify(hit.owner_id, "permit_issued", permit.permit_id,
                             f"涉及管段 {hit.ref_id} 的许可证 {permit.permit_id} 已签发")
        return permit

    # ------------------------------------------------------------------
    # 变更、延期、暂停、紧急抢修与重新评估
    # ------------------------------------------------------------------

    def _replace_application(self, app: OccupancyApplication) -> None:
        self.store.applications[app.application_id].append(app)

    def _suspend_permit_locked(self, actor: Actor, permit: Permit, reason: str) -> Permit:
        _require(
            permit.state in (PermitState.ACTIVE, PermitState.PROVISIONAL),
            StateError(f"许可证当前状态 {permit.state.value} 不能暂停"),
        )
        suspended = replace(permit, revision=permit.revision + 1, state=PermitState.SUSPENDED,
                            updated_at=self.clock())
        self.store.permits[permit.permit_id].append(suspended)
        app = self._current_application(permit.application_id)
        self.store.applications[app.application_id][-1] = replace(app, state=ApplicationState.SUSPENDED)
        self._event("permit_suspended", actor, permit.permit_id, {"reason": reason})
        self._notify(app.applicant_id, "permit_suspended", permit.permit_id,
                     f"许可证 {permit.permit_id} 已暂停：{reason}")
        return suspended

    def suspend_permit(self, actor: Actor, permit_id: str, *, reason: str) -> Permit:
        """暂停许可证并重新评估与其时空相交的后续占用。"""
        self._register(actor)
        _require(
            actor.role in (Role.COORDINATOR, Role.CONTRACTOR),
            PermissionDenied("只有协调员或施工方可以暂停许可证"),
        )
        with self.store.lock:
            permit = self._current_permit(permit_id)
            if actor.role is Role.CONTRACTOR:
                app = self._current_application(permit.application_id)
                _require(app.applicant_id == actor.actor_id, PermissionDenied("只能暂停本项目的许可证"))
            suspended = self._suspend_permit_locked(actor, permit, reason)
            self._reevaluate_affected(permit.application_id, AssessmentTrigger.SUSPENSION)
        return suspended

    def design_change(
        self,
        actor: Actor,
        application_id: str,
        *,
        path: Optional[Sequence[Point]] = None,
        impact_radius: Optional[float] = None,
        method: Optional[str] = None,
        depth_top: Optional[float] = None,
        depth_bottom: Optional[float] = None,
        window_start: Optional[datetime] = None,
        window_end: Optional[datetime] = None,
    ) -> tuple[OccupancyApplication, Assessment]:
        """设计变更：生成申请新版本并重新筛查，已签发许可证先行暂停。"""
        self._register(actor)
        with self.store.lock:
            app = self._current_application(application_id)
            _require(
                actor.actor_id == app.applicant_id or actor.role is Role.COORDINATOR,
                PermissionDenied("只有申请方或协调员可以发起设计变更"),
            )
            _require(app.state not in _TERMINAL_STATES, StateError(f"申请已终结（{app.state.value}）"))
            changed = replace(
                app,
                revision=app.revision + 1,
                path=tuple((float(x), float(y)) for x, y in path) if path is not None else app.path,
                impact_radius=float(impact_radius) if impact_radius is not None else app.impact_radius,
                method=method or app.method,
                depth_top=float(depth_top) if depth_top is not None else app.depth_top,
                depth_bottom=float(depth_bottom) if depth_bottom is not None else app.depth_bottom,
                window_start=window_start or app.window_start,
                window_end=window_end or app.window_end,
                state=ApplicationState.SUBMITTED,
                submitted_at=self.clock(),
            )
            _require(changed.window_end > changed.window_start, DomainError("时间窗结束必须晚于开始"))
            _require(changed.depth_bottom >= changed.depth_top >= 0, DomainError("深度区间不合法"))
            for permit in self._active_permits(exclude_application=None):
                if permit.application_id == application_id:
                    self._suspend_permit_locked(actor, permit, "设计变更，等待重新评估")
            self._replace_application(changed)
            self._event("design_changed", actor, application_id, {"revision": changed.revision})
            changed, assessment = self._screen_and_route(changed, AssessmentTrigger.DESIGN_CHANGE)
            self._reevaluate_affected(application_id, AssessmentTrigger.DESIGN_CHANGE)
        return changed, assessment

    def delay_project(
        self, actor: Actor, application_id: str, *, new_window_end: datetime
    ) -> tuple[OccupancyApplication, Assessment]:
        """工程延期：时间窗变更触发重新评估。"""
        self._register(actor)
        with self.store.lock:
            app = self._current_application(application_id)
            _require(
                actor.actor_id == app.applicant_id or actor.role is Role.COORDINATOR,
                PermissionDenied("只有申请方或协调员可以申请延期"),
            )
            _require(app.state not in _TERMINAL_STATES, StateError(f"申请已终结（{app.state.value}）"))
            _require(new_window_end > app.window_end, DomainError("延期后的结束时间必须晚于原结束时间"))
            changed = replace(
                app,
                revision=app.revision + 1,
                window_end=new_window_end,
                state=ApplicationState.SUBMITTED,
                submitted_at=self.clock(),
            )
            for permit in self._active_permits():
                if permit.application_id == application_id:
                    self._suspend_permit_locked(actor, permit, "工程延期，等待重新评估")
            self._replace_application(changed)
            self._event("project_delayed", actor, application_id,
                        {"revision": changed.revision, "new_window_end": new_window_end.isoformat()})
            changed, assessment = self._screen_and_route(changed, AssessmentTrigger.DELAY)
            self._reevaluate_affected(application_id, AssessmentTrigger.DELAY)
        return changed, assessment

    def emergency_repair(
        self,
        actor: Actor,
        *,
        request_id: str,
        project_code: str,
        path: Sequence[Point],
        impact_radius: float,
        method: str,
        depth_top: float,
        depth_bottom: float,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[OccupancyApplication, Permit]:
        """紧急抢修：先行临时占位，冲突抢占在册许可，未确认高风险绝不标记为安全。"""
        self._register(actor)
        with self.store.lock:
            app, assessment = self.apply_occupancy(
                actor,
                request_id=request_id,
                project_code=project_code,
                path=path,
                impact_radius=impact_radius,
                method=method,
                depth_top=depth_top,
                depth_bottom=depth_bottom,
                window_start=window_start,
                window_end=window_end,
                emergency=True,
            )
            existing = [p for p in self._active_permits() if p.application_id == app.application_id]
            if existing:
                return app, existing[0]  # 幂等

            # 抢占：暂停时空相交的在册许可
            for permit in self._occupancy_conflicts(app):
                self._suspend_permit_locked(actor, permit, "紧急抢修优先占位")

            unconfirmed_high = [
                h for h in assessment.utility_hits
                if h.risk is RiskLevel.HIGH
                and not any(
                    c.assessment_id == assessment.assessment_id and c.segment_id == h.ref_id and c.accepted
                    for c in self.store.confirmations
                )
            ]
            permit = Permit(
                permit_id=self.store.next_id("permit"),
                revision=1,
                application_id=app.application_id,
                assessment_id=assessment.assessment_id,
                window_start=app.window_start,
                window_end=app.window_end,
                state=PermitState.PROVISIONAL,
                provisional=True,
                issued_at=self.clock(),
                updated_at=self.clock(),
                snapshot_refs=assessment.snapshot_refs,
            )
            self.store.permits[permit.permit_id] = [permit]
            self.store.applications[app.application_id][-1] = replace(
                self._current_application(app.application_id), state=ApplicationState.APPROVED
            )
            self._event(
                "emergency_permit_issued",
                actor,
                permit.permit_id,
                {
                    "application_id": app.application_id,
                    "assessment_id": assessment.assessment_id,
                    "unconfirmed_high_risk": [h.ref_id for h in unconfirmed_high],
                },
            )
            for hit in assessment.utility_hits:
                self._notify(
                    hit.owner_id,
                    "urgent_confirmation_requested",
                    app.application_id,
                    f"紧急抢修 {app.application_id} 涉及管段 {hit.ref_id}（风险 {hit.risk.value}），请立即确认",
                )
            for hit in unconfirmed_high:
                self._notify(
                    "role:coordinator",
                    "unconfirmed_high_risk",
                    app.application_id,
                    f"高风险管段 {hit.ref_id} 尚未确认，临时许可 {permit.permit_id} 不得视为安全",
                )
            self._reevaluate_affected(app.application_id, AssessmentTrigger.EMERGENCY)
        return self._current_application(app.application_id), permit

    def resume_permit(self, actor: Actor, permit_id: str) -> Permit:
        """恢复被暂停的许可证：按当前资料重新评估，确认与会签齐全后方可恢复。"""
        self._register(actor)
        _require(actor.role is Role.COORDINATOR, PermissionDenied("只有协调员可以恢复许可证"))
        with self.store.lock:
            permit = self._current_permit(permit_id)
            _require(permit.state is PermitState.SUSPENDED, StateError("许可证未处于暂停状态"))
            app = self._current_application(permit.application_id)
            # 暂停后已做过评估则复用，供权属确认/会签在同一评估版本上收敛
            latest = self._latest_assessment(app.application_id)
            if latest.created_at >= permit.updated_at:
                assessment = latest
            else:
                assessment = self._screen(app, AssessmentTrigger.RESUME)
            blockers = self._issuance_blockers(app, assessment)
            if blockers:
                if app.state in (ApplicationState.SUSPENDED, ApplicationState.SUBMITTED):
                    routed = self._route_after_screen(app, assessment)
                    self.store.applications[app.application_id][-1] = routed
                raise StateError("恢复条件未满足：" + "；".join(blockers))
            conflicts = self._occupancy_conflicts(app)
            if conflicts:
                raise OccupancyConflictError(
                    "与已签发许可证占位冲突：" + "、".join(p.permit_id for p in conflicts)
                )
            resumed = replace(permit, revision=permit.revision + 1, state=PermitState.ACTIVE,
                              updated_at=self.clock())
            self.store.permits[permit_id].append(resumed)
            self.store.applications[app.application_id][-1] = replace(app, state=ApplicationState.APPROVED)
            self._event("permit_resumed", actor, permit_id, {"assessment_id": assessment.assessment_id})
            self._notify(app.applicant_id, "permit_resumed", permit_id, f"许可证 {permit_id} 已恢复")
        return resumed

    def complete_work(self, actor: Actor, permit_id: str) -> Permit:
        """完工核销：释放占位并重新评估后续占用。"""
        self._register(actor)
        with self.store.lock:
            permit = self._current_permit(permit_id)
            app = self._current_application(permit.application_id)
            _require(
                actor.actor_id == app.applicant_id or actor.role is Role.COORDINATOR,
                PermissionDenied("只有申请方或协调员可以核销完工"),
            )
            _require(
                permit.state in (PermitState.ACTIVE, PermitState.PROVISIONAL, PermitState.SUSPENDED),
                StateError(f"许可证当前状态 {permit.state.value} 不能核销"),
            )
            done = replace(permit, revision=permit.revision + 1, state=PermitState.COMPLETED,
                           updated_at=self.clock())
            self.store.permits[permit_id].append(done)
            self.store.applications[app.application_id][-1] = replace(app, state=ApplicationState.COMPLETED)
            self._event("work_completed", actor, permit_id, {"application_id": app.application_id})
            self._reevaluate_affected(app.application_id, AssessmentTrigger.COMPLETION)
        return done

    def cancel_application(self, actor: Actor, application_id: str) -> OccupancyApplication:
        self._register(actor)
        with self.store.lock:
            app = self._current_application(application_id)
            _require(
                actor.actor_id == app.applicant_id or actor.role is Role.COORDINATOR,
                PermissionDenied("只有申请方或协调员可以撤销申请"),
            )
            _require(app.state not in _TERMINAL_STATES, StateError("申请已终结"))
            _require(
                not any(p.application_id == application_id for p in self._active_permits()),
                StateError("存在有效许可证，请先暂停或核销"),
            )
            app = replace(app, state=ApplicationState.CANCELLED)
            self.store.applications[application_id][-1] = app
            self._event("application_cancelled", actor, application_id, {})
        return app

    def _reevaluate_affected(self, source_application_id: str, trigger: AssessmentTrigger) -> None:
        """重新评估与源申请时间窗相交的全部未结占用（调用方须持锁）。"""
        source = self._current_application(source_application_id)
        for app_id in list(self.store.applications):
            if app_id == source_application_id:
                continue
            app = self._current_application(app_id)
            if app.state not in _REEVAL_STATES:
                continue
            if not windows_overlap(app.window_start, app.window_end, source.window_start, source.window_end):
                continue
            assessment = self._screen(app, trigger)
            active_permit = next(
                (p for p in self._active_permits() if p.application_id == app_id), None
            )
            if active_permit is not None:
                previous = self.store.assessments[active_permit.assessment_id]
                known = {h.ref_id for h in previous.hits}
                new_threats = [h for h in assessment.hits if h.ref_id not in known]
                if new_threats:
                    self._suspend_permit_locked(
                        Actor(actor_id="system", role=Role.COORDINATOR),
                        active_permit,
                        "重新评估发现新增冲突：" + "、".join(h.ref_id for h in new_threats),
                    )
                continue
            routed = self._route_after_screen(app, assessment)
            self.store.applications[app_id][-1] = routed
            self._notify(
                app.applicant_id,
                "reevaluated",
                app_id,
                f"因{trigger.value}事件，申请已按最新资料重新评估",
            )

    # ------------------------------------------------------------------
    # 查询与最小披露
    # ------------------------------------------------------------------

    def get_application(self, application_id: str) -> OccupancyApplication:
        return self._current_application(application_id)

    def get_assessment(self, assessment_id: str) -> Assessment:
        assessment = self.store.assessments.get(assessment_id)
        _require(assessment is not None, NotFound(f"评估 {assessment_id} 不存在"))
        return assessment

    def get_permit(self, permit_id: str) -> Permit:
        return self._current_permit(permit_id)

    @staticmethod
    def disclose_segment(snapshot: SegmentSnapshot, viewer: Actor) -> dict:
        """按角色最小披露：敏感坐标仅对协调员/审计/权属本单位完整开放。"""
        base = {
            "segment_id": snapshot.segment_id,
            "revision": snapshot.revision,
            "owner_id": snapshot.owner_id,
            "utility": snapshot.utility.value,
            "confidentiality": snapshot.confidentiality.value,
            "risk": snapshot.risk.value,
            "effective_from": snapshot.effective_from.isoformat(),
            "effective_to": snapshot.effective_to.isoformat() if snapshot.effective_to else None,
        }
        privileged = viewer.role in (Role.COORDINATOR, Role.AUDITOR) or (
            viewer.role is Role.UTILITY_OWNER and viewer.owner_id == snapshot.owner_id
        )
        if privileged or snapshot.confidentiality is Confidentiality.PUBLIC:
            base.update(
                path=[list(p) for p in snapshot.path],
                depth_top=snapshot.depth_top,
                depth_bottom=snapshot.depth_bottom,
                disclosure="full",
            )
        elif snapshot.confidentiality is Confidentiality.INTERNAL:
            base.update(
                path=[list(p) for p in fuzz_path(snapshot.path)],
                depth_top=fuzz_depth(snapshot.depth_top),
                depth_bottom=fuzz_depth(snapshot.depth_bottom),
                disclosure="fuzzed",
            )
        else:
            base.update(path=None, depth_top=None, depth_bottom=None, disclosure="withheld")
        return base

    def list_segments(self, viewer: Actor, *, at: Optional[datetime] = None) -> list[dict]:
        """按查看者角色披露 at 时刻可见的管段版本。"""
        self._register(viewer)
        as_of = at or self.clock()
        return [self.disclose_segment(s, viewer) for s in self._visible_snapshots(as_of)]
