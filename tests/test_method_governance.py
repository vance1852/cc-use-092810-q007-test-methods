from __future__ import annotations

import json
import unittest

from component_quality.service import ComponentQualityService


def method_definition(version: int = 1, **overrides) -> dict:
    definition = {
        "method_id": "CELL-RESP",
        "version": version,
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
    definition.update(overrides)
    return definition


class GovernanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ComponentQualityService()
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "component-admin")
        for user_id, role in (("engineer", "engineer"), ("quality", "quality"), ("operator", "operator")):
            self.service.auth.create_user(user_id, f"{user_id}-pass", role)
        self.engineer = self.service.auth.login("engineer", "engineer-pass")
        self.quality = self.service.auth.login("quality", "quality-pass")
        self.operator = self.service.auth.login("operator", "operator-pass")
        self.published = self.service.publish_method(self.admin, method_definition())

    def qualify(self, lab_id: str, instrument: str, observed: float = 0.9) -> None:
        self.service.register_instrument(self.admin, lab_id, instrument, "SpecPro 9000")
        self.service.record_calibration(self.engineer, instrument, f"CAL-{instrument}", f"CERT-{instrument}", "2027-01-01T00:00:00+00:00")
        self.service.record_reference_comparison(self.engineer, lab_id, instrument, "CELL-RESP", 1, "REF-STD-1", 0.9, observed, 0.05)

    def make_lot(self, lot_id: str = "LOT-1", kind: str = "formal") -> None:
        self.service.create_lot(self.engineer, lot_id, "CMOS image sensor", "P3.2", 10, lot_kind=kind)

    def add_readings(self, lot_id: str, lab_id: str, instrument: str, values: tuple[float, ...] = (.9, .93, .88)) -> list[str]:
        ids = []
        for frequency, value in zip((450, 520, 650), values):
            result = self.service.add_measurement(
                self.operator, lot_id, lab_id=lab_id, instrument=instrument, method_id="CELL-RESP",
                method_version=1, test_frequency_hz=frequency, raw_value=value, raw_unit="A/W", noise=.01,
            )
            ids.append(result["measurement_id"])
        return ids


class MethodPublicationTests(GovernanceTestCase):
    def test_publish_freezes_definition_and_supersedes_old_versions(self) -> None:
        first = self.service.get_method(self.quality, "CELL-RESP", 1)
        self.assertEqual(first["status"], "published")
        self.assertEqual(first["sha256"], self.published["sha256"])
        self.assertEqual(first["definition"]["measurement_range"], {"min": 0.0, "max": 1.5, "unit": "A/W"})
        self.assertEqual(first["definition"]["sampling_requirements"]["min_samples"], 3)
        second = method_definition(2)
        second["decision_thresholds"] = dict(second["decision_thresholds"], response_min=0.8)
        self.service.publish_method(self.admin, second)
        self.assertEqual(self.service.get_method(self.quality, "CELL-RESP", 1)["status"], "superseded")
        self.assertEqual(self.service.get_method(self.quality, "CELL-RESP")["version"], 2)
        # 旧版本定义仍然冻结可查
        self.assertEqual(
            self.service.get_method(self.quality, "CELL-RESP", 1)["definition"]["decision_thresholds"]["response_min"],
            0.75,
        )

    def test_version_must_increase_and_content_must_change(self) -> None:
        with self.assertRaises(ValueError):
            self.service.publish_method(self.admin, method_definition(1))
        with self.assertRaises(ValueError):
            self.service.publish_method(self.admin, method_definition(2))  # 内容完全相同

    def test_invalid_definition_rejected(self) -> None:
        broken = method_definition(2, measurement_range={"min": 2.0, "max": 1.0, "unit": "A/W"})
        with self.assertRaises(ValueError):
            self.service.publish_method(self.admin, broken)
        missing_anchor = method_definition(2, unit_conversions={"mA/W": {"factor": 0.001}})
        with self.assertRaises(ValueError):
            self.service.publish_method(self.admin, missing_anchor)

    def test_governance_requires_permission(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.publish_method(self.engineer, method_definition(2))
        with self.assertRaises(PermissionError):
            self.service.register_lab(self.operator, "lab-x", "实验室X")


class LabQualificationTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.register_lab(self.admin, "lab-a", "实验室A")
        self.make_lot()

    def test_formal_lot_requires_calibration_and_reference_comparison(self) -> None:
        self.service.register_instrument(self.admin, "lab-a", "spect-1", "SpecPro 9000")
        with self.assertRaises(ValueError):
            self.add_readings("LOT-1", "lab-a", "spect-1")
        self.service.record_calibration(self.engineer, "spect-1", "CAL-1", "CERT-1", "2027-01-01T00:00:00+00:00")
        with self.assertRaises(ValueError):
            self.add_readings("LOT-1", "lab-a", "spect-1")
        self.service.record_reference_comparison(self.engineer, "lab-a", "spect-1", "CELL-RESP", 1, "REF-STD-1", 0.9, 0.901, 0.05)
        ids = self.add_readings("LOT-1", "lab-a", "spect-1")
        self.assertEqual(len(ids), 3)

    def test_failed_or_expired_evidence_blocks_qualification(self) -> None:
        self.service.register_instrument(self.admin, "lab-a", "spect-1", "SpecPro 9000")
        self.service.record_calibration(self.engineer, "spect-1", "CAL-1", "CERT-1", "2020-01-01T00:00:00+00:00")
        self.service.record_reference_comparison(self.engineer, "lab-a", "spect-1", "CELL-RESP", 1, "REF-STD-1", 0.9, 0.99, 0.05)
        status = self.service.lab_qualification(self.quality, "lab-a", "CELL-RESP", 1)
        self.assertFalse(status["qualified"])
        self.assertFalse(status["instruments"][0]["calibration"]["valid"])
        self.assertFalse(status["instruments"][0]["comparison"]["passed"])
        with self.assertRaises(ValueError):
            self.add_readings("LOT-1", "lab-a", "spect-1")

    def test_engineering_lot_is_exempt_from_qualification(self) -> None:
        self.service.register_instrument(self.admin, "lab-a", "spect-1", "SpecPro 9000")
        self.make_lot("LOT-ENG", kind="engineering")
        ids = self.add_readings("LOT-ENG", "lab-a", "spect-1")
        self.assertEqual(len(ids), 3)


class ReadingGovernanceTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.register_lab(self.admin, "lab-a", "实验室A")
        self.qualify("lab-a", "spect-1")
        self.make_lot()

    def test_raw_reading_keeps_unit_and_device_identity(self) -> None:
        result = self.service.add_measurement(
            self.operator, "LOT-1", lab_id="lab-a", instrument="spect-1", method_id="CELL-RESP",
            method_version=1, test_frequency_hz=450, raw_value=850.0, raw_unit="mA/W",
        )
        self.assertAlmostEqual(result["response"], 0.85)
        trace = self.service.traceability(self.quality, "LOT-1")
        reading = trace["readings"][0]
        self.assertEqual(reading["raw_value"], 850.0)
        self.assertEqual(reading["raw_unit"], "mA/W")
        self.assertEqual(reading["instrument"], "spect-1")
        self.assertEqual(reading["lab_id"], "lab-a")
        self.assertEqual((reading["method_id"], reading["method_version"]), ("CELL-RESP", 1))

    def test_out_of_range_and_unknown_unit_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.service.add_measurement(
                self.operator, "LOT-1", lab_id="lab-a", instrument="spect-1", method_id="CELL-RESP",
                method_version=1, test_frequency_hz=450, raw_value=2.0, raw_unit="A/W",
            )
        with self.assertRaises(ValueError):
            self.service.add_measurement(
                self.operator, "LOT-1", lab_id="lab-a", instrument="spect-1", method_id="CELL-RESP",
                method_version=1, test_frequency_hz=450, raw_value=1.0, raw_unit="V",
            )

    def test_method_applicability_is_enforced(self) -> None:
        self.service.create_lot(self.engineer, "LOT-OTHER", "power module", "P1.0", 5)
        with self.assertRaises(ValueError):
            self.add_readings("LOT-OTHER", "lab-a", "spect-1")

    def test_exclusion_pins_method_version_and_permission(self) -> None:
        ids = self.add_readings("LOT-1", "lab-a", "spect-1")
        with self.assertRaises(PermissionError):
            self.service.exclude_measurement(self.operator, ids[0], "漂移", "CELL-RESP", 1)
        with self.assertRaises(KeyError):
            self.service.exclude_measurement(self.quality, ids[0], "漂移", "CELL-RESP", 99)
        excluded = self.service.exclude_measurement(self.quality, ids[0], "仪器漂移", "CELL-RESP", 1)
        self.assertTrue(excluded["excluded"])
        analysis = self.service.analyze(self.engineer, "LOT-1", "CELL-RESP", 1)
        self.assertEqual(analysis["result"]["included_count"], 2)
        self.assertEqual(analysis["result"]["conclusion"], "insufficient")
        trace = self.service.traceability(self.quality, "LOT-1")
        record = next(row for row in trace["readings"] if row["measurement_id"] == ids[0])
        self.assertEqual(record["exclusion_method_version"], 1)


class AnalysisAndReportTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.register_lab(self.admin, "lab-a", "实验室A")
        self.qualify("lab-a", "spect-1")
        self.make_lot()
        self.add_readings("LOT-1", "lab-a", "spect-1")

    def test_analysis_is_versioned_and_idempotent(self) -> None:
        first = self.service.analyze(self.engineer, "LOT-1", "CELL-RESP", 1)
        second = self.service.analyze(self.quality, "LOT-1", "CELL-RESP", 1)
        self.assertEqual(first["analysis_id"], second["analysis_id"])
        self.assertEqual(first["result"]["method_sha256"], self.published["sha256"])
        self.assertEqual(first["result"]["conclusion"], "pass")
        self.assertEqual(first["result"]["canonical_unit"], "A/W")

    def test_release_requires_pass_report(self) -> None:
        with self.assertRaises(ValueError):
            self.service.approve(self.quality, "LOT-1", "release", "直接放行")
        self.service.analyze(self.engineer, "LOT-1", "CELL-RESP", 1)
        report = self.service.issue_report(self.quality, "LOT-1", "CELL-RESP", 1)
        self.assertEqual(report["conclusion"], "pass")
        lot = self.service.approve(self.quality, "LOT-1", "release", "报告结论通过")
        self.assertEqual(lot["status"], "released")

    def test_issued_report_is_not_recomputed_and_reevaluation_supersedes(self) -> None:
        self.service.analyze(self.engineer, "LOT-1", "CELL-RESP", 1)
        report = self.service.issue_report(self.quality, "LOT-1", "CELL-RESP", 1)
        with self.assertRaises(ValueError):
            self.service.issue_report(self.quality, "LOT-1", "CELL-RESP", 1)
        before = self.service.db.execute(
            "SELECT snapshot_json, conclusion FROM reports WHERE report_id=?", (report["report_id"],)
        ).fetchone()
        # 新方法版本收紧阈值，只能通过复评产生后继结论
        stricter = method_definition(2)
        stricter["decision_thresholds"] = dict(stricter["decision_thresholds"], response_min=0.95)
        self.service.publish_method(self.admin, stricter)
        successor = self.service.reevaluate(self.quality, "LOT-1", "CELL-RESP", 2, "集团统一升级判定阈值")
        self.assertEqual(successor["conclusion"], "fail")
        self.assertEqual(successor["supersedes_report_id"], report["report_id"])
        old = self.service.db.execute("SELECT * FROM reports WHERE report_id=?", (report["report_id"],)).fetchone()
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["snapshot_json"], before["snapshot_json"])  # 已出具报告不被静默重算
        self.assertEqual(old["conclusion"], before["conclusion"])
        with self.assertRaises(ValueError):
            self.service.approve(self.quality, "LOT-1", "release", "现行结论为不通过")

    def test_reevaluation_requires_report_or_suspension(self) -> None:
        with self.assertRaises(ValueError):
            self.service.reevaluate(self.quality, "LOT-1", "CELL-RESP", 1, "没有现行报告")


class CrossLabInvestigationTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        for lab_id, instrument in (("lab-a", "spect-a"), ("lab-b", "spect-b")):
            self.service.register_lab(self.admin, lab_id, f"实验室{lab_id[-1].upper()}")
            self.qualify(lab_id, instrument)
        self.make_lot("LOT-1")
        self.add_readings("LOT-1", "lab-a", "spect-a", (.9, .93, .88))
        self.add_readings("LOT-1", "lab-b", "spect-b", (.5, .55, .52))
        self.make_lot("LOT-2")
        self.add_readings("LOT-2", "lab-a", "spect-a", (.91, .9, .89))

    def test_systematic_bias_suspends_pending_releases_and_opens_investigation(self) -> None:
        result = self.service.compare_labs(self.quality, "LOT-1", "CELL-RESP", 1)
        self.assertTrue(result["systematic_bias"])
        self.assertGreater(result["max_deviation"], 0.1)
        self.assertEqual(result["implicated_labs"], ["lab-a", "lab-b"])
        self.assertEqual(sorted(result["suspended_lots"]), ["LOT-1", "LOT-2"])
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["status"], "suspended")
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["previous_status"], "engineering")
        with self.assertRaises(ValueError):
            self.service.approve(self.quality, "LOT-1", "release", "暂停期间不能放行")
        with self.assertRaises(ValueError):
            self.service.issue_report(self.quality, "LOT-1", "CELL-RESP", 1)
        investigations = self.service.list_investigations(self.quality, status="open")
        self.assertEqual(len(investigations), 1)
        self.assertEqual(sorted(investigations[0]["affected_lots"]), ["LOT-1", "LOT-2"])

    def test_released_lot_is_scoped_but_not_suspended(self) -> None:
        self.make_lot("LOT-3")
        self.add_readings("LOT-3", "lab-a", "spect-a", (.9, .92, .91))
        self.service.analyze(self.engineer, "LOT-3", "CELL-RESP", 1)
        self.service.issue_report(self.quality, "LOT-3", "CELL-RESP", 1)
        self.service.approve(self.quality, "LOT-3", "release", "先行放行")
        self.service.compare_labs(self.quality, "LOT-1", "CELL-RESP", 1)
        self.assertEqual(self.service.get_lot(self.quality, "LOT-3")["status"], "released")
        investigation = self.service.list_investigations(self.quality)[0]
        self.assertIn("LOT-3", investigation["affected_lots"])
        self.assertNotIn("LOT-3", investigation["suspended_lots"])

    def test_close_investigation_resumes_or_holds(self) -> None:
        investigation_id = self.service.compare_labs(self.quality, "LOT-1", "CELL-RESP", 1)["investigation_id"]
        closed = self.service.close_investigation(self.quality, investigation_id, "lab-b 校准批次漂移，读数作废", resume_lots=False)
        self.assertEqual(closed["resumed_lots"], [])
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["status"], "suspended")
        # 复评产生后继结论并解除暂停
        successor = self.service.reevaluate(self.quality, "LOT-1", "CELL-RESP", 1, "剔除漂移实验室后复评")
        self.assertEqual(successor["status"], "issued")
        self.assertEqual(self.service.get_lot(self.quality, "LOT-1")["status"], "engineering")
        # 另一场景：调查确认无问题则直接恢复
        investigation_id = self.service.compare_labs(self.quality, "LOT-1", "CELL-RESP", 1)["investigation_id"]
        closed = self.service.close_investigation(self.quality, investigation_id, "复核无系统偏差", resume_lots=True)
        self.assertIn("LOT-2", closed["resumed_lots"])
        self.assertEqual(self.service.get_lot(self.quality, "LOT-2")["status"], "engineering")

    def test_committee_views_expose_method_evidence_differences_and_suspensions(self) -> None:
        self.service.compare_labs(self.quality, "LOT-1", "CELL-RESP", 1)
        overview = self.service.committee_overview(self.quality)
        self.assertEqual(sorted(lot["lot_id"] for lot in overview["suspended_lots"]), ["LOT-1", "LOT-2"])
        self.assertEqual(len(overview["open_investigations"]), 1)
        difference = next(item for item in overview["interlab_differences"] if item["lot_id"] == "LOT-1")
        self.assertFalse(difference["within_tolerance"])
        self.assertEqual(set(difference["per_lab"]), {"lab-a", "lab-b"})
        self.assertEqual(overview["methods"][0]["sha256"], self.published["sha256"])
        trace = self.service.traceability(self.quality, "LOT-1")
        self.assertEqual(trace["readings"][0]["method_id"], "CELL-RESP")
        evidence = trace["calibration_evidence"][0]
        self.assertTrue(evidence["methods"][0]["qualified"])
        self.assertEqual(evidence["methods"][0]["calibration"]["calibration_batch"], "CAL-spect-a")
        self.assertFalse(trace["interlab"][0]["within_tolerance"])


if __name__ == "__main__":
    unittest.main()
