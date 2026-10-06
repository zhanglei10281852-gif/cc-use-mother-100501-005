"""审计重建测试：还原参与方在事发前看到的版本、警示、决定与通知。"""

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from utility_coordination import (
    Actor,
    CoordinationService,
    PermissionDenied,
    Role,
    participant_view,
    verify_audit_trail,
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


class AuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = CoordinationService(clock=self.clock)
        self.coordinator = Actor("coord-1", Role.COORDINATOR)
        self.gas = Actor("gas-ops", Role.UTILITY_OWNER, owner_id="gas-corp")
        self.builder = Actor("builder-1", Role.CONTRACTOR)
        self.auditor = Actor("audit-1", Role.AUDITOR)

        self.service.submit_segment_snapshot(
            self.gas, segment_id="gas-01", utility="gas", confidentiality="secret",
            path=[(0.0, 0.0), (100.0, 0.0)], depth_top=1.2, depth_bottom=1.8,
            effective_from=dt(1), risk="high",
        )
        self.app, self.assessment = self.service.apply_occupancy(
            self.builder,
            request_id="req-audit-1",
            project_code="road-rebuild",
            path=[(40.0, -3.0), (60.0, 3.0)],
            impact_radius=2.0,
            method="open_cut",
            depth_top=0.5,
            depth_bottom=2.0,
            window_start=dt(2, 8),
            window_end=dt(6, 18),
        )
        self.t_screened = self.clock.t
        self.clock.advance(hours=1)
        self.service.confirm_segment(self.gas, self.app.application_id, "gas-01", accept=True)
        self.service.co_sign(self.coordinator, self.app.application_id, approve=True)
        self.clock.advance(hours=1)
        self.permit = self.service.issue_permit(self.coordinator, self.app.application_id)

    def test_view_before_confirmation_shows_unconfirmed_warning(self) -> None:
        view = participant_view(self.service, self.auditor, "builder-1", at=self.t_screened)
        kinds = {w["kind"] for w in view["warnings"]}
        self.assertIn("unconfirmed_high_risk", kinds)
        self.assertEqual(view["decisions"]["permits"], [])
        self.assertEqual(view["decisions"]["confirmations"], [])
        # 当时可见的评估版本引用当时的快照
        self.assertEqual(view["assessments"][0]["snapshot_refs"], [["gas-01", 1]])

    def test_view_after_issuance_contains_decisions_and_notifications(self) -> None:
        view = participant_view(self.service, self.auditor, "builder-1")
        self.assertEqual(len(view["decisions"]["permits"]), 1)
        self.assertEqual(view["decisions"]["permits"][0]["state"], "active")
        self.assertEqual(len(view["decisions"]["confirmations"]), 1)
        self.assertEqual(len(view["decisions"]["endorsements"]), 1)
        kinds = {n["kind"] for n in view["notifications"]}
        self.assertIn("permit_issued", kinds)
        self.assertIn("conflicts_found", kinds)
        # 高风险已确认，不再出现未确认警示
        self.assertNotIn("unconfirmed_high_risk", {w["kind"] for w in view["warnings"]})

    def test_view_redacts_coordinates_by_participant_role(self) -> None:
        contractor_view = participant_view(self.service, self.auditor, "builder-1")
        secret = next(s for s in contractor_view["segments"] if s["segment_id"] == "gas-01")
        self.assertEqual(secret["disclosure"], "withheld")
        self.assertIsNone(secret["path"])
        owner_view = participant_view(self.service, self.auditor, "gas-ops")
        own = next(s for s in owner_view["segments"] if s["segment_id"] == "gas-01")
        self.assertEqual(own["disclosure"], "full")

    def test_participant_can_view_self_but_not_others(self) -> None:
        view = participant_view(self.service, self.builder, "builder-1")
        self.assertEqual(view["participant"]["actor_id"], "builder-1")
        with self.assertRaises(PermissionDenied):
            participant_view(self.service, self.builder, "gas-ops")

    def test_unknown_participant_rejected(self) -> None:
        with self.assertRaises(Exception):
            participant_view(self.service, self.auditor, "nobody")

    def test_gas_owner_view_contains_confirmation_request(self) -> None:
        view = participant_view(self.service, self.auditor, "gas-ops")
        kinds = {n["kind"] for n in view["notifications"]}
        self.assertIn("confirmation_requested", kinds)
        self.assertIn("permit_issued", kinds)

    def test_audit_chain_valid_and_tamper_evident(self) -> None:
        status = verify_audit_trail(self.service)
        self.assertTrue(status["chain_valid"])
        self.assertGreater(status["events"], 0)
        store = self.service.store
        store.events[2] = replace(store.events[2], kind="forged")
        self.assertFalse(store.verify_chain())


if __name__ == "__main__":
    unittest.main()
