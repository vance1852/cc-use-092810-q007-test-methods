from __future__ import annotations

import unittest

from component_quality.acceptance import method_spec
from component_quality.service import ComponentQualityService


class GovernanceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ComponentQualityService()
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "component-admin")
        self.service.auth.create_user("op", "operator-pw", "operator")
        self.operator = self.service.auth.login("op", "operator-pw")

    def spec(self, **overrides) -> dict:
        spec = method_spec(noise_limit=0.20)
        if overrides:
            spec.update(overrides)
        return spec

    def publish(self, **overrides) -> dict:
        return self.service.publish_method(self.token, self.spec(**overrides))

    def enable_instrument(self, lab="LAB-1", instrument="SPEC-1", calibration="CAL-1",
                          observed=0.902, method="M-CELL-SPECTRAL", revision=1) -> None:
        self.service.register_laboratory(self.token, lab, f"{lab} 实验室")
        self.service.register_instrument(self.token, instrument, lab, f"SN-{instrument}")
        self.service.record_calibration(
            self.token, calibration, instrument, method, revision,
            f"CERT-{calibration}", "2026-09-01", "2027-09-01")
        self.service.record_reference_check(
            self.token, calibration, "REF-07", 0.9, "ratio", observed, "ratio", 0.01)

    def measure_three(self, lot="LOT-1", instrument="SPEC-1", lab="LAB-1", calibration="CAL-1",
                      readings=((450, 0.86), (520, 0.93), (650, 0.84))) -> None:
        for frequency, response in readings:
            self.service.add_measurement(
                self.token, lot, frequency, response, 0.01, instrument,
                lab_id=lab, calibration_id=calibration,
                units={"test_frequency": "Hz", "response": "ratio", "noise": "ratio"})


class MethodGovernanceTests(GovernanceTestBase):
    def test_publish_freezes_and_content_change_creates_successor(self) -> None:
        r1 = self.publish()
        self.assertEqual(r1["revision"], 1)
        self.assertEqual(r1["status"], "published")
        self.assertEqual(len(r1["fingerprint"]), 64)
        # 同内容重复发布为幂等
        again = self.publish()
        self.assertEqual(again["revision"], 1)
        self.assertEqual(again["fingerprint"], r1["fingerprint"])
        # 阈值变化 → 新版本，旧版本被取代，指纹不同
        r2 = self.service.publish_method(self.token, self.spec(noise_limit=0.15))
        self.assertEqual(r2["revision"], 2)
        self.assertNotEqual(r2["fingerprint"], r1["fingerprint"])
        self.assertEqual(self.service.get_method(self.token, "M-CELL-SPECTRAL", 1)["status"], "superseded")
        methods = self.service.list_methods(self.token)
        self.assertEqual([(m["revision"], m["status"]) for m in methods], [(1, "superseded"), (2, "published")])

    def test_withdraw_blocks_formal_use(self) -> None:
        self.publish()
        self.service.withdraw_method(self.token, "M-CELL-SPECTRAL", 1, "阈值有误")
        with self.assertRaises(ValueError):
            self.service.create_lot(self.token, "LOT-1", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)

    def test_product_applicability_is_frozen_at_lot_creation(self) -> None:
        self.publish()
        with self.assertRaises(ValueError):
            self.service.create_lot(self.token, "LOT-1", "电芯组件-B", "P3.2", 3, "M-CELL-SPECTRAL", 1)

    def test_operator_cannot_govern_methods(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.publish_method(self.operator, self.spec())


class CalibrationGateTests(GovernanceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.publish()

    def test_formal_measurement_requires_lab_and_calibration(self) -> None:
        self.service.create_lot(self.token, "LOT-1", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
        with self.assertRaises(ValueError):
            self.service.add_measurement(
                self.token, "LOT-1", 450, 0.86, 0.01, "SPEC-1",
                units={"response": "ratio"})

    def test_pending_calibration_cannot_measure(self) -> None:
        self.service.register_laboratory(self.token, "LAB-1", "一实验室")
        self.service.register_instrument(self.token, "SPEC-1", "LAB-1", "SN-1")
        self.service.record_calibration(
            self.token, "CAL-1", "SPEC-1", "M-CELL-SPECTRAL", 1, "CERT-1", "2026-09-01", "2027-09-01")
        self.service.create_lot(self.token, "LOT-1", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
        with self.assertRaises(ValueError):
            self.service.add_measurement(
                self.token, "LOT-1", 450, 0.86, 0.01, "SPEC-1", lab_id="LAB-1", calibration_id="CAL-1",
                units={"response": "ratio"})

    def test_failed_reference_check_rejects_calibration(self) -> None:
        self.service.register_laboratory(self.token, "LAB-1", "一实验室")
        self.service.register_instrument(self.token, "SPEC-1", "LAB-1", "SN-1")
        self.service.record_calibration(
            self.token, "CAL-1", "SPEC-1", "M-CELL-SPECTRAL", 1, "CERT-1", "2026-09-01", "2027-09-01")
        result = self.service.record_reference_check(
            self.token, "CAL-1", "REF-07", 0.9, "ratio", 0.80, "ratio", 0.01)
        self.assertEqual(result["status"], "rejected")

    def test_calibration_is_method_revision_specific(self) -> None:
        self.enable_instrument()
        self.service.publish_method(self.token, self.spec(noise_limit=0.15))  # r2
        self.service.create_lot(self.token, "LOT-1", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 2)
        with self.assertRaises(ValueError):  # r1 的校准不能用于 r2 批次
            self.service.add_measurement(
                self.token, "LOT-1", 450, 0.86, 0.01, "SPEC-1", lab_id="LAB-1", calibration_id="CAL-1",
                units={"response": "ratio"})

    def test_instrument_must_belong_to_declared_lab(self) -> None:
        self.enable_instrument()
        self.service.register_laboratory(self.token, "LAB-2", "二实验室")
        self.service.create_lot(self.token, "LOT-1", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
        with self.assertRaises(ValueError):
            self.service.add_measurement(
                self.token, "LOT-1", 450, 0.86, 0.01, "SPEC-1", lab_id="LAB-2", calibration_id="CAL-1",
                units={"response": "ratio"})

    def test_verified_calibration_with_reference_check_opens_measurement(self) -> None:
        self.enable_instrument()
        self.service.create_lot(self.token, "LOT-1", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
        result = self.service.add_measurement(
            self.token, "LOT-1", 450, 0.86, 0.01, "SPEC-1", lab_id="LAB-1", calibration_id="CAL-1",
            units={"test_frequency": "Hz", "response": "ratio", "noise": "ratio"})
        self.assertTrue(result["measurement_id"])
        calibration = self.service.get_calibration(self.token, "CAL-1")
        self.assertEqual(calibration["status"], "verified")
        self.assertEqual(calibration["reference_checks"][0]["within_tolerance"], 1)


class RawReadingTests(GovernanceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.publish()
        self.enable_instrument()
        self.service.create_lot(self.token, "LOT-1", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)

    def test_raw_value_unit_and_device_identity_are_preserved(self) -> None:
        self.service.add_measurement(
            self.token, "LOT-1", 0.45, 91, 1.0, "SPEC-1", lab_id="LAB-1", calibration_id="CAL-1",
            units={"test_frequency": "kHz", "response": "percent", "noise": "percent"})
        readings = self.service.lot_evidence(self.token, "LOT-1")["readings"]
        raw = readings[0]
        self.assertEqual(raw["instrument"], "SPEC-1")
        self.assertEqual(raw["lab_id"], "LAB-1")
        self.assertEqual(raw["calibration_id"], "CAL-1")
        self.assertAlmostEqual(raw["response"], 0.91)        # 规范值
        import json
        raw_json = json.loads(raw["raw_json"])
        self.assertEqual(raw_json["response"]["value"], 91)  # 原始值保留
        self.assertEqual(raw_json["response"]["unit"], "percent")
        self.assertAlmostEqual(raw_json["canonical"]["test_frequency"], 450.0)

    def test_undeclared_unit_and_out_of_range_are_rejected(self) -> None:
        with self.assertRaises(ValueError):  # 未在方法中声明的单位
            self.service.add_measurement(
                self.token, "LOT-1", 450, 860, 0.01, "SPEC-1", lab_id="LAB-1", calibration_id="CAL-1",
                units={"response": "mv"})
        with self.assertRaises(ValueError):  # 换算后超量程（响应上限 2.0）
            self.service.add_measurement(
                self.token, "LOT-1", 450, 500, 0.01, "SPEC-1", lab_id="LAB-1", calibration_id="CAL-1",
                units={"response": "percent"})


class ImmutableConclusionTests(GovernanceTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.publish()
        self.enable_instrument()
        self.service.create_lot(self.token, "LOT-1", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
        self.measure_three()

    def test_analysis_versions_append_and_reference_method(self) -> None:
        a1 = self.service.run_analysis(self.token, "LOT-1")
        self.assertEqual(a1["sequence_no"], 1)
        self.assertEqual(a1["revision"], 1)
        self.assertEqual(len(a1["input_fingerprint"]), 64)
        self.assertEqual(a1["method_fingerprint"], self.publish()["fingerprint"])
        self.assertEqual(a1["result"]["calibrations_used"], {"CAL-1": "CERT-CAL-1"})
        # 追加读数 → 新版本，旧版本不变
        self.service.add_measurement(
            self.token, "LOT-1", 600, 0.90, 0.01, "SPEC-1", lab_id="LAB-1", calibration_id="CAL-1",
            units={"test_frequency": "Hz", "response": "ratio", "noise": "ratio"})
        a2 = self.service.run_analysis(self.token, "LOT-1")
        self.assertEqual(a2["sequence_no"], 2)
        self.assertEqual(self.service.get_analysis(self.token, a1["analysis_id"])["sequence_no"], 1)

    def test_exclusion_must_reference_method_version(self) -> None:
        readings = self.service.lot_evidence(self.token, "LOT-1")["readings"]
        target = readings[0]["measurement_id"]
        self.service.exclude_measurement(self.token, target, "M-CELL-SPECTRAL", 1, "现场干扰")
        with self.assertRaises(KeyError):
            self.service.exclude_measurement(self.token, "missing", "M-CELL-SPECTRAL", 1, "x")

    def test_report_is_not_silently_recomputed_when_method_advances(self) -> None:
        self.service.run_analysis(self.token, "LOT-1")
        report = self.service.issue_report(self.token, "LOT-1", "release", "r1 pass")
        self.assertEqual(report["revision"], 1)
        self.service.publish_method(self.token, self.spec(noise_limit=0.15))
        # 已出具的报告不随新版本变化
        self.assertEqual(self.service.get_report(self.token, report["report_id"])["revision"], 1)


class CrossLabInvestigationTests(GovernanceTestBase):
    def _two_labs_and_lots(self) -> str:
        self.publish()
        self.enable_instrument("LAB-1", "SPEC-1", "CAL-1", 0.902)
        self.enable_instrument("LAB-2", "SPEC-2", "CAL-2", 0.898)
        self.service.create_lot(self.token, "LOT-PENDING", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
        self.measure_three("LOT-PENDING", "SPEC-1", "LAB-1", "CAL-1")
        self.service.run_analysis(self.token, "LOT-PENDING")
        self.service.create_lot(self.token, "LOT-RELEASED", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
        self.measure_three("LOT-RELEASED", "SPEC-2", "LAB-2", "CAL-2",
                           ((460, 0.88), (530, 0.90), (640, 0.89)))
        self.service.run_analysis(self.token, "LOT-RELEASED")
        return self.service.issue_report(self.token, "LOT-RELEASED", "release", "r1 pass")["report_id"]

    def test_bias_suspends_pending_lots_but_keeps_issued_reports(self) -> None:
        report_id = self._two_labs_and_lots()
        comparison = self.service.record_comparison(
            self.token, "CMP-1", "M-CELL-SPECTRAL", 1, "REF-07",
            [{"lab_id": "LAB-1", "metrics": {"peak_response": 0.93, "mean_response": 0.90}},
             {"lab_id": "LAB-2", "metrics": {"peak_response": 0.80, "mean_response": 0.84}}])
        self.assertEqual(comparison["result"], "systematic_bias")
        self.assertIsNotNone(comparison["investigation_id"])
        pending = self.service.get_lot(self.token, "LOT-PENDING")
        self.assertTrue(pending["suspended_at"])
        self.assertEqual(pending["investigation_id"], comparison["investigation_id"])
        # 已放行批次不被冻结
        self.assertFalse(self.service.get_lot(self.token, "LOT-RELEASED")["suspended_at"])
        overview = self.service.committee_overview(self.token)
        self.assertEqual([lot["lot_id"] for lot in overview["suspended_lots"]], ["LOT-PENDING"])
        self.assertEqual(overview["comparisons"][0]["result"], "systematic_bias")
        # 已出具的报告原样保留
        self.assertEqual(self.service.get_report(self.token, report_id)["revision"], 1)

    def test_suspension_blocks_release_measurement_and_recompute(self) -> None:
        self._two_labs_and_lots()
        self.service.record_comparison(
            self.token, "CMP-1", "M-CELL-SPECTRAL", 1, "REF-07",
            [{"lab_id": "LAB-1", "metrics": {"peak_response": 0.93, "mean_response": 0.90}},
             {"lab_id": "LAB-2", "metrics": {"peak_response": 0.80, "mean_response": 0.84}}])
        with self.assertRaises(PermissionError):
            self.service.issue_report(self.token, "LOT-PENDING", "release", "暂停期间放行")
        with self.assertRaises(PermissionError):  # 受疑版本不能重算
            self.service.run_analysis(self.token, "LOT-PENDING", "M-CELL-SPECTRAL", 1)
        with self.assertRaises(ValueError):  # 不能补读数
            self.service.add_measurement(
                self.token, "LOT-PENDING", 600, 0.9, 0.01, "SPEC-1", lab_id="LAB-1", calibration_id="CAL-1",
                units={"response": "ratio"})

    def test_resolution_requires_successor_and_reanalysis(self) -> None:
        report_id = self._two_labs_and_lots()
        comparison = self.service.record_comparison(
            self.token, "CMP-1", "M-CELL-SPECTRAL", 1, "REF-07",
            [{"lab_id": "LAB-1", "metrics": {"peak_response": 0.93, "mean_response": 0.90}},
             {"lab_id": "LAB-2", "metrics": {"peak_response": 0.80, "mean_response": 0.84}}])
        investigation_id = comparison["investigation_id"]
        # 不能用同版本或更旧版本关闭
        with self.assertRaises(ValueError):
            self.service.close_investigation(self.token, investigation_id, "M-CELL-SPECTRAL", 1, "无效")
        self.service.publish_method(self.token, self.spec(noise_limit=0.15))
        self.service.close_investigation(
            self.token, investigation_id, "M-CELL-SPECTRAL", 2, "算法版本差异已纠正")
        self.assertFalse(self.service.get_lot(self.token, "LOT-PENDING")["suspended_at"])
        investigation = self.service.get_investigation(self.token, investigation_id)
        self.assertEqual(investigation["status"], "closed")
        self.assertEqual(len(investigation["lots"]), 1)  # 调查版本保留受影响批次快照
        # 复评产生后继结论
        reanalysis = self.service.run_analysis(self.token, "LOT-PENDING", "M-CELL-SPECTRAL", 2)
        self.assertEqual(reanalysis["revision"], 2)
        self.assertEqual(reanalysis["sequence_no"], 2)
        # 不能用旧分析放行，必须用复评结论
        old_analysis = self.service.lot_evidence(self.token, "LOT-PENDING")["analyses"][0]["analysis_id"]
        with self.assertRaises(PermissionError):
            self.service.issue_report(self.token, "LOT-PENDING", "release", "旧结论", old_analysis)
        new_report = self.service.issue_report(self.token, "LOT-PENDING", "release", "r2 复评 pass")
        self.assertEqual(new_report["revision"], 2)
        # 历史报告仍是 r1，没有被静默重算
        self.assertEqual(self.service.get_report(self.token, report_id)["revision"], 1)

    def test_within_tolerance_comparison_opens_no_investigation(self) -> None:
        self._two_labs_and_lots()
        comparison = self.service.record_comparison(
            self.token, "CMP-1", "M-CELL-SPECTRAL", 1, "REF-07",
            [{"lab_id": "LAB-1", "metrics": {"peak_response": 0.905, "mean_response": 0.90}},
             {"lab_id": "LAB-2", "metrics": {"peak_response": 0.895, "mean_response": 0.895}}])
        self.assertEqual(comparison["result"], "within_tolerance")
        self.assertIsNone(comparison["investigation_id"])
        self.assertFalse(self.service.get_lot(self.token, "LOT-PENDING")["suspended_at"])


class CommitteeOverviewTests(GovernanceTestBase):
    def test_overview_aggregates_governance_evidence(self) -> None:
        self.publish()
        self.enable_instrument()
        self.service.create_lot(self.token, "LOT-1", "电芯组件-A", "P3.2", 3, "M-CELL-SPECTRAL", 1)
        self.measure_three()
        self.service.run_analysis(self.token, "LOT-1")
        overview = self.service.committee_overview(self.token)
        self.assertEqual(overview["methods"][0]["revision"], 1)
        self.assertEqual(overview["calibrations"][0]["status"], "verified")
        self.assertEqual(overview["calibrations"][0]["lab_id"], "LAB-1")
        lot_view = next(lot for lot in overview["lots"] if lot["lot_id"] == "LOT-1")
        self.assertEqual(lot_view["latest_analysis"]["revision"], 1)
        evidence = self.service.lot_evidence(self.token, "LOT-1")
        self.assertEqual(evidence["method"]["status"], "published")
        self.assertEqual(len(evidence["readings"]), 3)


class LegacyCompatibilityTests(GovernanceTestBase):
    def test_unbound_lot_keeps_original_flow(self) -> None:
        self.service.create_lot(self.token, "LOT-OLD", "legacy sensor", "P1.0", 10)
        for frequency, response in ((450, 0.71), (520, 0.93), (650, 0.84)):
            self.service.add_measurement(self.token, "LOT-OLD", frequency, response, 0.01, "spectrometer-1")
        result = self.service.analyze(self.token, "LOT-OLD")
        self.assertEqual(result["response_profile"]["peak_test_frequency_hz"], 520)
        lot = self.service.approve(self.token, "LOT-OLD", "hold", "等待复核")
        self.assertEqual(lot["status"], "hold")


if __name__ == "__main__":
    unittest.main()
