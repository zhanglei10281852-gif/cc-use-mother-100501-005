"""协同服务主流程测试：冲突计算、审批链、重新评估、并发占位与最小披露。"""

import threading
import unittest
from datetime import datetime, timedelta, timezone

from utility_coordination import (
    Actor,
    ApplicationState,
    AssessmentTrigger,
    CoordinationService,
    OccupancyConflictError,
    PermissionDenied,
    PermitState,
    Role,
    StateError,
)

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs: float) -> None:
        self.t += timedelta(**kwargs)


def dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = CoordinationService(clock=self.clock)
        self.coordinator = Actor("coord-1", Role.COORDINATOR)
        self.gas = Actor("gas-ops", Role.UTILITY_OWNER, owner_id="gas-corp")
        self.water = Actor("water-ops", Role.UTILITY_OWNER, owner_id="water-corp")
        self.builder = Actor("builder-1", Role.CONTRACTOR)
        self.builder2 = Actor("builder-2", Role.CONTRACTOR)
        self.auditor = Actor("audit-1", Role.AUDITOR)

    def submit_gas_line(self, **overrides: object) -> str:
        params: dict[str, object] = dict(
            segment_id="gas-01",
            utility="gas",
            confidentiality="internal",
            path=[(0.0, 0.0), (100.0, 0.0)],
            depth_top=1.2,
            depth_bottom=1.8,
            effective_from=dt(1),
            risk="high",
        )
        params.update(overrides)
        self.service.submit_segment_snapshot(self.gas, **params)  # type: ignore[arg-type]
        return str(params["segment_id"])

    def apply(self, actor: Actor, request_id: str, **overrides: object):
        params: dict[str, object] = dict(
            request_id=request_id,
            project_code="road-rebuild",
            path=[(40.0, -3.0), (60.0, 3.0)],
            impact_radius=2.0,
            method="open_cut",
            depth_top=0.5,
            depth_bottom=2.0,
            window_start=dt(2, 8),
            window_end=dt(6, 18),
        )
        params.update(overrides)
        return self.service.apply_occupancy(actor, **params)  # type: ignore[arg-type]


class ConflictScreeningTests(ServiceTestBase):
    def test_spatial_and_temporal_hit(self) -> None:
        self.submit_gas_line()
        app, assessment = self.apply(self.builder, "req-1")
        self.assertEqual(app.state, ApplicationState.CONFIRMING)
        self.assertEqual(len(assessment.utility_hits), 1)
        hit = assessment.utility_hits[0]
        self.assertEqual(hit.ref_id, "gas-01")
        self.assertEqual(hit.owner_id, "gas-corp")
        self.assertGreater(hit.required_separation, hit.min_distance)

    def test_distant_corridor_has_no_hit(self) -> None:
        self.submit_gas_line()
        app, assessment = self.apply(self.builder, "req-2", path=[(40.0, -60.0), (60.0, -60.0)])
        self.assertEqual(app.state, ApplicationState.CO_SIGNING)
        self.assertEqual(assessment.hits, ())

    def test_depth_separation_avoids_hit(self) -> None:
        self.submit_gas_line()
        _, assessment = self.apply(self.builder, "req-3", depth_top=5.0, depth_bottom=6.0)
        self.assertEqual(assessment.utility_hits, ())

    def test_snapshot_outside_effective_period_is_ignored(self) -> None:
        self.submit_gas_line(effective_from=dt(20), effective_to=dt(25))
        _, assessment = self.apply(self.builder, "req-4")
        self.assertEqual(assessment.utility_hits, ())
        self.assertEqual(assessment.snapshot_refs, ())

    def test_screening_uses_only_snapshots_visible_at_that_time(self) -> None:
        _, first = self.apply(self.builder, "req-5")
        self.assertEqual(first.snapshot_refs, ())
        self.clock.advance(hours=1)
        self.submit_gas_line()
        _, second = self.apply(self.builder, "req-6")
        self.assertEqual(second.snapshot_refs, (("gas-01", 1),))
        # 已完成的评估仍引用当时可见的版本集合
        self.assertEqual(self.service.get_assessment(first.assessment_id).snapshot_refs, ())


class ApprovalFlowTests(ServiceTestBase):
    def test_unconfirmed_high_risk_blocks_issuance(self) -> None:
        self.submit_gas_line()
        app, _ = self.apply(self.builder, "req-10")
        with self.assertRaisesRegex(StateError, "不得默认为安全"):
            self.service.issue_permit(self.coordinator, app.application_id)
        self.service.confirm_segment(self.gas, app.application_id, "gas-01", accept=True)
        self.service.co_sign(self.coordinator, app.application_id, approve=True)
        permit = self.service.issue_permit(self.coordinator, app.application_id)
        self.assertEqual(permit.state, PermitState.ACTIVE)
        self.assertFalse(permit.provisional)

    def test_cosign_requires_completed_confirmations(self) -> None:
        self.submit_gas_line()
        app, _ = self.apply(self.builder, "req-11")
        with self.assertRaisesRegex(StateError, "权属确认未齐全"):
            self.service.co_sign(self.coordinator, app.application_id, approve=True)

    def test_owner_rejection_rejects_application(self) -> None:
        self.submit_gas_line()
        app, _ = self.apply(self.builder, "req-12")
        self.service.confirm_segment(self.gas, app.application_id, "gas-01",
                                     accept=False, comment="与次高压管线净距不足")
        self.assertEqual(self.service.get_application(app.application_id).state,
                         ApplicationState.REJECTED)

    def test_wrong_owner_cannot_confirm(self) -> None:
        self.submit_gas_line()
        app, _ = self.apply(self.builder, "req-13")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_segment(self.water, app.application_id, "gas-01", accept=True)

    def test_contractor_cannot_submit_segment(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.service.submit_segment_snapshot(
                self.builder, segment_id="x", utility="water", confidentiality="public",
                path=[(0, 0), (1, 1)], depth_top=1.0, depth_bottom=2.0,
                effective_from=dt(1), risk="low", owner_id="water-corp",
            )

    def test_idempotent_apply_and_issue(self) -> None:
        app1, _ = self.apply(self.builder, "req-14")
        app2, _ = self.apply(self.builder, "req-14")
        self.assertEqual(app1.application_id, app2.application_id)
        self.service.co_sign(self.coordinator, app1.application_id, approve=True)
        permit1 = self.service.issue_permit(self.coordinator, app1.application_id)
        permit2 = self.service.issue_permit(self.coordinator, app1.application_id)
        self.assertEqual(permit1.permit_id, permit2.permit_id)


class ReevaluationTests(ServiceTestBase):
    def _approved(self, request_id: str = "req-20", actor: Actor | None = None, **overrides: object):
        app, _ = self.apply(actor or self.builder, request_id, **overrides)
        self.service.co_sign(self.coordinator, app.application_id, approve=True)
        permit = self.service.issue_permit(self.coordinator, app.application_id)
        return app, permit

    def test_completed_assessment_keeps_original_snapshot_refs(self) -> None:
        self.submit_gas_line()
        app, first = self.apply(self.builder, "req-21")
        self.assertEqual(first.snapshot_refs, (("gas-01", 1),))
        self.clock.advance(hours=1)
        self.submit_gas_line()  # 权属单位更新为第 2 版
        changed, second = self.service.design_change(
            self.builder, app.application_id, path=[(40.0, -2.0), (60.0, 2.0)]
        )
        self.assertEqual(second.trigger, AssessmentTrigger.DESIGN_CHANGE)
        self.assertEqual(second.snapshot_refs, (("gas-01", 2),))
        # 已完成的评估仍引用原始快照版本
        self.assertEqual(self.service.get_assessment(first.assessment_id).snapshot_refs,
                         (("gas-01", 1),))
        self.assertEqual(changed.revision, 2)

    def test_design_change_suspends_permit_and_requires_reapproval(self) -> None:
        self.submit_gas_line()
        app, _ = self.apply(self.builder, "req-22")
        self.service.confirm_segment(self.gas, app.application_id, "gas-01", accept=True)
        self.service.co_sign(self.coordinator, app.application_id, approve=True)
        permit = self.service.issue_permit(self.coordinator, app.application_id)

        self.service.design_change(self.builder, app.application_id,
                                   path=[(40.0, -1.0), (60.0, 1.0)])
        self.assertEqual(self.service.get_permit(permit.permit_id).state, PermitState.SUSPENDED)
        self.assertEqual(self.service.get_application(app.application_id).state,
                         ApplicationState.CONFIRMING)
        # 新一轮确认与会签后才能恢复
        self.service.confirm_segment(self.gas, app.application_id, "gas-01", accept=True)
        self.service.co_sign(self.coordinator, app.application_id, approve=True)
        resumed = self.service.resume_permit(self.coordinator, permit.permit_id)
        self.assertEqual(resumed.state, PermitState.ACTIVE)
        self.assertEqual(resumed.revision, permit.revision + 2)

    def test_delay_reevaluates_and_suspends(self) -> None:
        app, permit = self._approved("req-23")
        changed, assessment = self.service.delay_project(
            self.builder, app.application_id, new_window_end=dt(9, 18)
        )
        self.assertEqual(assessment.trigger, AssessmentTrigger.DELAY)
        self.assertEqual(self.service.get_permit(permit.permit_id).state, PermitState.SUSPENDED)
        self.assertEqual(changed.window_end, dt(9, 18))

    def test_suspension_reevaluates_subsequent_occupancies(self) -> None:
        app_a, permit_a = self._approved("req-24")
        app_b, _ = self.apply(self.builder2, "req-25",
                              path=[(10.0, 30.0), (20.0, 30.0)])
        self.assertEqual(app_b.state, ApplicationState.CO_SIGNING)
        before = self.service._latest_assessment(app_b.application_id)
        self.clock.advance(hours=2)
        self.service.suspend_permit(self.coordinator, permit_a.permit_id, reason="管线迁改")
        after = self.service._latest_assessment(app_b.application_id)
        self.assertNotEqual(before.assessment_id, after.assessment_id)
        self.assertEqual(after.trigger, AssessmentTrigger.SUSPENSION)
        notified = [n for n in self.service.store.notifications
                    if n.recipient == "builder-2" and n.kind == "reevaluated"]
        self.assertTrue(notified)

    def test_emergency_repair_preempts_and_flags_unconfirmed_risk(self) -> None:
        self.submit_gas_line()
        app_a, permit_a = self._approved("req-26", path=[(10.0, 40.0), (20.0, 40.0)])
        self.clock.advance(hours=1)
        emergency_app, provisional = self.service.emergency_repair(
            self.gas,
            request_id="req-emg-1",
            project_code="gas-leak-fix",
            path=[(45.0, -2.0), (55.0, 2.0)],
            impact_radius=3.0,
            method="open_cut",
            depth_top=0.8,
            depth_bottom=2.2,
            window_start=dt(3, 0),
            window_end=dt(4, 0),
        )
        self.assertTrue(provisional.provisional)
        self.assertEqual(provisional.state, PermitState.PROVISIONAL)
        # 高风险管段未确认：留下警示与加急通知，绝不默认安全
        urgent = [n for n in self.service.store.notifications
                  if n.kind == "urgent_confirmation_requested" and n.recipient == "gas-corp"]
        self.assertTrue(urgent)
        warnings = [n for n in self.service.store.notifications
                    if n.kind == "unconfirmed_high_risk"]
        self.assertTrue(warnings)
        # 与在册许可时空相交时被抢占暂停（不相交的许可不受影响）
        self.assertEqual(self.service.get_permit(permit_a.permit_id).state, PermitState.ACTIVE)

    def test_emergency_preempts_overlapping_permit(self) -> None:
        app_a, permit_a = self._approved("req-28", path=[(40.0, -3.0), (60.0, 3.0)])
        _, provisional = self.service.emergency_repair(
            self.gas,
            request_id="req-emg-2",
            project_code="gas-leak-fix-2",
            path=[(45.0, -2.0), (55.0, 2.0)],
            impact_radius=3.0,
            method="open_cut",
            depth_top=0.8,
            depth_bottom=2.2,
            window_start=dt(3, 0),
            window_end=dt(4, 0),
        )
        self.assertEqual(self.service.get_permit(permit_a.permit_id).state, PermitState.SUSPENDED)
        notified = [n for n in self.service.store.notifications
                    if n.recipient == "builder-1" and n.kind == "permit_suspended"]
        self.assertTrue(notified)

    def test_complete_work_releases_occupancy(self) -> None:
        app_a, permit_a = self._approved("req-29")
        done = self.service.complete_work(self.builder, permit_a.permit_id)
        self.assertEqual(done.state, PermitState.COMPLETED)
        app_b, _ = self.apply(self.builder2, "req-30")
        self.service.co_sign(self.coordinator, app_b.application_id, approve=True)
        permit_b = self.service.issue_permit(self.coordinator, app_b.application_id)
        self.assertEqual(permit_b.state, PermitState.ACTIVE)


class ConcurrencyTests(ServiceTestBase):
    def test_only_one_legal_occupancy_for_overlapping_applications(self) -> None:
        app_a, _ = self.apply(self.builder, "req-40")
        app_b, _ = self.apply(self.builder2, "req-41")
        self.service.co_sign(self.coordinator, app_a.application_id, approve=True)
        self.service.co_sign(self.coordinator, app_b.application_id, approve=True)
        self.service.issue_permit(self.coordinator, app_a.application_id)
        with self.assertRaises(OccupancyConflictError):
            self.service.issue_permit(self.coordinator, app_b.application_id)

    def test_concurrent_issue_has_exactly_one_winner(self) -> None:
        app_a, _ = self.apply(self.builder, "req-42")
        app_b, _ = self.apply(self.builder2, "req-43")
        self.service.co_sign(self.coordinator, app_a.application_id, approve=True)
        self.service.co_sign(self.coordinator, app_b.application_id, approve=True)

        results: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def issue(app_id: str, key: str) -> None:
            barrier.wait()
            try:
                self.service.issue_permit(self.coordinator, app_id)
                results[key] = "ok"
            except OccupancyConflictError:
                results[key] = "conflict"

        threads = [
            threading.Thread(target=issue, args=(app_a.application_id, "a")),
            threading.Thread(target=issue, args=(app_b.application_id, "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results.values()), ["conflict", "ok"])
        active = [p for p in self.service._active_permits()]
        self.assertEqual(len(active), 1)


class DisclosureTests(ServiceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.service.submit_segment_snapshot(
            self.gas, segment_id="gas-public", utility="gas", confidentiality="public",
            path=[(0.0, 0.0), (10.0, 0.0)], depth_top=1.0, depth_bottom=2.0,
            effective_from=dt(1), risk="medium",
        )
        self.service.submit_segment_snapshot(
            self.gas, segment_id="gas-internal", utility="gas", confidentiality="internal",
            path=[(13.0, 27.0), (61.0, 99.0)], depth_top=1.3, depth_bottom=2.4,
            effective_from=dt(1), risk="high",
        )
        self.service.submit_segment_snapshot(
            self.gas, segment_id="gas-secret", utility="gas", confidentiality="secret",
            path=[(123.4, 456.7), (234.5, 567.8)], depth_top=0.9, depth_bottom=1.5,
            effective_from=dt(1), risk="high",
        )

    def _view(self, actor: Actor) -> dict[str, dict]:
        return {s["segment_id"]: s for s in self.service.list_segments(actor)}

    def test_contractor_minimal_disclosure(self) -> None:
        view = self._view(self.builder)
        self.assertEqual(view["gas-public"]["disclosure"], "full")
        self.assertEqual(view["gas-public"]["path"], [[0.0, 0.0], [10.0, 0.0]])
        self.assertEqual(view["gas-internal"]["disclosure"], "fuzzed")
        self.assertEqual(view["gas-internal"]["path"], [[0.0, 50.0], [50.0, 100.0]])
        secret = view["gas-secret"]
        self.assertEqual(secret["disclosure"], "withheld")
        self.assertIsNone(secret["path"])
        self.assertEqual(secret["risk"], "high")  # 风险等级仍披露，保障施工安全

    def test_owner_sees_own_full_geometry(self) -> None:
        view = self._view(self.gas)
        self.assertEqual(view["gas-secret"]["disclosure"], "full")
        self.assertEqual(view["gas-secret"]["path"], [[123.4, 456.7], [234.5, 567.8]])

    def test_other_owner_gets_redacted(self) -> None:
        view = self._view(self.water)
        self.assertEqual(view["gas-secret"]["disclosure"], "withheld")
        self.assertEqual(view["gas-internal"]["disclosure"], "fuzzed")

    def test_coordinator_and_auditor_see_full(self) -> None:
        for actor in (self.coordinator, self.auditor):
            view = self._view(actor)
            self.assertEqual(view["gas-secret"]["disclosure"], "full")


if __name__ == "__main__":
    unittest.main()
