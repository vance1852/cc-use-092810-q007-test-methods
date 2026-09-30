"""容器和快照检查使用的冒烟验收命令。"""

from __future__ import annotations

import argparse
import json

from .service import ComponentQualityService

DEMO_METHOD = {
    "method_id": "CELL-RESP",
    "version": 1,
    "title": "电芯组件光谱响应检测方法",
    "applicable_products": ["CMOS image sensor"],
    "sampling_requirements": {"min_samples": 3, "required_frequencies_hz": [450, 520, 650]},
    "metric_formulas": {
        "response_profile": {"function": "summarize_response_profile", "params": {"threshold": 0.8}},
        "response_ci": {"function": "confidence_interval", "params": {"confidence": 0.95}},
        "yield": {"function": "yield_rate", "params": {}},
    },
    "unit_conversions": {"A/W": {"factor": 1.0}, "mA/W": {"factor": 0.001}},
    "measurement_range": {"min": 0.0, "max": 1.5, "unit": "A/W"},
    "decision_thresholds": {
        "reading_pass_response": 0.8,
        "response_min": 0.75,
        "yield_min": 0.2,
        "interlab_max_deviation": 0.1,
    },
}


def run() -> dict:
    service = ComponentQualityService()
    service.bootstrap_admin()
    token = service.auth.login("admin", "component-admin")
    method = service.publish_method(token, DEMO_METHOD)
    service.register_lab(token, "lab-a", "华东检测实验室")
    service.register_instrument(token, "lab-a", "spectrometer-1", "SpecPro 9000")
    service.record_calibration(token, "spectrometer-1", "CAL-2026-09", "CERT-2026-001", "2027-09-01T00:00:00+00:00")
    service.record_reference_comparison(token, "lab-a", "spectrometer-1", "CELL-RESP", 1, "REF-STD-1", 0.9, 0.901, 0.01)
    service.create_lot(token, "LOT-DEMO", "CMOS image sensor", "P3.2", 10)
    for test_frequency, response in ((450, .71), (520, .93), (650, .84)):
        service.add_measurement(
            token, "LOT-DEMO", lab_id="lab-a", instrument="spectrometer-1",
            method_id="CELL-RESP", method_version=1,
            test_frequency_hz=test_frequency, raw_value=response, raw_unit="A/W", noise=.01,
        )
    analysis = service.analyze(token, "LOT-DEMO", "CELL-RESP", 1)
    comparison = service.compare_labs(token, "LOT-DEMO", "CELL-RESP", 1)
    report = service.issue_report(token, "LOT-DEMO", "CELL-RESP", 1)
    service.approve(token, "LOT-DEMO", "release", "方法治理链路验收")
    overview = service.committee_overview(token)
    return {
        "status": "ok",
        "lot": "LOT-DEMO",
        "method_sha256": method["sha256"],
        "peak": analysis["result"]["response_profile"]["peak_test_frequency_hz"],
        "conclusion": report["conclusion"],
        "systematic_bias": comparison["systematic_bias"],
        "suspended": len(overview["suspended_lots"]),
        "events": len(service.audit(token, "LOT-DEMO")),
    }


def main() -> None:
    argparse.ArgumentParser().parse_args()
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
