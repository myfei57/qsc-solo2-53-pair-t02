"""在线称重秤状态机。

秤以"装煤开始时置零、给煤过程中回读累计吨位"的方式工作。正常状态下回读即
真实累计；``disturb`` 模拟现场漂移（部分失败），之后的回读无效，必须
``recover`` 才能继续累计。补读（``re_read``）是漂移时再取一次数，取到的仍然
是无效读数——部分失败不能靠多读一次变成有效，更不能退化成估数。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import InterlockError

STATE_OK = "ok"
STATE_DRIFT = "drift"
STATE_FAULT = "fault"


@dataclass(frozen=True)
class WeighReading:
    """一次称重回读。有效读数携带累计吨位；无效读数吨位为空。"""

    state: str
    cumulative_t: float | None
    valid: bool
    reread: bool = False

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "cumulative_t": round(self.cumulative_t, 3) if self.cumulative_t is not None else None,
            "valid": self.valid,
            "reread": self.reread,
        }


class OnlineWeigher:
    """在线称重秤：累计真实给煤量，并暴露漂移/故障注入。"""

    def __init__(self) -> None:
        self._state = STATE_OK
        self._cumulative_t = 0.0

    @property
    def state(self) -> str:
        return self._state

    def tare(self) -> None:
        """一炉开始前置零。只在秤正常时允许，带病秤必须先恢复再开炉。"""

        if self._state != STATE_OK:
            raise InterlockError("称重未恢复正常，禁止置零开炉", component="weigher")
        self._cumulative_t = 0.0

    def observe(self, delivered_t: float) -> WeighReading:
        """给煤执行器送出一批煤后回读；漂移/故障时回读无效且不累计。"""

        if delivered_t < 0:
            raise ValueError("给煤量不能为负")
        if self._state == STATE_OK:
            self._cumulative_t = round(self._cumulative_t + delivered_t, 3)
            return WeighReading(self._state, self._cumulative_t, True)
        return WeighReading(self._state, None, False)

    def re_read(self) -> WeighReading:
        """补读一次。秤正常时返回当前累计；部分失败时补读依旧无效。"""

        if self._state == STATE_OK:
            return WeighReading(self._state, self._cumulative_t, True, reread=True)
        return WeighReading(self._state, None, False, reread=True)

    def disturb(self, *, fault: bool = False) -> str:
        """注入一次异常：``fault=False`` 为漂移（部分失败），否则为完全故障。"""

        self._state = STATE_FAULT if fault else STATE_DRIFT
        return self._state

    def recover(self) -> WeighReading:
        """排障后恢复。恢复后以最后一次有效累计为基线继续，不回填空窗期。"""

        self._state = STATE_OK
        return WeighReading(self._state, self._cumulative_t, True)

    def cumulative_t(self) -> float:
        """返回最后一次有效累计（空窗期内保持不变，绝不按估数补足）。"""

        return self._cumulative_t

    def snapshot(self) -> dict:
        return {
            "state": self._state,
            "cumulative_t": round(self._cumulative_t, 3),
            "healthy": self._state == STATE_OK,
        }
