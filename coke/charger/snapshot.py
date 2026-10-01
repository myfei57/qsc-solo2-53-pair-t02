"""装煤车状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChargerSnapshot:
    """装煤车某一时刻的状态。

    ``fed_t`` 是执行器本炉累计送出量；``weighed_t`` 是在线称重最后有效累计
    （秤漂移时保持上一有效读数，不会被估数顶替）；``target_t`` 是整炉目标，
    ``segment_total`` 是分段总数，``segment_index`` 是当前段（0 起，未开炉为
    0、给完为分段总数），三者供控制台展示分段累计进度。
    """

    oven: str
    hopper_measured_mm: float
    deviation_mm: float
    aligned: bool
    coal_load_t: float
    fed_t: float
    feeding: bool
    target_t: float
    weighed_t: float | None
    segment_total: int
    segment_index: int

    def as_dict(self) -> dict:
        return {
            "oven": self.oven,
            "hopper_measured_mm": round(self.hopper_measured_mm, 2),
            "deviation_mm": round(self.deviation_mm, 2),
            "aligned": self.aligned,
            "coal_load_t": round(self.coal_load_t, 3),
            "fed_t": round(self.fed_t, 3),
            "feeding": self.feeding,
            "target_t": round(self.target_t, 3),
            "weighed_t": round(self.weighed_t, 3) if self.weighed_t is not None else None,
            "segment_total": self.segment_total,
            "segment_index": self.segment_index,
        }
