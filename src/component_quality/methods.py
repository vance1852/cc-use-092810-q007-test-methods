"""检测方法版本的冻结契约、单位换算与内容摘要。

方法版本一经发布即不可变：适用产品、采样要求、指标公式、量程和判定阈值
都冻结在 definition_json 中，任何换算、剔除、合并与判定都必须引用确定的方法版本。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping


class MethodValidationError(ValueError):
    """方法版本定义不满足冻结契约。"""


REQUIRED_FORMULAS = {
    "response_profile": "summarize_response_profile",
    "response_ci": "confidence_interval",
    "yield": "yield_rate",
}

THRESHOLD_KEYS = ("reading_pass_response", "response_min", "yield_min", "interlab_max_deviation")


def canonical_json(value: object) -> str:
    """生成跨平台一致的紧凑 JSON 文本。"""

    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def content_digest(text: str) -> str:
    """对规范化文本计算 SHA-256 内容摘要。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fail(path: str, message: str) -> None:
    raise MethodValidationError(f"{path} {message}")


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "必须是非空字符串")
    return value.strip()


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(path, "必须是数值")
    result = float(value)
    if not math.isfinite(result):
        _fail(path, "必须是有限数值")
    return result


def _positive(value: object, path: str) -> float:
    result = _number(value, path)
    if result <= 0:
        _fail(path, "必须大于零")
    return result


def _mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(path, "必须是对象")
    return value


def _string_list(value: object, path: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        _fail(path, "必须是非空数组")
    items = [_text(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if len(set(items)) != len(items):
        _fail(path, "不能重复")
    return list(items)


def validate_definition(raw: object) -> dict:
    """校验并规范化方法定义；通过后即可冻结为不可变版本。"""

    data = _mapping(raw, "method")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        _fail("method.version", "必须是正整数")
    products = _string_list(data.get("applicable_products"), "method.applicable_products")

    sampling = _mapping(data.get("sampling_requirements"), "method.sampling_requirements")
    min_samples = sampling.get("min_samples")
    if isinstance(min_samples, bool) or not isinstance(min_samples, int) or min_samples < 1:
        _fail("method.sampling_requirements.min_samples", "必须是正整数")
    raw_freqs = sampling.get("required_frequencies_hz", [])
    if not isinstance(raw_freqs, (list, tuple)):
        _fail("method.sampling_requirements.required_frequencies_hz", "必须是数组")
    freqs = [
        _positive(item, f"method.sampling_requirements.required_frequencies_hz[{index}]")
        for index, item in enumerate(raw_freqs)
    ]
    if len(set(freqs)) != len(freqs):
        _fail("method.sampling_requirements.required_frequencies_hz", "不能重复")

    formulas_raw = _mapping(data.get("metric_formulas"), "method.metric_formulas")
    if set(formulas_raw) != set(REQUIRED_FORMULAS):
        _fail("method.metric_formulas", f"必须恰好包含 {sorted(REQUIRED_FORMULAS)}")
    formulas: dict[str, dict] = {}
    for key, function in REQUIRED_FORMULAS.items():
        spec = _mapping(formulas_raw[key], f"method.metric_formulas.{key}")
        if spec.get("function") != function:
            _fail(f"method.metric_formulas.{key}.function", f"必须是 {function}")
        params = dict(_mapping(spec.get("params", {}), f"method.metric_formulas.{key}.params"))
        cleaned: dict[str, float] = {}
        if key == "response_profile":
            threshold = _number(params.pop("threshold", None), f"method.metric_formulas.{key}.params.threshold")
            if not 0 < threshold < 1:
                _fail(f"method.metric_formulas.{key}.params.threshold", "必须在 0 和 1 之间")
            cleaned["threshold"] = threshold
        elif key == "response_ci":
            confidence = _number(params.pop("confidence", None), f"method.metric_formulas.{key}.params.confidence")
            if not 0 < confidence < 1:
                _fail(f"method.metric_formulas.{key}.params.confidence", "必须在 0 和 1 之间")
            cleaned["confidence"] = confidence
        if params:
            _fail(f"method.metric_formulas.{key}.params", f"含未支持的参数 {sorted(params)}")
        formulas[key] = {"function": function, "params": cleaned}

    conversions_raw = _mapping(data.get("unit_conversions"), "method.unit_conversions")
    if not conversions_raw:
        _fail("method.unit_conversions", "不能为空")
    conversions: dict[str, dict] = {}
    for unit, spec_raw in conversions_raw.items():
        unit_name = _text(unit, "method.unit_conversions.<unit>")
        spec = _mapping(spec_raw, f"method.unit_conversions.{unit_name}")
        conversions[unit_name] = {"factor": _positive(spec.get("factor"), f"method.unit_conversions.{unit_name}.factor")}

    range_raw = _mapping(data.get("measurement_range"), "method.measurement_range")
    canonical_unit = _text(range_raw.get("unit"), "method.measurement_range.unit")
    range_min = _number(range_raw.get("min"), "method.measurement_range.min")
    range_max = _number(range_raw.get("max"), "method.measurement_range.max")
    if not range_min < range_max:
        _fail("method.measurement_range", "min 必须小于 max")
    anchor = conversions.get(canonical_unit)
    if anchor is None or anchor["factor"] != 1.0:
        _fail("method.unit_conversions", "必须包含量程单位且换算系数为 1")

    thresholds_raw = _mapping(data.get("decision_thresholds"), "method.decision_thresholds")
    if set(thresholds_raw) != set(THRESHOLD_KEYS):
        _fail("method.decision_thresholds", f"必须恰好包含 {sorted(THRESHOLD_KEYS)}")
    thresholds = {key: _number(thresholds_raw[key], f"method.decision_thresholds.{key}") for key in THRESHOLD_KEYS}
    if not 0 <= thresholds["yield_min"] <= 1:
        _fail("method.decision_thresholds.yield_min", "必须在 0 和 1 之间")
    if thresholds["interlab_max_deviation"] <= 0:
        _fail("method.decision_thresholds.interlab_max_deviation", "必须大于零")

    return {
        "method_id": _text(data.get("method_id"), "method.method_id"),
        "version": version,
        "title": _text(data.get("title"), "method.title"),
        "applicable_products": products,
        "sampling_requirements": {"min_samples": min_samples, "required_frequencies_hz": freqs},
        "metric_formulas": formulas,
        "unit_conversions": conversions,
        "measurement_range": {"min": range_min, "max": range_max, "unit": canonical_unit},
        "decision_thresholds": thresholds,
    }


def convert_to_canonical(definition: Mapping[str, Any], raw_value: float, raw_unit: str) -> float:
    """按方法版本冻结的换算表把原始读数换算为量程单位。"""

    spec = definition["unit_conversions"].get(raw_unit)
    if spec is None:
        raise ValueError(f"方法 {definition['method_id']}@{definition['version']} 不支持原始单位 {raw_unit}")
    return float(raw_value) * spec["factor"]


def in_measurement_range(definition: Mapping[str, Any], value: float) -> bool:
    """判断换算后的读数是否落在方法版本冻结的量程内。"""

    span = definition["measurement_range"]
    return span["min"] <= value <= span["max"]
