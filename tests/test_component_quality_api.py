from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from component_quality.api import Handler
from component_quality.service import ComponentQualityService


def method_definition() -> dict:
    return {
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


class ComponentQualityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        Handler.service = ComponentQualityService()
        Handler.service.bootstrap_admin()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def _request(self, method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_governed_chain_over_http(self) -> None:
        status, body = self._request("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        status, body = self._request("POST", "/login", {"user_id": "admin", "password": "component-admin"})
        token = body["token"]
        # 未认证请求被拒绝
        status, _ = self._request("POST", "/methods", method_definition())
        self.assertEqual(status, 403)
        status, body = self._request("POST", "/methods", method_definition(), token)
        self.assertEqual(status, 201)
        self.assertEqual(len(body["sha256"]), 64)
        self._request("POST", "/labs", {"lab_id": "lab-a", "name": "实验室A"}, token)
        self._request("POST", "/labs/lab-a/instruments", {"instrument_id": "spect-1", "model": "SpecPro"}, token)
        self._request("POST", "/instruments/spect-1/calibrations", {
            "calibration_batch": "CAL-1", "certificate_ref": "CERT-1", "valid_until": "2027-01-01T00:00:00+00:00",
        }, token)
        status, body = self._request("POST", "/labs/lab-a/comparisons", {
            "instrument_id": "spect-1", "method_id": "CELL-RESP", "method_version": 1,
            "reference_sample_id": "REF-1", "expected_value": 0.9, "observed_value": 0.901, "tolerance": 0.05,
        }, token)
        self.assertTrue(body["passed"])
        status, body = self._request("GET", "/labs/lab-a/qualification?method_id=CELL-RESP&method_version=1", token=token)
        self.assertTrue(body["qualified"])
        self._request("POST", "/lots", {
            "lot_id": "LOT-1", "product": "CMOS image sensor", "process_rev": "P3.2", "component_count": 10,
        }, token)
        for frequency, value in ((450, .9), (520, .93), (650, .88)):
            status, _ = self._request("POST", "/lots/LOT-1/measurements", {
                "lab_id": "lab-a", "instrument": "spect-1", "method_id": "CELL-RESP", "method_version": 1,
                "test_frequency_hz": frequency, "raw_value": value, "raw_unit": "A/W",
            }, token)
            self.assertEqual(status, 201)
        status, body = self._request("POST", "/lots/LOT-1/analysis", {"method_id": "CELL-RESP", "method_version": 1}, token)
        self.assertEqual(body["result"]["conclusion"], "pass")
        status, body = self._request("POST", "/lots/LOT-1/reports", {"method_id": "CELL-RESP", "method_version": 1}, token)
        self.assertEqual(status, 201)
        status, body = self._request("POST", "/lots/LOT-1/approve", {"decision": "release", "reason": "结论通过"}, token)
        self.assertEqual(body["status"], "released")
        status, body = self._request("GET", "/lots/LOT-1/traceability", token=token)
        self.assertEqual(len(body["reports"]), 1)
        self.assertEqual(body["readings"][0]["method_version"], 1)
        status, body = self._request("GET", "/committee/overview", token=token)
        self.assertEqual(body["suspended_lots"], [])
        self.assertEqual(body["current_conclusions"][0]["conclusion"], "pass")


if __name__ == "__main__":
    unittest.main()
