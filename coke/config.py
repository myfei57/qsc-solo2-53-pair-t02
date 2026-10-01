"""平台配置。

配置来自 JSON 文件（标准库解析），覆盖推焦行程基线、导焦栅对齐公差、集气
管压力带、熄焦喷淋时长、换向间隔、炉温确认带、除尘挡板联动参数与存储目录。
任何缺失或越界取值都在装配阶段抛出 :class:`ConfigError`，不允许带着半份
配置启动控制链。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ConfigError

_MISSING = object()


def _section(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if value is None:
        raise ConfigError(f"配置缺少段落 {key!r}", component="config")
    if not isinstance(value, Mapping):
        raise ConfigError(f"配置段落 {key!r} 必须是映射", component="config")
    return value


def _number(section: Mapping[str, Any], key: str, *, minimum: float | None = None) -> float:
    value = section.get(key, _MISSING)
    if value is _MISSING:
        raise ConfigError(f"配置缺少字段 {key!r}", component="config")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置字段 {key!r} 必须是数值", component="config")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"配置字段 {key!r} 不得小于 {minimum}", component="config")
    return number


@dataclass(frozen=True)
class PusherLimits:
    """推焦机行程与速度约束。"""

    stroke_baseline_mm: float
    stroke_tolerance_mm: float
    max_stroke_mm: float
    speed_min_mm_s: float
    speed_max_mm_s: float
    speed_default_mm_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "PusherLimits":
        limits = cls(
            stroke_baseline_mm=_number(section, "stroke_baseline_mm", minimum=0.0),
            stroke_tolerance_mm=_number(section, "stroke_tolerance_mm", minimum=0.0),
            max_stroke_mm=_number(section, "max_stroke_mm", minimum=1.0),
            speed_min_mm_s=_number(section, "speed_min_mm_s", minimum=0.0),
            speed_max_mm_s=_number(section, "speed_max_mm_s", minimum=0.1),
            speed_default_mm_s=_number(section, "speed_default_mm_s", minimum=0.1),
        )
        if limits.stroke_baseline_mm > limits.max_stroke_mm:
            raise ConfigError("推焦行程基线不得超过最大行程", component="config")
        if not limits.speed_min_mm_s < limits.speed_max_mm_s:
            raise ConfigError("推焦速度下限必须小于上限", component="config")
        if not limits.speed_min_mm_s <= limits.speed_default_mm_s <= limits.speed_max_mm_s:
            raise ConfigError("推焦默认速度必须落在速度上下限之间", component="config")
        return limits


@dataclass(frozen=True)
class GuideLimits:
    """拦焦车导焦栅对齐与锁定约束。"""

    align_tolerance_mm: float
    apron_lock_travel_mm: float
    apron_lock_timeout_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "GuideLimits":
        return cls(
            align_tolerance_mm=_number(section, "align_tolerance_mm", minimum=0.0),
            apron_lock_travel_mm=_number(section, "apron_lock_travel_mm", minimum=1.0),
            apron_lock_timeout_s=_number(section, "apron_lock_timeout_s", minimum=1.0),
        )


@dataclass(frozen=True)
class CoalGradeSettings:
    """单一煤种的装煤核对策略。

    ``deviation_policy`` 按煤种定死段偏差超限后的处置：``stop_report``
    先停后续给煤并把这一炉报出来；``correct_continue`` 用在线称重差值做
    一次修正给煤后继续。任何煤种在称重漂移（部分失败）时都不许按估数补足，
    策略字段管不到这一条。
    """

    name: str
    deviation_policy: str
    segment_tolerance_t: float
    segment_tolerance_ratio: float
    trim_budget_t: float

    @classmethod
    def from_mapping(cls, name: str, section: Mapping[str, Any]) -> "CoalGradeSettings":
        policy = section.get("deviation_policy", _MISSING)
        if policy is _MISSING:
            raise ConfigError(f"煤种 {name!r} 缺少字段 deviation_policy", component="config")
        if policy not in DEVIATION_POLICIES:
            raise ConfigError(
                f"煤种 {name!r} 的 deviation_policy 必须是 {DEVIATION_POLICIES} 之一",
                component="config",
            )
        settings = cls(
            name=name,
            deviation_policy=str(policy),
            segment_tolerance_t=_number(section, "segment_tolerance_t", minimum=0.0),
            segment_tolerance_ratio=_number(section, "segment_tolerance_ratio", minimum=0.0),
            trim_budget_t=_number(section, "trim_budget_t", minimum=0.0),
        )
        if settings.segment_tolerance_t <= 0.0 and settings.segment_tolerance_ratio <= 0.0:
            raise ConfigError(
                f"煤种 {name!r} 的段公差绝对量与比例不能同时为零", component="config"
            )
        if settings.deviation_policy == POLICY_CORRECT_CONTINUE and settings.trim_budget_t <= 0.0:
            raise ConfigError(
                f"煤种 {name!r} 采用修正续给策略时 trim_budget_t 必须为正",
                component="config",
            )
        return settings

    def tolerance_for(self, segment_target_t: float) -> float:
        """段公差取绝对量与比例量的较大者，避免小目标段被比例量卡死。"""

        return max(self.segment_tolerance_t, self.segment_tolerance_ratio * segment_target_t)

    def as_mapping(self) -> dict:
        return {
            "deviation_policy": self.deviation_policy,
            "segment_tolerance_t": self.segment_tolerance_t,
            "segment_tolerance_ratio": self.segment_tolerance_ratio,
            "trim_budget_t": self.trim_budget_t,
        }


POLICY_STOP_REPORT = "stop_report"
POLICY_CORRECT_CONTINUE = "correct_continue"
DEVIATION_POLICIES = (POLICY_STOP_REPORT, POLICY_CORRECT_CONTINUE)
DEFAULT_GRADE = "default"


@dataclass(frozen=True)
class ChargerLimits:
    """装煤车煤斗对齐、分段给煤与煤种策略约束。"""

    align_tolerance_mm: float
    coal_target_t: float
    feed_rate_t_per_min: float
    segment_count: int
    coal_grades: dict[str, CoalGradeSettings]

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "ChargerLimits":
        align_tolerance_mm = _number(section, "align_tolerance_mm", minimum=0.0)
        if align_tolerance_mm <= 0.0:
            raise ConfigError("煤斗对齐公差必须为正", component="config")
        segment_count = int(_number(section, "segment_count", minimum=1.0))
        grades_section = section.get("coal_grades", _MISSING)
        if grades_section is _MISSING:
            raise ConfigError("配置缺少字段 coal_grades", component="config")
        if not isinstance(grades_section, Mapping):
            raise ConfigError("字段 coal_grades 必须是映射", component="config")
        grades = {
            str(name): CoalGradeSettings.from_mapping(str(name), grade)
            for name, grade in grades_section.items()
            if isinstance(grade, Mapping)
        }
        if len(grades) != len(grades_section):
            raise ConfigError("煤种配置每一项都必须是映射", component="config")
        if DEFAULT_GRADE not in grades:
            raise ConfigError(f"coal_grades 必须包含兜底煤种 {DEFAULT_GRADE!r}", component="config")
        return cls(
            align_tolerance_mm=align_tolerance_mm,
            coal_target_t=_number(section, "coal_target_t", minimum=0.1),
            feed_rate_t_per_min=_number(section, "feed_rate_t_per_min", minimum=0.1),
            segment_count=segment_count,
            coal_grades=grades,
        )

    def grade(self, name: str | None = None) -> CoalGradeSettings:
        """按名取煤种策略；未登记的煤种在运行期按联锁拒绝处理。"""

        from .errors import InterlockError

        key = name or DEFAULT_GRADE
        try:
            return self.coal_grades[key]
        except KeyError:
            raise InterlockError(f"未登记的煤种: {key}", component="charger") from None

    def segment_targets(self) -> list[float]:
        """按炭化室容量等分整炉给煤量，余量并入末段，避免累计漂移。"""

        base = round(self.coal_target_t / self.segment_count, 3)
        targets = [base] * self.segment_count
        targets[-1] = round(self.coal_target_t - base * (self.segment_count - 1), 3)
        return targets


@dataclass(frozen=True)
class QuenchLimits:
    """熄焦车走行与喷淋约束。"""

    travel_distance_m: float
    travel_speed_m_s: float
    spray_duration_s: float
    water_flow_m3_h: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "QuenchLimits":
        return cls(
            travel_distance_m=_number(section, "travel_distance_m", minimum=1.0),
            travel_speed_m_s=_number(section, "travel_speed_m_s", minimum=0.1),
            spray_duration_s=_number(section, "spray_duration_s", minimum=1.0),
            water_flow_m3_h=_number(section, "water_flow_m3_h", minimum=1.0),
        )


@dataclass(frozen=True)
class CollectorLimits:
    """集气管压力与调节约束。"""

    pressure_target_pa: float
    pressure_band_pa: float
    regulate_rate_pa_per_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "CollectorLimits":
        limits = cls(
            pressure_target_pa=_number(section, "pressure_target_pa"),
            pressure_band_pa=_number(section, "pressure_band_pa", minimum=1.0),
            regulate_rate_pa_per_s=_number(section, "regulate_rate_pa_per_s", minimum=0.1),
        )
        if limits.pressure_target_pa <= 0.0:
            raise ConfigError("集气管压力目标必须为正值", component="config")
        return limits

    def settled(self, pressure_pa: float) -> bool:
        """压力是否进入目标带。"""

        return abs(pressure_pa - self.pressure_target_pa) <= self.pressure_band_pa


@dataclass(frozen=True)
class DoorLimits:
    """炉门清扫闩锁约束。"""

    latch_travel_mm: float
    clean_duration_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "DoorLimits":
        return cls(
            latch_travel_mm=_number(section, "latch_travel_mm", minimum=1.0),
            clean_duration_s=_number(section, "clean_duration_s", minimum=1.0),
        )


@dataclass(frozen=True)
class HeatingLimits:
    """加热换向与炉温确认约束。"""

    reversal_interval_s: float
    temp_target_c: float
    temp_band_c: float
    waste_close_lead_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "HeatingLimits":
        limits = cls(
            reversal_interval_s=_number(section, "reversal_interval_s", minimum=1.0),
            temp_target_c=_number(section, "temp_target_c", minimum=1.0),
            temp_band_c=_number(section, "temp_band_c", minimum=1.0),
            waste_close_lead_s=_number(section, "waste_close_lead_s", minimum=0.0),
        )
        if limits.waste_close_lead_s >= limits.reversal_interval_s:
            raise ConfigError("废气阀提前关闭时间必须小于换向间隔", component="config")
        return limits

    def confirmed(self, temperature_c: float) -> bool:
        """炉温是否落在确认带内。"""

        return abs(temperature_c - self.temp_target_c) <= self.temp_band_c


@dataclass(frozen=True)
class DustLimits:
    """除尘挡板联动约束。"""

    damper_open_deg: float
    damper_rate_deg_per_s: float
    linkage_lag_s: float

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "DustLimits":
        limits = cls(
            damper_open_deg=_number(section, "damper_open_deg", minimum=1.0),
            damper_rate_deg_per_s=_number(section, "damper_rate_deg_per_s", minimum=0.1),
            linkage_lag_s=_number(section, "linkage_lag_s", minimum=0.0),
        )
        if limits.damper_open_deg > 90.0:
            raise ConfigError("除尘挡板开度不得超过 90 度", component="config")
        return limits


@dataclass(frozen=True)
class PlatformConfig:
    """一份完整的平台配置。"""

    name: str
    storage_root: str
    pusher: PusherLimits
    guide: GuideLimits
    charger: ChargerLimits
    quench: QuenchLimits
    collector: CollectorLimits
    door: DoorLimits
    heating: HeatingLimits
    dust: DustLimits

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "PlatformConfig":
        meta = _section(mapping, "platform")
        name = meta.get("name")
        storage_root = meta.get("storage_root")
        if not isinstance(name, str) or not name:
            raise ConfigError("platform.name 必须是非空字符串", component="config")
        if not isinstance(storage_root, str) or not storage_root:
            raise ConfigError("platform.storage_root 必须是非空字符串", component="config")

        limits = _section(mapping, "limits")
        return cls(
            name=name,
            storage_root=storage_root,
            pusher=PusherLimits.from_mapping(_section(limits, "pusher")),
            guide=GuideLimits.from_mapping(_section(limits, "guide")),
            charger=ChargerLimits.from_mapping(_section(limits, "charger")),
            quench=QuenchLimits.from_mapping(_section(limits, "quench")),
            collector=CollectorLimits.from_mapping(_section(limits, "collector")),
            door=DoorLimits.from_mapping(_section(limits, "door")),
            heating=HeatingLimits.from_mapping(_section(limits, "heating")),
            dust=DustLimits.from_mapping(_section(limits, "dust")),
        )


def load_config(path: str) -> PlatformConfig:
    """从 JSON 文件读取配置。"""

    if not os.path.isfile(path):
        raise ConfigError(f"配置文件不存在: {path}", component="config")
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, Mapping):
        raise ConfigError("配置文件顶层必须是映射", component="config")
    return PlatformConfig.from_mapping(raw)


def default_config() -> PlatformConfig:
    """内置默认配置，供未指定配置文件时装配使用。"""

    return PlatformConfig(
        name="sequence-control",
        storage_root="var/state",
        pusher=PusherLimits(3200.0, 25.0, 3350.0, 12.0, 38.0, 24.0),
        guide=GuideLimits(15.0, 120.0, 20.0),
        charger=ChargerLimits(
            20.0,
            28.0,
            4.0,
            4,
            {
                DEFAULT_GRADE: CoalGradeSettings(
                    DEFAULT_GRADE, POLICY_CORRECT_CONTINUE, 0.15, 0.02, 0.5
                ),
                "level_strict": CoalGradeSettings(
                    "level_strict", POLICY_STOP_REPORT, 0.12, 0.015, 0.0
                ),
            },
        ),
        quench=QuenchLimits(42.0, 1.6, 90.0, 320.0),
        collector=CollectorLimits(90.0, 18.0, 6.0),
        door=DoorLimits(60.0, 45.0),
        heating=HeatingLimits(1800.0, 1180.0, 35.0, 4.0),
        dust=DustLimits(70.0, 14.0, 1.5),
    )


def config_to_mapping(cfg: PlatformConfig) -> dict:
    """把配置还原成可打印的映射。"""

    return {
        "platform": {"name": cfg.name, "storage_root": cfg.storage_root},
        "limits": {
            "pusher": {
                "stroke_baseline_mm": cfg.pusher.stroke_baseline_mm,
                "stroke_tolerance_mm": cfg.pusher.stroke_tolerance_mm,
                "max_stroke_mm": cfg.pusher.max_stroke_mm,
                "speed_min_mm_s": cfg.pusher.speed_min_mm_s,
                "speed_max_mm_s": cfg.pusher.speed_max_mm_s,
                "speed_default_mm_s": cfg.pusher.speed_default_mm_s,
            },
            "guide": {
                "align_tolerance_mm": cfg.guide.align_tolerance_mm,
                "apron_lock_travel_mm": cfg.guide.apron_lock_travel_mm,
                "apron_lock_timeout_s": cfg.guide.apron_lock_timeout_s,
            },
            "charger": {
                "align_tolerance_mm": cfg.charger.align_tolerance_mm,
                "coal_target_t": cfg.charger.coal_target_t,
                "feed_rate_t_per_min": cfg.charger.feed_rate_t_per_min,
                "segment_count": cfg.charger.segment_count,
                "coal_grades": {
                    name: grade.as_mapping() for name, grade in cfg.charger.coal_grades.items()
                },
            },
            "quench": {
                "travel_distance_m": cfg.quench.travel_distance_m,
                "travel_speed_m_s": cfg.quench.travel_speed_m_s,
                "spray_duration_s": cfg.quench.spray_duration_s,
                "water_flow_m3_h": cfg.quench.water_flow_m3_h,
            },
            "collector": {
                "pressure_target_pa": cfg.collector.pressure_target_pa,
                "pressure_band_pa": cfg.collector.pressure_band_pa,
                "regulate_rate_pa_per_s": cfg.collector.regulate_rate_pa_per_s,
            },
            "door": {
                "latch_travel_mm": cfg.door.latch_travel_mm,
                "clean_duration_s": cfg.door.clean_duration_s,
            },
            "heating": {
                "reversal_interval_s": cfg.heating.reversal_interval_s,
                "temp_target_c": cfg.heating.temp_target_c,
                "temp_band_c": cfg.heating.temp_band_c,
                "waste_close_lead_s": cfg.heating.waste_close_lead_s,
            },
            "dust": {
                "damper_open_deg": cfg.dust.damper_open_deg,
                "damper_rate_deg_per_s": cfg.dust.damper_rate_deg_per_s,
                "linkage_lag_s": cfg.dust.linkage_lag_s,
            },
        },
    }
