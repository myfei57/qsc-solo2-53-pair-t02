"""装煤车状态机。

煤斗对位沿用"实测位置 / 目标位置 / 公差"口径；给煤按吨位与给煤速率推进。
给煤必须在煤斗对位之后进行，未对位直接拒绝。

分段给煤由 ``begin_charge`` 按炭化室容量与煤种开账，``feed_to`` 推到指定累计
上限（供逐段闭环和修正给煤使用）。``mark_weighed`` 把在线称重累计回报给状
态机，秤漂移时传 ``None``，快照里的称重累计保持上一有效值，绝不按估数补。
"""

from __future__ import annotations

from ..config import ChargerLimits
from ..errors import AlignmentError
from ..guide.align import Alignment
from .snapshot import ChargerSnapshot


class ChargingCar:
    """装煤车煤斗状态机。"""

    def __init__(self, limits: ChargerLimits) -> None:
        self._limits = limits
        self._oven = ""
        self._hopper_mm = 0.0
        self._deviation_mm = 0.0
        self._aligned = False
        self._coal_load_t = limits.coal_target_t
        self._fed_t = 0.0
        self._feeding = False
        self._target_t = limits.coal_target_t
        self._weighed_t: float | None = 0.0
        self._segment_total = limits.segment_count
        self._segment_index = 0

    @property
    def limits(self) -> ChargerLimits:
        return self._limits

    def align_hopper(self, oven: str, target_mm: float, measured_mm: float) -> Alignment:
        """核对煤斗对位，返回核对结果。"""

        alignment = Alignment(float(target_mm), float(measured_mm), self._limits.align_tolerance_mm)
        self._oven = oven
        self._hopper_mm = alignment.measured_mm
        self._deviation_mm = alignment.deviation_mm
        self._aligned = alignment.ok
        return alignment

    def is_aligned(self) -> bool:
        """煤斗是否已对位。"""

        return self._aligned

    def begin_charge(self, oven: str, target_t: float, segment_total: int) -> None:
        """开一炉分段给煤账：重置执行器累计与分段进度。"""

        if not self.is_aligned():
            raise AlignmentError("煤斗未对位，禁止给煤", component="charger")
        if target_t <= 0 or segment_total < 1:
            raise ValueError("整炉目标与段数必须为正")
        self._oven = oven
        self._target_t = target_t
        self._segment_total = segment_total
        self._segment_index = 0
        self._fed_t = 0.0
        self._weighed_t = 0.0
        self._coal_load_t = target_t
        self._feeding = True

    def feed_to(self, limit_t: float, dt: float, variance_t: float = 0.0) -> float:
        """按步长把执行器累计推到 ``limit_t``，返回本段实际送出量。

        ``variance_t`` 模拟给煤执行器相对目标的偏差（正：多给，负：少给），
        只作用在本段最后一口，供段偏差判定核对；正常闭环时传 0。
        """

        if not self.is_aligned():
            raise AlignmentError("煤斗未对位，禁止给煤", component="charger")
        if dt <= 0:
            raise ValueError("步长必须为正")
        start = self._fed_t
        rate = self._limits.feed_rate_t_per_min / 60.0
        while True:
            remaining_to_limit = limit_t - self._fed_t
            if remaining_to_limit <= 1e-9:
                break
            bite = rate * dt
            if bite >= remaining_to_limit:
                # 最后一口叠加执行器偏差；无论是否因此越过/欠到上限都收束，
                # 欠量留给段判定按称重回读去闭环。
                self._fed_t = round(limit_t + variance_t, 3)
                break
            self._fed_t = round(self._fed_t + bite, 3)
        delivered = round(self._fed_t - start, 3)
        if self.feed_complete():
            self._feeding = False
            self._coal_load_t = 0.0
        return delivered

    def advance_segment(self) -> None:
        """一段闭环结束，推进当前段序号（供控制台展示）。"""

        self._segment_index = min(self._segment_index + 1, self._segment_total)

    def mark_weighed(self, cumulative_t: float | None) -> None:
        """回报在线称重累计；``None`` 表示称重无效，保留上一有效值。"""

        if cumulative_t is not None:
            self._weighed_t = round(float(cumulative_t), 3)

    def feed_complete(self) -> bool:
        """是否已达到目标装煤量。"""

        return self._fed_t >= self._target_t

    def snapshot(self) -> ChargerSnapshot:
        """返回装煤车状态快照。"""

        return ChargerSnapshot(
            oven=self._oven,
            hopper_measured_mm=self._hopper_mm,
            deviation_mm=self._deviation_mm,
            aligned=self._aligned,
            coal_load_t=self._coal_load_t,
            fed_t=self._fed_t,
            feeding=self._feeding,
            target_t=self._target_t,
            weighed_t=self._weighed_t,
            segment_total=self._segment_total,
            segment_index=self._segment_index,
        )
