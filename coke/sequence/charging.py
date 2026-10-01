"""装煤顺序：煤斗对位 → 集气管调压 → 桥管翻板 → 分段称重给煤。

给煤按炭化室容量分成固定段数，**逐段判定偏差**（口径定死，见
``charger/ledger.py``）：每段给完立即用在线称重增量核对该段目标，超限按煤种
策略处置——``stop_report`` 先停后续给煤并把这一炉报出来，
``correct_continue`` 用称重差值做一次修正给煤后继续。称重漂移属于部分失败，
无论煤种策略如何都停下报炉，禁止按估数补足。

每段产生一条 ``feed_segment`` 步骤（扁平标量字段，平台直接转审计事件），
``report["ledger"]`` 是分段累计账（供控制台展示）。落审计后的"分段累计账 ↔
称重回读 ↔ 审计流水"三方核对由平台 ``reconcile_charge`` 完成。
"""

from __future__ import annotations

from ..charger.ledger import (
    ChargeLedger,
    SegmentRecord,
    evaluate_segment,
)
from ..charger.machine import ChargingCar
from ..config import CoalGradeSettings
from ..gas.collector import CollectorMain
from ..gas.flap import BridgeFlap
from ..weigher.weigher import OnlineWeigher

MAX_STEPS = 600


def _segment_step(index: int, record: SegmentRecord, severity: str) -> dict:
    return {
        "component": "charger",
        "action": "feed_segment",
        "severity": severity,
        "detail": {
            "segment": index,
            "target_t": round(record.target_t, 3),
            "tolerance_t": round(record.tolerance_t, 3),
            "delivered_t": round(record.delivered_t, 3),
            "weighed_delta_t": (
                round(record.weighed_delta_t, 3)
                if record.weighed_delta_t is not None
                else None
            ),
            "weigh_before_t": round(record.weigh_before_t, 3),
            "weigh_after_t": (
                round(record.weigh_after_t, 3) if record.weigh_after_t is not None else None
            ),
            "deviation_t": round(record.deviation_t, 3) if record.deviation_t is not None else None,
            "within_tolerance": record.within_tolerance,
            "result": record.result,
            "policy": record.policy,
            "trim_t": round(record.trim_t, 3),
            "weigh_valid": record.weigh_valid,
        },
    }


_SEGMENT_SEVERITY = {
    "ok": "notice",
    "corrected": "warning",
    "stopped": "alarm",
    "weigh_failed": "alarm",
    "overfed": "alarm",
}


def charge_oven(
    charger: ChargingCar,
    collector: CollectorMain,
    flap: BridgeFlap,
    weigher: OnlineWeigher,
    grade: CoalGradeSettings,
    oven: str,
    hopper_target_mm: float,
    hopper_measured_mm: float,
    segment_variance_t: list[float] | None = None,
    drift_at_segment: int | None = None,
    dt: float = 1.0,
) -> dict:
    """执行一炉分段称重装煤，返回带逐段步骤、累计账与违约项的报告。"""

    steps: list[dict] = []
    violations: list[str] = []

    alignment = charger.align_hopper(oven, hopper_target_mm, hopper_measured_mm)
    steps.append({"component": "charger", "action": "align_hopper", "detail": alignment.as_dict()})
    if not alignment.ok:
        violations.append("hopper_not_aligned")
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    flap.reset_latch()
    for _ in range(MAX_STEPS):
        if collector.settled():
            break
        collector.regulate(dt)
    steps.append(
        {
            "component": "gas",
            "action": "regulate_pressure",
            "detail": {
                "pressure_pa": round(collector.pressure_pa, 2),
                "target_pa": collector.limits.pressure_target_pa,
                "settled": collector.settled(),
            },
        }
    )
    if not collector.settled():
        violations.append("pressure_not_settled")
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    angle = flap.open()
    steps.append({"component": "gas", "action": "open_flap", "detail": {"angle_deg": angle}})

    targets = charger.limits.segment_targets()
    charger.begin_charge(oven, sum(targets), len(targets))
    weigher.tare()
    ledger = ChargeLedger(oven, grade.name, targets)

    def close_with(ok: bool) -> dict:
        flap.close()
        steps.append({"component": "gas", "action": "close_flap", "detail": {"angle_deg": flap.angle_deg}})
        return {
            "oven": oven,
            "ok": ok,
            "steps": steps,
            "violations": violations,
            "grade": grade.name,
            "deviation_policy": grade.deviation_policy,
            "deviation_basis": "per_segment",
            "fed_t": ledger.delivered_total_t,
            "weighed_t": ledger.weighed_total_t,
            "target_t": ledger.target_total_t,
            "stopped": ledger.stopped,
            "stop_index": ledger.stop_index,
            "ledger": ledger.as_dict(),
        }

    trim_used = 0.0
    variances = segment_variance_t or []
    for index, target in enumerate(targets):
        variance = variances[index] if index < len(variances) else 0.0
        weigh_before = weigher.cumulative_t()
        limit_t = round(ledger.delivered_total_t + target, 3)
        delivered = charger.feed_to(limit_t, dt, variance_t=variance)
        if drift_at_segment == index:
            weigher.disturb()
        reading = weigher.observe(delivered)

        if not reading.valid:
            # 部分失败：补读一次仍是无效读数，不能按估数补足，先停后续给煤。
            reread = weigher.re_read()
            weigh_after = reread.cumulative_t if reread.valid else None
            charger.mark_weighed(weigh_after)
            raw = SegmentRecord(
                index=index,
                target_t=target,
                tolerance_t=grade.tolerance_for(target),
                delivered_t=delivered,
                weigh_before_t=weigh_before,
                weigh_after_t=None,
                weighed_delta_t=None,
                deviation_t=None,
                within_tolerance=False,
                result="weigh_failed",
                policy=grade.deviation_policy,
                weigh_valid=False,
            )
        else:
            weigh_after = reading.cumulative_t
            charger.mark_weighed(weigh_after)
            delta = round((weigh_after or 0.0) - weigh_before, 3)
            raw = SegmentRecord(
                index=index,
                target_t=target,
                tolerance_t=grade.tolerance_for(target),
                delivered_t=delivered,
                weigh_before_t=weigh_before,
                weigh_after_t=weigh_after,
                weighed_delta_t=delta,
                deviation_t=round(target - delta, 3),
                within_tolerance=abs(target - delta) <= grade.tolerance_for(target) + 1e-9,
                result="pending",
                policy=grade.deviation_policy,
            )

        record = evaluate_segment(raw, grade, trim_used)
        if record.result == "corrected":
            # 按称重差值补一段修正给煤，并用新回读入账（只许一次，受额度约束）。
            trim = record.trim_t
            trim_delivered = charger.feed_to(round(charger.snapshot().fed_t + trim, 3), dt)
            trim_reading = weigher.observe(trim_delivered)
            trim_after = trim_reading.cumulative_t if trim_reading.valid else None
            charger.mark_weighed(trim_after)
            if trim_after is None:
                # 修正给煤期间秤漂移：这批修正煤无法核对，按部分失败停炉、不估
                # 补；送出量只保留已核对的主给煤，未核对的修正量由告警上报。
                record = SegmentRecord(
                    index=index,
                    target_t=target,
                    tolerance_t=grade.tolerance_for(target),
                    delivered_t=record.delivered_t,
                    weigh_before_t=weigh_before,
                    weigh_after_t=None,
                    weighed_delta_t=None,
                    deviation_t=None,
                    within_tolerance=False,
                    result="weigh_failed",
                    policy=grade.deviation_policy,
                    weigh_valid=False,
                )
            else:
                trim_used = round(trim_used + trim, 3)
                new_delta = round(trim_after - weigh_before, 3)
                # 修正额度只够补一部分：补后仍超段公差，转停下报炉，不带病续给。
                corrected_result = (
                    "corrected"
                    if abs(target - new_delta) <= grade.tolerance_for(target) + 1e-9
                    else "stopped"
                )
                record = SegmentRecord(
                    index=index,
                    target_t=target,
                    tolerance_t=grade.tolerance_for(target),
                    delivered_t=round(record.delivered_t + trim_delivered, 3),
                    weigh_before_t=weigh_before,
                    weigh_after_t=trim_after,
                    weighed_delta_t=new_delta,
                    deviation_t=round(target - new_delta, 3),
                    within_tolerance=corrected_result == "corrected",
                    result=corrected_result,
                    policy=grade.deviation_policy,
                    trim_t=round(trim_delivered, 3),
                )

        ledger.append(record)
        charger.advance_segment()
        steps.append(_segment_step(index, record, _SEGMENT_SEVERITY[record.result]))

        if record.result == "weigh_failed":
            violations.append("weigh_drift_partial_failure")
        elif record.result == "overfed":
            violations.append("segment_overfed")
        elif record.result == "stopped":
            violations.append("segment_deviation_exceeded")

        if ledger.stopped:
            # 先停后续给煤并把这一炉报出来：剩余段不再执行。
            steps.append(
                {
                    "component": "audit",
                    "action": "charge_flagged",
                    "severity": "alarm",
                    "detail": {
                        "stop_index": index,
                        "reason": record.result,
                        "grade": grade.name,
                        "fed_t": ledger.delivered_total_t,
                        "weighed_t": ledger.weighed_total_t,
                    },
                }
            )
            return close_with(False)

    return close_with(True)
