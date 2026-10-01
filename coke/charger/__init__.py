"""装煤车。

装煤车把煤斗送到炭化室顶部并对位，随后在集气管压力稳定、桥管翻板打开之后
分段给煤。煤斗对位是装煤的硬前置；每段给煤量由在线称重核对，累计账在
:mod:`coke.charger.ledger`。
"""

from __future__ import annotations

from .ledger import (
    ChargeLedger,
    SegmentRecord,
    evaluate_segment,
    reconcile_charge,
)
from .machine import ChargingCar
from .snapshot import ChargerSnapshot

__all__ = [
    "ChargingCar",
    "ChargerSnapshot",
    "ChargeLedger",
    "SegmentRecord",
    "evaluate_segment",
    "reconcile_charge",
]
