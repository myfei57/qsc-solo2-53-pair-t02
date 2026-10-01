"""分段给煤、在线称重核对与三处一致性。

覆盖需求里定死的口径：

* 偏差按**每段**判，不按整炉累计判（整炉累计只对账）；
* 段越限按煤种策略：``stop_report`` 停下报炉，``correct_continue`` 按真实称重
  差值修正后继续；
* 称重漂移、补读用尽属于**部分失败**：停后续给煤、报炉，绝不按估数补足；
* 分段累计台账、在线称重回读、审计流水三处必须对得上；
* 分段累计同时供控制台展示并落审计。
"""

from __future__ import annotations

import pytest

from coke.charger.segmented.ledger import (
    POLICY_CORRECT_CONTINUE,
    POLICY_STOP_REPORT,
    ChargeBook,
)
from coke.charger.segmented.plan import DEVIATION_BASIS, build_segment_plan
from coke.charger.segmented.reconcile import reconcile
from coke.charger.weighbridge import SCALE_DRIFT, SCALE_LINK, SCALE_OK, Weighbridge
from coke.config import ConfigError, config_to_mapping, default_config, load_config
from coke.console.api import Api
from coke.errors import CokeError


def test_segment_plan_splits_capacity_without_losing_total():
    plans = build_segment_plan(28.0, 4)
    assert [plan.index for plan in plans] == [0, 1, 2, 3]
    assert round(sum(plan.target_t for plan in plans), 3) == 28.0
    assert plans[0].target_t == 7.0


def test_segment_plan_remainder_lands_on_last_segment():
    plans = build_segment_plan(28.0, 3)
    assert round(sum(plan.target_t for plan in plans), 3) == 28.0
    assert plans[-1].target_t > plans[0].target_t


def test_deviation_basis_is_locked_to_segment(platform):
    # 口径定死：按段判，不允许悄悄改成整炉累计。
    platform.start()
    report = platform.charge(oven="oven01")
    assert DEVIATION_BASIS == "segment"
    assert report["deviation_basis"] == "segment"
    assert report["reconciliation"]["deviation_basis"] == "segment"
    for segment in report["segments"]:
        assert "verdict" in segment


def test_normal_segmented_charge_weighs_each_segment_and_reconciles(platform):
    platform.start()
    report = platform.charge(oven="oven01")
    assert report["ok"] is True
    assert report["violations"] == []
    assert len(report["segments"]) == 4
    assert report["weighed_total_t"] == 28.0
    assert {segment["verdict"] for segment in report["segments"]} == {"within"}
    # 三处一致性：台账 / 称重回读 / 审计。
    result = report["reconciliation"]
    assert result["ok"] is True
    assert result["segments_checked"] == 4
    assert result["weighed_total_t"] == result["scale_total_t"] == result["audit_total_t"]


def test_stop_report_grade_halts_further_segments_and_flags_oven(platform):
    platform.start()
    report = platform.charge(
        oven="oven02", coal_grade="standard", segment_ratios=[1.08, 1.0, 1.0, 1.0]
    )
    assert report["ok"] is False
    assert "segment_deviation" in report["violations"]
    assert len(report["segments"]) == 1
    assert report["segments"][0]["verdict"] == "over_limit"
    assert report["book"]["stopped"] is True
    assert report["book"]["report_reason"] == "segment_deviation"
    assert report["policy"] == POLICY_STOP_REPORT
    alarms = platform.audit.alarms()
    assert any(item["action"] == "charge_oven_flagged" for item in alarms)
    assert any(item["action"] == "interlock_rejected" for item in alarms)


def test_correct_continue_grade_uses_real_weighed_delta_and_finishes(platform):
    platform.start()
    report = platform.charge(
        oven="oven03", coal_grade="blend_b", segment_ratios=[1.08, 1.0, 1.0, 1.0]
    )
    assert report["ok"] is True
    assert report["violations"] == []
    verdicts = [segment["verdict"] for segment in report["segments"]]
    assert verdicts[0] == "corrected"
    # 第一段多给 0.56t，后续目标必须按真实称重差值下调。
    assert report["segments"][1]["corrected_target_t"] < report["segments"][1]["planned_target_t"]
    assert report["weighed_total_t"] == 28.0
    assert report["policy"] == POLICY_CORRECT_CONTINUE


def test_correct_continue_recovers_transient_underfeed(platform):
    platform.start()
    report = platform.charge(
        oven="oven04", coal_grade="blend_b", segment_ratios=[0.92, 1.0, 1.0, 1.0]
    )
    assert report["ok"] is True
    assert report["weighed_total_t"] == 28.0
    assert report["segments"][0]["verdict"] == "corrected"


def test_persistent_shortfall_is_reported_at_final_segment(platform):
    platform.start()
    report = platform.charge(
        oven="oven05", coal_grade="blend_b", segment_ratios=[0.92, 0.92, 0.92, 0.92]
    )
    # 持续少给，差值在末段仍补不回来：按策略允许修正，但末段无法再修正 → 报炉。
    assert report["ok"] is False
    assert "uncorrectable_final_segment" in report["violations"]
    assert report["segments"][-1]["verdict"] == "over_limit"
    assert report["book"]["report_reason"] == "uncorrectable_final_segment"


def test_scale_drift_is_partial_failure_no_estimated_makeup(platform):
    platform.start()
    platform.disturb_scale_drift(0.5)  # 门槛 0.2t
    report = platform.charge(oven="oven06")
    assert report["ok"] is False
    assert "scale_drift" in report["violations"]
    assert len(report["segments"]) == 1
    assert report["segments"][0]["scale_status"] == SCALE_DRIFT
    assert report["book"]["stopped"] is True
    assert report["book"]["report_reason"] == "scale_drift"
    # 没有任何补估字段：台账里这一段只记录真实（漂移）读数，不产生修正段。
    assert report["segments"][0]["verdict"] == "over_limit"
    alarms = platform.audit.alarms()
    assert any(item["action"] == "charge_oven_flagged" for item in alarms)


def test_link_failure_rereads_within_budget_then_succeeds(platform):
    platform.start()
    platform.disturb_scale_link(1)  # 允许补 2 次，1 次就恢复
    report = platform.charge(oven="oven07")
    assert report["ok"] is True
    first = report["scale_readbacks"][0]
    assert first["status"] == SCALE_OK
    assert first["reread_attempts"] == 1


def test_link_failure_exhausting_rereads_is_partial_failure(platform):
    platform.start()
    platform.disturb_scale_link(5)  # 超过 2 次补读预算
    report = platform.charge(oven="oven08")
    assert report["ok"] is False
    assert "scale_readback_failed" in report["violations"]
    assert report["segments"][0]["scale_status"] == SCALE_LINK
    assert report["segments"][0]["reread_attempts"] == 2
    assert report["book"]["stopped"] is True
    assert report["book"]["report_reason"] == "scale_readback_failed"
    alarms = platform.audit.alarms()
    assert any(item["action"] == "charge_oven_flagged" for item in alarms)


def test_weighbridge_drift_is_sticky_until_reset():
    scale = Weighbridge(drift_tolerance_t=0.2, max_rereads=2)
    scale.accumulate(7.0)
    first = scale.read_segment(0.0)
    assert first.status == SCALE_OK
    scale.inject_drift(0.3)
    scale.accumulate(7.0)
    second = scale.read_segment(7.0)
    assert second.status == SCALE_DRIFT


def test_partial_failure_reading_carries_no_estimated_makeup():
    # 部分失败的读数只是把秤面状态如实带出：没有任何"建议补足量"字段。
    scale = Weighbridge(drift_tolerance_t=0.2, max_rereads=2)
    scale.accumulate(7.0)
    scale.inject_drift(0.3)
    reading = scale.read_segment(0.0)
    assert reading.status == SCALE_DRIFT
    assert set(reading.as_dict()) == {
        "status",
        "gross_t",
        "segment_t",
        "zero_offset_t",
        "reread_attempts",
    }
    # 链路失败且无漂移时，补读用尽的结果同样不带估值。
    fresh = Weighbridge(drift_tolerance_t=0.2, max_rereads=2)
    fresh.accumulate(7.0)
    fresh.force_link_failure(3)
    link_reading = fresh.read_segment(0.0)
    assert link_reading.status == SCALE_LINK
    assert set(link_reading.as_dict()) == {
        "status",
        "gross_t",
        "segment_t",
        "zero_offset_t",
        "reread_attempts",
    }


def test_deviation_is_judged_per_segment_not_on_oven_total(platform):
    """边界两段都在带内（±0.35t）时，即使合计差值较大也不得判越限。"""

    platform.start()
    # 段重 7t：+0.34 与 -0.34 都在 0.35 带内，整炉累计相消但逐段都独立判定。
    ratio_hi = (7.0 + 0.34) / 7.0
    ratio_lo = (7.0 - 0.34) / 7.0
    report = platform.charge(
        oven="oven17",
        coal_grade="standard",
        segment_ratios=[ratio_hi, ratio_lo, ratio_hi, ratio_lo],
    )
    assert report["ok"] is True
    assert {segment["verdict"] for segment in report["segments"]} == {"within"}
    assert abs(report["weighed_total_t"] - 28.0) < 1e-6


def test_segment_ledger_exposed_to_console_and_audit(platform):
    platform.start()
    report = platform.charge(oven="oven09")
    op_id = report["op_id"]
    api = Api(platform)
    status, body = api.handle("GET", "/api/charge-books", {}, {})
    assert status == 200
    assert any(book["op_id"] == op_id for book in body["books"])
    status, book = api.handle("GET", f"/api/charge-book/{op_id}", {}, {})
    assert status == 200
    assert book["oven"] == "oven09"
    assert book["deviation_basis"] == "segment"
    assert len(book["segments"]) == 4
    # 审计流水里同一 op_id 下逐段都在，且字段就是台账字段。
    entries = platform.audit.entries(limit=200)
    segment_entries = [
        item
        for item in entries
        if item["action"] == "charge_segment" and item["payload"].get("op_id") == op_id
    ]
    assert len(segment_entries) == 4
    segments = sorted(item["payload"]["segment"] for item in segment_entries)
    assert segments == [0, 1, 2, 3]


def test_reconcile_detects_tampered_audit(platform):
    platform.start()
    report = platform.charge(oven="oven10")
    op_id = report["op_id"]
    book = platform.books[op_id]
    readbacks = report["scale_readbacks"]
    entries = platform.audit.entries(limit=2000)
    # 篡改审计里第一段的称重：三处必须对不上。
    tampered = []
    for item in entries:
        if item["action"] == "charge_segment" and item["payload"].get("segment") == 0:
            payload = dict(item["payload"])
            payload["weighed_t"] = round(payload["weighed_t"] + 0.3, 3)
            item = {**item, "payload": payload}
        tampered.append(item)
    result = reconcile(book, readbacks, tampered)
    assert result["ok"] is False
    assert "segment0_weighed_audit" in result["mismatches"]


def test_reconcile_detects_missing_scale_readback(platform):
    platform.start()
    report = platform.charge(oven="oven11")
    op_id = report["op_id"]
    book = platform.books[op_id]
    entries = platform.audit.entries(limit=2000)
    result = reconcile(book, report["scale_readbacks"][:-1], entries)
    assert result["ok"] is False
    assert "scale_readback_count" in result["mismatches"]


def test_status_exposes_segmented_charger_view(platform):
    platform.start()
    platform.charge(oven="oven12")
    status = platform.status()
    charger = status["charger"]
    assert charger["segment_count"] == 4
    assert charger["deviation_basis"] == "segment"
    assert charger["scale"]["drift_tolerance_t"] == 0.2
    assert {grade["code"] for grade in charger["coal_grades"]} == {"standard", "blend_b"}
    assert status["charge_books"]


def test_unknown_coal_grade_is_rejected(platform):
    platform.start()
    with pytest.raises(ConfigError):
        platform.charge(oven="oven13", coal_grade="no-such-grade")


def test_segment_ratios_must_match_segment_count(platform):
    platform.start()
    with pytest.raises(CokeError):
        platform.charge(oven="oven14", segment_ratios=[1.0, 1.0])


def test_config_carries_segmentation_and_coal_grades(tmp_path):
    mapping = config_to_mapping(default_config())
    assert mapping["limits"]["charger"]["segment_count"] == 4
    assert mapping["limits"]["charger"]["scale_max_rereads"] == 2
    grades = {item["code"]: item for item in mapping["coal_grades"]}
    assert grades["standard"]["policy"] == POLICY_STOP_REPORT
    assert grades["blend_b"]["policy"] == POLICY_CORRECT_CONTINUE
    import json

    path = tmp_path / "coke.json"
    path.write_text(json.dumps(mapping), encoding="utf-8")
    loaded = load_config(str(path))
    assert loaded.charger.segment_count == 4
    assert loaded.coal_grades.default_code() == "standard"


def test_config_rejects_duplicate_grade_codes():
    mapping = config_to_mapping(default_config())
    mapping["coal_grades"].append(dict(mapping["coal_grades"][0]))
    with pytest.raises(ConfigError):
        default_config().__class__.from_mapping(mapping)


def test_config_rejects_bad_grade_policy():
    mapping = config_to_mapping(default_config())
    mapping["coal_grades"][0]["policy"] = "guess_and_fill"
    with pytest.raises(ConfigError):
        default_config().__class__.from_mapping(mapping)


def test_config_rejects_non_positive_segment_count():
    mapping = config_to_mapping(default_config())
    mapping["limits"]["charger"]["segment_count"] = 0
    with pytest.raises(ConfigError):
        default_config().__class__.from_mapping(mapping)


def test_reconcile_requires_op_id_isolation(platform):
    platform.start()
    first = platform.charge(oven="oven15")
    second = platform.charge(oven="oven16")
    # 两炉都在同一审计流里，对账只按各自 op_id 取本段，不串炉。
    assert first["reconciliation"]["ok"] is True
    assert second["reconciliation"]["ok"] is True
    assert first["op_id"] != second["op_id"]


def test_chargebook_supports_console_shape():
    book = ChargeBook("ovenA", "op-1", "standard", POLICY_STOP_REPORT, 28.0)
    payload = book.as_dict()
    assert payload["deviation_basis"] == "segment"
    assert payload["weighed_total_t"] == 0.0
    assert payload["segments"] == []
    assert payload["stopped"] is False


def test_scale_requires_positive_feed():
    scale = Weighbridge(0.2, 2)
    with pytest.raises(ValueError):
        scale.accumulate(-1.0)
