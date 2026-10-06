"""存储层：内存仓库 + 哈希链审计日志 + JSON 文件持久化。

不依赖外部数据库；JsonFileStore 以单个 JSON 文件保存全部状态，
供命令行多次调用之间共享数据。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional

from .models import (
    Actor,
    ApplicationState,
    Assessment,
    AssessmentTrigger,
    ConflictHit,
    Confidentiality,
    Event,
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
    parse_dt,
    utcnow,
)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _event_hash(prev_hash: str, body: dict[str, Any]) -> str:
    return sha256((prev_hash + "|" + _canonical(body)).encode("utf-8")).hexdigest()


class Store:
    """聚合全部实体表与审计事件日志。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.segments: dict[str, list[SegmentSnapshot]] = {}
        self.applications: dict[str, list[OccupancyApplication]] = {}
        self.assessments: dict[str, Assessment] = {}
        self.confirmations: list[OwnerConfirmation] = []
        self.endorsements: list[RiskEndorsement] = []
        self.permits: dict[str, list[Permit]] = {}
        self.notifications: list[Notification] = []
        self.events: list[Event] = []
        self.request_index: dict[str, str] = {}  # request_id -> application_id
        self.participants: dict[str, Actor] = {}
        self._counters: dict[str, int] = {}

    # ---- 标识与序号 -----------------------------------------------------

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:06d}"

    # ---- 审计事件 --------------------------------------------------------

    def append_event(
        self,
        kind: str,
        actor_id: str,
        actor_role: Role,
        subject_id: str,
        payload: Optional[dict[str, Any]] = None,
        at: Optional[datetime] = None,
    ) -> Event:
        body = {
            "seq": len(self.events) + 1,
            "at": (at or utcnow()).isoformat(),
            "kind": kind,
            "actor_id": actor_id,
            "actor_role": actor_role.value,
            "subject_id": subject_id,
            "payload": payload or {},
        }
        prev_hash = self.events[-1].hash if self.events else "GENESIS"
        event = Event(
            seq=body["seq"],
            at=parse_dt(body["at"]),
            kind=kind,
            actor_id=actor_id,
            actor_role=actor_role.value,
            subject_id=subject_id,
            payload=tuple(sorted((str(k), _canonical(v)) for k, v in body["payload"].items())),
            prev_hash=prev_hash,
            hash=_event_hash(prev_hash, body),
        )
        self.events.append(event)
        return event

    def verify_chain(self) -> bool:
        prev = "GENESIS"
        for event in self.events:
            body = {
                "seq": event.seq,
                "at": event.at.isoformat(),
                "kind": event.kind,
                "actor_id": event.actor_id,
                "actor_role": event.actor_role,
                "subject_id": event.subject_id,
                "payload": {k: json.loads(v) for k, v in event.payload},
            }
            if _event_hash(prev, body) != event.hash or event.prev_hash != prev:
                return False
            prev = event.hash
        return True

    # ---- 序列化 -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        def dump(value: Any) -> Any:
            if isinstance(value, datetime):
                return {"__dt__": value.isoformat()}
            if isinstance(value, Enum):  # type: ignore[arg-type]
                return {"__enum__": f"{type(value).__name__}:{value.value}"}
            if isinstance(value, tuple):
                return {"__tuple__": [dump(v) for v in value]}
            if isinstance(value, list):
                return [dump(v) for v in value]
            if isinstance(value, dict):
                return {k: dump(v) for k, v in value.items()}
            return value

        return {
            "segments": {k: [dump(asdict(s)) for s in v] for k, v in self.segments.items()},
            "applications": {k: [dump(asdict(a)) for a in v] for k, v in self.applications.items()},
            "assessments": {k: dump(asdict(a)) for k, a in self.assessments.items()},
            "confirmations": [dump(asdict(c)) for c in self.confirmations],
            "endorsements": [dump(asdict(e)) for e in self.endorsements],
            "permits": {k: [dump(asdict(p)) for p in v] for k, v in self.permits.items()},
            "notifications": [dump(asdict(n)) for n in self.notifications],
            "events": [dump(asdict(e)) for e in self.events],
            "request_index": dict(self.request_index),
            "participants": {
                k: {"actor_id": a.actor_id, "role": a.role.value, "owner_id": a.owner_id}
                for k, a in self.participants.items()
            },
            "counters": dict(self._counters),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Store":
        enums = {
            name: cls_
            for name, cls_ in [
                ("ApplicationState", ApplicationState),
                ("AssessmentTrigger", AssessmentTrigger),
                ("Confidentiality", Confidentiality),
                ("PermitState", PermitState),
                ("RiskLevel", RiskLevel),
                ("UtilityType", UtilityType),
            ]
        }

        def load(value: Any) -> Any:
            if isinstance(value, dict):
                if "__dt__" in value:
                    return parse_dt(value["__dt__"])
                if "__enum__" in value:
                    name, raw = value["__enum__"].split(":", 1)
                    return enums[name](raw)
                if "__tuple__" in value:
                    return tuple(load(v) for v in value["__tuple__"])
                return {k: load(v) for k, v in value.items()}
            if isinstance(value, list):
                return [load(v) for v in value]
            return value

        store = cls()
        store.segments = {
            k: [SegmentSnapshot(**load(s)) for s in v] for k, v in data.get("segments", {}).items()
        }
        store.applications = {
            k: [OccupancyApplication(**load(a)) for a in v]
            for k, v in data.get("applications", {}).items()
        }
        store.assessments = {}
        for k, raw in data.get("assessments", {}).items():
            payload = load(raw)
            payload["hits"] = tuple(ConflictHit(**h) for h in payload["hits"])
            store.assessments[k] = Assessment(**payload)
        store.confirmations = [OwnerConfirmation(**load(c)) for c in data.get("confirmations", [])]
        store.endorsements = [RiskEndorsement(**load(e)) for e in data.get("endorsements", [])]
        store.permits = {k: [Permit(**load(p)) for p in v] for k, v in data.get("permits", {}).items()}
        store.notifications = [Notification(**load(n)) for n in data.get("notifications", [])]
        store.events = [Event(**load(e)) for e in data.get("events", [])]
        store.request_index = dict(data.get("request_index", {}))
        store.participants = {
            k: Actor(actor_id=v["actor_id"], role=Role(v["role"]), owner_id=v.get("owner_id"))
            for k, v in data.get("participants", {}).items()
        }
        store._counters = dict(data.get("counters", {}))
        return store


class JsonFileStore(Store):
    """每次变更后落盘的 JSON 文件存储，供 CLI 使用。"""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        if self.path.exists():
            loaded = Store.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
            self.__dict__.update({k: v for k, v in loaded.__dict__.items() if k != "path"})
            self.path = Path(path)

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)
