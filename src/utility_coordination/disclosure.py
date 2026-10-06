"""按角色最小披露。

核心原则：坐标只在“业务必需 + 密级允许”时给出精确值；
不足密级时几何被抹除、距离被粗化，但冲突存在性与风险等级仍保留，
保证工程方能收到警示、审计方能完整还原。
"""

from __future__ import annotations

from typing import Any

from .models import (
    Actor,
    Classification,
    ConflictAssessment,
    OccupancyConflict,
    PipeConflict,
    PipeSnapshot,
    Role,
    entity_to_dict,
)

# 不同角色对“非己方”资料的最高可见密级
_ROLE_CEILING: dict[Role, int] = {
    Role.COORDINATOR: Classification.CONFIDENTIAL,
    Role.AUDITOR: Classification.CONFIDENTIAL,
    Role.OWNER: Classification.INTERNAL,
    Role.CONTRACTOR: Classification.PUBLIC,
}

# 距离粗化粒度（米）：密级不足时只给近似距离
_COARSE_GRANULARITY = {
    Classification.INTERNAL: 1.0,
    Classification.CONFIDENTIAL: 5.0,
}


def _effective_clearance(viewer: Actor, owner_id: str | None) -> int:
    ceiling = max(viewer.clearance, _ROLE_CEILING[viewer.role])
    # 权属单位对自己的资料拥有完整可见性
    if viewer.role == Role.OWNER and owner_id is not None and viewer.owner_id == owner_id:
        return Classification.CONFIDENTIAL
    return ceiling


def can_see_exact_geometry(viewer: Actor, classification: Classification, owner_id: str) -> bool:
    return _effective_clearance(viewer, owner_id) >= int(classification)


def _coarse_distance(distance_m: float, classification: Classification, allowed: int) -> float:
    if allowed >= int(classification):
        return round(distance_m, 2)
    if classification == Classification.PUBLIC:
        return round(distance_m, 2)
    step = _COARSE_GRANULARITY[classification]
    # 向上取整到粗化粒度，避免“近似距离即精确坐标”
    import math

    return round(math.ceil(distance_m / step) * step, 2)


def snapshot_view(viewer: Actor, snapshot: PipeSnapshot) -> dict[str, Any]:
    data = entity_to_dict(snapshot)
    allowed = _effective_clearance(viewer, snapshot.owner_id)
    if allowed < int(snapshot.classification):
        data["geometry"] = None
        data["redacted"] = True
        data["redaction_reason"] = "classification"
    else:
        data["redacted"] = False
    return data


def pipe_conflict_view(viewer: Actor, conflict: PipeConflict) -> dict[str, Any]:
    data = entity_to_dict(conflict)
    allowed = _effective_clearance(viewer, conflict.owner_id)
    if allowed < int(conflict.classification):
        data["distance_m"] = _coarse_distance(
            conflict.distance_m, conflict.classification, allowed
        )
        data["geometry_redacted"] = True
        if conflict.classification == Classification.CONFIDENTIAL:
            # 敏感管段：不向无关方暴露管段标识与权属，仅保留风险与类型警示
            data["pipe_code"] = f"redacted-{conflict.utility_type.value}"
            data["owner_id"] = None
    else:
        data["distance_m"] = round(conflict.distance_m, 2)
        data["geometry_redacted"] = False
    return data


def occupancy_conflict_view(conflict: OccupancyConflict) -> dict[str, Any]:
    return entity_to_dict(conflict)


def assessment_view(viewer: Actor, assessment: ConflictAssessment) -> dict[str, Any]:
    return {
        "assessment_id": assessment.assessment_id,
        "application_id": assessment.application_id,
        "revision": assessment.revision,
        "created_at": assessment.created_at,
        "visible_as_of": assessment.visible_as_of,
        "review_deadline": assessment.review_deadline,
        "cause": assessment.cause,
        "supersedes_assessment": assessment.supersedes_assessment,
        "warnings": list(assessment.warnings),
        "pipe_conflicts": [pipe_conflict_view(viewer, c) for c in assessment.pipe_conflicts],
        "occupancy_conflicts": [
            occupancy_conflict_view(c) for c in assessment.occupancy_conflicts
        ],
    }
