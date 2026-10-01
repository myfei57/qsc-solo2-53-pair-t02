"""装煤车状态机。

煤斗对位沿用"实测位置 / 目标位置 / 公差"口径；给煤按吨位与给煤速率推进，
装满目标吨位后停止。给煤必须在煤斗对位之后进行，未对位直接拒绝。

分段给煤走 :meth:`feed_segment`：螺旋按自己的指令量出料，实际给入 =
指令量 × 给煤系数（``delivery_ratio``，现场默认 1.0，给多给少由这里体现），
实际给入才是送在线秤过秤的量；段内是否给够由编排侧看秤回读决定，机器不会
自动追平称重值。重新对位一炉时累计清零。
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
        self._delivered_t = 0.0
        self._delivery_ratio = 1.0
        self._feeding = False

    @property
    def limits(self) -> ChargerLimits:
        return self._limits

    @property
    def delivery_ratio(self) -> float:
        """实际给入相对指令量的系数（1.0 = 不多不少）。"""

        return self._delivery_ratio

    def set_delivery_ratio(self, ratio: float) -> None:
        """设定给煤系数，必须为正；用于现场标定给多给少的偏差来源。"""

        if ratio <= 0.0:
            raise ValueError("给煤系数必须为正")
        self._delivery_ratio = float(ratio)

    def align_hopper(self, oven: str, target_mm: float, measured_mm: float) -> Alignment:
        """核对煤斗对位，返回核对结果。对位一炉即重置本炉给煤累计。"""

        alignment = Alignment(float(target_mm), float(measured_mm), self._limits.align_tolerance_mm)
        self._oven = oven
        self._hopper_mm = alignment.measured_mm
        self._deviation_mm = alignment.deviation_mm
        self._aligned = alignment.ok
        if alignment.ok:
            self._fed_t = 0.0
            self._delivered_t = 0.0
            self._feeding = False
            self._coal_load_t = self._limits.coal_target_t
        return alignment

    def is_aligned(self) -> bool:
        """煤斗是否已对位。"""

        return self._aligned

    def feed_segment(
        self,
        dt: float,
        segment_remaining_t: float,
        delivery_ratio: float | None = None,
    ) -> tuple[float, float]:
        """推进一个固定步长，返回 ``(指令吨位, 实际过秤吨位)``。

        给煤螺旋只认自己的指令量，实际给入 = 指令 × 给煤系数；给多给少要等
        在线秤回读才知道，机器不会在段内自动追平称重值。``segment_remaining_t``
        只是提前停料的上限，防止指令量超出本段计划；``delivery_ratio`` 可按
        步长临时覆盖给煤系数（现场标定 / 分段核对用）。
        """

        if not self._aligned:
            raise AlignmentError("煤斗未对位，禁止给煤", component="charger")
        if dt <= 0:
            raise ValueError("步长必须为正")
        if segment_remaining_t < 0.0:
            raise ValueError("段剩余不得为负")
        ratio = self._delivery_ratio if delivery_ratio is None else float(delivery_ratio)
        if ratio <= 0.0:
            raise ValueError("给煤系数必须为正")
        commanded = self._limits.feed_rate_t_per_min * dt / 60.0
        # 上限同时受本段剩余与炭化室剩余容量约束；后者按**实际过秤口径**折算，
        # 这样前一段少给后，后面多补的指令量才放得进来，整炉仍以称重 28t 收尾。
        oven_remaining = self._limits.coal_target_t - self._delivered_t
        commanded = min(commanded, segment_remaining_t, oven_remaining / ratio)
        if commanded <= 0.0:
            return 0.0, 0.0
        actual = commanded * ratio
        self._fed_t += commanded
        self._delivered_t += actual
        self._feeding = self._delivered_t + 1e-9 < self._limits.coal_target_t
        if self.feed_complete():
            self._coal_load_t = 0.0
        return commanded, actual

    def feed(self, dt: float) -> float:
        """按整炉目标给煤一个步长，返回实际给煤吨位（不分段的旧口径）。"""

        oven_remaining = self._limits.coal_target_t - self._delivered_t
        commanded, actual = self.feed_segment(dt, oven_remaining / self._delivery_ratio)
        if commanded <= 0.0:
            self._feeding = False
            return 0.0
        return actual

    @property
    def delivered_t(self) -> float:
        """本炉已实际给入（过秤口径）的累计吨位。"""

        return self._delivered_t

    @property
    def fed_t(self) -> float:
        """本炉已指令给入的累计吨位。"""

        return self._fed_t

    def feed_complete(self) -> bool:
        """是否已达到目标装煤量（以实际过秤口径为准）。"""

        return self._delivered_t + 1e-9 >= self._limits.coal_target_t

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
            delivered_t=self._delivered_t,
            delivery_ratio=self._delivery_ratio,
        )
