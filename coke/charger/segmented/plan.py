"""分段给煤计划。

按炭化室容量（总装煤目标）把一炉给煤切成若干等重段；除不尽时尾段吃余数，
保证各段目标之和**恰好**等于炭化室容量，不允许在分段阶段就把总量弄丢。
"""

from __future__ import annotations

from dataclasses import dataclass

# 偏差判据定死：按每段判，不按整炉累计判。
# 整炉累计只用于三处一致性对账，不产生越限停车。
DEVIATION_BASIS = "segment"


@dataclass(frozen=True)
class SegmentPlan:
    """一段给煤的计划。"""

    index: int
    target_t: float

    def as_dict(self) -> dict:
        return {"index": self.index, "target_t": round(self.target_t, 3)}


def build_segment_plan(total_target_t: float, segment_count: int) -> list[SegmentPlan]:
    """把总容量切成 ``segment_count`` 段，尾段承担舍入余数。"""

    if total_target_t <= 0.0:
        raise ValueError("炭化室容量必须为正")
    if segment_count < 1:
        raise ValueError("分段数至少为 1")
    base = round(total_target_t / segment_count, 3)
    plans = [SegmentPlan(index, base) for index in range(segment_count)]
    assigned = round(base * (segment_count - 1), 3)
    plans[-1] = SegmentPlan(segment_count - 1, round(total_target_t - assigned, 3))
    return plans
