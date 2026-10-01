"""分段给煤：计划、分段累计台账与三处一致性对账。

偏差判据固定按段（:data:`DEVIATION_BASIS`）；煤种策略分"停下报炉"与
"按差值修正后继续"两种；称重漂移或补读失败属于部分失败，只停料报炉，
绝不用估数补足。
"""

from __future__ import annotations

from .ledger import (
    POLICY_CORRECT_CONTINUE,
    POLICY_STOP_REPORT,
    ChargeBook,
    SegmentRecord,
)
from .plan import DEVIATION_BASIS, SegmentPlan, build_segment_plan
from .reconcile import reconcile

__all__ = [
    "DEVIATION_BASIS",
    "POLICY_CORRECT_CONTINUE",
    "POLICY_STOP_REPORT",
    "ChargeBook",
    "SegmentPlan",
    "SegmentRecord",
    "build_segment_plan",
    "reconcile",
]
