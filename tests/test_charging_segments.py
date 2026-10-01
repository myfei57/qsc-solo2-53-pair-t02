"""分段称重给煤：分段、逐段判偏差、煤种策略、称重部分失败与三方核对。"""

from __future__ import annotations

import pytest

from coke.charger.ledger import ChargeLedger, SegmentRecord, reconcile_charge
from coke.charger.machine import ChargingCar
from coke.config import (
    DEFAULT_GRADE,
    CoalGradeSettings,
    ConfigError,
    PlatformConfig,
    POLICY_CORRECT_CONTINUE,
    POLICY_STOP_REPORT,
    config_to_mapping,
    default_config,
)
from coke.errors import InterlockError
from coke.gas.collector import CollectorMain
from coke.gas.flap import BridgeFlap
from coke.gas.latch import FlapLatch
from coke.sequence import charge_oven
from coke.weigher.weigher import STATE_DRIFT, OnlineWeigher


def _rig():
    config = default_config()
    return (
        config,
        ChargingCar(config.charger),
        CollectorMain(config.collector),
        BridgeFlap(FlapLatch()),
        OnlineWeigher(),
    )


def test_segment_targets_split_capacity_without_drift():
    charger_limits = default_config().charger
    targets = charger_limits.segment_targets()
    assert len(targets) == charger_limits.segment_count
    assert sum(targets) == pytest.approx(charger_limits.coal_target_t, abs=1e-9)
    assert all(target > 0 for target in targets)


def test_normal_charge_all_segments_ok_and_reconciles():
    config, charger, collector, flap, weigher = _rig()
    grade = config.charger.grade(DEFAULT_GRADE)
    report = charge_oven(charger, collector, flap, weigher, grade, "oven01", 0.0, 0.0)
    assert report["ok"] is True
    assert report["stopped"] is False
    results = [segment["result"] for segment in report["ledger"]["segments"]]
    assert results == ["ok"] * config.charger.segment_count
    # 段段给够：执行器送出量与称重增量都等于整炉目标。
    assert report["fed_t"] == pytest.approx(config.charger.coal_target_t)
    assert report["weighed_t"] == pytest.approx(config.charger.coal_target_t)


def test_deviation_is_judged_per_segment_not_total():
    # 两段给少、两段给多可在整炉口径下互相抵消；逐段口径必须各自判出来。
    config, charger, collector, flap, weigher = _rig()
    grade = config.charger.grade("level_strict")
    report = charge_oven(
        charger,
        collector,
        flap,
        weigher,
        grade,
        "oven01",
        0.0,
        0.0,
        segment_variance_t=[-0.5, 0.5, 0.0, 0.0],
    )
    # stop_report：第一段给少超差即停，不看整炉累计是否为零。
    assert report["ok"] is False
    assert report["stopped"] is True
    assert report["stop_index"] == 0
    assert report["ledger"]["segments"][0]["result"] == "stopped"
    assert "segment_deviation_exceeded" in report["violations"]
    assert len(report["ledger"]["segments"]) == 1  # 后续给煤被先停下


def test_correct_continue_trims_by_weighed_delta():
    config, charger, collector, flap, weigher = _rig()
    grade = config.charger.grade(DEFAULT_GRADE)
    report = charge_oven(
        charger,
        collector,
        flap,
        weigher,
        grade,
        "oven01",
        0.0,
        0.0,
        segment_variance_t=[-0.3, 0.0, 0.0, 0.0],
    )
    assert report["ok"] is True
    first = report["ledger"]["segments"][0]
    assert first["result"] == "corrected"
    assert first["trim_t"] == pytest.approx(0.3, abs=1e-6)
    # 修正后该段称重增量回到目标。
    assert first["weighed_delta_t"] == pytest.approx(first["target_t"], abs=1e-6)
    assert report["weighed_t"] == pytest.approx(config.charger.coal_target_t, abs=1e-6)


def test_correct_continue_stops_when_trim_budget_exhausted():
    # 每段给少 0.2t，修正额度 0.5t：前两段补正（0.4t），第三段只够补 0.1t
    # 而残余 0.1t 落在 0.15t 段公差内仍判合格，第四段额度耗尽转停下报炉。
    config, charger, collector, flap, weigher = _rig()
    grade = config.charger.grade(DEFAULT_GRADE)
    report = charge_oven(
        charger,
        collector,
        flap,
        weigher,
        grade,
        "oven01",
        0.0,
        0.0,
        segment_variance_t=[-0.2, -0.2, -0.2, -0.2],
    )
    assert report["ok"] is False
    assert report["stopped"] is True
    assert report["stop_index"] == 3
    results = [segment["result"] for segment in report["ledger"]["segments"]]
    assert results == ["corrected", "corrected", "corrected", "stopped"]
    assert "segment_deviation_exceeded" in report["violations"]


def test_correct_continue_stops_if_trim_cannot_close_gap():
    # 单段缺口 0.3t、修正额度仅 0.1t：补后仍超段公差，不能挂"已修正"放行。
    config, charger, collector, flap, weigher = _rig()
    grade = config.charger.grade(DEFAULT_GRADE)
    report = charge_oven(
        charger,
        collector,
        flap,
        weigher,
        grade,
        "oven01",
        0.0,
        0.0,
        segment_variance_t=[-0.3, 0.0, 0.0, 0.0],
    )
    # 默认额度 0.5t 能补平，先验证补平口径；再用小额度煤种验证补不平即停。
    assert report["ledger"]["segments"][0]["result"] == "corrected"

    from coke.config import CoalGradeSettings

    tight = CoalGradeSettings("tight", "correct_continue", 0.15, 0.02, 0.1)
    config2, charger2, collector2, flap2, weigher2 = (
        config,
        ChargingCar(config.charger),
        CollectorMain(config.collector),
        BridgeFlap(FlapLatch()),
        OnlineWeigher(),
    )
    report2 = charge_oven(
        charger2, collector2, flap2, weigher2, tight, "oven02", 0.0, 0.0,
        segment_variance_t=[-0.3, 0.0, 0.0, 0.0],
    )
    first = report2["ledger"]["segments"][0]
    assert first["result"] == "stopped"
    assert report2["stop_index"] == 0


def test_overfeed_always_stops_regardless_of_grade():
    config, charger, collector, flap, weigher = _rig()
    grade = config.charger.grade(DEFAULT_GRADE)  # 修正策略对给多无能为力
    report = charge_oven(
        charger,
        collector,
        flap,
        weigher,
        grade,
        "oven01",
        0.0,
        0.0,
        segment_variance_t=[0.4, 0.0, 0.0, 0.0],
    )
    assert report["ok"] is False
    assert report["ledger"]["segments"][0]["result"] == "overfed"
    assert "segment_overfed" in report["violations"]


def test_weigh_drift_is_partial_failure_no_estimated_makeup():
    # 煤种即便允许修正续给，称重漂移也属于部分失败：停下报炉，不许估数补足。
    config, charger, collector, flap, weigher = _rig()
    grade = config.charger.grade(DEFAULT_GRADE)
    report = charge_oven(
        charger,
        collector,
        flap,
        weigher,
        grade,
        "oven01",
        0.0,
        0.0,
        drift_at_segment=1,  # 第二段回读前秤漂移
    )
    assert report["ok"] is False
    assert report["stopped"] is True
    assert report["stop_index"] == 1
    assert report["ledger"]["segments"][1]["result"] == "weigh_failed"
    assert "weigh_drift_partial_failure" in report["violations"]
    # 前两段存在称重空窗，整炉称重账不给估数。
    assert report["weighed_t"] is None
    # 漂移秤补读依旧无效，恢复后以最后有效累计为基线。
    assert weigher.re_read().valid is False
    assert weigher.state == STATE_DRIFT
    recovered = weigher.recover()
    assert recovered.valid is True
    assert weigher.cumulative_t() == 7.0


def test_weigher_keeps_last_valid_reading_until_recovery():
    weigher = OnlineWeigher()
    weigher.tare()
    weigher.observe(3.0)
    assert weigher.cumulative_t() == 3.0
    weigher.disturb()
    invalid = weigher.observe(5.0)  # 漂移期间的送出量不得入账
    assert invalid.valid is False
    assert weigher.cumulative_t() == 3.0
    recovered = weigher.recover()
    assert recovered.valid is True
    assert weigher.cumulative_t() == 3.0  # 恢复不回填空窗期


def test_three_way_reconciliation_detects_mismatch():
    config = default_config()
    targets = config.charger.segment_targets()
    grade = config.charger.grade(DEFAULT_GRADE)
    records = [
        SegmentRecord(
            index=i,
            target_t=target,
            tolerance_t=grade.tolerance_for(target),
            delivered_t=target,
            weigh_before_t=round(sum(targets[:i]), 3),
            weigh_after_t=round(sum(targets[: i + 1]), 3),
            weighed_delta_t=target,
            deviation_t=0.0,
            within_tolerance=True,
            result="ok",
            policy=grade.deviation_policy,
        )
        for i, target in enumerate(targets)
    ]
    ledger = ChargeLedger("oven01", grade.name, targets, records)
    weigh_events = [{"segment": i, "weighed_delta_t": target} for i, target in enumerate(targets)]
    audit_events = [dict(event) for event in weigh_events]
    assert reconcile_charge(ledger, weigh_events, audit_events)["consistent"] is True

    # 审计侧被改了一个数字：三方核对必须报不一致。
    audit_events[1]["weighed_delta_t"] = round(audit_events[1]["weighed_delta_t"] + 0.05, 3)
    result = reconcile_charge(ledger, weigh_events, audit_events)
    assert result["consistent"] is False
    assert result["segments"][1]["match"] is False


def test_unknown_grade_rejected_as_interlock():
    config = default_config()
    with pytest.raises(InterlockError):
        config.charger.grade("not-a-grade")


def test_config_requires_default_grade_and_segments():
    payload = config_to_mapping(default_config())
    payload["limits"]["charger"].pop("coal_grades")
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)

    payload = config_to_mapping(default_config())
    payload["limits"]["charger"]["coal_grades"].pop("default")
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)

    payload = config_to_mapping(default_config())
    payload["limits"]["charger"]["segment_count"] = 0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_config_rejects_unknown_policy_and_zero_trim_for_correction():
    payload = config_to_mapping(default_config())
    payload["limits"]["charger"]["coal_grades"]["default"]["deviation_policy"] = "guess"
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)

    payload = config_to_mapping(default_config())
    payload["limits"]["charger"]["coal_grades"]["default"]["trim_budget_t"] = 0.0
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_tolerance_takes_max_of_absolute_and_ratio():
    grade = CoalGradeSettings("g", POLICY_STOP_REPORT, 0.2, 0.02, 0.0)
    assert grade.tolerance_for(5.0) == 0.2
    assert grade.tolerance_for(20.0) == pytest.approx(0.4)
    assert POLICY_CORRECT_CONTINUE != POLICY_STOP_REPORT


def test_platform_publishes_segments_to_console_ledger_and_audit(platform):
    platform.start()
    report = platform.charge(oven="oven01")
    assert report["ok"] is True
    op_id = report["op_id"]

    # 分段累计账供控制台按作业号取。
    ledger = platform.charge_ledger(op_id)
    assert ledger["oven"] == "oven01"
    assert len(ledger["ledger"]["segments"]) == platform.config.charger.segment_count

    # 审计流水里逐段事件带段号与称重增量，严重度随判定结果。
    entries = [
        entry for entry in platform.audit.entries(limit=256)
        if entry["action"] == "feed_segment" and entry["payload"].get("op_id") == op_id
    ]
    assert sorted(entry["payload"]["segment"] for entry in entries) == [0, 1, 2, 3]
    assert all(entry["severity"] == "notice" for entry in entries)

    status = platform.status()
    assert status["charger"]["segment_total"] == 4
    assert status["weigher"]["healthy"] is True


def test_platform_flags_oven_on_stop_and_on_reconcile_mismatch(platform):
    platform.start()
    report = platform.charge(
        oven="oven07", coal_grade="level_strict", segment_variance_t=[-0.4, 0.0, 0.0, 0.0]
    )
    assert report["ok"] is False
    assert "oven07" in platform.flagged_ovens()


def test_platform_mid_charge_drift_flags_oven_without_estimated_makeup(platform):
    platform.start()
    report = platform.charge(oven="oven08", drift_at_segment=2)
    assert report["stopped"] is True
    assert report["stop_index"] == 2
    # 空窗段进入审计也不允许凑一个数字，三方核对因此不一致并报炉。
    assert report["weighed_t"] is None
    assert report["reconcile"]["consistent"] is False
    assert "oven08" in platform.flagged_ovens()
    alarms = [entry["action"] for entry in platform.audit.entries(limit=256) if entry["severity"] == "alarm"]
    assert "charge_flagged" in alarms
    assert "charge_reconcile_mismatch" in alarms


def test_console_endpoints_cover_segments_and_weigher(platform):
    from coke.console.api import Api

    platform.start()
    api = Api(platform)
    status, body = api.handle("POST", "/api/charge", {}, {"oven": "oven01"})
    assert status == 200 and body["ok"] is True
    status, ledger = api.handle("GET", f"/api/charge-ledger/{body['op_id']}", {}, {})
    assert status == 200 and ledger["reconcile"]["consistent"] is True
    status, flagged = api.handle("GET", "/api/flagged-ovens", {}, {})
    assert status == 200 and flagged == {"ovens": []}
    status, drift = api.handle("POST", "/api/weigher/drift", {}, {})
    assert status == 200 and drift["healthy"] is False
    status, recovered = api.handle("POST", "/api/weigher/recover", {}, {})
    assert status == 200 and recovered["healthy"] is True
