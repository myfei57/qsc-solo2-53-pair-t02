"""分段累计台账。

一炉一本台账，逐段登记"计划目标 / 实际称重 / 偏差 / 判定 / 修正后目标"。
台账是三处一致性对帐中的第一处；控制台分段累计页直接读本台账，审计写的是
同一批字段的逐条副本，两边口径一致。

偏差判据固定为按段判（见 :data:`coke.charger.segmented.plan.DEVIATION_BASIS`）：
``within``   本段偏差在带内，继续；
``corrected`` 本段越限但煤种策略允许修正，已按**真实称重差值**调整后续目标；
``over_limit`` 本段越限且策略为停下报炉（或末段多给无法再修正），停止后续给煤。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .plan import DEVIATION_BASIS

# 段判定结果。
SEGMENT_WITHIN = "within"
SEGMENT_CORRECTED = "corrected"
SEGMENT_OVER_LIMIT = "over_limit"

# 越限后的煤种策略。
POLICY_STOP_REPORT = "stop_report"
POLICY_CORRECT_CONTINUE = "correct_continue"


@dataclass(frozen=True)
class SegmentRecord:
    """一段给煤的累计记录。"""

    index: int
    planned_target_t: float
    corrected_target_t: float
    weighed_t: float
    deviation_t: float
    tolerance_t: float
    verdict: str
    scale_status: str
    reread_attempts: int

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "planned_target_t": round(self.planned_target_t, 3),
            "corrected_target_t": round(self.corrected_target_t, 3),
            "weighed_t": round(self.weighed_t, 3),
            "deviation_t": round(self.deviation_t, 3),
            "tolerance_t": round(self.tolerance_t, 3),
            "verdict": self.verdict,
            "scale_status": self.scale_status,
            "reread_attempts": self.reread_attempts,
        }


@dataclass
class ChargeBook:
    """一炉的分段累计台账。"""

    oven: str
    op_id: str
    coal_grade: str
    policy: str
    capacity_t: float
    deviation_basis: str = DEVIATION_BASIS
    records: list[SegmentRecord] = field(default_factory=list)
    stopped: bool = False
    report_reason: str = ""

    def record(self, entry: SegmentRecord) -> None:
        """登记一段结果。"""

        self.records.append(entry)

    def stop(self, reason: str) -> None:
        """停止后续给煤并登记报炉原因。"""

        self.stopped = True
        if not self.report_reason:
            self.report_reason = reason

    def weighed_total_t(self) -> float:
        """已给各段的真实称重累计。"""

        return round(sum(item.weighed_t for item in self.records), 3)

    def planned_total_t(self) -> float:
        """已给各段的（修正后）目标累计。"""

        return round(sum(item.corrected_target_t for item in self.records), 3)

    def segments(self) -> list[dict]:
        """分段明细，供控制台展示。"""

        return [item.as_dict() for item in self.records]

    def as_dict(self) -> dict:
        """台账摘要，供控制台展示与审计汇总。"""

        return {
            "oven": self.oven,
            "op_id": self.op_id,
            "coal_grade": self.coal_grade,
            "policy": self.policy,
            "capacity_t": round(self.capacity_t, 3),
            "deviation_basis": self.deviation_basis,
            "weighed_total_t": self.weighed_total_t(),
            "planned_total_t": self.planned_total_t(),
            "stopped": self.stopped,
            "report_reason": self.report_reason,
            "segments": self.segments(),
        }
