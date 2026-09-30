"""协调认证、方法治理、批次、检测和放行门禁的应用服务。"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from .analytics import (
    confidence_interval,
    max_pairwise_deviation,
    per_lab_means,
    summarize_response_profile,
    yield_rate,
)
from .auth import Auth
from .methods import (
    MethodValidationError,
    canonical_json,
    content_digest,
    convert_to_canonical,
    in_measurement_range,
    validate_definition,
)
from .storage import connect, event, governance_event, transaction, utcnow

PENDING_STATUSES = ("engineering", "hold")


def _parse_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"时间格式无效: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class ComponentQualityService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "component-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    # --- 方法治理：发布即冻结 ---

    def publish_method(self, token: str, definition: Mapping[str, Any]) -> dict:
        actor = self.auth.require(token, "govern")
        try:
            frozen = validate_definition(definition)
        except MethodValidationError as exc:
            raise ValueError(str(exc)) from exc
        method_id, version = frozen["method_id"], frozen["version"]
        latest = self.db.execute("SELECT MAX(version) FROM method_versions WHERE method_id=?", (method_id,)).fetchone()[0]
        if latest is not None:
            if version <= latest:
                raise ValueError("方法版本必须严格递增")
            previous, _ = self._method(method_id, latest)
            if {key: value for key, value in previous.items() if key != "version"} == {
                key: value for key, value in frozen.items() if key != "version"
            }:
                raise ValueError("方法内容未变化，不能发布新版本")
        text = canonical_json(frozen)
        digest = content_digest(text)
        with transaction(self.db):
            try:
                self.db.execute(
                    "INSERT INTO method_versions VALUES(?,?,?,?,?,?,?)",
                    (method_id, version, text, digest, "published", actor.user_id, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("方法版本或内容摘要已经存在") from exc
            self.db.execute(
                "UPDATE method_versions SET status='superseded' WHERE method_id=? AND version<?",
                (method_id, version),
            )
            governance_event(self.db, "method", f"{method_id}@{version}", "method.published", actor.user_id, {"sha256": digest})
        return {"method_id": method_id, "version": version, "sha256": digest, "status": "published"}

    def _method(self, method_id: str, version: int) -> tuple[dict, str]:
        row = self.db.execute(
            "SELECT definition_json,content_sha256 FROM method_versions WHERE method_id=? AND version=?",
            (method_id, version),
        ).fetchone()
        if not row:
            raise KeyError(f"{method_id}@{version}")
        return json.loads(row["definition_json"]), row["content_sha256"]

    def get_method(self, token: str, method_id: str, version: int | None = None) -> dict:
        self.auth.require(token, "read")
        if version is None:
            row = self.db.execute("SELECT MAX(version) FROM method_versions WHERE method_id=?", (method_id,)).fetchone()
            if not row or row[0] is None:
                raise KeyError(method_id)
            version = row[0]
        version = int(version)
        definition, digest = self._method(method_id, version)
        meta = self.db.execute(
            "SELECT status,published_by,published_at FROM method_versions WHERE method_id=? AND version=?",
            (method_id, version),
        ).fetchone()
        return {
            "method_id": method_id,
            "version": version,
            "status": meta["status"],
            "sha256": digest,
            "definition": definition,
            "published_by": meta["published_by"],
            "published_at": meta["published_at"],
        }

    def list_methods(self, token: str) -> list[dict]:
        self.auth.require(token, "read")
        rows = self.db.execute("SELECT * FROM method_versions ORDER BY method_id, version").fetchall()
        return [
            {
                "method_id": row["method_id"],
                "version": row["version"],
                "status": row["status"],
                "sha256": row["content_sha256"],
                "published_by": row["published_by"],
                "published_at": row["published_at"],
            }
            for row in rows
        ]

    # --- 实验室、仪器与校准证据 ---

    def register_lab(self, token: str, lab_id: str, name: str) -> dict:
        actor = self.auth.require(token, "govern")
        if not lab_id.strip() or not name.strip():
            raise ValueError("lab fields are invalid")
        with transaction(self.db):
            try:
                self.db.execute("INSERT INTO laboratories VALUES(?,?,?)", (lab_id, name.strip(), utcnow()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("实验室编号已经存在") from exc
            governance_event(self.db, "lab", lab_id, "lab.registered", actor.user_id, {"name": name.strip()})
        return {"lab_id": lab_id, "name": name.strip()}

    def register_instrument(self, token: str, lab_id: str, instrument_id: str, model: str) -> dict:
        actor = self.auth.require(token, "govern")
        if not self.db.execute("SELECT 1 FROM laboratories WHERE lab_id=?", (lab_id,)).fetchone():
            raise KeyError(lab_id)
        if not instrument_id.strip() or not model.strip():
            raise ValueError("instrument fields are invalid")
        with transaction(self.db):
            try:
                self.db.execute("INSERT INTO instruments VALUES(?,?,?,?)", (instrument_id, lab_id, model.strip(), utcnow()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("仪器编号已经存在") from exc
            governance_event(
                self.db, "instrument", instrument_id, "instrument.registered", actor.user_id,
                {"lab_id": lab_id, "model": model.strip()},
            )
        return {"instrument_id": instrument_id, "lab_id": lab_id, "model": model.strip()}

    def record_calibration(
        self, token: str, instrument_id: str, calibration_batch: str, certificate_ref: str, valid_until: str
    ) -> dict:
        actor = self.auth.require(token, "calibrate")
        if not self.db.execute("SELECT 1 FROM instruments WHERE instrument_id=?", (instrument_id,)).fetchone():
            raise KeyError(instrument_id)
        if not calibration_batch.strip() or not certificate_ref.strip():
            raise ValueError("calibration fields are invalid")
        expiry = _parse_instant(valid_until).isoformat()
        calibration_id = uuid.uuid4().hex
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO calibrations VALUES(?,?,?,?,?,?,?)",
                (calibration_id, instrument_id, calibration_batch.strip(), certificate_ref.strip(), utcnow(), expiry, actor.user_id),
            )
            governance_event(
                self.db, "instrument", instrument_id, "calibration.recorded", actor.user_id,
                {"calibration_id": calibration_id, "calibration_batch": calibration_batch.strip(), "valid_until": expiry},
            )
        return {"calibration_id": calibration_id, "instrument_id": instrument_id, "valid_until": expiry}

    def record_reference_comparison(
        self,
        token: str,
        lab_id: str,
        instrument_id: str,
        method_id: str,
        method_version: int,
        reference_sample_id: str,
        expected_value: float,
        observed_value: float,
        tolerance: float,
    ) -> dict:
        actor = self.auth.require(token, "calibrate")
        if not self.db.execute("SELECT 1 FROM laboratories WHERE lab_id=?", (lab_id,)).fetchone():
            raise KeyError(lab_id)
        instrument = self.db.execute("SELECT lab_id FROM instruments WHERE instrument_id=?", (instrument_id,)).fetchone()
        if not instrument:
            raise KeyError(instrument_id)
        if instrument["lab_id"] != lab_id:
            raise ValueError("仪器不属于该实验室")
        method_version = int(method_version)
        self._method(method_id, method_version)
        tolerance = float(tolerance)
        if not math.isfinite(tolerance) or tolerance <= 0:
            raise ValueError("tolerance must be positive")
        expected, observed = float(expected_value), float(observed_value)
        if not (math.isfinite(expected) and math.isfinite(observed)):
            raise ValueError("comparison values must be finite")
        if not reference_sample_id.strip():
            raise ValueError("reference_sample_id is required")
        passed = 1 if abs(observed - expected) <= tolerance else 0
        comparison_id = uuid.uuid4().hex
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO reference_comparisons VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (comparison_id, lab_id, instrument_id, method_id, method_version, reference_sample_id.strip(),
                 expected, observed, tolerance, passed, utcnow()),
            )
            governance_event(
                self.db, "instrument", instrument_id, "comparison.recorded", actor.user_id,
                {"comparison_id": comparison_id, "method_id": method_id, "method_version": method_version, "passed": bool(passed)},
            )
        return {"comparison_id": comparison_id, "passed": bool(passed)}

    def _instrument_evidence(self, instrument_id: str, method_id: str, method_version: int) -> dict:
        calibration = self.db.execute(
            "SELECT * FROM calibrations WHERE instrument_id=? ORDER BY calibrated_at DESC, calibration_id DESC LIMIT 1",
            (instrument_id,),
        ).fetchone()
        comparison = self.db.execute(
            "SELECT * FROM reference_comparisons WHERE instrument_id=? AND method_id=? AND method_version=? "
            "ORDER BY compared_at DESC, comparison_id DESC LIMIT 1",
            (instrument_id, method_id, method_version),
        ).fetchone()
        now = datetime.now(timezone.utc)
        calibration_valid = calibration is not None and _parse_instant(calibration["valid_until"]) >= now
        comparison_passed = comparison is not None and comparison["passed"] == 1
        return {
            "calibration": None if calibration is None else {
                "calibration_id": calibration["calibration_id"],
                "calibration_batch": calibration["calibration_batch"],
                "certificate_ref": calibration["certificate_ref"],
                "valid_until": calibration["valid_until"],
                "valid": calibration_valid,
            },
            "comparison": None if comparison is None else {
                "comparison_id": comparison["comparison_id"],
                "reference_sample_id": comparison["reference_sample_id"],
                "expected_value": comparison["expected_value"],
                "observed_value": comparison["observed_value"],
                "tolerance": comparison["tolerance"],
                "passed": bool(comparison["passed"]),
            },
            "qualified": calibration_valid and comparison_passed,
        }

    def lab_qualification(self, token: str, lab_id: str, method_id: str, method_version: int) -> dict:
        self.auth.require(token, "read")
        if not self.db.execute("SELECT 1 FROM laboratories WHERE lab_id=?", (lab_id,)).fetchone():
            raise KeyError(lab_id)
        method_version = int(method_version)
        self._method(method_id, method_version)
        instruments = self.db.execute("SELECT * FROM instruments WHERE lab_id=? ORDER BY instrument_id", (lab_id,)).fetchall()
        entries = [
            {
                "instrument_id": row["instrument_id"],
                "model": row["model"],
                **self._instrument_evidence(row["instrument_id"], method_id, method_version),
            }
            for row in instruments
        ]
        return {
            "lab_id": lab_id,
            "method_id": method_id,
            "method_version": method_version,
            "qualified": any(entry["qualified"] for entry in entries),
            "instruments": entries,
        }

    # --- 批次与原始读数 ---

    def create_lot(
        self, token: str, lot_id: str, product: str, process_rev: str, component_count: int, lot_kind: str = "formal"
    ) -> dict:
        actor = self.auth.require(token, "submit")
        if component_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        if lot_kind not in {"formal", "engineering"}:
            raise ValueError("lot_kind must be formal or engineering")
        now = utcnow()
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO chip_lots(lot_id,product,process_rev,component_count,lot_kind,status,previous_status,owner,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (lot_id, product, process_rev, component_count, lot_kind, "engineering", None, actor.user_id, now, now),
            )
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev, "lot_kind": lot_kind})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(
        self,
        token: str,
        lot_id: str,
        *,
        lab_id: str,
        instrument: str,
        method_id: str,
        method_version: int,
        test_frequency_hz: float,
        raw_value: float,
        raw_unit: str,
        noise: float = 0.0,
    ) -> dict:
        actor = self.auth.require(token, "measure")
        lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        method_version = int(method_version)
        definition, _ = self._method(method_id, method_version)
        if lot["product"] not in definition["applicable_products"]:
            raise ValueError("方法版本不适用于该批次产品")
        instrument_row = self.db.execute("SELECT lab_id FROM instruments WHERE instrument_id=?", (instrument,)).fetchone()
        if not instrument_row:
            raise KeyError(instrument)
        if instrument_row["lab_id"] != lab_id:
            raise ValueError("仪器不属于该实验室")
        if lot["lot_kind"] == "formal" and not self._instrument_evidence(instrument, method_id, method_version)["qualified"]:
            raise ValueError("实验室未完成仪器校准和参考样件比对，不能用于正式批次")
        raw_value = float(raw_value)
        if not math.isfinite(raw_value):
            raise ValueError("raw_value must be finite")
        if not isinstance(raw_unit, str) or not raw_unit.strip():
            raise ValueError("raw_unit is required")
        response = convert_to_canonical(definition, raw_value, raw_unit.strip())
        if not in_measurement_range(definition, response):
            raise ValueError("换算后的读数超出方法量程")
        frequency = float(test_frequency_hz)
        if not math.isfinite(frequency) or frequency <= 0:
            raise ValueError("test_frequency_hz must be positive")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO measurements(measurement_id,lot_id,lab_id,instrument,method_id,method_version,"
                "test_frequency_hz,raw_value,raw_unit,response,noise,excluded,operator,measured_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (measurement_id, lot_id, lab_id, instrument, method_id, method_version, frequency,
                 raw_value, raw_unit.strip(), response, float(noise), actor.user_id, utcnow()),
            )
            event(
                self.db, lot_id, "measurement", actor.user_id,
                {"measurement_id": measurement_id, "method_id": method_id, "method_version": method_version, "lab_id": lab_id},
            )
        return {"measurement_id": measurement_id, "lot_id": lot_id, "response": response}

    def exclude_measurement(self, token: str, measurement_id: str, reason: str, method_id: str, method_version: int) -> dict:
        actor = self.auth.require(token, "exclude")
        if not reason.strip():
            raise ValueError("exclusion reason is required")
        method_version = int(method_version)
        self._method(method_id, method_version)
        row = self.db.execute("SELECT lot_id, excluded FROM measurements WHERE measurement_id=?", (measurement_id,)).fetchone()
        if not row:
            raise KeyError(measurement_id)
        if row["excluded"]:
            raise ValueError("读数已被剔除")
        with transaction(self.db):
            self.db.execute(
                "UPDATE measurements SET excluded=1, exclusion_reason=?, exclusion_method_id=?, exclusion_method_version=? "
                "WHERE measurement_id=?",
                (reason.strip(), method_id, method_version, measurement_id),
            )
            event(
                self.db, row["lot_id"], "exclusion", actor.user_id,
                {"measurement_id": measurement_id, "reason": reason.strip(), "method_id": method_id, "method_version": method_version},
            )
        return {"measurement_id": measurement_id, "excluded": True}

    # --- 分析、跨实验室比对与报告 ---

    def _converted(self, lot_id: str, definition: Mapping[str, Any]) -> tuple[list[dict], list[str]]:
        rows = self.db.execute(
            "SELECT measurement_id, lab_id, test_frequency_hz, raw_value, raw_unit FROM measurements "
            "WHERE lot_id=? AND excluded=0 ORDER BY measurement_id",
            (lot_id,),
        ).fetchall()
        converted: list[dict] = []
        out_of_range: list[str] = []
        for row in rows:
            value = convert_to_canonical(definition, row["raw_value"], row["raw_unit"])
            if in_measurement_range(definition, value):
                converted.append({
                    "measurement_id": row["measurement_id"],
                    "lab_id": row["lab_id"],
                    "test_frequency_hz": row["test_frequency_hz"],
                    "value": value,
                })
            else:
                out_of_range.append(row["measurement_id"])
        return converted, out_of_range

    def _run_analysis(
        self, lot: Mapping[str, Any], definition: Mapping[str, Any], digest: str, converted: list[dict], out_of_range: list[str]
    ) -> dict:
        sampling = definition["sampling_requirements"]
        thresholds = definition["decision_thresholds"]
        insufficient: list[str] = []
        if len(converted) < sampling["min_samples"]:
            insufficient.append(f"有效读数 {len(converted)} 少于采样要求 {sampling['min_samples']}")
        present = {item["test_frequency_hz"] for item in converted}
        missing = [freq for freq in sampling["required_frequencies_hz"] if freq not in present]
        if missing:
            insufficient.append(f"缺少采样频点 {missing}")
        result: dict[str, Any] = {
            "lot_id": lot["lot_id"],
            "method_id": definition["method_id"],
            "method_version": definition["version"],
            "method_sha256": digest,
            "canonical_unit": definition["measurement_range"]["unit"],
            "included_count": len(converted),
            "out_of_range": out_of_range,
            "insufficient": insufficient,
        }
        if insufficient:
            result["conclusion"] = "insufficient"
            return result
        freqs = [item["test_frequency_hz"] for item in converted]
        values = [item["value"] for item in converted]
        formulas = definition["metric_formulas"]
        profile = summarize_response_profile(freqs, values, threshold=formulas["response_profile"]["params"]["threshold"])
        ci = confidence_interval(values, confidence=formulas["response_ci"]["params"]["confidence"])
        passed = sum(1 for value in values if value >= thresholds["reading_pass_response"])
        rates = yield_rate(lot["component_count"], passed, 0)
        per_lab = per_lab_means((item["lab_id"], item["value"]) for item in converted)
        conclusion = (
            "pass"
            if profile.mean_response >= thresholds["response_min"] and rates["yield"] >= thresholds["yield_min"]
            else "fail"
        )
        result.update({
            "response_profile": {**profile.__dict__, "pass_band_nm": list(profile.pass_band_nm)},
            "response_ci": [ci[0], ci[1]],
            "yield": rates,
            "per_lab": per_lab,
            "decision_thresholds": thresholds,
            "conclusion": conclusion,
        })
        return result

    def _ensure_analysis(self, actor_id: str, lot_id: str, method_id: str, method_version: int) -> dict:
        lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        definition, digest = self._method(method_id, method_version)
        if lot["product"] not in definition["applicable_products"]:
            raise ValueError("方法版本不适用于该批次产品")
        rows = self.db.execute(
            "SELECT measurement_id, test_frequency_hz, raw_value, raw_unit FROM measurements "
            "WHERE lot_id=? AND excluded=0 ORDER BY measurement_id",
            (lot_id,),
        ).fetchall()
        input_payload = {
            "method_sha256": digest,
            "readings": [
                {
                    "measurement_id": row["measurement_id"],
                    "test_frequency_hz": row["test_frequency_hz"],
                    "raw_unit": row["raw_unit"],
                    "raw_value": row["raw_value"],
                }
                for row in rows
            ],
        }
        input_sha = content_digest(canonical_json(input_payload))
        existing = self.db.execute(
            "SELECT analysis_id, result_json FROM analyses "
            "WHERE lot_id=? AND method_id=? AND method_version=? AND input_sha256=?",
            (lot_id, method_id, method_version, input_sha),
        ).fetchone()
        if existing:
            return {
                "analysis_id": existing["analysis_id"],
                "lot_id": lot_id,
                "method_id": method_id,
                "method_version": method_version,
                "input_sha256": input_sha,
                "result": json.loads(existing["result_json"]),
            }
        converted, out_of_range = self._converted(lot_id, definition)
        result = self._run_analysis(lot, definition, digest, converted, out_of_range)
        analysis_id = uuid.uuid4().hex
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO analyses VALUES(?,?,?,?,?,?,?,?)",
                (analysis_id, lot_id, method_id, method_version, input_sha, canonical_json(result), actor_id, utcnow()),
            )
            event(
                self.db, lot_id, "analysis", actor_id,
                {"analysis_id": analysis_id, "method_id": method_id, "method_version": method_version, "conclusion": result["conclusion"]},
            )
        return {
            "analysis_id": analysis_id,
            "lot_id": lot_id,
            "method_id": method_id,
            "method_version": method_version,
            "input_sha256": input_sha,
            "result": result,
        }

    def analyze(self, token: str, lot_id: str, method_id: str, method_version: int) -> dict:
        actor = self.auth.require(token, "analyze")
        return self._ensure_analysis(actor.user_id, lot_id, method_id, int(method_version))

    def compare_labs(self, token: str, lot_id: str, method_id: str, method_version: int) -> dict:
        actor = self.auth.require(token, "analyze")
        method_version = int(method_version)
        if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
            raise KeyError(lot_id)
        definition, _ = self._method(method_id, method_version)
        converted, _ = self._converted(lot_id, definition)
        per_lab = per_lab_means((item["lab_id"], item["value"]) for item in converted)
        deviation = max_pairwise_deviation(per_lab)
        threshold = definition["decision_thresholds"]["interlab_max_deviation"]
        summary: dict[str, Any] = {
            "lot_id": lot_id,
            "method_id": method_id,
            "method_version": method_version,
            "per_lab": per_lab,
            "max_deviation": deviation,
            "interlab_max_deviation": threshold,
            "systematic_bias": False,
            "investigation_id": None,
            "suspended_lots": [],
        }
        if len(per_lab) < 2 or deviation <= threshold:
            return summary
        labs = sorted(per_lab)
        implicated = sorted({a for a in labs for b in labs if abs(per_lab[a] - per_lab[b]) > threshold})
        placeholders = ",".join("?" for _ in implicated)
        affected = self.db.execute(
            "SELECT DISTINCT m.lot_id, c.status, c.previous_status FROM measurements m JOIN chip_lots c ON c.lot_id=m.lot_id "
            f"WHERE m.method_id=? AND m.method_version=? AND m.excluded=0 AND m.lab_id IN ({placeholders})",
            (method_id, method_version, *implicated),
        ).fetchall()
        detail = {
            "trigger_lot_id": lot_id,
            "per_lab": per_lab,
            "max_deviation": deviation,
            "interlab_max_deviation": threshold,
            "implicated_labs": implicated,
        }
        now = utcnow()
        suspended: list[str] = []
        with transaction(self.db):
            investigation = self.db.execute(
                "SELECT investigation_id FROM investigations WHERE method_id=? AND method_version=? AND status='open'",
                (method_id, method_version),
            ).fetchone()
            if investigation:
                investigation_id = investigation["investigation_id"]
            else:
                investigation_id = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO investigations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (investigation_id, method_id, method_version, lot_id, json.dumps(detail, sort_keys=True),
                     "open", actor.user_id, now, None, None, None),
                )
                governance_event(
                    self.db, "investigation", investigation_id, "investigation.opened", actor.user_id,
                    {"method_id": method_id, "method_version": method_version, "implicated_labs": implicated},
                )
            for row in affected:
                member = self.db.execute(
                    "SELECT suspended FROM investigation_lots WHERE investigation_id=? AND lot_id=?",
                    (investigation_id, row["lot_id"]),
                ).fetchone()
                if member is not None:
                    continue
                if row["status"] in PENDING_STATUSES:
                    pending, previous = 1, row["status"]
                elif row["status"] == "suspended":
                    # 已被其他调查暂停：保留最初状态，不重复改写
                    pending, previous = 1, row["previous_status"]
                else:
                    pending, previous = 0, None
                self.db.execute(
                    "INSERT INTO investigation_lots VALUES(?,?,?,?)",
                    (investigation_id, row["lot_id"], previous, pending),
                )
                if pending and row["status"] in PENDING_STATUSES:
                    self.db.execute(
                        "UPDATE chip_lots SET status='suspended', previous_status=?, updated_at=? WHERE lot_id=?",
                        (row["status"], now, row["lot_id"]),
                    )
                    event(
                        self.db, row["lot_id"], "suspended", actor.user_id,
                        {"investigation_id": investigation_id, "method_id": method_id, "method_version": method_version},
                    )
                    suspended.append(row["lot_id"])
            governance_event(
                self.db, "investigation", investigation_id, "investigation.updated", actor.user_id,
                {"affected_lots": [row["lot_id"] for row in affected], "newly_suspended": suspended},
            )
        summary.update({
            "systematic_bias": True,
            "implicated_labs": implicated,
            "investigation_id": investigation_id,
            "suspended_lots": suspended,
        })
        return summary

    def _current_report(self, lot_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM reports WHERE lot_id=? AND status='issued'", (lot_id,)).fetchone()

    def issue_report(self, token: str, lot_id: str, method_id: str, method_version: int) -> dict:
        actor = self.auth.require(token, "release")
        method_version = int(method_version)
        lot = self.db.execute("SELECT status FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        if lot["status"] == "suspended":
            raise ValueError("批次处于调查暂停状态，不能出具报告")
        self._method(method_id, method_version)
        if self._current_report(lot_id):
            raise ValueError("该批次已有现行报告，新结论只能通过复评产生")
        analysis = self.db.execute(
            "SELECT * FROM analyses WHERE lot_id=? AND method_id=? AND method_version=? "
            "ORDER BY created_at DESC, analysis_id DESC LIMIT 1",
            (lot_id, method_id, method_version),
        ).fetchone()
        if not analysis:
            raise ValueError("请先按该方法版本完成分析")
        result = json.loads(analysis["result_json"])
        report_id = uuid.uuid4().hex
        snapshot = canonical_json({
            "analysis_id": analysis["analysis_id"],
            "input_sha256": analysis["input_sha256"],
            "method_sha256": result["method_sha256"],
            "result": result,
        })
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (report_id, lot_id, analysis["analysis_id"], method_id, method_version,
                 result["conclusion"], snapshot, "issued", "initial", None, actor.user_id, utcnow()),
            )
            event(
                self.db, lot_id, "report_issued", actor.user_id,
                {"report_id": report_id, "method_id": method_id, "method_version": method_version, "conclusion": result["conclusion"]},
            )
        return {
            "report_id": report_id,
            "lot_id": lot_id,
            "method_id": method_id,
            "method_version": method_version,
            "conclusion": result["conclusion"],
            "status": "issued",
            "supersedes_report_id": None,
        }

    def reevaluate(self, token: str, lot_id: str, method_id: str, method_version: int, reason: str) -> dict:
        actor = self.auth.require(token, "release")
        if not reason.strip():
            raise ValueError("reevaluation reason is required")
        method_version = int(method_version)
        lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        current = self._current_report(lot_id)
        if current is None and lot["status"] != "suspended":
            raise ValueError("该批次没有现行报告，请直接出具报告")
        analysis = self._ensure_analysis(actor.user_id, lot_id, method_id, method_version)
        result = analysis["result"]
        report_id = uuid.uuid4().hex
        snapshot = canonical_json({
            "analysis_id": analysis["analysis_id"],
            "input_sha256": analysis["input_sha256"],
            "method_sha256": result["method_sha256"],
            "result": result,
            "reevaluation_reason": reason.strip(),
        })
        now = utcnow()
        with transaction(self.db):
            if current is not None:
                cursor = self.db.execute(
                    "UPDATE reports SET status='superseded' WHERE report_id=? AND status='issued'",
                    (current["report_id"],),
                )
                if cursor.rowcount != 1:
                    raise ValueError("现行报告状态已变化")
            self.db.execute(
                "INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (report_id, lot_id, analysis["analysis_id"], method_id, method_version, result["conclusion"],
                 snapshot, "issued", "reevaluation", None if current is None else current["report_id"], actor.user_id, now),
            )
            if lot["status"] == "suspended":
                self.db.execute(
                    "UPDATE chip_lots SET status=?, previous_status=NULL, updated_at=? WHERE lot_id=?",
                    (lot["previous_status"] or "engineering", now, lot_id),
                )
                event(self.db, lot_id, "resumed", actor.user_id, {"reason": "reevaluation", "report_id": report_id})
            event(
                self.db, lot_id, "reevaluated", actor.user_id,
                {
                    "report_id": report_id,
                    "method_id": method_id,
                    "method_version": method_version,
                    "conclusion": result["conclusion"],
                    "supersedes_report_id": None if current is None else current["report_id"],
                },
            )
        return {
            "report_id": report_id,
            "lot_id": lot_id,
            "method_id": method_id,
            "method_version": method_version,
            "conclusion": result["conclusion"],
            "status": "issued",
            "supersedes_report_id": None if current is None else current["report_id"],
        }

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        lot = self.db.execute("SELECT status FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        if lot["status"] == "suspended":
            raise ValueError("批次处于调查暂停状态，放行决定被阻止")
        if decision == "release":
            current = self._current_report(lot_id)
            if current is None or current["conclusion"] != "pass":
                raise ValueError("缺少结论为通过的现行报告，不能放行")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    # --- 调查 ---

    def close_investigation(self, token: str, investigation_id: str, findings: str, resume_lots: bool) -> dict:
        actor = self.auth.require(token, "investigate")
        if not findings.strip():
            raise ValueError("findings are required")
        row = self.db.execute("SELECT status FROM investigations WHERE investigation_id=?", (investigation_id,)).fetchone()
        if not row:
            raise KeyError(investigation_id)
        if row["status"] != "open":
            raise ValueError("调查已经关闭")
        now = utcnow()
        resumed: list[str] = []
        with transaction(self.db):
            self.db.execute(
                "UPDATE investigations SET status='closed', closed_by=?, closed_at=?, findings=? WHERE investigation_id=?",
                (actor.user_id, now, findings.strip(), investigation_id),
            )
            if resume_lots:
                members = self.db.execute(
                    "SELECT lot_id, previous_status FROM investigation_lots WHERE investigation_id=? AND suspended=1",
                    (investigation_id,),
                ).fetchall()
                for member in members:
                    self.db.execute(
                        "UPDATE chip_lots SET status=?, previous_status=NULL, updated_at=? WHERE lot_id=?",
                        (member["previous_status"] or "engineering", now, member["lot_id"]),
                    )
                    self.db.execute(
                        "UPDATE investigation_lots SET suspended=0 WHERE investigation_id=? AND lot_id=?",
                        (investigation_id, member["lot_id"]),
                    )
                    event(self.db, member["lot_id"], "resumed", actor.user_id, {"investigation_id": investigation_id})
                    resumed.append(member["lot_id"])
            governance_event(
                self.db, "investigation", investigation_id, "investigation.closed", actor.user_id,
                {"findings": findings.strip(), "resume_lots": bool(resume_lots), "resumed": resumed},
            )
        return {"investigation_id": investigation_id, "status": "closed", "resumed_lots": resumed}

    def list_investigations(self, token: str, status: str | None = None) -> list[dict]:
        self.auth.require(token, "read")
        query = "SELECT * FROM investigations"
        args: tuple = ()
        if status:
            query += " WHERE status=?"
            args = (status,)
        rows = self.db.execute(query + " ORDER BY opened_at, investigation_id", args).fetchall()
        result = []
        for row in rows:
            members = self.db.execute(
                "SELECT lot_id, suspended FROM investigation_lots WHERE investigation_id=? ORDER BY lot_id",
                (row["investigation_id"],),
            ).fetchall()
            result.append({
                "investigation_id": row["investigation_id"],
                "method_id": row["method_id"],
                "method_version": row["method_version"],
                "trigger_lot_id": row["trigger_lot_id"],
                "status": row["status"],
                "detail": json.loads(row["detail_json"]),
                "affected_lots": [member["lot_id"] for member in members],
                "suspended_lots": [member["lot_id"] for member in members if member["suspended"]],
                "opened_by": row["opened_by"],
                "opened_at": row["opened_at"],
                "closed_by": row["closed_by"],
                "closed_at": row["closed_at"],
                "findings": row["findings"],
            })
        return result

    # --- 委员会视图 ---

    def _interlab_summary(self, lot_id: str) -> list[dict]:
        methods_used = self.db.execute(
            "SELECT DISTINCT method_id, method_version FROM measurements WHERE lot_id=? ORDER BY method_id, method_version",
            (lot_id,),
        ).fetchall()
        summary = []
        for row in methods_used:
            definition, digest = self._method(row["method_id"], row["method_version"])
            converted, _ = self._converted(lot_id, definition)
            per_lab = per_lab_means((item["lab_id"], item["value"]) for item in converted)
            threshold = definition["decision_thresholds"]["interlab_max_deviation"]
            deviation = max_pairwise_deviation(per_lab)
            summary.append({
                "method_id": row["method_id"],
                "method_version": row["method_version"],
                "method_sha256": digest,
                "per_lab": per_lab,
                "max_deviation": deviation,
                "interlab_max_deviation": threshold,
                "within_tolerance": deviation <= threshold,
            })
        return summary

    def traceability(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        readings = [
            dict(row)
            for row in self.db.execute(
                "SELECT measurement_id, lab_id, instrument, method_id, method_version, test_frequency_hz, "
                "raw_value, raw_unit, response, noise, excluded, exclusion_reason, "
                "exclusion_method_id, exclusion_method_version, operator, measured_at "
                "FROM measurements WHERE lot_id=? ORDER BY measured_at, measurement_id",
                (lot_id,),
            ).fetchall()
        ]
        devices = sorted({(row["lab_id"], row["instrument"]) for row in readings})
        methods_used = sorted({(row["method_id"], row["method_version"]) for row in readings})
        evidence = []
        for lab_id, instrument in devices:
            evidence.append({
                "lab_id": lab_id,
                "instrument": instrument,
                "methods": [
                    {"method_id": method_id, "method_version": version, **self._instrument_evidence(instrument, method_id, version)}
                    for method_id, version in methods_used
                ],
            })
        analyses = [
            {
                "analysis_id": row["analysis_id"],
                "method_id": row["method_id"],
                "method_version": row["method_version"],
                "input_sha256": row["input_sha256"],
                "conclusion": json.loads(row["result_json"])["conclusion"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            }
            for row in self.db.execute("SELECT * FROM analyses WHERE lot_id=? ORDER BY created_at", (lot_id,)).fetchall()
        ]
        reports = [
            dict(row)
            for row in self.db.execute(
                "SELECT report_id, analysis_id, method_id, method_version, conclusion, status, cause, "
                "supersedes_report_id, issued_by, issued_at FROM reports WHERE lot_id=? ORDER BY issued_at",
                (lot_id,),
            ).fetchall()
        ]
        investigations = [item for item in self.list_investigations(token) if lot_id in item["affected_lots"]]
        return {
            "lot": dict(lot),
            "readings": readings,
            "calibration_evidence": evidence,
            "analyses": analyses,
            "reports": reports,
            "investigations": investigations,
            "interlab": self._interlab_summary(lot_id),
        }

    def committee_overview(self, token: str) -> dict:
        self.auth.require(token, "read")
        suspended = [
            dict(row)
            for row in self.db.execute(
                "SELECT lot_id, product, process_rev, lot_kind, status, previous_status, updated_at "
                "FROM chip_lots WHERE status='suspended' ORDER BY lot_id"
            ).fetchall()
        ]
        interlab: list[dict] = []
        conclusions: list[dict] = []
        for row in self.db.execute("SELECT lot_id FROM chip_lots ORDER BY lot_id").fetchall():
            current = self._current_report(row["lot_id"])
            if current:
                conclusions.append({
                    "lot_id": row["lot_id"],
                    "report_id": current["report_id"],
                    "conclusion": current["conclusion"],
                    "method_id": current["method_id"],
                    "method_version": current["method_version"],
                    "issued_at": current["issued_at"],
                })
            for item in self._interlab_summary(row["lot_id"]):
                if len(item["per_lab"]) >= 2:
                    interlab.append({"lot_id": row["lot_id"], **item})
        return {
            "methods": self.list_methods(token),
            "suspended_lots": suspended,
            "open_investigations": self.list_investigations(token, status="open"),
            "interlab_differences": interlab,
            "current_conclusions": conclusions,
        }

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
