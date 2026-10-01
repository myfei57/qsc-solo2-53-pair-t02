"""三处一致性对账。

一炉给煤结束后，三处必须对得上，差一点都要标出来：

1. **分段累计台账**（:class:`ChargeBook`）——逐段登记的目标、称重与判定；
2. **在线称重回读**——每段结束时秤自己给出的毛重 / 净重 / 补读 / 漂移状态；
3. **审计流水**——同一 ``op_id`` 下逐条 ``charge_segment`` 落盘字段。

整炉累计只在这一步出现：它是对账口径，**不**作为越限判据；判偏始终按段。
"""

from __future__ import annotations

from .ledger import ChargeBook
from .plan import DEVIATION_BASIS

# 三处都按三位小数落盘 / 展示，对账容差取半档。
RECONCILE_TOLERANCE_T = 0.005


def _close(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) <= RECONCILE_TOLERANCE_T


def _audit_segments(entries: list[dict], op_id: str) -> list[dict]:
    found = [
        item.get("payload", {})
        for item in entries
        if item.get("action") == "charge_segment" and item.get("payload", {}).get("op_id") == op_id
    ]
    return sorted(found, key=lambda payload: int(payload.get("segment", 0)))


def reconcile(book: ChargeBook, scale_readbacks: list[dict], audit_entries: list[dict]) -> dict:
    """核对台账、称重回读与审计三处的一致性。"""

    mismatches: list[str] = []
    ledger_segments = book.segments()
    audit_segments = _audit_segments(audit_entries, book.op_id)

    if len(ledger_segments) != len(scale_readbacks):
        mismatches.append("scale_readback_count")
    if len(ledger_segments) != len(audit_segments):
        mismatches.append("audit_segment_count")

    for position, ledger in enumerate(ledger_segments):
        index = ledger["index"]
        scale = scale_readbacks[position] if position < len(scale_readbacks) else None
        audit = audit_segments[position] if position < len(audit_segments) else None
        if scale is None or audit is None:
            continue
        if int(audit.get("segment", -1)) != index:
            mismatches.append(f"segment{index}_index")
        weighed = ledger["weighed_t"]
        if not _close(scale.get("segment_t", 0.0), weighed):
            mismatches.append(f"segment{index}_weighed_scale")
        if not _close(audit.get("weighed_t", 0.0), weighed):
            mismatches.append(f"segment{index}_weighed_audit")
        if str(scale.get("status", "")) != str(ledger["scale_status"]):
            mismatches.append(f"segment{index}_scale_status")
        if str(audit.get("scale_status", "")) != str(ledger["scale_status"]):
            mismatches.append(f"segment{index}_audit_status")
        if str(audit.get("verdict", "")) != str(ledger["verdict"]):
            mismatches.append(f"segment{index}_verdict")

    scale_total = round(sum(float(item.get("segment_t", 0.0)) for item in scale_readbacks), 3)
    audit_total = round(sum(float(item.get("weighed_t", 0.0)) for item in audit_segments), 3)
    ledger_total = book.weighed_total_t()
    if not _close(scale_total, ledger_total):
        mismatches.append("total_weighed_scale")
    if not _close(audit_total, ledger_total):
        mismatches.append("total_weighed_audit")

    return {
        "ok": not mismatches,
        "op_id": book.op_id,
        "oven": book.oven,
        "deviation_basis": DEVIATION_BASIS,
        "segments_checked": len(ledger_segments),
        "weighed_total_t": ledger_total,
        "scale_total_t": scale_total,
        "audit_total_t": audit_total,
        "tolerance_t": RECONCILE_TOLERANCE_T,
        "mismatches": mismatches,
    }
