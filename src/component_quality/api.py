"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import ComponentQualityService


class Handler(BaseHTTPRequestHandler):
    service = ComponentQualityService()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _token(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/health":
                return self._json(200, {"status": "ok", "service": "component-quality"})
            token = self._token()
            if parts == ["methods"]:
                return self._json(200, {"methods": self.service.list_methods(token)})
            if len(parts) == 2 and parts[0] == "methods":
                version = query.get("version", [None])[0]
                return self._json(200, self.service.get_method(token, parts[1], None if version is None else int(version)))
            if len(parts) == 3 and parts[0] == "labs" and parts[2] == "qualification":
                return self._json(200, self.service.lab_qualification(
                    token, parts[1], query["method_id"][0], int(query["method_version"][0])))
            if len(parts) == 2 and parts[0] == "lots":
                return self._json(200, self.service.get_lot(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "traceability":
                return self._json(200, self.service.traceability(token, parts[1]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "audit":
                return self._json(200, {"events": self.service.audit(token, parts[1])})
            if parts == ["investigations"]:
                return self._json(200, {"investigations": self.service.list_investigations(token, query.get("status", [None])[0])})
            if parts == ["committee", "overview"]:
                return self._json(200, self.service.committee_overview(token))
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except KeyError as exc:
            return self._json(404, {"error": str(exc)})
        except Exception as exc:
            return self._json(400, {"error": str(exc)})

    def do_POST(self):
        parts = [part for part in urlparse(self.path).path.split("/") if part]
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if parts == ["login"]:
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self._token()
            if parts == ["methods"]:
                return self._json(201, self.service.publish_method(token, body))
            if parts == ["labs"]:
                return self._json(201, self.service.register_lab(token, body["lab_id"], body["name"]))
            if len(parts) == 3 and parts[0] == "labs" and parts[2] == "instruments":
                return self._json(201, self.service.register_instrument(token, parts[1], body["instrument_id"], body["model"]))
            if len(parts) == 3 and parts[0] == "labs" and parts[2] == "comparisons":
                return self._json(201, self.service.record_reference_comparison(
                    token, parts[1], body["instrument_id"], body["method_id"], body["method_version"],
                    body["reference_sample_id"], body["expected_value"], body["observed_value"], body["tolerance"]))
            if len(parts) == 3 and parts[0] == "instruments" and parts[2] == "calibrations":
                return self._json(201, self.service.record_calibration(
                    token, parts[1], body["calibration_batch"], body["certificate_ref"], body["valid_until"]))
            if parts == ["lots"]:
                return self._json(201, self.service.create_lot(
                    token, body["lot_id"], body["product"], body["process_rev"], body["component_count"],
                    body.get("lot_kind", "formal")))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "measurements":
                return self._json(201, self.service.add_measurement(
                    token, parts[1], lab_id=body["lab_id"], instrument=body["instrument"],
                    method_id=body["method_id"], method_version=body["method_version"],
                    test_frequency_hz=body["test_frequency_hz"], raw_value=body["raw_value"],
                    raw_unit=body["raw_unit"], noise=body.get("noise", 0.0)))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "analysis":
                return self._json(200, self.service.analyze(token, parts[1], body["method_id"], body["method_version"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "compare-labs":
                return self._json(200, self.service.compare_labs(token, parts[1], body["method_id"], body["method_version"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "reports":
                return self._json(201, self.service.issue_report(token, parts[1], body["method_id"], body["method_version"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "reevaluate":
                return self._json(200, self.service.reevaluate(
                    token, parts[1], body["method_id"], body["method_version"], body["reason"]))
            if len(parts) == 3 and parts[0] == "lots" and parts[2] == "approve":
                return self._json(200, self.service.approve(token, parts[1], body["decision"], body["reason"]))
            if len(parts) == 3 and parts[0] == "measurements" and parts[2] == "exclude":
                return self._json(200, self.service.exclude_measurement(
                    token, parts[1], body["reason"], body["method_id"], body["method_version"]))
            if len(parts) == 3 and parts[0] == "investigations" and parts[2] == "close":
                return self._json(200, self.service.close_investigation(
                    token, parts[1], body["findings"], bool(body.get("resume_lots", False))))
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except KeyError as exc:
            return self._json(404, {"error": str(exc)})
        except Exception as exc:
            return self._json(400, {"error": str(exc)})


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
