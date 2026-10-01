"""分段累计账与三方核对。

一炉给煤按炭化室容量切成若干段（见 :meth:`ChargerLimits.segment_targets`），
每段记一条 :class:`SegmentRecord`：执行器送出量、称重回读累计、段目标、段公
差、判定结果、是否修正。

偏差判定口径**定死为逐段判定**：每段给完立即用该段称重增量对该段目标判偏
差，超限即按煤种策略（停下报炉 / 按称重差值修正后续给）。整炉累计只在炉末
做一次核对性汇总，不具备中途停续给的效力——逐段闭环才能保证炉顶平整，按整
炉判会把前面几段的偏差攒到最后，来不及补救。

三方一致性指三处数字必须对得上：

1. 分段累计账（``ChargeLedger``，供控制台展示）；
2. 称重回读（``OnlineWeigher`` 的历史回读）；
3. 审计流水（逐段发布的 ``feed_segment`` 事件）。

:func:`reconcile_charge` 把三处的分段增量与整炉汇总放到一张表里逐项比对，任
一处对不上即判不一致；这一炉即便给完也算问题炉，要报出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import CoalGradeSettings

# 段判定结果。
SEGMENT_OK = "ok"                    # 段偏差在公差带内
SEGMENT_CORRECTED = "corrected"      # 超差，按称重差值修正后续给
SEGMENT_STOPPED = "stopped"          # 超差，停后续给煤并报炉
SEGMENT_WEIGH_FAILED = "weigh_failed"  # 称重部分失败，禁止按估数补足
SEGMENT_OVERFED = "overfed"          # 给多了，煤收不回来，只能停炉报出

# 三方账的字段名，核对时统一用这套键。
LEDGER = "segment_ledger"
WEIGHER = "weigh_readback"
AUDIT = "audit_trail"


@dataclass(frozen=True)
class SegmentRecord:
    """一段给煤的累计与判定。"""

    index: int
    target_t: float
    tolerance_t: float
    delivered_t: float
    weigh_before_t: float
    weigh_after_t: float | None
    weighed_delta_t: float | None
    deviation_t: float | None
    within_tolerance: bool
    result: str
    policy: str
    trim_t: float = 0.0
    weigh_valid: bool = True

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "target_t": round(self.target_t, 3),
            "tolerance_t": round(self.tolerance_t, 3),
            "delivered_t": round(self.delivered_t, 3),
            "weigh_before_t": round(self.weigh_before_t, 3),
            "weigh_after_t": (
                round(self.weigh_after_t, 3) if self.weigh_after_t is not None else None
            ),
            "weighed_delta_t": (
                round(self.weighed_delta_t, 3) if self.weighed_delta_t is not None else None
            ),
            "deviation_t": round(self.deviation_t, 3) if self.deviation_t is not None else None,
            "within_tolerance": self.within_tolerance,
            "result": self.result,
            "policy": self.policy,
            "trim_t": round(self.trim_t, 3),
            "weigh_valid": self.weigh_valid,
        }


@dataclass
class ChargeLedger:
    """一炉的分段累计账。"""

    oven: str
    grade: str
    targets_t: list[float]
    records: list[SegmentRecord] = field(default_factory=list)

    @property
    def segment_count(self) -> int:
        return len(self.targets_t)

    @property
    def stopped(self) -> bool:
        """是否在某段停下（超差报炉 / 称重部分失败 / 给多）。"""

        return any(record.result in (SEGMENT_STOPPED, SEGMENT_WEIGH_FAILED, SEGMENT_OVERFED)
                   for record in self.records)

    @property
    def stop_index(self) -> int | None:
        for record in self.records:
            if record.result in (SEGMENT_STOPPED, SEGMENT_WEIGH_FAILED, SEGMENT_OVERFED):
                return record.index
        return None

    @property
    def delivered_total_t(self) -> float:
        return round(sum(record.delivered_t for record in self.records), 3)

    @property
    def weighed_total_t(self) -> float | None:
        """有效称重段的累计；任一段称重无效则整炉称重账存在空窗，返回 None。"""

        if any(not record.weigh_valid for record in self.records):
            return None
        return round(sum(record.weighed_delta_t or 0.0 for record in self.records), 3)

    @property
    def target_total_t(self) -> float:
        return round(sum(self.targets_t), 3)

    def segment_weighed_deltas(self) -> list[float | None]:
        """分段称重增量，供与称重回读、审计逐段比对。"""

        return [record.weighed_delta_t for record in self.records]

    def append(self, record: SegmentRecord) -> None:
        self.records.append(record)

    def as_dict(self) -> dict:
        return {
            "oven": self.oven,
            "grade": self.grade,
            "segment_count": self.segment_count,
            "targets_t": [round(value, 3) for value in self.targets_t],
            "delivered_total_t": self.delivered_total_t,
            "weighed_total_t": self.weighed_total_t,
            "target_total_t": self.target_total_t,
            "stopped": self.stopped,
            "stop_index": self.stop_index,
            "segments": [record.as_dict() for record in self.records],
        }


def evaluate_segment(
    record: SegmentRecord,
    grade: CoalGradeSettings,
    trim_used_t: float,
) -> SegmentRecord:
    """按段判定并套用煤种策略，返回落账后的段记录（修正量只用于决策）。

    口径顺序定死：先看称重是否有效（无效即部分失败，停且不许估数补足）；
    再看是否给多（正偏差超带只能停炉报出）；最后对给少按煤种策略处置——
    ``stop_report`` 停后续给煤报炉，``correct_continue`` 在修正额度内按称重差
    值补一段修正给煤。修正段的实际补煤与补后回读由编排层执行后再落账，这里
    只给出允许的修正量，不提前把修正量累加进送出量和称重增量。
    """

    if not record.weigh_valid:
        return SegmentRecord(
            index=record.index,
            target_t=record.target_t,
            tolerance_t=record.tolerance_t,
            delivered_t=record.delivered_t,
            weigh_before_t=record.weigh_before_t,
            weigh_after_t=None,
            weighed_delta_t=None,
            deviation_t=None,
            within_tolerance=False,
            result=SEGMENT_WEIGH_FAILED,
            policy=grade.deviation_policy,
            weigh_valid=False,
        )

    deviation = round(record.target_t - record.weighed_delta_t, 3)  # 正：给少了
    within = abs(deviation) <= record.tolerance_t + 1e-9
    if within:
        return SegmentRecord(
            record.index, record.target_t, record.tolerance_t, record.delivered_t,
            record.weigh_before_t, record.weigh_after_t, record.weighed_delta_t,
            deviation, True, SEGMENT_OK, grade.deviation_policy,
        )
    if deviation < 0:
        return SegmentRecord(
            record.index, record.target_t, record.tolerance_t, record.delivered_t,
            record.weigh_before_t, record.weigh_after_t, record.weighed_delta_t,
            deviation, False, SEGMENT_OVERFED, grade.deviation_policy,
        )
    if grade.deviation_policy == "stop_report":
        return SegmentRecord(
            record.index, record.target_t, record.tolerance_t, record.delivered_t,
            record.weigh_before_t, record.weigh_after_t, record.weighed_delta_t,
            deviation, False, SEGMENT_STOPPED, grade.deviation_policy,
        )

    # correct_continue：按称重差值做一次修正给煤，且不得突破修正额度。
    remaining_budget = round(grade.trim_budget_t - trim_used_t, 3)
    trim = min(deviation, remaining_budget)
    if trim <= 1e-9:
        return SegmentRecord(
            record.index, record.target_t, record.tolerance_t, record.delivered_t,
            record.weigh_before_t, record.weigh_after_t, record.weighed_delta_t,
            deviation, False, SEGMENT_STOPPED, grade.deviation_policy,
        )
    return SegmentRecord(
        record.index, record.target_t, record.tolerance_t, record.delivered_t,
        record.weigh_before_t, record.weigh_after_t, record.weighed_delta_t,
        deviation, False, SEGMENT_CORRECTED, grade.deviation_policy,
        trim_t=round(trim, 3),
    )


def _series(records: list[dict], delta_key: str) -> list[float | None]:
    """从审计/称重的扁平记录里按段号取称重增量。"""

    ordered = sorted(records, key=lambda item: int(item.get("segment", 0)))
    return [item.get(delta_key) for item in ordered]


def reconcile_charge(
    ledger: ChargeLedger,
    weigh_events: list[dict],
    audit_events: list[dict],
) -> dict:
    """三方核对：分段累计账 vs 称重回读 vs 审计流水。

    ``weigh_events`` / ``audit_events`` 取逐段事件（含 ``segment`` 与
    ``weighed_delta_t``）。逐段增量必须一一相等，三方各自的整炉合计也必须相
    等；缺段、空窗（None）或合计不符都判不一致。
    """

    ledger_series = ledger.segment_weighed_deltas()
    weigher_series = _series(weigh_events, "weighed_delta_t")
    audit_series = _series(audit_events, "weighed_delta_t")

    segment_rows: list[dict] = []
    consistent = len(ledger_series) == len(weigher_series) == len(audit_series)
    for index in range(ledger.segment_count):
        ledger_value = ledger_series[index] if index < len(ledger_series) else None
        weigher_value = weigher_series[index] if index < len(weigher_series) else None
        audit_value = audit_series[index] if index < len(audit_series) else None
        values = (ledger_value, weigher_value, audit_value)
        match = (
            all(value is not None for value in values)
            and max(values) - min(values) <= 1e-6  # type: ignore[arg-type]
        )
        consistent = consistent and match
        segment_rows.append(
            {
                "segment": index,
                LEDGER: ledger_value,
                WEIGHER: weigher_value,
                AUDIT: audit_value,
                "match": match,
            }
        )

    def _total(series: list[float | None]) -> float | None:
        if any(value is None for value in series):
            return None
        return round(sum(value for value in series if value is not None), 3)  # type: ignore[arg-type]

    totals = {
        LEDGER: _total(ledger_series),
        WEIGHER: _total(weigher_series),
        AUDIT: _total(audit_series),
    }
    total_values = [value for value in totals.values() if value is not None]
    totals_match = (
        len(total_values) == 3 and max(total_values) - min(total_values) <= 1e-6
    )
    consistent = consistent and totals_match

    return {
        "oven": ledger.oven,
        "consistent": consistent,
        "segments": segment_rows,
        "totals": totals,
        "totals_match": totals_match,
        "sources": [LEDGER, WEIGHER, AUDIT],
    }
