"""地下管网施工协同的核心领域模型。

所有实体均为不可变对象：任何变更都产生新的版本（revision），
已完成的审核（Assessment）永远引用其计算时使用的管段快照版本，
保证事故回溯时可以还原"当时可见的资料"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .geometry import Path


def utcnow() -> datetime:
    """统一的当前时间来源（UTC， aware）。"""
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> datetime:
    """解析 ISO-8601 时间；无时区时按 UTC 处理。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class Role(str, Enum):
    """参与方角色，决定坐标披露粒度与可执行操作。"""

    COORDINATOR = "coordinator"  # 市政建设协调员
    UTILITY_OWNER = "utility_owner"  # 管线权属单位
    CONTRACTOR = "contractor"  # 工程施工方
    AUDITOR = "auditor"  # 审计人员


class UtilityType(str, Enum):
    WATER = "water"
    HEATING = "heating"
    GAS = "gas"
    TELECOM = "telecom"


class Confidentiality(str, Enum):
    """管段资料保密等级。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    SECRET = "secret"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ApplicationState(str, Enum):
    SUBMITTED = "submitted"  # 已提交，待筛查
    CONFIRMING = "confirming"  # 等待权属确认
    CO_SIGNING = "co_signing"  # 等待风险会签
    APPROVED = "approved"  # 许可证已签发
    REJECTED = "rejected"
    SUSPENDED = "suspended"  # 因暂停/变更等待重新评估
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PermitState(str, Enum):
    ACTIVE = "active"
    PROVISIONAL = "provisional"  # 紧急抢修先行占位，高风险确认尚待补齐
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    REVOKED = "revoked"


class AssessmentTrigger(str, Enum):
    INITIAL = "initial"
    DESIGN_CHANGE = "design_change"
    DELAY = "delay"
    SUSPENSION = "suspension"
    EMERGENCY = "emergency"
    RESUME = "resume"
    COMPLETION = "completion"


# 各工种安全缓冲（米）：权属单位设施外侧要求的最小水平净距
SAFETY_BUFFER: dict[UtilityType, float] = {
    UtilityType.WATER: 2.0,
    UtilityType.HEATING: 3.0,
    UtilityType.GAS: 5.0,
    UtilityType.TELECOM: 1.5,
}

# 风险等级对缓冲的放大系数
RISK_MULTIPLIER: dict[RiskLevel, float] = {
    RiskLevel.LOW: 1.0,
    RiskLevel.MEDIUM: 1.5,
    RiskLevel.HIGH: 2.0,
}

# 工法对影响面的放大系数
METHOD_MULTIPLIER: dict[str, float] = {
    "manual": 0.8,  # 人工开挖
    "trenchless": 1.0,  # 非开挖顶管/定向钻
    "open_cut": 1.2,  # 明挖
    "blasting": 2.0,  # 爆破
}
DEFAULT_METHOD_MULTIPLIER = 1.0

# 深度方向要求的垂直净距（米）
VERTICAL_CLEARANCE = 0.5


@dataclass(frozen=True, slots=True)
class Actor:
    """操作发起方。owner_id 将权属单位账号与其管段关联。"""

    actor_id: str
    role: Role
    owner_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.actor_id.strip():
            raise ValueError("actor_id 不能为空")
        if self.role is Role.UTILITY_OWNER and not (self.owner_id or "").strip():
            raise ValueError("权属单位角色必须携带 owner_id")


@dataclass(frozen=True, slots=True)
class SegmentSnapshot:
    """权属单位提交的管段快照：带生效期与保密等级的不可变版本。"""

    segment_id: str
    revision: int
    owner_id: str
    utility: UtilityType
    confidentiality: Confidentiality
    path: Path
    depth_top: float
    depth_bottom: float
    effective_from: datetime
    effective_to: Optional[datetime]
    risk: RiskLevel
    submitted_at: datetime

    def covers_window(self, start: datetime, end: datetime) -> bool:
        """快照生效期是否覆盖施工时间窗（生效期结束为空表示长期有效）。"""
        if self.effective_from > end:
            return False
        if self.effective_to is not None and self.effective_to < start:
            return False
        return True


@dataclass(frozen=True, slots=True)
class OccupancyApplication:
    """工程方的占用申请：施工范围、工法、时间窗与影响面。"""

    application_id: str
    revision: int
    request_id: str  # 幂等键：重复提交返回同一申请
    project_code: str
    applicant_id: str
    path: Path
    impact_radius: float  # 影响面半宽（米）
    method: str  # 工法
    depth_top: float
    depth_bottom: float
    window_start: datetime
    window_end: datetime
    state: ApplicationState
    emergency: bool
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class ConflictHit:
    """一次空间+时间冲突命中。kind 区分管线冲突与在册占用冲突。"""

    kind: str  # "utility" | "occupancy"
    ref_id: str  # segment_id 或 permit_id
    ref_revision: int
    owner_id: str
    utility: Optional[UtilityType]
    risk: RiskLevel
    min_distance: float
    required_separation: float


@dataclass(frozen=True, slots=True)
class Assessment:
    """冲突评估：按 as_of 时刻可见资料计算，完成后不可变。"""

    assessment_id: str
    application_id: str
    application_revision: int
    trigger: AssessmentTrigger
    as_of: datetime
    created_at: datetime
    snapshot_refs: tuple[tuple[str, int], ...]  # (segment_id, revision) 计算所用版本
    hits: tuple[ConflictHit, ...]

    @property
    def utility_hits(self) -> tuple[ConflictHit, ...]:
        return tuple(h for h in self.hits if h.kind == "utility")

    @property
    def occupancy_hits(self) -> tuple[ConflictHit, ...]:
        return tuple(h for h in self.hits if h.kind == "occupancy")


@dataclass(frozen=True, slots=True)
class OwnerConfirmation:
    """权属单位对某一冲突管段的确认决定。"""

    application_id: str
    assessment_id: str
    segment_id: str
    owner_id: str
    accepted: bool
    comment: str
    decided_by: str
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class RiskEndorsement:
    """协调员组织的风险会签。"""

    application_id: str
    assessment_id: str
    approved: bool
    comment: str
    signed_by: str
    signed_at: datetime


@dataclass(frozen=True, slots=True)
class Permit:
    """施工许可证：唯一合法占位的载体，引用签发时的评估版本。"""

    permit_id: str
    revision: int
    application_id: str
    assessment_id: str
    window_start: datetime
    window_end: datetime
    state: PermitState
    provisional: bool
    issued_at: datetime
    updated_at: datetime
    snapshot_refs: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Notification:
    """面向参与方的通知；recipient 为参与方 id 或 role:<角色> 广播。"""

    notification_id: str
    recipient: str
    kind: str
    subject_id: str
    message: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Event:
    """审计事件：追加式日志，哈希链防篡改。"""

    seq: int
    at: datetime
    kind: str
    actor_id: str
    actor_role: str
    subject_id: str
    payload: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    prev_hash: str = ""
    hash: str = ""
