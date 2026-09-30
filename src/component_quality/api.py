"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import ComponentQualityService


class Handler(BaseHTTPRequestHandler):
    service = ComponentQualityService()

    def _json(self, status: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def _error(self, exc: Exception) -> int:
        if isinstance(exc, PermissionError):
            return self._json(403, {"error": str(exc)})
        if isinstance(exc, KeyError):
            return self._json(404, {"error": str(exc).strip("'\"")})
        return self._json(400, {"error": str(exc)})

    def do_GET(self):
        path = self.path
        if path == "/health":
            return self._json(200, {"status": "ok", "service": "component-quality"})
        try:
            if path == "/methods":
                return self._json(200, self.service.list_methods(self._token()))
            if path == "/committee/overview":
                return self._json(200, self.service.committee_overview(self._token()))
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "methods":
                return self._json(200, self.service.get_method(self._token(), parts[1], int(parts[2])))
            if len(parts) == 2 and parts[0] == "lots":
                return self._json(200, self.service.get_lot(self._token(), parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "evidence":
                return self._json(200, self.service.lot_evidence(self._token(), parts[1]))
            if len(parts) == 2 and parts[0] == "labs":
                return self._json(200, self.service.get_laboratory(self._token(), parts[1]))
            if len(parts) == 2 and parts[0] == "calibrations":
                return self._json(200, self.service.get_calibration(self._token(), parts[1]))
            if len(parts) == 2 and parts[0] == "analyses":
                return self._json(200, self.service.get_analysis(self._token(), parts[1]))
            if len(parts) == 2 and parts[0] == "comparisons":
                return self._json(200, self.service.get_comparison(self._token(), parts[1]))
            if len(parts) == 2 and parts[0] == "investigations":
                return self._json(200, self.service.get_investigation(self._token(), parts[1]))
            return self._json(404, {"error": "not found"})
        except Exception as exc:
            return self._error(exc)

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "invalid json"})
        path = self.path
        try:
            if path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            parts = path.strip("/").split("/")

            if path == "/methods":
                return self._json(201, self.service.publish_method(token, body["spec"] if "spec" in body else body))
            if len(parts) == 4 and parts[0] == "methods" and parts[3] == "withdraw":
                return self._json(200, self.service.withdraw_method(token, parts[1], int(parts[2]), body["reason"]))
            if path == "/labs":
                return self._json(201, self.service.register_laboratory(token, body["lab_id"], body["name"]))
            if path == "/instruments":
                return self._json(201, self.service.register_instrument(
                    token, body["instrument_id"], body["lab_id"], body["serial"]))
            if path == "/calibrations":
                return self._json(201, self.service.record_calibration(
                    token, body["calibration_id"], body["instrument_id"],
                    body["method_id"], int(body["revision"]), body["certificate_ref"],
                    body["calibrated_at"], body["valid_until"]))
            if len(parts) == 3 and parts[0] == "calibrations" and parts[2] == "reference-checks":
                return self._json(201, self.service.record_reference_check(
                    token, parts[1], body["reference_sample_id"], body["nominal_value"], body["nominal_unit"],
                    body["observed_value"], body["observed_unit"], body["tolerance_abs"], body.get("note")))
            if path == "/lots":
                return self._json(201, self.service.create_lot(
                    token, body["lot_id"], body["product"], body["process_rev"], body["component_count"],
                    body.get("method_id"), body.get("revision")))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return self._json(201, self.service.add_measurement(
                    token, parts[1], body["test_frequency_hz"], body["response"], body.get("noise", 0.0),
                    body["instrument"], body.get("lab_id"), body.get("calibration_id"), body.get("units")))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return self._json(200, self.service.run_analysis(
                    token, parts[1], body.get("method_id"), body.get("revision")))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "reports":
                return self._json(201, self.service.issue_report(
                    token, parts[1], body["decision"], body["reason"], body.get("analysis_id")))
            if len(parts) == 3 and parts[0] == "measurements" and parts[2] == "exclusions":
                return self._json(201, self.service.exclude_measurement(
                    token, parts[1], body["method_id"], int(body["revision"]), body["reason"]))
            if path == "/comparisons":
                return self._json(201, self.service.record_comparison(
                    token, body["comparison_id"], body["method_id"], int(body["revision"]),
                    body["reference_sample_id"], body["observations"]))
            if len(parts) == 3 and parts[0] == "investigations" and parts[2] == "close":
                return self._json(200, self.service.close_investigation(
                    token, parts[1], body["resolution_method_id"], int(body["resolution_revision"]),
                    body["note"]))
            return self._json(404, {"error": "not found"})
        except Exception as exc:
            return self._error(exc)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = ComponentQualityService(args.database)
    Handler.service.bootstrap_admin()
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
