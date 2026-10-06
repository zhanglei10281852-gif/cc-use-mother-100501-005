"""事故还原：从只追加审计日志重放事故发生前各方看到的内容。

不依赖在线服务状态，只读取哈希链事件；可被 API 与命令行共同使用。
"""

from __future__ import annotations

from typing import Any

from .events import Event, EventLog
from .timeutil import parse_ts


class IncidentReplay:
    def __init__(self, log: EventLog) -> None:
        self.log = log

    def verify(self) -> None:
        self.log.verify_chain()

    def _events_before(self, before: str | None) -> list[Event]:
        if before is None:
            return list(self.log.events)
        cutoff = parse_ts(before)
        return [e for e in self.log.events if parse_ts(e.timestamp) < cutoff]

    def actor_view(self, actor_id: str, before: str | None = None) -> dict[str, Any]:
        """还原某参与方在事故前看到的：通知、其掌握的快照版本、其做出的回复。"""
        events = self._events_before(before)
        notifications: list[dict[str, Any]] = []
        own_snapshots: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        for event in events:
            if event.event_type == "notification.dispatched":
                view = event.payload["viewer"]
                if view["actor_id"] == actor_id:
                    notifications.append(event.payload["notification"])
            elif event.event_type == "snapshot.submitted":
                snap = event.payload
                if snap["submitted_by"] == actor_id:
                    own_snapshots.append(
                        {
                            "snapshot_id": snap["snapshot_id"],
                            "pipe_code": snap["pipe_code"],
                            "version": snap["version"],
                            "utility_type": snap["utility_type"],
                            "classification": snap["classification"],
                            "submitted_at": snap["submitted_at"],
                            "effective_from": snap["effective_from"],
                        }
                    )
            elif event.event_type == "pipe.responded" and event.actor_id == actor_id:
                decisions.append(
                    {
                        "at": event.timestamp,
                        "application_id": event.payload["record"]["application_id"],
                        "revision": event.payload["record"]["revision"],
                        "snapshot_id": event.payload["record"]["snapshot_id"],
                        "snapshot_fingerprint": event.payload["snapshot_fingerprint"],
                        "decision": event.payload["record"]["decision"],
                        "comment": event.payload["record"]["comment"],
                    }
                )
        return {
            "actor_id": actor_id,
            "as_of_before": before,
            "snapshots_submitted": own_snapshots,
            "decisions": decisions,
            "notifications_received": notifications,
        }

    def application_timeline(self, application_id: str, before: str | None = None) -> dict[str, Any]:
        events = self._events_before(before)
        revisions: dict[int, dict[str, Any]] = {}
        status_changes: list[dict[str, Any]] = []
        assessments: list[dict[str, Any]] = []
        permits: list[dict[str, Any]] = []
        responses: list[dict[str, Any]] = []
        notifications: list[dict[str, Any]] = []
        for event in events:
            p = event.payload
            t = event.event_type
            if t == "application.submitted" and p.get("application_id") == application_id:
                revisions[p["revision"]] = {"submitted": event.timestamp, "application": p}
            elif t == "application.revised" and p.get("application_id") == application_id:
                revisions[p["to_revision"]] = {
                    "submitted": event.timestamp,
                    "change_note": p.get("change_note"),
                }
            elif t == "application.status_changed" and p.get("application_id") == application_id:
                status_changes.append({"at": event.timestamp, **p})
            elif t == "assessment.created" and p["assessment"]["application_id"] == application_id:
                a = p["assessment"]
                assessments.append(
                    {
                        "at": event.timestamp,
                        "assessment_id": a["assessment_id"],
                        "revision": a["revision"],
                        "cause": a["cause"],
                        "visible_as_of": a["visible_as_of"],
                        "review_deadline": a["review_deadline"],
                        "holds_placeholder": p["holds_placeholder"],
                        "warnings": a["warnings"],
                        "referenced_snapshots": [
                            {
                                "snapshot_id": c["snapshot_id"],
                                "pipe_code": c["pipe_code"],
                                "owner_id": c["owner_id"],
                                "utility_type": c["utility_type"],
                                "classification": c["classification"],
                                "risk_level": c["risk_level"],
                                "distance_m": c["distance_m"],
                                "requirement": c["requirement"],
                                "state_at_assessment": c["confirmation_state"],
                            }
                            for c in a["pipe_conflicts"]
                        ],
                        "occupancy_conflicts": a["occupancy_conflicts"],
                    }
                )
            elif t in ("pipe.responded", "pipe.no_response_timeout"):
                rec = p.get("record", {})
                if rec.get("application_id") == application_id:
                    responses.append({"at": event.timestamp, "event": t, **p})
            elif t == "permit.issued" and p["permit"]["application_id"] == application_id:
                permits.append({"at": event.timestamp, **p["permit"]})
            elif t == "permit.invalidated":
                # 与申请关联在暂停事件中体现，这里单独列出
                pass
            elif t == "notification.dispatched":
                note = p["notification"]
                if note["related_id"] == application_id or (
                    note["related_type"] == "assessment"
                    and any(x["assessment_id"] == note["related_id"] for x in assessments)
                ):
                    notifications.append(
                        {
                            "at": note["created_at"],
                            "recipient": p["viewer"]["actor_id"],
                            "recipient_role": p["viewer"]["role"],
                            "subject": note["subject"],
                            "body": note["body"],
                            "redacted": note["redacted"],
                        }
                    )
        return {
            "application_id": application_id,
            "as_of_before": before,
            "revisions": [
                {"revision": rev, **revisions[rev]} for rev in sorted(revisions)
            ],
            "status_changes": status_changes,
            "assessments": assessments,
            "responses": responses,
            "permits": permits,
            "notifications": notifications,
        }

    def incident_report(self, before: str | None = None) -> dict[str, Any]:
        events = self._events_before(before)
        application_ids = sorted(
            {
                p.get("application_id")
                for e in events
                for p in [e.payload]
                if isinstance(p, dict)
                and (
                    e.event_type in ("application.status_changed", "application.revised")
                    or (e.event_type == "assessment.created")
                )
                and p.get("application_id")
            }
            | {
                e.payload["application_id"]
                for e in events
                if e.event_type == "application.submitted"
            }
        )
        return {
            "as_of_before": before,
            "chain_head": events[-1].block_hash() if events else None,
            "event_count": len(events),
            "applications": [self.application_timeline(aid, before) for aid in application_ids],
            "actor_views": {
                e.actor_id: self.actor_view(e.actor_id, before)
                for e in events
                if e.event_type == "actor.registered"
            },
        }
