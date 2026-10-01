"""装煤顺序：煤斗对位 → 集气管调压 → 桥管翻板 → 分段给煤（在线称重核对）。

分段给煤把炭化室容量切成若干段，每段给完从在线秤回读本段净重，**按段**判偏：

* 段偏差在带内 → 继续下一段；
* 越限且煤种策略为 ``correct_continue`` → 用**真实称重差值**修正后续段目标后
  继续（任何修正都只来自秤的读数，不来自估算）；
* 越限且策略为 ``stop_report``，或末段多给已无法再修正 → 停止后续给煤，这一
  炉直接报出来；
* 称重漂移、或在线补读用尽仍读不到（``link``）属于部分失败：只停料报炉，
  绝不按估数补足。

整炉累计不参与判偏，只在三处一致性对账里使用（见
:mod:`coke.charger.segmented.reconcile`）。
"""

from __future__ import annotations

from typing import Callable

from ..charger.machine import ChargingCar
from ..charger.segmented.ledger import (
    POLICY_CORRECT_CONTINUE,
    POLICY_STOP_REPORT,
    SEGMENT_CORRECTED,
    SEGMENT_OVER_LIMIT,
    SEGMENT_WITHIN,
    ChargeBook,
    SegmentRecord,
)
from ..charger.segmented.plan import DEVIATION_BASIS, SegmentPlan, build_segment_plan
from ..charger.weighbridge import SCALE_DRIFT, SCALE_LINK, ScaleReading, Weighbridge
from ..config import CoalGrade
from ..gas.collector import CollectorMain
from ..gas.flap import BridgeFlap

MAX_STEPS = 600


def charge_oven(
    charger: ChargingCar,
    collector: CollectorMain,
    flap: BridgeFlap,
    oven: str,
    hopper_target_mm: float,
    hopper_measured_mm: float,
    dt: float = 1.0,
) -> dict:
    """执行一炉装煤（不分段的旧口径），返回带步骤与违约项的报告。"""

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

    fed_t = 0.0
    for _ in range(MAX_STEPS):
        if charger.feed_complete():
            break
        fed_t += charger.feed(dt)
    steps.append(
        {
            "component": "charger",
            "action": "feed",
            "detail": {"fed_t": round(fed_t, 3), "aligned": charger.is_aligned()},
        }
    )
    if not charger.feed_complete():
        violations.append("feed_incomplete")
        flap.close()
        steps.append({"component": "gas", "action": "close_flap", "detail": {"angle_deg": flap.angle_deg}})
        return {"oven": oven, "ok": False, "steps": steps, "violations": violations}

    flap.close()
    steps.append({"component": "gas", "action": "close_flap", "detail": {"angle_deg": flap.angle_deg}})
    return {"oven": oven, "ok": True, "steps": steps, "violations": violations, "fed_t": fed_t}


def charge_oven_segmented(
    charger: ChargingCar,
    collector: CollectorMain,
    flap: BridgeFlap,
    scale: Weighbridge,
    grade: CoalGrade,
    oven: str,
    hopper_target_mm: float,
    hopper_measured_mm: float,
    op_id: str,
    segment_count: int,
    dt: float = 1.0,
    ratio_for_segment: Callable[[int], float] | None = None,
) -> tuple[dict, ChargeBook, list[dict]]:
    """分段给煤装煤，返回 ``(报告, 分段累计台账, 各段称重回读)``。

    ``ratio_for_segment`` 可选，按段号返回该段给煤系数（现场标定 / 注入偏差用）。
    """

    steps: list[dict] = []
    violations: list[str] = []
    readbacks: list[dict] = []

    alignment = charger.align_hopper(oven, hopper_target_mm, hopper_measured_mm)
    steps.append({"component": "charger", "action": "align_hopper", "detail": alignment.as_dict()})
    if not alignment.ok:
        violations.append("hopper_not_aligned")
        empty_book = ChargeBook(oven, op_id, grade.code, grade.policy, charger.limits.coal_target_t)
        return (
            {"oven": oven, "ok": False, "steps": steps, "violations": violations},
            empty_book,
            readbacks,
        )

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
        empty_book = ChargeBook(oven, op_id, grade.code, grade.policy, charger.limits.coal_target_t)
        return (
            {"oven": oven, "ok": False, "steps": steps, "violations": violations},
            empty_book,
            readbacks,
        )

    angle = flap.open()
    steps.append({"component": "gas", "action": "open_flap", "detail": {"angle_deg": angle}})

    plans = build_segment_plan(charger.limits.coal_target_t, segment_count)
    book = ChargeBook(oven, op_id, grade.code, grade.policy, charger.limits.coal_target_t)
    # 每段的有效目标：初始为计划目标，越限修正后按真实差值调整。
    targets = [plan.target_t for plan in plans]
    scale.reset()
    baseline_t = 0.0

    for plan in plans:
        index = plan.index
        command_start = charger.fed_t
        segment_ratio = ratio_for_segment(index) if ratio_for_segment is not None else None
        for _ in range(MAX_STEPS):
            commanded_total = charger.fed_t - command_start
            if commanded_total + 1e-9 >= targets[index]:
                break
            commanded, actual = charger.feed_segment(
                dt, max(targets[index] - commanded_total, 0.0), segment_ratio
            )
            if commanded <= 0.0:
                break
            scale.accumulate(actual)

        reading = scale.read_segment(baseline_t)
        baseline_t = scale.gross_t()
        weighed_t = reading.segment_t
        deviation_t = round(weighed_t - targets[index], 3)
        readbacks.append(
            {
                "segment": index,
                "status": reading.status,
                "gross_t": round(reading.gross_t, 3),
                "segment_t": round(reading.segment_t, 3),
                "zero_offset_t": round(reading.zero_offset_t, 3),
                "reread_attempts": reading.reread_attempts,
            }
        )

        # 部分失败（称重漂移 / 补读失败）：只停料报炉，绝不按估数补足。
        if reading.status == SCALE_DRIFT:
            verdict = SEGMENT_OVER_LIMIT
            book.record(_segment_record(plan, targets, weighed_t, deviation_t, grade, verdict, reading))
            book.stop("scale_drift")
            violations.append("scale_drift")
            steps.append(_segment_step(index, targets[index], weighed_t, deviation_t, reading, verdict))
            break
        if reading.status == SCALE_LINK:
            verdict = SEGMENT_OVER_LIMIT
            book.record(_segment_record(plan, targets, weighed_t, deviation_t, grade, verdict, reading))
            book.stop("scale_readback_failed")
            violations.append("scale_readback_failed")
            steps.append(_segment_step(index, targets[index], weighed_t, deviation_t, reading, verdict))
            break

        within = abs(deviation_t) <= grade.segment_deviation_t
        if within:
            verdict = SEGMENT_WITHIN
            book.record(_segment_record(plan, targets, weighed_t, deviation_t, grade, verdict, reading))
            steps.append(_segment_step(index, targets[index], weighed_t, deviation_t, reading, verdict))
            continue

        # 越限：按煤种策略决定停料报炉还是按真实称重差值修正后续段。
        if grade.policy == POLICY_STOP_REPORT or index == len(plans) - 1:
            verdict = SEGMENT_OVER_LIMIT
            book.record(_segment_record(plan, targets, weighed_t, deviation_t, grade, verdict, reading))
            reason = "uncorrectable_final_segment" if index == len(plans) - 1 else "segment_deviation"
            book.stop(reason)
            violations.append(reason)
            steps.append(_segment_step(index, targets[index], weighed_t, deviation_t, reading, verdict))
            break

        # 少给 → 把真实称重差值摊到剩余所有段（补差值）；多给 → 从剩余段等比
        # 扣回。摊到所有剩余段是为了让每段的指令量都不超过炭化室剩余容量；
        # 修正只来自本段真实称重，不引入任何估算。下一段给完立即再称重、再
        # 按段判偏。
        remaining_count = len(plans) - index - 1
        carry = 0.0
        for later in range(index + 1, len(plans)):
            share = round(-deviation_t / remaining_count, 3)
            if later == len(plans) - 1:
                share = round(-deviation_t - carry, 3)
            targets[later] = round(max(targets[later] + share, 0.0), 3)
            carry += share
        verdict = SEGMENT_CORRECTED
        book.record(_segment_record(plan, targets, weighed_t, deviation_t, grade, verdict, reading))
        steps.append(_segment_step(index, targets[index], weighed_t, deviation_t, reading, verdict))

    flap.close()
    steps.append({"component": "gas", "action": "close_flap", "detail": {"angle_deg": flap.angle_deg}})

    completed = not book.stopped and len(book.records) == len(plans)
    if not completed and not violations:
        violations.append("feed_incomplete")
    report = {
        "oven": oven,
        "ok": completed,
        "steps": steps,
        "violations": violations,
        "deviation_basis": DEVIATION_BASIS,
        "segments": book.segments(),
        "weighed_total_t": book.weighed_total_t(),
        "fed_t": charger.delivered_t,
    }
    return report, book, readbacks


def _segment_record(
    plan: SegmentPlan,
    targets: list[float],
    weighed_t: float,
    deviation_t: float,
    grade: CoalGrade,
    verdict: str,
    reading: ScaleReading,
) -> SegmentRecord:
    return SegmentRecord(
        index=plan.index,
        planned_target_t=round(plan.target_t, 3),
        corrected_target_t=round(targets[plan.index], 3),
        weighed_t=round(weighed_t, 3),
        deviation_t=deviation_t,
        tolerance_t=grade.segment_deviation_t,
        verdict=verdict,
        scale_status=reading.status,
        reread_attempts=reading.reread_attempts,
    )


def _segment_step(
    index: int,
    target_t: float,
    weighed_t: float,
    deviation_t: float,
    reading: ScaleReading,
    verdict: str,
) -> dict:
    return {
        "component": "charger",
        "action": "charge_segment",
        "detail": {
            "segment": index,
            "target_t": round(target_t, 3),
            "weighed_t": round(weighed_t, 3),
            "deviation_t": deviation_t,
            "verdict": verdict,
            "scale_status": reading.status,
            "reread_attempts": reading.reread_attempts,
        },
    }
