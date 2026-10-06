"""端到端演示场景：道路改造命中燃气支线的完整协同流程。

同时被 CLI、冒烟脚本与集成测试使用。场景按时间推进，最后可直接做事故还原。
"""

from __future__ import annotations

from typing import Any

from .models import (
    Actor,
    Classification,
    Decision,
    Priority,
    RiskLevel,
    Role,
    UtilityType,
    WorkMethod,
)
from .service import CoordinationService
from .timeutil import MutableClock, format_ts

# 道路走向 x 轴，单位米（局部投影坐标）
GAS_PIPE = [(0.0, 50.0), (40.0, 50.0), (80.0, 50.0)]
WATER_PIPE = [(10.0, 10.0), (90.0, 10.0)]
WORK_AREA = [(30.0, 30.0), (60.0, 30.0), (60.0, 70.0), (30.0, 70.0)]
FAR_AREA = [(200.0, 200.0), (240.0, 200.0), (240.0, 240.0), (200.0, 240.0)]


def build_demo(service: CoordinationService, clock: MutableClock) -> dict[str, Any]:
    ids: dict[str, Any] = {"steps": []}

    def step(name: str, payload: Any) -> None:
        ids["steps"].append({"at": clock.now().isoformat(), "step": name, "result": payload})

    # 参与方
    coord = service.register_actor(Actor("coord-1", "协调员-林", Role.COORDINATOR))
    gas = service.register_actor(
        Actor("owner-gas", "燃气-周", Role.OWNER, owner_id="gas-bureau", clearance=Classification.CONFIDENTIAL)
    )
    water = service.register_actor(
        Actor("owner-water", "供水-吴", Role.OWNER, owner_id="water-bureau", clearance=Classification.INTERNAL)
    )
    contractor = service.register_actor(Actor("contractor-a", "道桥公司-赵", Role.CONTRACTOR))
    contractor_b = service.register_actor(Actor("contractor-b", "管网公司-钱", Role.CONTRACTOR))
    auditor = service.register_actor(Actor("auditor-1", "审计-孙", Role.AUDITOR))
    ids.update(coord=coord.actor_id, gas=gas.actor_id, water=water.actor_id,
               contractor=contractor.actor_id, contractor_b=contractor_b.actor_id, auditor=auditor.actor_id)

    # 权属单位提交带生效期与密级的管段快照（版本不同是事故根因之一）
    gas_snap = service.submit_pipe_snapshot(
        gas.actor_id,
        pipe_code="gas-branch-17",
        utility_type=UtilityType.GAS,
        classification=Classification.CONFIDENTIAL,
        risk_level=RiskLevel.HIGH,
        geometry=GAS_PIPE,
        diameter_mm=200,
        pressure="0.4MPa",
        effective_from="2026-01-01T00:00:00Z",
    )
    water_snap = service.submit_pipe_snapshot(
        water.actor_id,
        pipe_code="water-main-3",
        utility_type=UtilityType.WATER,
        classification=Classification.INTERNAL,
        risk_level=RiskLevel.LOW,
        geometry=WATER_PIPE,
        diameter_mm=400,
        pressure="0.25MPa",
        effective_from="2026-01-01T00:00:00Z",
    )
    ids.update(gas_snapshot=gas_snap.snapshot_id, water_snapshot=water_snap.snapshot_id)

    # 工程方 A：明挖改造，时间窗与影响面
    app_a = service.apply_for_occupancy(
        contractor.actor_id,
        project_code="road-renew-中山北路",
        work_area=WORK_AREA,
        road_closure=WORK_AREA,
        method=WorkMethod.OPEN_CUT,
        window_start="2026-10-10T08:00:00Z",
        window_end="2026-10-20T18:00:00Z",
        impact_radius_m=10.0,
    )
    ids["application_a"] = app_a.application_id
    asm_a = service.latest_assessment(app_a.application_id)
    ids["assessment_a"] = asm_a.assessment_id
    step("A 提交申请：命中燃气管段，进入风险会签",
         {"status": service.get_application(app_a.application_id).status.value, "warnings": list(asm_a.warnings)})

    # 并发申请 B：时空重叠 → 不能获得合法占位
    app_b = service.apply_for_occupancy(
        contractor_b.actor_id,
        project_code="cable-laying-并行工程",
        work_area=[(35.0, 35.0), (70.0, 35.0), (70.0, 75.0), (35.0, 75.0)],
        road_closure=[(35.0, 35.0), (70.0, 35.0), (70.0, 75.0), (35.0, 75.0)],
        method=WorkMethod.TRENCHLESS,
        window_start="2026-10-12T08:00:00Z",
        window_end="2026-10-18T18:00:00Z",
        impact_radius_m=5.0,
    )
    ids["application_b"] = app_b.application_id
    step("B 并发申请：封路/缓冲冲突，被阻塞且不持有占位",
         {"status": service.get_application(app_b.application_id).status.value})

    # 回复期未过，签发必须被拒
    clock.advance(hours=10)
    try:
        service.issue_permit(coord.actor_id, app_a.application_id)
    except Exception as exc:  # noqa: BLE001
        step("会签未完成即签发 → 拒绝", {"error": str(exc)})

    # 逾期收口（普通优先级 48h）：高风险未回复保持阻断
    clock.advance(hours=40)
    blocked = service.expire_pending_responses("system")
    step("回复期截止：高风险燃气未回复，不默认安全，持续阻断", {"blocked": blocked})
    try:
        service.issue_permit(coord.actor_id, app_a.application_id)
    except Exception as exc:  # noqa: BLE001
        step("高风险未回复仍签发 → 拒绝", {"error": str(exc)})

    # 燃气权属会签同意 → 审核收口 → 签发
    service.respond_pipe(gas.actor_id, app_a.application_id, gas_snap.snapshot_id, Decision.APPROVE,
                         comment="已交底，要求人工探挖先行")
    permit = service.issue_permit(coord.actor_id, app_a.application_id)
    ids["permit_a"] = permit.permit_code
    step("燃气完成会签，许可证签发", {"permit": permit.permit_code})

    # 工程延期（时间窗变更）→ 新版本重新评估；燃气管段快照未变，意见沿用 carried
    asm_rev = service.revise_application(
        contractor.actor_id,
        app_a.application_id,
        window_start="2026-10-10T08:00:00Z",
        window_end="2026-10-25T18:00:00Z",
        change_note="降雨预警，工程延期 5 天",
    )
    carried = [c for c in asm_rev.pipe_conflicts if c.confirmation_state.value == "carried"]
    step("延期触发 rev.2 重评估；同一燃气快照的历史意见沿用", {"carried": len(carried)})
    permit2 = service.issue_permit(coord.actor_id, app_a.application_id)
    ids["permit_a_rev2"] = permit2.permit_code

    # 权属单位发布新版燃气快照（坐标校正）→ 自动重新评估，旧审核仍引用旧快照
    clock.advance(hours=2)
    gas_snap_v2 = service.submit_pipe_snapshot(
        gas.actor_id,
        pipe_code="gas-branch-17",
        utility_type=UtilityType.GAS,
        classification=Classification.CONFIDENTIAL,
        risk_level=RiskLevel.HIGH,
        geometry=[(0.0, 47.0), (40.0, 47.0), (80.0, 47.0)],
        diameter_mm=200,
        pressure="0.4MPa",
        effective_from=clock.now().isoformat().replace("+00:00", "Z"),
    )
    ids["gas_snapshot_v2"] = gas_snap_v2.snapshot_id
    asm_v2 = service.latest_assessment(app_a.application_id)
    step("燃气新版快照发布 → 自动重评估，需重新会签；历史评估保留旧快照引用",
         {"assessment": asm_v2.assessment_id, "cause": asm_v2.cause})
    service.respond_pipe(gas.actor_id, app_a.application_id, gas_snap_v2.snapshot_id, Decision.APPROVE,
                         comment="新坐标已确认")
    clock.advance(hours=1)
    service.expire_pending_responses("system")
    permit3 = service.issue_permit(coord.actor_id, app_a.application_id)
    ids["permit_a_rev2_reissued"] = permit3.permit_code

    # 紧急抢修：第三方爆管，抢占 A 施工窗内的重叠时空，A 的许可证暂停
    emergency = service.emergency_repair(
        contractor_b.actor_id,
        project_code="emergency-供水爆管",
        work_area=[(38.0, 40.0), (55.0, 40.0), (55.0, 60.0), (38.0, 60.0)],
        window_start="2026-10-15T02:00:00Z",
        window_end="2026-10-15T14:00:00Z",
        impact_radius_m=8.0,
        description="路口供水主管爆裂紧急抢修",
    )
    ids["emergency"] = emergency.application_id
    # 抢修申请命中高风险燃气且时间紧迫：无书面确认不得签发
    try:
        service.issue_permit(coord.actor_id, emergency.application_id)
    except Exception as exc:  # noqa: BLE001
        step("紧急抢修高风险未会签 → 仍拒绝（不默认安全）", {"error": str(exc)})
    # 燃气快速会签后签发抢修许可
    emergency_asm = service.latest_assessment(emergency.application_id)
    emergency_gas = next(c for c in emergency_asm.pipe_conflicts if c.utility_type == UtilityType.GAS)
    service.respond_pipe(gas.actor_id, emergency.application_id, emergency_gas.snapshot_id,
                         Decision.APPROVE, comment="现场配合，降压运行")
    emergency_permit = service.issue_permit(coord.actor_id, emergency.application_id)
    ids["emergency_permit"] = emergency_permit.permit_code
    step("紧急抢修改按快速会签完成后签发，A 许可证保持暂停", {
        "a_status": service.get_application(app_a.application_id).status.value,
        "emergency_permit": emergency_permit.permit_code,
    })

    ids["incident_moment"] = format_ts(clock.now())
    return ids
