"""在线称重（煤斗秤）。

给煤量不再由给煤速率推算，而是每段给完后从在线秤回读本段净重（毛重差值）。
读数分三种状态：

``ok``     读数有效，可以作为累计与判偏依据；
``drift``  零点漂移超出门槛——属于**部分失败**：秤的累计口径已不可信，
           该段不能按估数补足，必须停止后续给煤并报炉；
``link``   读数链路故障，允许在段内做有限次在线补读（reread）；补读次数
           用尽仍读不到，同样按部分失败处理。

秤只负责忠实回读，任何"少了就按估值填上"的动作都不允许发生在这里。
"""

from __future__ import annotations

from dataclasses import dataclass

# 读数状态：有效 / 称重漂移（部分失败）/ 链路故障（可补读）。
SCALE_OK = "ok"
SCALE_DRIFT = "drift"
SCALE_LINK = "link"


@dataclass(frozen=True)
class ScaleReading:
    """一次称重回读（或补读）的结果。"""

    status: str
    gross_t: float
    segment_t: float
    zero_offset_t: float
    reread_attempts: int

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "gross_t": round(self.gross_t, 3),
            "segment_t": round(self.segment_t, 3),
            "zero_offset_t": round(self.zero_offset_t, 3),
            "reread_attempts": self.reread_attempts,
        }


class Weighbridge:
    """煤斗在线秤：累计毛重、零点漂移判定与段内补读。"""

    def __init__(
        self,
        drift_tolerance_t: float,
        max_rereads: int = 2,
    ) -> None:
        if drift_tolerance_t <= 0.0:
            raise ValueError("称重漂移门槛必须为正")
        if max_rereads < 0:
            raise ValueError("补读次数不得为负")
        self._drift_tolerance_t = float(drift_tolerance_t)
        self._max_rereads = int(max_rereads)
        self._gross_t = 0.0
        self._zero_offset_t = 0.0
        # 链路故障只影响"下一次回读"，用于现场模拟瞬时通讯中断后的在线补读。
        self._link_down = 0
        # 漂移一旦出现就保持：漂移不是瞬时抖动，恢复必须走重新标定。
        self._drifted = False

    @property
    def drift_tolerance_t(self) -> float:
        return self._drift_tolerance_t

    @property
    def max_rereads(self) -> int:
        return self._max_rereads

    def gross_t(self) -> float:
        """当前秤面毛重（含漂移，仅供核查显示）。"""

        return self._gross_t

    def zero_offset(self) -> float:
        """当前零点偏移量。"""

        return self._zero_offset_t

    def reset(self) -> None:
        """一炉开始前清零秤面毛重，作为本段净重的基线。

        注意：零点漂移与链路故障是现场注入的**状态**，清零不能把它们一并清掉
        （否则"先注入漂移再开一炉"就验不出部分失败了）；漂移恢复必须走重新
        标定，链路故障由段内补读消耗。
        """

        self._gross_t = 0.0

    def accumulate(self, actual_t: float) -> None:
        """把本段实际卸下的吨位累计到秤面。"""

        if actual_t < 0.0:
            raise ValueError("过秤吨位不得为负")
        self._gross_t += float(actual_t)

    def read_segment(self, baseline_t: float) -> ScaleReading:
        """回读本段净重，链路故障时按 ``max_rereads`` 在线补读。

        漂移优先判定：即使链路恢复，只要零点偏移超门槛，读数就是 ``drift``。
        """

        # 首次回读；若链路故障，在段内在线补读，最多补 max_rereads 次。
        rereads = 0
        link_ok = self._link_down <= 0
        if not link_ok:
            self._link_down -= 1
            for rereads in range(1, self._max_rereads + 1):
                if self._link_down <= 0:
                    link_ok = True
                    break
                self._link_down -= 1
        self._link_down = 0
        if self._drifted or abs(self._zero_offset_t) > self._drift_tolerance_t:
            self._drifted = True
            status = SCALE_DRIFT
        elif not link_ok:
            status = SCALE_LINK
        else:
            status = SCALE_OK

        net = self._gross_t - baseline_t
        return ScaleReading(status, self._gross_t, max(net, 0.0), self._zero_offset_t, rereads)

    def force_link_failure(self, count: int = 1) -> None:
        """模拟下一次回读的链路故障；``count`` 为需要补读的次数。"""

        if count < 1:
            raise ValueError("故障次数必须为正")
        self._link_down = int(count)

    def inject_drift(self, offset_t: float) -> None:
        """模拟零点漂移，偏移绝对值超门槛即构成部分失败。"""

        self._zero_offset_t += float(offset_t)
