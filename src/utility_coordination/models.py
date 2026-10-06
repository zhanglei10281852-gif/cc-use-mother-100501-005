"""地下管网施工协同的领域模型。

所有实体均为不可变 dataclass：变更通过产生新版本完成，历史行不会被就地改写。
枚举值使用稳定的中文字符串，便于审计日志与 API 直接阅读。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from hashlib import sha256
from typing import Any, Mapping


class UtilityType(str, Enum):
    WATER = "water"            # 供水
    HEAT = "heat"             # 供热
    GAS = "gas"               # 燃气
    TELECOM = "telecom"       # 通信


class Classification(int, Enum):
    """保密等级：数值越大越敏感。"""

    PUBLIC = 1         # 公开
    INTERNAL = 2       # 内部
    CONFIDENTIAL = 3   # 敏感


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Role(str, Enum):
    COORDINATOR = "coordinator"   # 市政建设协调员 / 城市更新部门
    OWNER = "owner"               # 权属单位（供水/供热/燃气/通信）
    CONTRACTOR = "contractor"     # 工程方
    AUDITOR = "auditor"           # 审计人员


class WorkMethod(str, Enum):
    OPEN_CUT = "open_cut"         # 明挖
    TRENCHLESS = "trenchless"     # 非开挖（顶管/定向钻等）
    MANUAL = "manual"             # 人工探挖
    EMERGENCY_REPAIR = "emergency_repair"  # 紧急抢修工法


class AppStatus(str, Enum):
    SUBMITTED = "submitted"                 # 已提交，尚未评估
    OCCUPANCY_BLOCKED = "occupancy_blocked" # 存在合法占位，本申请不持有占位
    IN_REVIEW = "in_review"                 # 会签中，持有唯一合法占位
    REVIEW_CLOSED = "review_closed"         # 审核收口，等待签发
    PERMIT_ISSUED = "permit_issued"         # 许可证已签发
    DENIED = "denied"                       # 会签驳回 / 不予许可
    SUPERSEDED = "superseded"               # 被变更/延期后的新版本取代
    SUSPENDED = "suspended"                 # 许可证暂停（含被紧急抢修抢占）
    COMPLETED = "completed"                 # 工程完成，占位释放


# 持有合法时空占位的状态
PLACEHOLDER_STATUSES = frozenset({AppStatus.IN_REVIEW, AppStatus.REVIEW_CLOSED, AppStatus.PERMIT_ISSUED})


class ReviewRequirement(str, Enum):
    CONFIRM = "confirm"   # 权属确认
    COSIGN = "co_sign"    # 风险会签（高风险）


class ConfirmationState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CARRIED = "carried"                       # 沿用同一快照版本的历史意见
    NO_RESPONSE_LOW_RISK = "no_response_low"  # 低风险逾期未复，不阻断但留痕
    STALE = "stale"                           # 评估后快照已更新，意见对新版本失效


class Decision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class Priority(str, Enum):
    NORMAL = "normal"
    EMERGENCY = "emergency"


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    name: str
    role: Role
    owner_id: str | None = None  # role=OWNER 时对应权属单位
    clearance: int = Classification.PUBLIC

    def __post_init__(self) -> None:
        if not self.actor_id.strip() or not self.name.strip():
            raise ValueError("actor_id 与 name 不能为空")
        if self.role == Role.OWNER and not self.owner_id:
            raise ValueError("权属单位角色必须提供 owner_id")


@dataclass(frozen=True, slots=True)
class PipeSnapshot:
    """权属单位提交的管段快照：带生效期、保密等级、版本链。"""

    snapshot_id: str
    pipe_code: str
    owner_id: str
    utility_type: UtilityType
    classification: Classification
    risk_level: RiskLevel
    geometry: tuple[tuple[float, float], ...]
    diameter_mm: int
    pressure: str
    effective_from: str          # ISO8601
    effective_to: str | None     # None 表示长期有效（开区间）
    submitted_by: str
    submitted_at: str
    version: int
    supersedes: str | None = None

    def __post_init__(self) -> None:
        if len(self.geometry) < 2:
            raise ValueError("管段折线至少需要 2 个坐标点")
        if self.version < 1:
            raise ValueError("version 必须大于零")


@dataclass(frozen=True, slots=True)
class OccupancyApplication:
    """工程方占用申请：范围、工法、时间窗、影响面；修订形成版本链。"""

    application_id: str
    project_code: str
    contractor_id: str
    work_area: tuple[tuple[float, float], ...]
    road_closure: tuple[tuple[float, float], ...]
    method: WorkMethod
    window_start: str
    window_end: str
    impact_radius_m: float
    submitted_by: str
    submitted_at: str
    revision: int = 1
    supersedes: str | None = None
    priority: Priority = Priority.NORMAL
    status: AppStatus = AppStatus.SUBMITTED
    status_reason: str | None = None
    change_note: str | None = None  # 变更/延期/抢修说明

    def __post_init__(self) -> None:
        if len(self.work_area) < 3:
            raise ValueError("施工范围至少需要 3 个坐标点")
        if self.window_start >= self.window_end:
            raise ValueError("时间窗必须满足 start < end")
        if self.impact_radius_m < 0:
            raise ValueError("影响面半径不能为负")
        if self.revision < 1:
            raise ValueError("revision 必须大于零")


@dataclass(frozen=True, slots=True)
class PipeConflict:
    """评估命中的管段：固化当时可见的快照版本与距离。"""

    snapshot_id: str
    pipe_code: str
    owner_id: str
    utility_type: UtilityType
    classification: Classification
    risk_level: RiskLevel
    distance_m: float
    requirement: ReviewRequirement
    confirmation_state: ConfirmationState = ConfirmationState.PENDING
    decided_by: str | None = None
    decided_at: str | None = None
    decision: Decision | None = None
    comment: str | None = None

    def with_state(self, **changes: Any) -> "PipeConflict":
        from dataclasses import replace

        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class OccupancyConflict:
    """相邻工程之间的封路 / 安全缓冲区（影响面）冲突。"""

    other_application_id: str
    other_project_code: str
    other_contractor_id: str
    road_closures_overlap: bool
    buffer_distance_m: float            # 两施工范围最短距离
    combined_buffer_m: float
    time_overlap: bool
    other_priority: Priority


@dataclass(frozen=True, slots=True)
class ConflictAssessment:
    """一次评估的完整结论；重新评估会生成新评估，不覆盖旧评估。"""

    assessment_id: str
    application_id: str
    revision: int
    created_at: str
    visible_as_of: str                  # “当时可见资料”的截止时刻
    review_deadline: str                # 未回复判定时刻（普通 48h / 紧急 4h）
    pipe_conflicts: tuple[PipeConflict, ...]
    occupancy_conflicts: tuple[OccupancyConflict, ...]
    warnings: tuple[str, ...]
    supersedes_assessment: str | None = None
    cause: str = "initial"  # initial / revision / snapshot_update / resume / occupancy_released


@dataclass(frozen=True, slots=True)
class ConfirmationRecord:
    """权属单位对某评估中某管段的回复（确认/会签/逾期留痕）。"""

    application_id: str
    revision: int
    snapshot_id: str
    state: ConfirmationState
    decided_by: str | None
    decided_at: str
    decision: Decision | None
    comment: str | None
    carried_from_revision: int | None = None


@dataclass(frozen=True, slots=True)
class Permit:
    """许可证签发记录。"""

    permit_code: str
    application_id: str
    revision: int
    issued_by: str
    issued_at: str
    window_start: str
    window_end: str
    valid: bool = True
    emergency_override: str | None = None  # 紧急抢修带未决高风险项时的强制说明


@dataclass(frozen=True, slots=True)
class Notification:
    """发给某个参与方的通知；落库时已按接收方权限完成最小披露渲染。"""

    notification_id: str
    created_at: str
    recipient_actor_id: str
    subject: str
    body: str
    related_type: str       # application / snapshot / permit / assessment
    related_id: str
    redacted: bool
    sensitive: bool = False


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"不可序列化的类型: {type(obj)!r}")


def canonical_payload(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)


def fingerprint_of(data: Mapping[str, Any]) -> str:
    return sha256(canonical_payload(data).encode("utf-8")).hexdigest()


def entity_to_dict(obj: Any) -> dict[str, Any]:
    return asdict(obj)
