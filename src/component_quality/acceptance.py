"""方法治理离线验收：发布冻结 → 校准门禁 → 版本化结论 → 跨实验室调查 → 后继方法复评。"""

from __future__ import annotations

import argparse
import json

from .service import ComponentQualityService


def method_spec(*, noise_limit: float) -> dict:
    return {
        "method_id": "M-CELL-SPECTRAL",
        "title": "电芯组件光谱响应检测方法",
        "applicable_products": ["电芯组件-A"],
        "channels": {
            "test_frequency": {
                "canonical_unit": "Hz",
                "units": [{"unit": "Hz", "factor": 1.0, "offset": 0.0},
                          {"unit": "kHz", "factor": 1000.0, "offset": 0.0}],
            },
            "response": {
                "canonical_unit": "ratio",
                "units": [{"unit": "ratio", "factor": 1.0, "offset": 0.0},
                          {"unit": "percent", "factor": 0.01, "offset": 0.0}],
            },
            "noise": {
                "canonical_unit": "ratio",
                "units": [{"unit": "ratio", "factor": 1.0, "offset": 0.0},
                          {"unit": "percent", "factor": 0.01, "offset": 0.0}],
            },
        },
        "sampling": {"min_points": 3, "frequency_window_hz": [400.0, 700.0]},
        "formulas": {
            "profile": "summarize_response_profile_v1",
            "ci_confidence": 0.95,
            "pass_band_threshold_ratio": 0.8,
        },
        "ranges": {
            "test_frequency": [400.0, 700.0],
            "response": [0.0, 2.0],
            "noise": [0.0, 1.0],
        },
        "thresholds": [
            {"metric": "yield", "operator": "gte", "value": 0.8},
            {"metric": "noise_rms", "operator": "lte", "value": noise_limit},
        ],
        "cross_lab": {
            "tolerances": [
                {"metric": "peak_response", "max_abs": 0.05},
                {"metric": "mean_response", "max_rel": 0.08},
            ]
        },
    }


def run() -> dict:
    service = ComponentQualityService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "component-admin")

    # 1. 发布冻结的方法版本（适用产品、采样、公式、量程、阈值、跨实验室容差）
    v1 = service.publish_method(token, method_spec(noise_limit=0.20))
    fingerprint_v1 = v1["fingerprint"]

    # 2. 两家实验室、两台仪器；校准证书 + 参考样件比对通过后才可用于正式批次
    for lab, instrument, calibration, observed in (
        ("LAB-1", "SPEC-1", "CAL-1", 0.902),
        ("LAB-2", "SPEC-2", "CAL-2", 0.898),
    ):
        service.register_laboratory(token, lab, f"{lab} 检测实验室")
        service.register_instrument(token, instrument, lab, f"SN-{instrument}")
        service.record_calibration(
            token, calibration, instrument, "M-CELL-SPECTRAL", 1,
            f"CERT-{calibration}", "2026-09-01", "2027-09-01")
        service.record_reference_check(
            token, calibration, "REF-MASTER-07", 0.9, "ratio", observed, "ratio", 0.01)

    # 3. 两个正式批次绑定方法版本；读数保留原值原单位与设备身份（91 percent 按方法换算）
    service.create_lot(token, "LOT-PENDING", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
    for frequency, response, unit in ((450, 0.86, "ratio"), (520, 0.93, "ratio"), (650, 91, "percent")):
        service.add_measurement(
            token, "LOT-PENDING", frequency, response, 0.01, "SPEC-1",
            lab_id="LAB-1", calibration_id="CAL-1",
            units={"test_frequency": "Hz", "response": unit, "noise": "ratio"})
    service.create_lot(token, "LOT-RELEASED", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
    for frequency, response in ((460, 0.88), (530, 0.90), (640, 0.89)):
        service.add_measurement(
            token, "LOT-RELEASED", frequency, response, 0.01, "SPEC-2",
            lab_id="LAB-2", calibration_id="CAL-2",
            units={"test_frequency": "Hz", "response": "ratio", "noise": "ratio"})

    # 4. 版本化分析（结论引用方法指纹、换算与校准证据），已放行批次先出具不可变报告
    analysis_pending = service.run_analysis(token, "LOT-PENDING")
    analysis_released = service.run_analysis(token, "LOT-RELEASED")
    report = service.issue_report(token, "LOT-RELEASED", "release", "方法 r1 结论 pass，准予放行")

    # 5. 跨实验室同一样件比对出现系统偏差：暂停未决放行批次并形成调查版本
    comparison = service.record_comparison(
        token, "CMP-001", "M-CELL-SPECTRAL", 1, "REF-MASTER-07",
        [{"lab_id": "LAB-1", "metrics": {"peak_response": 0.93, "mean_response": 0.90}},
         {"lab_id": "LAB-2", "metrics": {"peak_response": 0.80, "mean_response": 0.84}}])
    investigation_id = comparison["investigation_id"]
    blocked = False
    try:
        service.issue_report(token, "LOT-PENDING", "release", "试图在暂停期间放行")
    except PermissionError:
        blocked = True

    # 6. 已出具的报告不被静默重算；新方法只能通过复评产生后继结论
    fingerprint_v2 = None
    if investigation_id:
        v2 = service.publish_method(token, method_spec(noise_limit=0.18))
        fingerprint_v2 = v2["fingerprint"]
        service.close_investigation(
            token, investigation_id, "M-CELL-SPECTRAL", 2,
            "按 r2 收紧噪声阈值并重新比对，系统偏差来自算法版本，已纠正")
        reanalysis = service.run_analysis(token, "LOT-PENDING", "M-CELL-SPECTRAL", 2)
    else:
        reanalysis = analysis_pending

    overview = service.committee_overview(token)
    return {
        "status": "ok",
        "method_fingerprint_v1": fingerprint_v1,
        "method_fingerprint_v2": fingerprint_v2,
        "comparison_result": comparison["result"],
        "investigation_opened": investigation_id is not None,
        "suspended_release_blocked": blocked,
        "reanalysis_sequence": reanalysis["sequence_no"],
        "reanalysis_revision": reanalysis["revision"],
        "released_report_untouched": service.get_report(token, report["report_id"])["revision"] == 1,
        "suspended_lots_now": len(overview["suspended_lots"]),
        "investigations": len(overview["investigations"]),
        "events": len(service.audit(token, "LOT-PENDING")),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
