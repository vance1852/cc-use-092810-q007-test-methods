"""光谱和良率测量的确定性科学计算。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class ResponseProfileSummary:
    count: int
    peak_test_frequency_hz: float
    peak_response: float
    mean_response: float
    noise_rms: float
    pass_band_nm: tuple[float, float]


def _pairs(test_frequencys: Sequence[float], response: Sequence[float]) -> list[tuple[float, float]]:
    if len(test_frequencys) != len(response) or len(test_frequencys) < 3:
        raise ValueError("at least three test_frequency/response pairs are required")
    pairs = sorted((float(w), float(r)) for w, r in zip(test_frequencys, response))
    if any(not math.isfinite(w) or not math.isfinite(r) for w, r in pairs):
        raise ValueError("measurements must be finite")
    return pairs


def summarize_response_profile(
    test_frequencys: Sequence[float], response: Sequence[float], threshold: float = 0.8
) -> ResponseProfileSummary:
    pairs = _pairs(test_frequencys, response)
    peak_w, peak_r = max(pairs, key=lambda p: p[1])
    values = [r for _, r in pairs]
    mean = statistics.fmean(values)
    noise = math.sqrt(statistics.fmean((r - mean) ** 2 for r in values))
    band = [w for w, r in pairs if r >= peak_r * threshold]
    return ResponseProfileSummary(len(pairs), peak_w, peak_r, mean, noise, (min(band), max(band)))


def analyze_lot_readings(
    readings: Sequence[tuple[float, float, float]],
    *,
    pass_band_threshold_ratio: float,
    ci_confidence: float,
    yield_operator: str,
    yield_value: float,
) -> dict:
    """按冻结方法版本的公式参数计算批次指标。

    readings 为已换算到规范单位、已剔除后的 (频率, 响应, 噪声) 三元组。
    良率以纳入判定的读数点为分母（同一组件可在多个频点采样）。
    """

    triples = sorted((float(w), float(r), float(n)) for w, r, n in readings)
    if len(triples) < 3:
        raise ValueError("at least three valid readings are required")
    if any(not all(math.isfinite(v) for v in triple) for triple in triples):
        raise ValueError("readings must be finite")
    frequencies = [triple[0] for triple in triples]
    responses = [triple[1] for triple in triples]
    noises = [triple[2] for triple in triples]
    profile = summarize_response_profile(frequencies, responses, pass_band_threshold_ratio)
    low, high = confidence_interval(responses, ci_confidence)
    if yield_operator == "gte":
        passed = sum(1 for value in responses if value >= yield_value)
    else:
        passed = sum(1 for value in responses if value <= yield_value)
    total = len(responses)
    rates = yield_rate(total, passed, 0)
    return {
        "count": profile.count,
        "peak_test_frequency_hz": profile.peak_test_frequency_hz,
        "peak_response": profile.peak_response,
        "mean_response": profile.mean_response,
        "noise_rms": profile.noise_rms,
        "pass_band_hz": list(profile.pass_band_nm),
        "response_ci": [low, high],
        "yield": rates["yield"],
        "reject_rate": rates["reject_rate"],
        "unknown_rate": rates["unknown_rate"],
    }


def confidence_interval(values: Iterable[float], confidence: float = 0.95) -> tuple[float, float]:
    data = [float(v) for v in values]
    if not data or not 0 < confidence < 1:
        raise ValueError("values and confidence are invalid")
    mean = statistics.fmean(data)
    if len(data) == 1:
        return mean, mean
    z = 1.96 if confidence >= 0.95 else 1.645
    margin = z * statistics.stdev(data) / math.sqrt(len(data))
    return mean - margin, mean + margin


def yield_rate(total: int, passed: int, rejected: int = 0) -> dict[str, float]:
    if total <= 0 or passed < 0 or rejected < 0 or passed + rejected > total:
        raise ValueError("inconsistent lot counts")
    return {"yield": passed / total, "reject_rate": rejected / total, "unknown_rate": (total - passed - rejected) / total}


def responsivity(current_ma: float, optical_power_mw: float) -> float:
    if optical_power_mw <= 0:
        raise ValueError("optical power must be positive")
    return current_ma / optical_power_mw
