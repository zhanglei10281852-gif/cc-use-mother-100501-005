"""只追加的哈希链式事件日志。

每个事件包含前一事件的哈希，形成可独立校验的链；
事件载荷固化“当时的完整事实”（含按接收方渲染后的通知正文），
因此事故复盘时可以离线重放任一参与方当时看到的内容。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterator

from .models import _json_default

GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    event_id: str
    timestamp: str
    actor_id: str
    event_type: str
    payload: dict[str, Any]
    prev_hash: str

    def block_hash(self) -> str:
        body = {
            "seq": self.seq,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "actor_id": self.actor_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }
        rendered = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default)
        return sha256(rendered.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "actor_id": self.actor_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.block_hash(),
        }


def event_from_dict(row: dict[str, Any]) -> Event:
    return Event(
        seq=row["seq"],
        event_id=row["event_id"],
        timestamp=row["timestamp"],
        actor_id=row["actor_id"],
        event_type=row["event_type"],
        payload=row["payload"],
        prev_hash=row["prev_hash"],
    )


class EventLog:
    """内存链式日志，可选持久化为 JSONL；加载时重放并校验哈希链。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self._events: list[Event] = []
        self._path = Path(path) if path else None
        if self._path is not None and self._path.exists():
            self._load()

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    def head_hash(self) -> str:
        return self._events[-1].block_hash() if self._events else GENESIS_HASH

    def append(
        self,
        event_id: str,
        timestamp: str,
        actor_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> Event:
        event = Event(
            seq=len(self._events) + 1,
            event_id=event_id,
            timestamp=timestamp,
            actor_id=actor_id,
            event_type=event_type,
            payload=payload,
            prev_hash=self.head_hash(),
        )
        # 先自校验，再落盘
        event.block_hash()
        self._events.append(event)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict(), ensure_ascii=False, default=_json_default) + "\n")
        return event

    def replay(self, handler: Callable[[Event], None], before: str | None = None) -> None:
        """按顺序重放事件；before 给定时只重放严格早于该时刻的事件（事故还原）。"""
        from .timeutil import parse_ts

        cutoff = parse_ts(before) if before else None
        for event in self._events:
            if cutoff is not None and parse_ts(event.timestamp) >= cutoff:
                break
            handler(event)

    def iter_events(self, event_type: str | None = None) -> Iterator[Event]:
        for event in self._events:
            if event_type is None or event.event_type == event_type:
                yield event

    def verify_chain(self) -> None:
        prev = GENESIS_HASH
        for event in self._events:
            if event.prev_hash != prev:
                raise ValueError(f"事件 #{event.seq} 前序哈希不匹配，日志可能被篡改")
            prev = event.block_hash()

    def _load(self) -> None:
        assert self._path is not None
        prev = GENESIS_HASH
        with self._path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                event = event_from_dict(row)
                if event.prev_hash != prev:
                    raise ValueError(f"审计日志第 {line_no} 行前序哈希断裂")
                if event.block_hash() != row["hash"]:
                    raise ValueError(f"审计日志第 {line_no} 行内容哈希不匹配")
                self._events.append(event)
                prev = event.block_hash()
