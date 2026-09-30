"""方法治理协调层：方法发布冻结、校准证据门禁、不可变结论与跨实验室调查。"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Any

from .analytics import (
    analyze_lot_readings,
    confidence_interval,
    summarize_response_profile,
    yield_rate,
)
from .auth import Auth
from .methods import MethodSpec, canonical_json, content_fingerprint
from .storage import connect, event, transaction, utcnow


def _parse_day(value: str) -> datetime:
    text = value.strip()
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=timezone.utc)


def governance_event(db, scope: str, ref_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO governance_events(scope,ref_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?,?)",
        (scope, ref_id, event_type, actor, json.dumps(payload, sort_keys=True, ensure_ascii=False), utcnow()),
    )


class ComponentQualityService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "component-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    # ------------------------------------------------------------------ 方法版本

    def publish_method(self, token: str, spec: dict) -> dict:
        """冻结方法内容并发布新版本；同内容重复提交为幂等。"""
        actor = self.auth.require(token, "govern")
        parsed = MethodSpec.from_dict(spec)
        canonical = canonical_json(spec)
        fingerprint = content_fingerprint(canonical)
        with transaction(self.db):
            existing = self.db.execute(
                "SELECT method_id,revision FROM method_revisions WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing:
                return self.get_method(token, existing["method_id"], existing["revision"])
            now = utcnow()
            self.db.execute(
                "INSERT OR IGNORE INTO methods VALUES(?,?,?,?)",
                (parsed.method_id, parsed.title, actor.user_id, now),
            )
            row = self.db.execute(
                "SELECT COALESCE(MAX(revision),0) AS rev FROM method_revisions WHERE method_id=?",
                (parsed.method_id,),
            ).fetchone()
            revision = row["rev"] + 1
            self.db.execute(
                "INSERT INTO method_revisions VALUES(?,?,?,?,?, 'published', ?, ?, NULL, NULL)",
                (parsed.method_id, revision, canonical, fingerprint,
                 revision - 1 if revision > 1 else None, actor.user_id, now),
            )
            if revision > 1:
                self.db.execute(
                    "UPDATE method_revisions SET status='superseded' WHERE method_id=? AND revision=? AND status='published'",
                    (parsed.method_id, revision - 1),
                )
            governance_event(self.db, "method", parsed.method_id, "method.published", actor.user_id,
                             {"revision": revision, "fingerprint": fingerprint,
                              "products": list(parsed.applicable_products)})
        return self.get_method(token, parsed.method_id, revision)

    def get_method(self, token: str, method_id: str, revision: int) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute(
            "SELECT * FROM method_revisions WHERE method_id=? AND revision=?", (method_id, revision)
        ).fetchone()
        if not row:
            raise KeyError(f"{method_id} r{revision}")
        result = dict(row)
        result["spec"] = json.loads(row["canonical_json"])
        return result

    def list_methods(self, token: str) -> list[dict]:
        self.auth.require(token, "read")
        rows = self.db.execute(
            "SELECT method_id,revision,status,fingerprint,published_at,supersedes_revision FROM method_revisions ORDER BY method_id,revision"
        ).fetchall()
        return [dict(row) for row in rows]

    def withdraw_method(self, token: str, method_id: str, revision: int, reason: str) -> dict:
        actor = self.auth.require(token, "govern")
        if not reason.strip():
            raise ValueError("reason is required")
        with transaction(self.db):
            cursor = self.db.execute(
                "UPDATE method_revisions SET status='withdrawn', withdrawn_by=?, withdrawn_at=? "
                "WHERE method_id=? AND revision=? AND status='published'",
                (actor.user_id, utcnow(), method_id, revision),
            )
            if cursor.rowcount == 0:
                raise ValueError("only a published revision can be withdrawn")
            governance_event(self.db, "method", method_id, "method.withdrawn", actor.user_id,
                             {"revision": revision, "reason": reason})
        return self.get_method(token, method_id, revision)

    def _load_method(self, method_id: str, revision: int) -> tuple[MethodSpec, dict]:
        row = self.db.execute(
            "SELECT * FROM method_revisions WHERE method_id=? AND revision=?", (method_id, revision)
        ).fetchone()
        if not row:
            raise KeyError(f"{method_id} r{revision}")
        if row["status"] != "published":
            raise ValueError(f"method {method_id} r{revision} 不是已发布状态（{row['status']}）")
        return MethodSpec.from_dict(json.loads(row["canonical_json"])), dict(row)

    # ------------------------------------------------------------------ 实验室与仪器

    def register_laboratory(self, token: str, lab_id: str, name: str) -> dict:
        actor = self.auth.require(token, "govern")
        if not lab_id.strip() or not name.strip():
            raise ValueError("lab_id and name are required")
        with transaction(self.db):
            self.db.execute("INSERT INTO laboratories VALUES(?,?,?,?)", (lab_id, name, actor.user_id, utcnow()))
            governance_event(self.db, "laboratory", lab_id, "laboratory.registered", actor.user_id, {"name": name})
        return self.get_laboratory(token, lab_id)

    def get_laboratory(self, token: str, lab_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM laboratories WHERE lab_id=?", (lab_id,)).fetchone()
        if not row:
            raise KeyError(lab_id)
        return dict(row)

    def register_instrument(self, token: str, instrument_id: str, lab_id: str, serial: str) -> dict:
        actor = self.auth.require(token, "govern")
        if not instrument_id.strip() or not serial.strip():
            raise ValueError("instrument_id and serial are required")
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM laboratories WHERE lab_id=?", (lab_id,)).fetchone():
                raise KeyError(lab_id)
            self.db.execute("INSERT INTO instruments VALUES(?,?,?,?,?)",
                            (instrument_id, lab_id, serial, actor.user_id, utcnow()))
            governance_event(self.db, "instrument", instrument_id, "instrument.registered", actor.user_id,
                             {"lab_id": lab_id, "serial": serial})
        return {"instrument_id": instrument_id, "lab_id": lab_id, "serial": serial}

    # ------------------------------------------------------------------ 校准与参考样件

    def record_calibration(
        self, token: str, calibration_id: str, instrument_id: str, method_id: str, revision: int,
        certificate_ref: str, calibrated_at: str, valid_until: str,
    ) -> dict:
        actor = self.auth.require(token, "govern")
        if not calibration_id.strip() or not certificate_ref.strip():
            raise ValueError("calibration_id and certificate_ref are required")
        valid_from = _parse_day(calibrated_at)
        valid_to = _parse_day(valid_until)
        if valid_to <= valid_from:
            raise ValueError("valid_until 必须晚于 calibrated_at")
        with transaction(self.db):
            instrument = self.db.execute("SELECT * FROM instruments WHERE instrument_id=?", (instrument_id,)).fetchone()
            if not instrument:
                raise KeyError(instrument_id)
            self._load_method(method_id, revision)
            self.db.execute(
                "INSERT INTO calibrations VALUES(?,?,?,?,?,?,?, 'pending', ?, ?, NULL, NULL)",
                (calibration_id, instrument_id, method_id, revision, certificate_ref,
                 calibrated_at, valid_until, actor.user_id, utcnow()),
            )
            governance_event(self.db, "calibration", calibration_id, "calibration.recorded", actor.user_id,
                             {"instrument_id": instrument_id, "method_id": method_id, "revision": revision,
                              "certificate_ref": certificate_ref, "valid_until": valid_until})
        return self.get_calibration(token, calibration_id)

    def record_reference_check(
        self, token: str, calibration_id: str, reference_sample_id: str,
        nominal_value: float, nominal_unit: str, observed_value: float, observed_unit: str,
        tolerance_abs: float, note: str | None = None,
    ) -> dict:
        actor = self.auth.require(token, "govern")
        with transaction(self.db):
            calibration = self.db.execute(
                "SELECT * FROM calibrations WHERE calibration_id=?", (calibration_id,)
            ).fetchone()
            if not calibration:
                raise KeyError(calibration_id)
            if calibration["status"] not in ("pending",):
                raise ValueError(f"校准 {calibration_id} 状态为 {calibration['status']}，不能再补比对")
            spec, _ = self._load_method(calibration["method_id"], calibration["revision"])
            channel = spec.channels["response"]
            canonical_nominal = channel.convert(float(nominal_value), nominal_unit)
            canonical_observed = channel.convert(float(observed_value), observed_unit)
            deviation_abs = abs(canonical_observed - canonical_nominal)
            deviation_rel = (
                abs(deviation_abs / canonical_nominal) if canonical_nominal != 0 else None
            )
            within = deviation_abs <= float(tolerance_abs)
            check_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO reference_checks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (check_id, calibration_id, reference_sample_id, float(nominal_value), nominal_unit,
                 float(observed_value), observed_unit, canonical_nominal, canonical_observed,
                 deviation_abs, deviation_rel, float(tolerance_abs), 1 if within else 0,
                 actor.user_id, utcnow(), note),
            )
            new_status = "verified" if within else "rejected"
            if within:
                self.db.execute(
                    "UPDATE calibrations SET status='verified', verified_by=?, verified_at=? WHERE calibration_id=?",
                    (actor.user_id, utcnow(), calibration_id),
                )
            else:
                self.db.execute(
                    "UPDATE calibrations SET status='rejected' WHERE calibration_id=?", (calibration_id,)
                )
            governance_event(self.db, "calibration", calibration_id, "reference_check.recorded", actor.user_id,
                             {"check_id": check_id, "reference_sample_id": reference_sample_id,
                              "deviation_abs": deviation_abs, "within_tolerance": within,
                              "calibration_status": new_status})
        return self.get_calibration(token, calibration_id)

    def get_calibration(self, token: str, calibration_id: str) -> dict:
        self.auth.require(token, "read")
        calibration = self.db.execute("SELECT * FROM calibrations WHERE calibration_id=?", (calibration_id,)).fetchone()
        if not calibration:
            raise KeyError(calibration_id)
        checks = self.db.execute(
            "SELECT * FROM reference_checks WHERE calibration_id=? ORDER BY checked_at", (calibration_id,)
        ).fetchall()
        result = dict(calibration)
        result["reference_checks"] = [dict(row) for row in checks]
        return result

    def _verified_calibration(self, calibration_id: str, method_id: str, revision: int, instrument_id: str, at: datetime) -> dict:
        calibration = self.db.execute(
            "SELECT * FROM calibrations WHERE calibration_id=?", (calibration_id,)
        ).fetchone()
        if not calibration:
            raise KeyError(calibration_id)
        if calibration["status"] != "verified":
            raise ValueError(f"校准 {calibration_id} 尚未通过参考样件比对（{calibration['status']}）")
        if (calibration["method_id"], calibration["revision"]) != (method_id, revision):
            raise ValueError(
                f"校准 {calibration_id} 针对 {calibration['method_id']} r{calibration['revision']}，"
                f"不适用于 {method_id} r{revision}"
            )
        if _parse_day(calibration["valid_until"]) < at:
            raise ValueError(f"校准 {calibration_id} 已过有效期")
        if calibration["instrument_id"] != instrument_id:
            raise ValueError("校准证据与所用仪器不一致")
        return dict(calibration)

    # ------------------------------------------------------------------ 批次

    def create_lot(
        self, token: str, lot_id: str, product: str, process_rev: str, component_count: int,
        method_id: str | None = None, revision: int | None = None,
    ) -> dict:
        actor = self.auth.require(token, "submit")
        if component_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        if (method_id is None) != (revision is None):
            raise ValueError("method_id 与 revision 必须同时给出")
        with transaction(self.db):
            if method_id is not None:
                spec, _ = self._load_method(method_id, revision)
                if not spec.applies_to(product):
                    raise ValueError(f"方法 {method_id} r{revision} 不适用于产品 {product}")
            now = utcnow()
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (lot_id, product, process_rev, component_count, "engineering",
                             actor.user_id, now, now, method_id, revision, None, None))
            event(self.db, lot_id, "created", actor.user_id,
                  {"product": product, "process_rev": process_rev,
                   "method_id": method_id, "revision": revision})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(
        self, token: str, lot_id: str, test_frequency_hz: float, response: float, noise: float,
        instrument: str, lab_id: str | None = None, calibration_id: str | None = None,
        units: dict | None = None,
    ) -> dict:
        """登记原始读数。原值、原单位、设备身份与实验室原样保留。

        治理模式（批次绑定方法）下必须给出实验室、仪器与有效校准证据，
        且校准覆盖所用方法版本并处于已验证、有效期内状态。
        """
        actor = self.auth.require(token, "measure")
        units = units or {}
        with transaction(self.db):
            lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if not lot:
                raise KeyError(lot_id)
            if lot["suspended_at"]:
                raise ValueError(f"批次 {lot_id} 已被跨实验室调查暂停，不能新增读数")
            raw = {
                "test_frequency": {"value": float(test_frequency_hz), "unit": units.get("test_frequency", "Hz")},
                "response": {"value": float(response), "unit": units.get("response")},
                "noise": {"value": float(noise), "unit": units.get("noise")},
            }
            stored_lab = lab_id
            stored_calibration = calibration_id
            if lot["method_id"]:
                if not lab_id or not calibration_id:
                    raise ValueError("正式批次读数必须给出实验室与校准证据")
                spec, _ = self._load_method(lot["method_id"], lot["revision"])
                instrument_row = self.db.execute(
                    "SELECT * FROM instruments WHERE instrument_id=?", (instrument,)
                ).fetchone()
                if not instrument_row:
                    raise KeyError(instrument)
                if instrument_row["lab_id"] != lab_id:
                    raise ValueError("仪器不属于所声明的实验室")
                self._verified_calibration(
                    calibration_id, lot["method_id"], lot["revision"], instrument, datetime.now(timezone.utc)
                )
                # 换算与量程检查都在冻结方法版本声明的单位表内完成
                canonical = {
                    key: spec.convert_reading(key, raw[key]["value"], raw[key]["unit"]) for key in spec.channels
                }
                for key, value in canonical.items():
                    if not spec.in_range(key, value):
                        raise ValueError(f"读数换算后超出方法 {lot['method_id']} r{lot['revision']} 声明的 {key} 量程")
                test_frequency_hz, response, noise = (
                    canonical["test_frequency"], canonical["response"], canonical["noise"]
                )
                raw["canonical"] = canonical
            measurement_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (measurement_id, lot_id, float(test_frequency_hz), float(response), float(noise),
                 instrument, actor.user_id, utcnow(), stored_lab, stored_calibration,
                 json.dumps(raw, ensure_ascii=False, sort_keys=True)),
            )
            event(self.db, lot_id, "measurement", actor.user_id,
                  {"measurement_id": measurement_id, "lab_id": stored_lab,
                   "calibration_id": stored_calibration, "instrument": instrument})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    # ------------------------------------------------------------------ 剔除（引用方法版本）

    def exclude_measurement(
        self, token: str, measurement_id: str, method_id: str, revision: int, reason: str
    ) -> dict:
        actor = self.auth.require(token, "analyze")
        if not reason.strip():
            raise ValueError("reason is required")
        with transaction(self.db):
            measurement = self.db.execute(
                "SELECT * FROM measurements WHERE measurement_id=?", (measurement_id,)
            ).fetchone()
            if not measurement:
                raise KeyError(measurement_id)
            self._load_method(method_id, revision)
            exclusion_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT OR REPLACE INTO measurement_exclusions VALUES(?,?,?,?,?,?,?,?)",
                (exclusion_id, measurement_id, measurement["lot_id"], method_id, revision,
                 reason, actor.user_id, utcnow()),
            )
            event(self.db, measurement["lot_id"], "measurement.excluded", actor.user_id,
                  {"measurement_id": measurement_id, "method_id": method_id, "revision": revision,
                   "reason": reason})
            return {"exclusion_id": exclusion_id, "measurement_id": measurement_id,
                    "method_id": method_id, "revision": revision, "reason": reason}

    # ------------------------------------------------------------------ 不可变分析版本

    def run_analysis(self, token: str, lot_id: str, method_id: str | None = None, revision: int | None = None) -> dict:
        actor = self.auth.require(token, "analyze")
        with transaction(self.db):
            lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if not lot:
                raise KeyError(lot_id)
            target_method = method_id or lot["method_id"]
            target_revision = revision or lot["revision"]
            if not target_method or not target_revision:
                raise ValueError("批次未绑定治理方法，无法进行版本化分析")
            spec, method_row = self._load_method(target_method, target_revision)
            if not spec.applies_to(lot["product"]):
                raise ValueError(f"方法 {target_method} r{target_revision} 不适用于产品 {lot['product']}")
            if target_method == lot["method_id"] and target_revision < lot["revision"]:
                raise ValueError("不能回退到更旧的方法版本")
            if lot["suspended_at"] and (target_method, target_revision) == (lot["method_id"], lot["revision"]):
                raise PermissionError("批次处于调查暂停状态：受疑方法版本不能重算，只能用后继方法复评")
            measurements = self.db.execute(
                "SELECT * FROM measurements WHERE lot_id=? ORDER BY measured_at,measurement_id", (lot_id,)
            ).fetchall()
            if not measurements:
                raise ValueError("批次尚无读数")
            exclusions = {
                row["measurement_id"]: dict(row)
                for row in self.db.execute(
                    "SELECT * FROM measurement_exclusions WHERE lot_id=? AND method_id=? AND revision=?",
                    (lot_id, target_method, target_revision),
                )
            }
            included, excluded_rows, out_of_range, conversions = [], [], [], []
            calibrations_used: dict[str, str] = {}
            for row in measurements:
                raw = json.loads(row["raw_json"]) if row["raw_json"] else None
                if row["measurement_id"] in exclusions:
                    excluded_rows.append({"measurement_id": row["measurement_id"],
                                          "reason": exclusions[row["measurement_id"]]["reason"]})
                    continue
                canonical_values = {}
                for key in spec.channels:
                    if raw and raw[key]["value"] is not None:
                        canonical = spec.convert_reading(key, raw[key]["value"], raw[key]["unit"])
                        canonical_values[key] = canonical
                        if raw[key]["unit"] != spec.channels[key].canonical_unit:
                            conversions.append(
                                {"measurement_id": row["measurement_id"], "channel": key,
                                 "raw_value": raw[key]["value"], "raw_unit": raw[key]["unit"],
                                 "canonical_value": canonical,
                                 "canonical_unit": spec.channels[key].canonical_unit,
                                 "method_id": target_method, "revision": target_revision})
                    else:
                        canonical_values[key] = row[
                            {"test_frequency": "test_frequency_hz", "response": "response", "noise": "noise"}[key]
                        ]
                if any(not spec.in_range(key, value) for key, value in canonical_values.items()):
                    out_of_range.append(row["measurement_id"])
                    continue
                included.append((row, canonical_values))
                if row["calibration_id"]:
                    certificate = self.db.execute(
                        "SELECT certificate_ref FROM calibrations WHERE calibration_id=?",
                        (row["calibration_id"],),
                    ).fetchone()
                    calibrations_used[row["calibration_id"]] = certificate["certificate_ref"] if certificate else None
            if len(included) < spec.sampling.min_points:
                raise ValueError(
                    f"有效读数 {len(included)} 个，少于方法要求的 {spec.sampling.min_points} 个"
                )
            frequencies = [values["test_frequency"] for _, values in included]
            if min(frequencies) < spec.sampling.frequency_window_hz[0] or max(frequencies) > spec.sampling.frequency_window_hz[1]:
                raise ValueError("采样频率超出方法冻结的采样窗口")
            yield_rule = next((rule for rule in spec.thresholds if rule.metric == "yield"), None)
            metrics = analyze_lot_readings(
                [(values["test_frequency"], values["response"], values["noise"]) for _, values in included],
                pass_band_threshold_ratio=spec.formulas["pass_band_threshold_ratio"],
                ci_confidence=spec.formulas["ci_confidence"],
                yield_operator=yield_rule.operator if yield_rule else "gte",
                yield_value=yield_rule.value if yield_rule else 0.8,
            )
            threshold_results, conclusion = spec.evaluate(metrics)
            input_material = [
                {"measurement_id": row["measurement_id"], "raw_json": json.loads(row["raw_json"]) if row["raw_json"] else None,
                 "calibration_id": row["calibration_id"], "lab_id": row["lab_id"]}
                for row, _ in included
            ]
            input_fingerprint = content_fingerprint(canonical_json(input_material))
            result = {
                "metrics": metrics,
                "thresholds": threshold_results,
                "included_measurements": [row["measurement_id"] for row, _ in included],
                "excluded": excluded_rows,
                "out_of_range": out_of_range,
                "conversions": conversions,
                "calibrations_used": calibrations_used,
            }
            sequence = self.db.execute(
                "SELECT COALESCE(MAX(sequence_no),0) AS seq FROM analysis_versions WHERE lot_id=?", (lot_id,)
            ).fetchone()["seq"] + 1
            analysis_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO analysis_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (analysis_id, lot_id, target_method, target_revision, sequence, input_fingerprint,
                 json.dumps(excluded_rows, ensure_ascii=False, sort_keys=True),
                 json.dumps(result, ensure_ascii=False, sort_keys=True), conclusion,
                 actor.user_id, utcnow()),
            )
            event(self.db, lot_id, "analysis.version_created", actor.user_id,
                  {"analysis_id": analysis_id, "sequence_no": sequence,
                   "method_id": target_method, "revision": target_revision,
                   "fingerprint": method_row["fingerprint"], "conclusion": conclusion})
        return self.get_analysis(token, analysis_id)

    def get_analysis(self, token: str, analysis_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM analysis_versions WHERE analysis_id=?", (analysis_id,)).fetchone()
        if not row:
            raise KeyError(analysis_id)
        result = dict(row)
        result["result"] = json.loads(row["result_json"])
        method = self.db.execute(
            "SELECT fingerprint,status FROM method_revisions WHERE method_id=? AND revision=?",
            (row["method_id"], row["revision"]),
        ).fetchone()
        result["method_fingerprint"] = method["fingerprint"]
        return result

    def latest_analysis(self, token: str, lot_id: str) -> dict | None:
        self.auth.require(token, "read")
        row = self.db.execute(
            "SELECT analysis_id FROM analysis_versions WHERE lot_id=? ORDER BY sequence_no DESC LIMIT 1", (lot_id,)
        ).fetchone()
        return self.get_analysis(token, row["analysis_id"]) if row else None

    # ------------------------------------------------------------------ 放行报告（不可静默重算）

    def issue_report(
        self, token: str, lot_id: str, decision: str, reason: str, analysis_id: str | None = None
    ) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
            if not lot:
                raise KeyError(lot_id)
            if lot["suspended_at"]:
                raise PermissionError(f"批次 {lot_id} 处于调查暂停状态，未决放行已冻结")
            if analysis_id is None:
                latest = self.db.execute(
                    "SELECT analysis_id FROM analysis_versions WHERE lot_id=? ORDER BY sequence_no DESC LIMIT 1",
                    (lot_id,),
                ).fetchone()
                if not latest:
                    raise ValueError("批次尚无分析版本")
                analysis_id = latest["analysis_id"]
            analysis = self.db.execute(
                "SELECT * FROM analysis_versions WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if not analysis or analysis["lot_id"] != lot_id:
                raise KeyError(analysis_id)
            prior_investigation = self.db.execute(
                "SELECT i.resolution_method_id, i.resolution_revision FROM investigations i "
                "JOIN investigation_lots il ON il.investigation_id=i.investigation_id "
                "WHERE il.lot_id=? AND i.status='closed' ORDER BY i.closed_at DESC LIMIT 1", (lot_id,)
            ).fetchone()
            if prior_investigation:
                valid_resolution = (
                    analysis["method_id"] == prior_investigation["resolution_method_id"]
                    and analysis["revision"] >= prior_investigation["resolution_revision"]
                )
                if not valid_resolution:
                    raise PermissionError("调查关闭后的放行必须基于后继方法版本的复评结论")
            if decision == "release" and analysis["conclusion"] != "pass":
                raise ValueError("分析结论不为 pass，不能放行")
            material = canonical_json({
                "lot_id": lot_id, "analysis_id": analysis_id,
                "method_id": analysis["method_id"], "revision": analysis["revision"],
                "input_fingerprint": analysis["input_fingerprint"],
                "decision": decision, "reason": reason,
            })
            report_id = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO release_reports VALUES(?,?,?,?,?,?,?,?,?,?)",
                (report_id, lot_id, analysis_id, analysis["method_id"], analysis["revision"],
                 content_fingerprint(material), decision, reason, actor.user_id, utcnow()),
            )
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "report.issued", actor.user_id,
                  {"report_id": report_id, "analysis_id": analysis_id,
                   "method_id": analysis["method_id"], "revision": analysis["revision"], "decision": decision})
        return self.get_report(token, report_id)

    def get_report(self, token: str, report_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM release_reports WHERE report_id=?", (report_id,)).fetchone()
        if not row:
            raise KeyError(report_id)
        return dict(row)

    # ------------------------------------------------------------------ 跨实验室比对与调查

    def record_comparison(
        self, token: str, comparison_id: str, method_id: str, revision: int, reference_sample_id: str,
        observations: list[dict],
    ) -> dict:
        """登记跨实验室同一样件比对；超方法容差即冻结未决放行并立案。"""
        actor = self.auth.require(token, "govern")
        if len(observations) < 2:
            raise ValueError("跨实验室比对至少需要两家实验室")
        with transaction(self.db):
            spec, _ = self._load_method(method_id, revision)
            if not spec.cross_lab:
                raise ValueError(f"方法 {method_id} r{revision} 未声明跨实验室容差")
            lab_metrics: dict[str, dict[str, float]] = {}
            for item in observations:
                lab_id = item["lab_id"]
                if not self.db.execute("SELECT 1 FROM laboratories WHERE lab_id=?", (lab_id,)).fetchone():
                    raise KeyError(lab_id)
                metrics = item["metrics"]
                missing = spec.cross_lab_metrics - set(metrics)
                if missing:
                    raise ValueError(f"实验室 {lab_id} 的比对数据缺少指标 {sorted(missing)}")
                lab_metrics[lab_id] = {key: float(metrics[key]) for key in spec.cross_lab_metrics}
            differences, verdict = spec.cross_lab_differences(lab_metrics)
            self.db.execute(
                "INSERT INTO comparison_sessions VALUES(?,?,?,?,?,?,?,?,?)",
                (comparison_id, method_id, revision, reference_sample_id, verdict,
                 json.dumps(differences, ensure_ascii=False, sort_keys=True), None,
                 actor.user_id, utcnow()),
            )
            investigation_id = None
            if verdict == "systematic_bias":
                investigation_id = uuid.uuid4().hex
                self.db.execute(
                    "INSERT INTO investigations VALUES(?,?,?,?,'open',?,?,?,?,?,NULL,NULL,NULL)",
                    (investigation_id, method_id, revision, comparison_id,
                     json.dumps(differences, ensure_ascii=False, sort_keys=True), None, None,
                     actor.user_id, utcnow()),
                )
                self.db.execute(
                    "UPDATE comparison_sessions SET investigation_id=? WHERE comparison_id=?",
                    (investigation_id, comparison_id),
                )
                pending_lots = self.db.execute(
                    "SELECT * FROM chip_lots WHERE method_id=? AND revision=? "
                    "AND status IN ('engineering','hold') AND suspended_at IS NULL",
                    (method_id, revision),
                ).fetchall()
                for lot in pending_lots:
                    latest = self.db.execute(
                        "SELECT analysis_id,conclusion,method_id,revision FROM analysis_versions "
                        "WHERE lot_id=? ORDER BY sequence_no DESC LIMIT 1", (lot["lot_id"],)
                    ).fetchone()
                    snapshot = {
                        "lot": dict(lot),
                        "latest_analysis": dict(latest) if latest else None,
                        "measurement_count": self.db.execute(
                            "SELECT COUNT(*) AS c FROM measurements WHERE lot_id=?", (lot["lot_id"],)
                        ).fetchone()["c"],
                    }
                    self.db.execute(
                        "INSERT INTO investigation_lots VALUES(?,?,?)",
                        (investigation_id, lot["lot_id"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True)),
                    )
                    self.db.execute(
                        "UPDATE chip_lots SET suspended_at=?, investigation_id=? WHERE lot_id=?",
                        (utcnow(), investigation_id, lot["lot_id"]),
                    )
                    event(self.db, lot["lot_id"], "lot.suspended", actor.user_id,
                          {"investigation_id": investigation_id, "comparison_id": comparison_id,
                           "method_id": method_id, "revision": revision})
                governance_event(self.db, "investigation", investigation_id, "investigation.opened", actor.user_id,
                                 {"method_id": method_id, "revision": revision, "comparison_id": comparison_id,
                                  "affected_lot_count": len(pending_lots)})
            for item in observations:
                self.db.execute(
                    "INSERT INTO comparison_observations VALUES(?,?,?,?,?)",
                    (comparison_id, item["lab_id"],
                     json.dumps(lab_metrics[item["lab_id"]], ensure_ascii=False, sort_keys=True),
                     actor.user_id, utcnow()),
                )
        return self.get_comparison(token, comparison_id)

    def get_comparison(self, token: str, comparison_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute(
            "SELECT * FROM comparison_sessions WHERE comparison_id=?", (comparison_id,)
        ).fetchone()
        if not row:
            raise KeyError(comparison_id)
        result = dict(row)
        result["differences"] = json.loads(row["differences_json"])
        result["observations"] = [
            dict(obs) for obs in self.db.execute(
                "SELECT lab_id,metrics_json,observed_at FROM comparison_observations WHERE comparison_id=?",
                (comparison_id,),
            )
        ]
        return result

    def get_investigation(self, token: str, investigation_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)).fetchone()
        if not row:
            raise KeyError(investigation_id)
        result = dict(row)
        result["differences"] = json.loads(row["differences_json"])
        result["lots"] = [
            {"lot_id": item["lot_id"], "snapshot": json.loads(item["snapshot_json"])}
            for item in self.db.execute(
                "SELECT * FROM investigation_lots WHERE investigation_id=? ORDER BY lot_id", (investigation_id,)
            )
        ]
        return result

    def close_investigation(
        self, token: str, investigation_id: str, resolution_method_id: str, resolution_revision: int, note: str
    ) -> dict:
        """用后继方法版本复评关闭调查并解除暂停；旧报告保持不变。"""
        actor = self.auth.require(token, "govern")
        if not note.strip():
            raise ValueError("resolution note is required")
        with transaction(self.db):
            investigation = self.db.execute(
                "SELECT * FROM investigations WHERE investigation_id=?", (investigation_id,)
            ).fetchone()
            if not investigation:
                raise KeyError(investigation_id)
            if investigation["status"] != "open":
                raise ValueError("调查已经关闭")
            successor, _ = self._load_method(resolution_method_id, resolution_revision)
            if resolution_method_id != investigation["method_id"] or resolution_revision <= investigation["revision"]:
                raise ValueError("关闭调查必须指定同一方法的后继版本")
            affected = self.db.execute(
                "SELECT lot_id FROM investigation_lots WHERE investigation_id=?", (investigation_id,)
            ).fetchall()
            for item in affected:
                lot = self.db.execute("SELECT product FROM chip_lots WHERE lot_id=?", (item["lot_id"],)).fetchone()
                if not successor.applies_to(lot["product"]):
                    raise ValueError(f"后继方法不适用于受影响批次 {item['lot_id']} 的产品")
                self.db.execute(
                    "UPDATE chip_lots SET suspended_at=NULL, investigation_id=NULL WHERE lot_id=?",
                    (item["lot_id"],),
                )
                event(self.db, item["lot_id"], "lot.unsuspended", actor.user_id,
                      {"investigation_id": investigation_id,
                       "resolution_method_id": resolution_method_id,
                       "resolution_revision": resolution_revision})
            self.db.execute(
                "UPDATE investigations SET status='closed', resolution_method_id=?, resolution_revision=?, "
                "closed_by=?, closed_at=?, resolution_note=? WHERE investigation_id=?",
                (resolution_method_id, resolution_revision, actor.user_id, utcnow(), note, investigation_id),
            )
            governance_event(self.db, "investigation", investigation_id, "investigation.closed", actor.user_id,
                             {"resolution_method_id": resolution_method_id,
                              "resolution_revision": resolution_revision, "note": note,
                              "affected_lot_count": len(affected)})
        return self.get_investigation(token, investigation_id)

    # ------------------------------------------------------------------ 委员会视图

    def committee_overview(self, token: str) -> dict:
        self.auth.require(token, "read")
        lots = self.db.execute(
            "SELECT lot_id,product,status,method_id,revision,suspended_at,investigation_id FROM chip_lots ORDER BY lot_id"
        ).fetchall()
        lot_views = []
        for lot in lots:
            latest = self.db.execute(
                "SELECT analysis_id,sequence_no,method_id,revision,conclusion,created_at FROM analysis_versions "
                "WHERE lot_id=? ORDER BY sequence_no DESC LIMIT 1", (lot["lot_id"],)
            ).fetchone()
            report = self.db.execute(
                "SELECT report_id,decision,method_id,revision,issued_at FROM release_reports "
                "WHERE lot_id=? ORDER BY issued_at DESC,rowid DESC LIMIT 1", (lot["lot_id"],)
            ).fetchone()
            lot_views.append({
                "lot_id": lot["lot_id"], "product": lot["product"], "status": lot["status"],
                "method_id": lot["method_id"], "revision": lot["revision"],
                "suspended": lot["suspended_at"] is not None,
                "investigation_id": lot["investigation_id"],
                "latest_analysis": dict(latest) if latest else None,
                "latest_report": dict(report) if report else None,
            })
        calibrations = self.db.execute(
            "SELECT c.calibration_id,c.instrument_id,c.method_id,c.revision,c.certificate_ref,"
            "c.status,c.valid_until,i.lab_id, "
            "(SELECT COUNT(*) FROM reference_checks rc WHERE rc.calibration_id=c.calibration_id) AS check_count, "
            "(SELECT MIN(rc.deviation_abs) FROM reference_checks rc WHERE rc.calibration_id=c.calibration_id AND rc.within_tolerance=1) AS best_deviation "
            "FROM calibrations c JOIN instruments i ON i.instrument_id=c.instrument_id ORDER BY c.calibration_id"
        ).fetchall()
        comparisons = self.db.execute(
            "SELECT comparison_id,method_id,revision,reference_sample_id,result,investigation_id,created_at "
            "FROM comparison_sessions ORDER BY created_at DESC"
        ).fetchall()
        investigations = self.db.execute(
            "SELECT investigation_id,method_id,revision,status,triggered_at,closed_at,"
            "resolution_method_id,resolution_revision FROM investigations ORDER BY triggered_at DESC"
        ).fetchall()
        return {
            "methods": self.list_methods(token),
            "calibrations": [dict(row) for row in calibrations],
            "comparisons": [dict(row) for row in comparisons],
            "investigations": [dict(row) for row in investigations],
            "suspended_lots": [view for view in lot_views if view["suspended"]],
            "lots": lot_views,
        }

    def lot_evidence(self, token: str, lot_id: str) -> dict:
        """一个批次的完整证据链：方法指纹、原始读数、换算、校准与全部结论版本。"""
        self.auth.require(token, "read")
        lot = self.get_lot(token, lot_id)
        readings = self.db.execute("SELECT * FROM measurements WHERE lot_id=? ORDER BY measured_at", (lot_id,)).fetchall()
        analyses = self.db.execute(
            "SELECT analysis_id,sequence_no,method_id,revision,conclusion,input_fingerprint,created_at "
            "FROM analysis_versions WHERE lot_id=? ORDER BY sequence_no", (lot_id,)
        ).fetchall()
        reports = self.db.execute(
            "SELECT report_id,analysis_id,method_id,revision,decision,reason,issued_by,issued_at "
            "FROM release_reports WHERE lot_id=? ORDER BY issued_at", (lot_id,)
        ).fetchall()
        method_fingerprint = None
        if lot["method_id"]:
            method_fingerprint = self.db.execute(
                "SELECT fingerprint,status FROM method_revisions WHERE method_id=? AND revision=?",
                (lot["method_id"], lot["revision"]),
            ).fetchone()
        return {
            "lot": lot,
            "method": None if not method_fingerprint else dict(method_fingerprint),
            "readings": [dict(row) for row in readings],
            "analyses": [dict(row) for row in analyses],
            "reports": [dict(row) for row in reports],
        }

    # ------------------------------------------------------------------ 兼容旧入口

    def analyze(self, token: str, lot_id: str) -> dict:
        """旧入口：绑定方法的批次走版本化分析，历史无方法批次保持原计算。"""
        self.auth.require(token, "analyze")
        lot = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not lot:
            raise KeyError(lot_id)
        if lot["method_id"]:
            return self.run_analysis(token, lot_id)
        rows = self.db.execute(
            "SELECT test_frequency_hz,response FROM measurements WHERE lot_id=? ORDER BY test_frequency_hz", (lot_id,)
        ).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_response_profile([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(lot["component_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "response_profile": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        """旧入口：治理批次出具不可变报告，历史批次沿用审批表。"""
        lot = self.get_lot(token, lot_id)
        if lot["method_id"]:
            report = self.issue_report(token, lot_id, decision, reason)
            return self.get_lot(token, lot_id) | {"report_id": report["report_id"]}
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)",
                            (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
