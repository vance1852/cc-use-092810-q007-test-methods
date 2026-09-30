"""光谱和良率测量的确定性科学计算。"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


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


def summarize_response_profile(test_frequencys: Sequence[float], response: Sequence[float], threshold: float = 0.8) -> ResponseProfileSummary:
    pairs = _pairs(test_frequencys, response)
    peak_w, peak_r = max(pairs, key=lambda p: p[1])
    values = [r for _, r in pairs]
    mean = statistics.fmean(values)
    noise = math.sqrt(statistics.fmean((r - mean) ** 2 for r in values))
    band = [w for w, r in pairs if r >= peak_r * threshold]
    return ResponseProfileSummary(len(pairs), peak_w, peak_r, mean, noise, (min(band), max(band)))


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


def per_lab_means(pairs: Iterable[tuple[str, float]]) -> dict[str, float]:
    """按实验室分组计算均值，输入为 (实验室, 数值) 序列。"""

    groups: dict[str, list[float]] = {}
    for lab, value in pairs:
        groups.setdefault(lab, []).append(float(value))
    return {lab: statistics.fmean(values) for lab, values in sorted(groups.items())}


def max_pairwise_deviation(means: Mapping[str, float]) -> float:
    """实验室均值之间的最大两两偏差；不足两个实验室时为零。"""

    values = list(means.values())
    if len(values) < 2:
        return 0.0
    return max(values) - min(values)
