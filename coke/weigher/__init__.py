"""在线称重。

装煤车给煤量由在线称重核对，平台只信称重累计，不接受人工估数。称重分三种
状态：

``ok``    回读有效，增量可以入账；
``drift`` 部分失败（漂移、读数不稳）：补读仍拿不到可信值。此时该段称重累计
          停在上一有效读数，**禁止按估数补足**；
``fault`` 完全失效，读数一律不可信。

漂移与故障的区别只影响排障口径，对给煤联锁的处置一致：没有有效回读就不能
继续按吨位闭环给煤。
"""

from __future__ import annotations

from .weigher import OnlineWeigher, WeighReading

__all__ = ["OnlineWeigher", "WeighReading"]
