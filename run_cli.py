"""地下管网施工冲突协同命令行冒烟入口。

演示一条最小闭环：权属单位提交管段快照 → 工程方申请占用并命中冲突 →
权属确认 → 风险会签 → 签发许可证 → 审计员还原工程方在签发前看到的视图。
"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from utility_coordination import (
    Actor,
    CoordinationService,
    Role,
    participant_view,
    parse_dt,
    verify_audit_trail,
)


def main() -> None:
    service = CoordinationService()
    coordinator = Actor("coord-1", Role.COORDINATOR)
    gas_owner = Actor("gas-ops-1", Role.UTILITY_OWNER, owner_id="gas-corp")
    contractor = Actor("builder-1", Role.CONTRACTOR)
    auditor = Actor("audit-1", Role.AUDITOR)

    snapshot = service.submit_segment_snapshot(
        gas_owner,
        segment_id="gas-line-01",
        utility="gas",
        confidentiality="internal",
        path=[(0.0, 0.0), (100.0, 0.0)],
        depth_top=1.2,
        depth_bottom=1.8,
        effective_from=parse_dt("2026-01-01T00:00:00+00:00"),
        risk="high",
    )
    application, assessment = service.apply_occupancy(
        contractor,
        request_id="req-demo-001",
        project_code="road-rebuild-07",
        path=[(40.0, -3.0), (60.0, 3.0)],
        impact_radius=2.0,
        method="open_cut",
        depth_top=0.5,
        depth_bottom=2.0,
        window_start=parse_dt("2026-11-01T08:00:00+00:00"),
        window_end=parse_dt("2026-11-10T18:00:00+00:00"),
    )
    service.confirm_segment(gas_owner, application.application_id, snapshot.segment_id,
                            accept=True, comment="已现场交底，按方案施工")
    service.co_sign(coordinator, application.application_id, approve=True, comment="同意，注意第三方监测")
    permit = service.issue_permit(coordinator, application.application_id)
    view = participant_view(service, auditor, "builder-1")

    print(json.dumps({
        "permit": asdict(permit),
        "assessment_hits": [asdict(h) for h in assessment.hits],
        "builder_view_warnings": view["warnings"],
        "audit": verify_audit_trail(service),
    }, ensure_ascii=False, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
