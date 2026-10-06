"""审计重建：还原任一参与方在指定时刻之前看到的版本、警示、决定与通知。

审计人员或协调员可查看任意参与方；参与方只能查看自己。
所有内容均按 at 时刻过滤，坐标披露遵循该参与方自身的角色权限——
即"他当时在系统里能看到什么"，而非"审计员现在能看到什么"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .models import (
    Actor,
    Assessment,
    OccupancyApplication,
    Permit,
    PermitState,
    RiskLevel,
    Role,
)
from .service import CoordinationService, NotFound, PermissionDenied
from .store import Store


def _application_at(store: Store, application_id: str, at: datetime) -> Optional[OccupancyApplication]:
    versions = [a for a in store.applications.get(application_id, []) if a.submitted_at <= at]
    return max(versions, key=lambda a: a.revision) if versions else None


def _permit_at(store: Store, permit_id: str, at: datetime) -> Optional[Permit]:
    versions = [p for p in store.permits.get(permit_id, []) if p.updated_at <= at]
    return max(versions, key=lambda p: p.revision) if versions else None


def _assessment_dict(assessment: Assessment) -> dict[str, Any]:
    return {
        "assessment_id": assessment.assessment_id,
        "application_id": assessment.application_id,
        "application_revision": assessment.application_revision,
        "trigger": assessment.trigger.value,
        "as_of": assessment.as_of.isoformat(),
        "snapshot_refs": [list(ref) for ref in assessment.snapshot_refs],
        "hits": [
            {
                "kind": h.kind,
                "ref_id": h.ref_id,
                "ref_revision": h.ref_revision,
                "owner_id": h.owner_id,
                "utility": h.utility.value if h.utility else None,
                "risk": h.risk.value,
                "min_distance": h.min_distance,
                "required_separation": h.required_separation,
            }
            for h in assessment.hits
        ],
    }


def participant_view(
    service: CoordinationService,
    actor: Actor,
    participant_id: str,
    *,
    at: Optional[datetime] = None,
) -> dict[str, Any]:
    """还原 participant 在 at 时刻之前的可见视图。"""
    service._register(actor)
    store = service.store
    as_of = at or service.clock()
    participant = store.participants.get(participant_id)
    if participant is None:
        raise NotFound(f"参与方 {participant_id} 不存在")
    if actor.role not in (Role.AUDITOR, Role.COORDINATOR) and actor.actor_id != participant_id:
        raise PermissionDenied("只能查看本人的历史视图")

    # 该参与方可见的申请：协调员/审计看全部，其余看本人发起的
    see_all = participant.role in (Role.COORDINATOR, Role.AUDITOR)
    applications: list[OccupancyApplication] = []
    for app_id in store.applications:
        app = _application_at(store, app_id, as_of)
        if app is None:
            continue
        if see_all or app.applicant_id == participant_id:
            applications.append(app)
    app_ids = {a.application_id for a in applications}

    assessments = [
        a
        for a in store.assessments.values()
        if a.application_id in app_ids and a.created_at <= as_of
    ]
    assessments.sort(key=lambda a: a.created_at)
    assessment_ids = {a.assessment_id for a in assessments}

    confirmations = [
        c
        for c in store.confirmations
        if c.decided_at <= as_of
        and (c.assessment_id in assessment_ids or c.decided_by == participant_id)
    ]
    endorsements = [
        e
        for e in store.endorsements
        if e.signed_at <= as_of and (e.assessment_id in assessment_ids or e.signed_by == participant_id)
    ]
    permits: list[Permit] = []
    for permit_id in store.permits:
        permit = _permit_at(store, permit_id, as_of)
        if permit is not None and permit.application_id in app_ids:
            permits.append(permit)

    # 通知归属：个人 id、权属单位 id 或角色广播
    recipients = {participant_id, f"role:{participant.role.value}"}
    if participant.owner_id:
        recipients.add(participant.owner_id)
    notifications = [
        n for n in store.notifications if n.created_at <= as_of and n.recipient in recipients
    ]

    # 警示：截至 at 仍未确认的高风险冲突、占用冲突、临时许可
    confirmed_pairs = {
        (c.assessment_id, c.segment_id) for c in store.confirmations if c.decided_at <= as_of and c.accepted
    }
    warnings: list[dict[str, Any]] = []
    for assessment in assessments:
        for hit in assessment.utility_hits:
            if hit.risk is RiskLevel.HIGH and (assessment.assessment_id, hit.ref_id) not in confirmed_pairs:
                warnings.append(
                    {
                        "kind": "unconfirmed_high_risk",
                        "assessment_id": assessment.assessment_id,
                        "segment_id": hit.ref_id,
                        "message": f"高风险管段 {hit.ref_id} 截至该时刻未获权属确认，不得视为安全",
                    }
                )
        for hit in assessment.occupancy_hits:
            warnings.append(
                {
                    "kind": "occupancy_conflict",
                    "assessment_id": assessment.assessment_id,
                    "permit_id": hit.ref_id,
                    "message": f"与许可证 {hit.ref_id} 的占位存在时空冲突",
                }
            )
    for permit in permits:
        if permit.state is PermitState.PROVISIONAL:
            warnings.append(
                {
                    "kind": "provisional_permit",
                    "permit_id": permit.permit_id,
                    "message": f"许可证 {permit.permit_id} 为紧急临时占位，确认程序尚未完成",
                }
            )

    return {
        "participant": {
            "actor_id": participant.actor_id,
            "role": participant.role.value,
            "owner_id": participant.owner_id,
        },
        "as_of": as_of.isoformat(),
        "segments": [
            service.disclose_segment(s, participant) for s in service._visible_snapshots(as_of)
        ],
        "applications": [
            {
                "application_id": a.application_id,
                "revision": a.revision,
                "project_code": a.project_code,
                "state": a.state.value,
                "emergency": a.emergency,
                "window_start": a.window_start.isoformat(),
                "window_end": a.window_end.isoformat(),
            }
            for a in sorted(applications, key=lambda a: a.application_id)
        ],
        "assessments": [_assessment_dict(a) for a in assessments],
        "decisions": {
            "confirmations": [
                {
                    "assessment_id": c.assessment_id,
                    "segment_id": c.segment_id,
                    "owner_id": c.owner_id,
                    "accepted": c.accepted,
                    "comment": c.comment,
                    "decided_by": c.decided_by,
                    "decided_at": c.decided_at.isoformat(),
                }
                for c in confirmations
            ],
            "endorsements": [
                {
                    "assessment_id": e.assessment_id,
                    "approved": e.approved,
                    "comment": e.comment,
                    "signed_by": e.signed_by,
                    "signed_at": e.signed_at.isoformat(),
                }
                for e in endorsements
            ],
            "permits": [
                {
                    "permit_id": p.permit_id,
                    "revision": p.revision,
                    "application_id": p.application_id,
                    "assessment_id": p.assessment_id,
                    "state": p.state.value,
                    "provisional": p.provisional,
                    "snapshot_refs": [list(ref) for ref in p.snapshot_refs],
                }
                for p in sorted(permits, key=lambda p: p.permit_id)
            ],
        },
        "warnings": warnings,
        "notifications": [
            {
                "notification_id": n.notification_id,
                "kind": n.kind,
                "subject_id": n.subject_id,
                "message": n.message,
                "created_at": n.created_at.isoformat(),
            }
            for n in notifications
        ],
    }


def verify_audit_trail(service: CoordinationService) -> dict[str, Any]:
    """校验审计哈希链完整性。"""
    return {"events": len(service.store.events), "chain_valid": service.store.verify_chain()}
