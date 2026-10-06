"""核心协同服务测试：快照版本、冲突评估、占位、会签、重评估、抢修、披露、审计。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from utility_coordination import (
    Actor,
    AppStatus,
    AuthzError,
    Classification,
    ConfirmationState,
    ConflictAssessment,
    Decision,
    EventLog,
    IncidentReplay,
    MutableClock,
    PipeSnapshot,
    Priority,
    RiskLevel,
    Role,
    ServiceError,
    UtilityType,
    WorkMethod,
)
from utility_coordination.service import CoordinationService

GAS = [(0.0, 50.0), (80.0, 50.0)]
WATER = [(10.0, 10.0), (90.0, 10.0)]
AREA = [(30.0, 30.0), (60.0, 30.0), (60.0, 70.0), (30.0, 70.0)]
WIN = ("2026-10-10T08:00:00Z", "2026-10-20T18:00:00Z")


def make_service(audit_path: str | None = None):
    clock = MutableClock("2026-09-01T00:00:00Z")
    service = CoordinationService(EventLog(audit_path), clock=clock.now)
    coord = service.register_actor(Actor("coord", "协调员", Role.COORDINATOR))
    gas = service.register_actor(
        Actor("gas", "燃气", Role.OWNER, owner_id="gas-bureau", clearance=Classification.CONFIDENTIAL)
    )
    water = service.register_actor(
        Actor("water", "供水", Role.OWNER, owner_id="water-bureau", clearance=Classification.INTERNAL)
    )
    ca = service.register_actor(Actor("ca", "承包商A", Role.CONTRACTOR))
    cb = service.register_actor(Actor("cb", "承包商B", Role.CONTRACTOR))
    auditor = service.register_actor(Actor("aud", "审计", Role.AUDITOR))
    return service, clock, coord, gas, water, ca, cb, auditor


def submit_gas(service, actor, *, geometry=GAS, risk=RiskLevel.HIGH, classification=Classification.CONFIDENTIAL,
               effective_from="2026-01-01T00:00:00Z", pipe_code="gas-17"):
    return service.submit_pipe_snapshot(
        actor,
        pipe_code=pipe_code,
        utility_type=UtilityType.GAS,
        classification=classification,
        risk_level=risk,
        geometry=geometry,
        diameter_mm=200,
        pressure="0.4MPa",
        effective_from=effective_from,
    )


def apply(service, actor, *, area=AREA, window=WIN, method=WorkMethod.OPEN_CUT, radius=10.0,
          priority=Priority.NORMAL, project="proj"):
    return service.apply_for_occupancy(
        actor,
        project_code=project,
        work_area=area,
        road_closure=area,
        method=method,
        window_start=window[0],
        window_end=window[1],
        impact_radius_m=radius,
        priority=priority,
    )


class SnapshotTests(unittest.TestCase):
    def test_version_chain_and_supersedes(self) -> None:
        svc, _, _, gas, *_ = make_service()
        v1 = submit_gas(svc, gas.actor_id)
        v2 = submit_gas(svc, gas.actor_id, geometry=[(0.0, 48.0), (80.0, 48.0)])
        self.assertEqual((v1.version, v2.version), (1, 2))
        self.assertEqual(v2.supersedes, v1.snapshot_id)
        self.assertEqual(v1.snapshot_id, v2.supersedes)

    def test_only_currently_visible_snapshots_are_referenced(self) -> None:
        svc, clock, _, gas, _, ca, *_ = make_service()
        v1 = submit_gas(svc, gas.actor_id)
        app = apply(svc, ca.actor_id)
        asm1 = svc.latest_assessment(app.application_id)
        self.assertEqual([c.snapshot_id for c in asm1.pipe_conflicts], [v1.snapshot_id])
        # 未来生效的快照在当前评估中不可见
        clock.set("2026-09-02T00:00:00Z")
        future = submit_gas(svc, gas.actor_id, geometry=[(0.0, 49.0), (80.0, 49.0)],
                            effective_from="2099-01-01T00:00:00Z")
        asm_now = svc.latest_assessment(app.application_id)
        self.assertNotIn(future.snapshot_id, [c.snapshot_id for c in asm_now.pipe_conflicts])


class ConflictAndPlaceholderTests(unittest.TestCase):
    def test_pipe_conflict_requires_high_risk_cosign(self) -> None:
        svc, _, _, gas, _, ca, *_ = make_service()
        snap = submit_gas(svc, gas.actor_id, risk=RiskLevel.MEDIUM)
        app = apply(svc, ca.actor_id)
        asm = svc.latest_assessment(app.application_id)
        self.assertEqual(len(asm.pipe_conflicts), 1)
        self.assertEqual(asm.pipe_conflicts[0].requirement.value, "confirm")

        svc2, _, _, gas2, _, ca2, *_ = make_service()
        submit_gas(svc2, gas2.actor_id, risk=RiskLevel.HIGH)
        app2 = apply(svc2, ca2.actor_id)
        self.assertEqual(svc2.latest_assessment(app2.application_id).pipe_conflicts[0].requirement.value, "co_sign")

    def test_concurrent_applications_only_one_holds_placeholder(self) -> None:
        svc, _, _, gas, _, ca, cb, *_ = make_service()
        submit_gas(svc, gas.actor_id)
        a = apply(svc, ca.actor_id, project="A")
        b = apply(
            svc, cb.actor_id,
            area=[(35.0, 35.0), (70.0, 35.0), (70.0, 75.0), (35.0, 75.0)],
            project="B",
        )
        self.assertEqual(svc.get_application(a.application_id).status, AppStatus.IN_REVIEW)
        self.assertEqual(svc.get_application(b.application_id).status, AppStatus.OCCUPANCY_BLOCKED)
        oc = svc.latest_assessment(b.application_id).occupancy_conflicts
        self.assertEqual(len(oc), 1)
        self.assertTrue(oc[0].road_closures_overlap)

    def test_disjoint_time_windows_do_not_block(self) -> None:
        svc, _, _, gas, _, ca, cb, *_ = make_service()
        submit_gas(svc, gas.actor_id)
        apply(svc, ca.actor_id, project="A")
        b = apply(svc, cb.actor_id,
                  window=("2026-11-01T08:00:00Z", "2026-11-10T18:00:00Z"), project="B")
        self.assertNotEqual(svc.get_application(b.application_id).status, AppStatus.OCCUPANCY_BLOCKED)

    def test_blocked_application_gets_placeholder_after_release(self) -> None:
        svc, clock, coord, gas, _, ca, cb, _ = make_service()
        snap = submit_gas(svc, gas.actor_id, risk=RiskLevel.LOW,
                          classification=Classification.PUBLIC)
        a = apply(svc, ca.actor_id, project="A")
        b = apply(svc, cb.actor_id,
                  area=[(35.0, 35.0), (70.0, 35.0), (70.0, 75.0), (35.0, 75.0)], project="B")
        # 推进至 48h 回复期之后，低风险逾期留痕收口
        clock.advance(hours=49)
        svc.expire_pending_responses("system")
        svc.issue_permit(coord.actor_id, a.application_id)
        self.assertEqual(svc.get_application(b.application_id).status, AppStatus.OCCUPANCY_BLOCKED)
        # A 完工释放占位 → B 自动重评估
        svc.complete_project(coord.actor_id, a.application_id)
        self.assertIn(
            svc.get_application(b.application_id).status,
            (AppStatus.IN_REVIEW, AppStatus.REVIEW_CLOSED),
        )

    def test_trenchless_has_smaller_reach(self) -> None:
        svc, _, _, gas, _, ca, *_ = make_service()
        # 管段距工作区约 8m：明挖（10m 半径）命中，非开挖（半径减半=5m）不命中
        submit_gas(svc, gas.actor_id, geometry=[(0.0, 78.0), (80.0, 78.0)])
        open_cut = apply(svc, ca.actor_id, method=WorkMethod.OPEN_CUT, project="open")
        self.assertEqual(len(svc.latest_assessment(open_cut.application_id).pipe_conflicts), 1)
        trenchless = apply(svc, ca.actor_id, method=WorkMethod.TRENCHLESS, project="trench")
        self.assertEqual(len(svc.latest_assessment(trenchless.application_id).pipe_conflicts), 0)


class ReviewAndPermitTests(unittest.TestCase):
    def test_high_risk_no_response_never_defaults_safe(self) -> None:
        svc, clock, coord, gas, _, ca, *_ = make_service()
        snap = submit_gas(svc, gas.actor_id, risk=RiskLevel.HIGH)
        app = apply(svc, ca.actor_id)
        # 截止前不能签发
        with self.assertRaises(ServiceError):
            svc.issue_permit(coord.actor_id, app.application_id)
        clock.advance(hours=49)
        blocked = svc.expire_pending_responses("system")
        self.assertEqual(blocked, [app.application_id])
        # 逾期后高风险仍阻断
        with self.assertRaises(ServiceError):
            svc.issue_permit(coord.actor_id, app.application_id)
        # 权属回复后才可签发
        svc.respond_pipe(gas.actor_id, app.application_id, snap.snapshot_id, Decision.APPROVE)
        permit = svc.issue_permit(coord.actor_id, app.application_id)
        self.assertTrue(permit.valid)

    def test_low_risk_timeout_closes_without_owner_backing(self) -> None:
        svc, clock, coord, gas, _, ca, *_ = make_service()
        submit_gas(svc, gas.actor_id, risk=RiskLevel.LOW, classification=Classification.PUBLIC)
        app = apply(svc, ca.actor_id)
        clock.advance(hours=49)
        svc.expire_pending_responses("system")
        permit = svc.issue_permit(coord.actor_id, app.application_id)
        recs = [r for r in svc._confirmations if r.application_id == app.application_id]
        self.assertEqual(recs[0].state, ConfirmationState.NO_RESPONSE_LOW_RISK)
        self.assertIsNone(recs[0].decided_by)
        self.assertTrue(permit.valid)

    def test_rejection_denies_application(self) -> None:
        svc, _, _, gas, _, ca, *_ = make_service()
        snap = submit_gas(svc, gas.actor_id)
        app = apply(svc, ca.actor_id)
        svc.respond_pipe(gas.actor_id, app.application_id, snap.snapshot_id, Decision.REJECT, "禁止明挖")
        self.assertEqual(svc.get_application(app.application_id).status, AppStatus.DENIED)
        with self.assertRaises(ServiceError):
            svc.issue_permit("coord", app.application_id)

    def test_owner_can_only_respond_own_pipes(self) -> None:
        svc, _, _, gas, water, ca, *_ = make_service()
        snap = submit_gas(svc, gas.actor_id)
        app = apply(svc, ca.actor_id)
        with self.assertRaises(AuthzError):
            svc.respond_pipe(water.actor_id, app.application_id, snap.snapshot_id, Decision.APPROVE)


class RevisionAndUpdateTests(unittest.TestCase):
    def _approved_app(self, svc, coord, gas, ca):
        snap = submit_gas(svc, gas.actor_id)
        app = apply(svc, ca.actor_id)
        svc.respond_pipe(gas.actor_id, app.application_id, snap.snapshot_id, Decision.APPROVE, "v1 同意")
        permit = svc.issue_permit(coord.actor_id, app.application_id)
        return snap, app, permit

    def test_extension_reevaluates_and_carries_opinion(self) -> None:
        svc, _, coord, gas, _, ca, *_ = make_service()
        snap, app, permit = self._approved_app(svc, coord, gas, ca)
        asm2 = svc.revise_application(
            ca.actor_id, app.application_id,
            window_start=WIN[0], window_end="2026-10-25T18:00:00Z",
            change_note="延期 5 天",
        )
        self.assertEqual(asm2.revision, 2)
        self.assertEqual(asm2.cause, "revision")
        carried = asm2.pipe_conflicts[0]
        self.assertEqual(carried.confirmation_state, ConfirmationState.CARRIED)
        self.assertEqual(carried.snapshot_id, snap.snapshot_id)
        # 旧许可证因变更失效，需重新签发
        self.assertFalse(svc.permits_for(app.application_id)[0].valid)
        new_permit = svc.issue_permit(coord.actor_id, app.application_id)
        self.assertTrue(new_permit.valid)
        # 历史评估原样保留，仍引用同一快照
        history = svc.assessment_history(app.application_id)
        self.assertEqual([a.revision for a in history], [1, 2])
        self.assertEqual(history[0].pipe_conflicts[0].snapshot_id, snap.snapshot_id)

    def test_snapshot_update_forces_new_cosign_on_new_version(self) -> None:
        svc, _, coord, gas, _, ca, *_ = make_service()
        v1, app, _ = self._approved_app(svc, coord, gas, ca)
        before = svc.latest_assessment(app.application_id)
        v2 = submit_gas(svc, gas.actor_id, geometry=[(0.0, 46.0), (80.0, 46.0)])
        after = svc.latest_assessment(app.application_id)
        self.assertNotEqual(before.assessment_id, after.assessment_id)
        self.assertEqual(after.cause, "snapshot_update")
        self.assertEqual([c.snapshot_id for c in after.pipe_conflicts], [v2.snapshot_id])
        # 新快照版本不沿用旧意见，必须重新会签
        self.assertEqual(after.pipe_conflicts[0].confirmation_state, ConfirmationState.PENDING)
        with self.assertRaises(ServiceError):
            svc.issue_permit(coord.actor_id, app.application_id)
        # 已完成的旧审核仍引用原始快照
        self.assertEqual(before.pipe_conflicts[0].snapshot_id, v1.snapshot_id)

    def test_suspend_and_resume_reevaluates(self) -> None:
        svc, _, coord, gas, _, ca, *_ = make_service()
        snap, app, permit = self._approved_app(svc, coord, gas, ca)
        svc.suspend_permit(coord.actor_id, app.application_id, "重大活动保障")
        self.assertEqual(svc.get_application(app.application_id).status, AppStatus.SUSPENDED)
        self.assertFalse(svc.permits_for(app.application_id)[-1].valid)
        asm = svc.resume_after_suspension(coord.actor_id, app.application_id)
        self.assertEqual(asm.cause, "resume")
        self.assertIn(svc.get_application(app.application_id).status,
                      (AppStatus.IN_REVIEW, AppStatus.REVIEW_CLOSED))


class EmergencyTests(unittest.TestCase):
    def _issued(self, svc, coord, gas, ca):
        snap = submit_gas(svc, gas.actor_id)
        app = apply(svc, ca.actor_id)
        svc.respond_pipe(gas.actor_id, app.application_id, snap.snapshot_id, Decision.APPROVE)
        svc.issue_permit(coord.actor_id, app.application_id)
        return app

    def test_emergency_preempts_and_blocks_unsafe_issue(self) -> None:
        svc, _, coord, gas, _, ca, cb, _ = make_service()
        app = self._issued(svc, coord, gas, ca)
        emergency = svc.emergency_repair(
            cb.actor_id,
            project_code="emergency-repair",
            work_area=[(38.0, 40.0), (55.0, 40.0), (55.0, 60.0), (38.0, 60.0)],
            window_start="2026-10-15T02:00:00Z",
            window_end="2026-10-15T14:00:00Z",
            impact_radius_m=8.0,
            description="爆管抢修",
        )
        self.assertEqual(svc.get_application(app.application_id).status, AppStatus.SUSPENDED)
        self.assertEqual(svc.latest_assessment(emergency.application_id).revision, 1)
        # 高风险未会签：紧急也不能签发
        with self.assertRaises(ServiceError):
            svc.issue_permit(coord.actor_id, emergency.application_id)
        # 书面风险确认也不能替代高风险会签
        with self.assertRaises(ServiceError):
            svc.issue_permit(coord.actor_id, emergency.application_id,
                             emergency_acknowledgement="指挥长要求立即施工")
        # 快速会签（4h 窗口）后签发，override 为空
        gas_snap = svc.latest_assessment(emergency.application_id).pipe_conflicts[0].snapshot_id
        svc.respond_pipe(gas.actor_id, emergency.application_id, gas_snap, Decision.APPROVE, "降压配合")
        permit = svc.issue_permit(coord.actor_id, emergency.application_id,
                                  emergency_acknowledgement="指挥长要求立即施工")
        self.assertTrue(permit.valid)
        self.assertIsNone(permit.emergency_override)

    def test_emergency_acknowledgement_recorded_for_low_risk_pending(self) -> None:
        svc, _, coord, gas, _, ca, cb, _ = make_service()
        # 仅低风险管线
        submit_gas(svc, gas.actor_id, risk=RiskLevel.LOW, classification=Classification.PUBLIC)
        app = apply(svc, ca.actor_id, priority=Priority.EMERGENCY)
        # 未到 4h 收口期，低风险未回复：凭书面确认签发并留痕
        permit = svc.issue_permit(
            coord.actor_id, app.application_id,
            emergency_acknowledgement="已电话告知供水值班，指挥长书面担责",
        )
        self.assertTrue(permit.valid)
        self.assertIn("书面担责", permit.emergency_override)


class DisclosureTests(unittest.TestCase):
    def test_confidential_geometry_redacted_by_role(self) -> None:
        svc, _, _, gas, _, ca, _, aud = make_service()
        snap = submit_gas(svc, gas.actor_id)
        app = apply(svc, ca.actor_id)
        # 权属单位本人看得到精确几何
        own = svc.view_snapshot(gas.actor_id, snap.snapshot_id)
        self.assertIsNotNone(own["geometry"])
        self.assertFalse(own["redacted"])
        # 工程方看不到敏感坐标
        redacted = svc.view_snapshot(ca.actor_id, snap.snapshot_id)
        self.assertIsNone(redacted["geometry"])
        # 工程方视角：管段标识与权属被掩码，距离粗化到 5m
        asm = svc.latest_assessment(app.application_id)
        view = svc.view_assessment(ca.actor_id, asm.assessment_id)
        pc = view["pipe_conflicts"][0]
        self.assertTrue(pc["geometry_redacted"])
        self.assertIsNone(pc["owner_id"])
        self.assertEqual(pc["pipe_code"], "redacted-gas")
        self.assertEqual(pc["distance_m"] % 5, 0)
        # 审计可见完整信息
        aud_view = svc.view_assessment(aud.actor_id, asm.assessment_id)
        self.assertFalse(aud_view["pipe_conflicts"][0]["geometry_redacted"])
        self.assertEqual(aud_view["pipe_conflicts"][0]["owner_id"], "gas-bureau")


class AuditReplayTests(unittest.TestCase):
    def test_chain_persists_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "audit.jsonl")
            svc, _, coord, gas, _, ca, *_ = make_service(path)
            snap = submit_gas(svc, gas.actor_id)
            app = apply(svc, ca.actor_id)
            svc.respond_pipe(gas.actor_id, app.application_id, snap.snapshot_id, Decision.APPROVE)
            svc.issue_permit(coord.actor_id, app.application_id)
            svc.log.verify_chain()
            head = svc.log.head_hash()
            # 用新实例从磁盘重放
            reloaded = EventLog(path)
            reloaded.verify_chain()
            self.assertEqual(reloaded.head_hash(), head)
            # 篡改一行
            lines = Path(path).read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[3])
            row["payload"]["x"] = "tampered"
            lines[3] = json.dumps(row, ensure_ascii=False)
            Path(path).write_text("\n".join(lines), encoding="utf-8")
            with self.assertRaises(ValueError):
                EventLog(path)

    def test_replay_restores_actor_views_and_timeline_before_incident(self) -> None:
        svc, clock, coord, gas, _, ca, cb, _ = make_service()
        snap = submit_gas(svc, gas.actor_id)
        app = apply(svc, ca.actor_id)
        svc.respond_pipe(gas.actor_id, app.application_id, snap.snapshot_id, Decision.APPROVE, "注意燃压")
        svc.issue_permit(coord.actor_id, app.application_id)
        moment = "2026-09-02T00:00:00Z"
        replay = IncidentReplay(svc.log)
        gas_view = replay.actor_view(gas.actor_id, moment)
        self.assertEqual(len(gas_view["decisions"]), 1)
        self.assertEqual(gas_view["decisions"][0]["comment"], "注意燃压")
        self.assertGreaterEqual(len(gas_view["notifications_received"]), 1)
        timeline = replay.application_timeline(app.application_id, moment)
        self.assertEqual(len(timeline["assessments"]), 1)
        self.assertEqual(timeline["assessments"][0]["referenced_snapshots"][0]["snapshot_id"],
                         snap.snapshot_id)
        self.assertEqual(len(timeline["permits"]), 1)
        # 事故时刻早于任何事件时为空
        early = IncidentReplay(svc.log).application_timeline(app.application_id, "2026-01-01T00:00:00Z")
        self.assertEqual(early["assessments"], [])
        # 通知以接收方视角固化（工程方收到的燃气通知应已掩码）
        contractor_view = replay.actor_view(ca.actor_id, moment)
        bodies = "\n".join(n["body"] for n in contractor_view["notifications_received"])
        self.assertNotIn(snap.snapshot_id, bodies)

    def test_replay_incident_report_covers_all_applications(self) -> None:
        svc, _, _, gas, _, ca, cb, _ = make_service()
        submit_gas(svc, gas.actor_id)
        apply(svc, ca.actor_id, project="A")
        apply(svc, cb.actor_id, area=[(200.0, 200.0), (240.0, 200.0), (240.0, 240.0), (200.0, 240.0)],
              project="B")
        report = IncidentReplay(svc.log).incident_report()
        self.assertEqual(len(report["applications"]), 2)
        self.assertIn("ca", report["actor_views"])


if __name__ == "__main__":
    unittest.main()
