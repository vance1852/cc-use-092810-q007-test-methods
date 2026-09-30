"""检测方法规范：发布冻结、单位换算、量程与判定阈值、跨实验室容差。

方法是跨实验室可比性的锚点。方法一经发布，其适用产品、采样要求、
指标公式、量程、判定阈值与跨实验室容差即被冻结为规范化 JSON 并生成
SHA-256 指纹；后续任何换算、剔除、合并与放行都必须引用确定的
(method_id, revision)。内容需要变化时只能发布后继版本。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

SUPPORTED_FORMULAS = frozenset({"summarize_response_profile_v1"})
KNOWN_CHANNELS = frozenset({"test_frequency", "response", "noise"})
JUDGEABLE_METRICS = frozenset({"peak_response", "mean_response", "noise_rms", "yield"})
COMPARABLE_METRICS = frozenset({"peak_response", "mean_response", "noise_rms"})
OPERATORS = frozenset({"gte", "lte"})


class MethodValidationError(ValueError):
    """方法规范不能满足治理契约。"""


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MethodValidationError(f"{path} 必须是对象")
    return value


def _sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MethodValidationError(f"{path} 必须是数组")
    return value


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MethodValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MethodValidationError(f"{path} 必须是数值")
    result = float(value)
    if not math.isfinite(result):
        raise MethodValidationError(f"{path} 必须是有限数值")
    return result


@dataclass(frozen=True, slots=True)
class UnitConversion:
    """某输入单位到规范单位的线性换算：canonical = value * factor + offset。"""

    unit: str
    factor: float
    offset: float

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "UnitConversion":
        data = _mapping(raw, path)
        factor = _number(data.get("factor"), f"{path}.factor")
        offset = _number(data.get("offset", 0.0), f"{path}.offset")
        if factor == 0:
            raise MethodValidationError(f"{path}.factor 不能为零")
        return cls(_text(data.get("unit"), f"{path}.unit"), factor, offset)


@dataclass(frozen=True, slots=True)
class Channel:
    key: str
    canonical_unit: str
    units: tuple[UnitConversion, ...]

    @classmethod
    def from_dict(cls, key: str, raw: object) -> "Channel":
        data = _mapping(raw, f"spec.channels.{key}")
        units = tuple(
            UnitConversion.from_dict(item, f"spec.channels.{key}.units[{i}]")
            for i, item in enumerate(_sequence(data.get("units"), f"spec.channels.{key}.units"))
        )
        unit_names = [item.unit for item in units]
        if len(unit_names) != len(set(unit_names)):
            raise MethodValidationError(f"spec.channels.{key}.units 单位重复")
        canonical = _text(data.get("canonical_unit"), f"spec.channels.{key}.canonical_unit")
        if canonical not in unit_names:
            raise MethodValidationError(f"spec.channels.{key} 的规范单位必须出现在换算表中")
        return cls(key, canonical, units)

    def convert(self, value: float, unit: str | None) -> float:
        target = unit or self.canonical_unit
        for item in self.units:
            if item.unit == target:
                return value * item.factor + item.offset
        raise MethodValidationError(f"通道 {self.key} 未声明单位 {target}")


@dataclass(frozen=True, slots=True)
class Sampling:
    min_points: int
    frequency_window_hz: tuple[float, float]

    @classmethod
    def from_dict(cls, raw: object) -> "Sampling":
        data = _mapping(raw, "spec.sampling")
        min_points = data.get("min_points")
        if isinstance(min_points, bool) or not isinstance(min_points, int) or min_points < 1:
            raise MethodValidationError("spec.sampling.min_points 必须是正整数")
        window = _sequence(data.get("frequency_window_hz"), "spec.sampling.frequency_window_hz")
        if len(window) != 2:
            raise MethodValidationError("spec.sampling.frequency_window_hz 必须是 [下限, 上限]")
        low, high = _number(window[0], "spec.sampling.frequency_window_hz[0]"), _number(
            window[1], "spec.sampling.frequency_window_hz[1]"
        )
        if low >= high:
            raise MethodValidationError("spec.sampling.frequency_window_hz 下限必须小于上限")
        return cls(min_points, (low, high))


@dataclass(frozen=True, slots=True)
class ThresholdRule:
    metric: str
    operator: str
    value: float

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "ThresholdRule":
        data = _mapping(raw, path)
        metric = _text(data.get("metric"), f"{path}.metric")
        if metric not in JUDGEABLE_METRICS:
            raise MethodValidationError(f"{path}.metric 不受支持：{metric}")
        operator = _text(data.get("operator"), f"{path}.operator")
        if operator not in OPERATORS:
            raise MethodValidationError(f"{path}.operator 必须是 gte 或 lte")
        return cls(metric, operator, _number(data.get("value"), f"{path}.value"))


@dataclass(frozen=True, slots=True)
class CrossLabTolerance:
    metric: str
    max_abs: float | None
    max_rel: float | None

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "CrossLabTolerance":
        data = _mapping(raw, path)
        metric = _text(data.get("metric"), f"{path}.metric")
        if metric not in COMPARABLE_METRICS:
            raise MethodValidationError(f"{path}.metric 不能用于跨实验室比较：{metric}")
        max_abs = data.get("max_abs")
        max_rel = data.get("max_rel")
        if max_abs is None and max_rel is None:
            raise MethodValidationError(f"{path} 至少声明 max_abs 或 max_rel")
        return cls(
            metric,
            None if max_abs is None else _number(max_abs, f"{path}.max_abs"),
            None if max_rel is None else _number(max_rel, f"{path}.max_rel"),
        )


@dataclass(frozen=True, slots=True)
class MethodSpec:
    method_id: str
    title: str
    applicable_products: tuple[str, ...]
    channels: dict[str, Channel]
    sampling: Sampling
    formulas: dict[str, Any]
    ranges: dict[str, tuple[float, float]]
    thresholds: tuple[ThresholdRule, ...]
    cross_lab: tuple[CrossLabTolerance, ...]

    @classmethod
    def from_dict(cls, raw: object) -> "MethodSpec":
        data = _mapping(raw, "spec")
        method_id = _text(data.get("method_id"), "spec.method_id")
        title = _text(data.get("title"), "spec.title")
        products = tuple(
            _text(item, f"spec.applicable_products[{i}]")
            for i, item in enumerate(_sequence(data.get("applicable_products"), "spec.applicable_products"))
        )
        if not products or len(set(products)) != len(products):
            raise MethodValidationError("spec.applicable_products 至少包含一个且不重复的产品")
        channel_data = _mapping(data.get("channels"), "spec.channels")
        if set(channel_data) != KNOWN_CHANNELS:
            raise MethodValidationError(f"spec.channels 必须恰好声明 {sorted(KNOWN_CHANNELS)}")
        channels = {key: Channel.from_dict(key, value) for key, value in channel_data.items()}
        sampling = Sampling.from_dict(data.get("sampling"))
        formula_data = _mapping(data.get("formulas"), "spec.formulas")
        formula_id = _text(formula_data.get("profile"), "spec.formulas.profile")
        if formula_id not in SUPPORTED_FORMULAS:
            raise MethodValidationError(f"spec.formulas.profile 不受支持：{formula_id}")
        confidence = _number(formula_data.get("ci_confidence", 0.95), "spec.formulas.ci_confidence")
        if not 0.5 < confidence < 1:
            raise MethodValidationError("spec.formulas.ci_confidence 必须在 0.5 与 1 之间")
        threshold_ratio = _number(
            formula_data.get("pass_band_threshold_ratio", 0.8), "spec.formulas.pass_band_threshold_ratio"
        )
        if not 0 < threshold_ratio <= 1:
            raise MethodValidationError("spec.formulas.pass_band_threshold_ratio 必须在 (0,1] 内")
        formulas = {"profile": formula_id, "ci_confidence": confidence, "pass_band_threshold_ratio": threshold_ratio}
        ranges: dict[str, tuple[float, float]] = {}
        range_data = data.get("ranges")
        if range_data is not None:
            for key, bounds in _mapping(range_data, "spec.ranges").items():
                if key not in KNOWN_CHANNELS:
                    raise MethodValidationError(f"spec.ranges.{key} 不是已声明通道")
                pair = _sequence(bounds, f"spec.ranges.{key}")
                if len(pair) != 2:
                    raise MethodValidationError(f"spec.ranges.{key} 必须是 [下限, 上限]")
                low = _number(pair[0], f"spec.ranges.{key}[0]")
                high = _number(pair[1], f"spec.ranges.{key}[1]")
                if low >= high:
                    raise MethodValidationError(f"spec.ranges.{key} 下限必须小于上限")
                ranges[key] = (low, high)
        threshold_data = _sequence(data.get("thresholds"), "spec.thresholds")
        if not threshold_data:
            raise MethodValidationError("spec.thresholds 至少包含一条判定规则")
        thresholds = tuple(
            ThresholdRule.from_dict(item, f"spec.thresholds[{i}]") for i, item in enumerate(threshold_data)
        )
        metric_names = [rule.metric for rule in thresholds]
        if len(metric_names) != len(set(metric_names)):
            raise MethodValidationError("spec.thresholds 同一指标不能重复判定")
        cross_lab: tuple[CrossLabTolerance, ...] = ()
        cross_data = data.get("cross_lab")
        if cross_data is not None:
            tolerances = _sequence(_mapping(cross_data, "spec.cross_lab").get("tolerances"), "spec.cross_lab.tolerances")
            cross_lab = tuple(
                CrossLabTolerance.from_dict(item, f"spec.cross_lab.tolerances[{i}]")
                for i, item in enumerate(tolerances)
            )
        return cls(method_id, title, products, channels, sampling, formulas, ranges, thresholds, cross_lab)

    def applies_to(self, product: str) -> bool:
        return product in self.applicable_products

    @property
    def cross_lab_metrics(self) -> frozenset[str]:
        return frozenset(item.metric for item in self.cross_lab)

    def convert_reading(self, channel: str, value: float, unit: str | None) -> float:
        return self.channels[channel].convert(value, unit)

    def in_range(self, channel: str, value: float) -> bool:
        bounds = self.ranges.get(channel)
        return bounds is None or bounds[0] <= value <= bounds[1]

    def evaluate(self, metrics: Mapping[str, float]) -> tuple[list[dict[str, Any]], str]:
        results = []
        for rule in self.thresholds:
            if rule.metric not in metrics:
                raise MethodValidationError(f"指标 {rule.metric} 尚未计算，无法判定")
            actual = float(metrics[rule.metric])
            passed = actual >= rule.value if rule.operator == "gte" else actual <= rule.value
            results.append(
                {
                    "metric": rule.metric,
                    "operator": rule.operator,
                    "threshold": rule.value,
                    "actual": actual,
                    "pass": bool(passed),
                }
            )
        return results, "pass" if all(item["pass"] for item in results) else "fail"

    def cross_lab_differences(
        self, lab_metrics: Mapping[str, Mapping[str, float]]
    ) -> tuple[list[dict[str, Any]], str]:
        differences: list[dict[str, Any]] = []
        biased = False
        for tolerance in self.cross_lab:
            values = {lab: float(metrics[tolerance.metric]) for lab, metrics in lab_metrics.items()}
            spread_abs = max(values.values()) - min(values.values())
            mean = sum(values.values()) / len(values)
            spread_rel = abs(spread_abs / mean) if mean != 0 else None
            within = True
            if tolerance.max_abs is not None:
                within = within and spread_abs <= tolerance.max_abs
            if tolerance.max_rel is not None and spread_rel is not None:
                within = within and spread_rel <= tolerance.max_rel
            biased = biased or not within
            differences.append(
                {
                    "metric": tolerance.metric,
                    "lab_values": values,
                    "spread_abs": spread_abs,
                    "spread_rel": spread_rel,
                    "max_abs": tolerance.max_abs,
                    "max_rel": tolerance.max_rel,
                    "within_tolerance": bool(within),
                }
            )
        return differences, ("within_tolerance" if not biased else "systematic_bias")


def canonical_json(spec: Mapping[str, Any]) -> str:
    """发布冻结内容：键排序、无空白的规范化 JSON。"""
    return json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_fingerprint(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
