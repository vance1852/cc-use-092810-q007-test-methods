"""电芯组件检测链的 SQLite 结构及事务辅助函数。

表分组：
- 方法治理：methods/method_revisions 保存冻结的方法版本与指纹；
- 实验室能力：laboratories/instruments/calibrations/reference_checks；
- 批次与读数：chip_lots/measurements/measurement_exclusions，读数保留原值原单位；
- 结论链：analysis_versions/release_reports 只追加、不回写；
- 跨实验室比对：comparison_sessions/comparison_observations/investigations。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 component_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 method_id TEXT, revision INTEGER,
 suspended_at TEXT, investigation_id TEXT,
 FOREIGN KEY(method_id,revision) REFERENCES method_revisions(method_id,revision));
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 test_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 lab_id TEXT, calibration_id TEXT, raw_json TEXT,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS measurement_exclusions(
 exclusion_id TEXT PRIMARY KEY, measurement_id TEXT NOT NULL REFERENCES measurements(measurement_id),
 lot_id TEXT NOT NULL, method_id TEXT NOT NULL, revision INTEGER NOT NULL,
 reason TEXT NOT NULL, excluded_by TEXT NOT NULL, excluded_at TEXT NOT NULL,
 UNIQUE(measurement_id,method_id,revision));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));

CREATE TABLE IF NOT EXISTS laboratories(
 lab_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS instruments(
 instrument_id TEXT PRIMARY KEY, lab_id TEXT NOT NULL REFERENCES laboratories(lab_id),
 serial TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lab_id, serial));
CREATE TABLE IF NOT EXISTS methods(
 method_id TEXT PRIMARY KEY, title TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS method_revisions(
 method_id TEXT NOT NULL REFERENCES methods(method_id),
 revision INTEGER NOT NULL CHECK(revision > 0),
 canonical_json TEXT NOT NULL, fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
 supersedes_revision INTEGER,
 status TEXT NOT NULL CHECK(status IN ('published','superseded','withdrawn')),
 published_by TEXT NOT NULL, published_at TEXT NOT NULL,
 withdrawn_by TEXT, withdrawn_at TEXT,
 PRIMARY KEY(method_id, revision), UNIQUE(fingerprint));
CREATE TABLE IF NOT EXISTS calibrations(
 calibration_id TEXT PRIMARY KEY,
 instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
 method_id TEXT NOT NULL, revision INTEGER NOT NULL,
 certificate_ref TEXT NOT NULL, calibrated_at TEXT NOT NULL, valid_until TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','verified','rejected','expired')),
 recorded_by TEXT NOT NULL, created_at TEXT NOT NULL,
 verified_by TEXT, verified_at TEXT,
 FOREIGN KEY(method_id,revision) REFERENCES method_revisions(method_id,revision));
CREATE TABLE IF NOT EXISTS reference_checks(
 check_id TEXT PRIMARY KEY, calibration_id TEXT NOT NULL REFERENCES calibrations(calibration_id),
 reference_sample_id TEXT NOT NULL,
 nominal_value REAL NOT NULL, nominal_unit TEXT NOT NULL,
 observed_value REAL NOT NULL, observed_unit TEXT NOT NULL,
 canonical_nominal REAL NOT NULL, canonical_observed REAL NOT NULL,
 deviation_abs REAL NOT NULL, deviation_rel REAL,
 tolerance_abs REAL NOT NULL, within_tolerance INTEGER NOT NULL CHECK(within_tolerance IN (0,1)),
 checked_by TEXT NOT NULL, checked_at TEXT NOT NULL, note TEXT);

CREATE TABLE IF NOT EXISTS analysis_versions(
 analysis_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 method_id TEXT NOT NULL, revision INTEGER NOT NULL, sequence_no INTEGER NOT NULL,
 input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64),
 excluded_json TEXT NOT NULL, result_json TEXT NOT NULL,
 conclusion TEXT NOT NULL CHECK(conclusion IN ('pass','fail')),
 created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id, sequence_no),
 FOREIGN KEY(method_id,revision) REFERENCES method_revisions(method_id,revision));
CREATE TABLE IF NOT EXISTS release_reports(
 report_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, analysis_id TEXT NOT NULL REFERENCES analysis_versions(analysis_id),
 method_id TEXT NOT NULL, revision INTEGER NOT NULL,
 fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
 decision TEXT NOT NULL CHECK(decision IN ('release','hold','reject')),
 reason TEXT NOT NULL, issued_by TEXT NOT NULL, issued_at TEXT NOT NULL,
 UNIQUE(lot_id, analysis_id));

CREATE TABLE IF NOT EXISTS comparison_sessions(
 comparison_id TEXT PRIMARY KEY, method_id TEXT NOT NULL, revision INTEGER NOT NULL,
 reference_sample_id TEXT NOT NULL,
 result TEXT NOT NULL CHECK(result IN ('within_tolerance','systematic_bias')),
 differences_json TEXT NOT NULL, investigation_id TEXT,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(method_id,revision) REFERENCES method_revisions(method_id,revision));
CREATE TABLE IF NOT EXISTS comparison_observations(
 comparison_id TEXT NOT NULL REFERENCES comparison_sessions(comparison_id),
 lab_id TEXT NOT NULL REFERENCES laboratories(lab_id),
 metrics_json TEXT NOT NULL, recorded_by TEXT NOT NULL, observed_at TEXT NOT NULL,
 PRIMARY KEY(comparison_id, lab_id));
CREATE TABLE IF NOT EXISTS investigations(
 investigation_id TEXT PRIMARY KEY, method_id TEXT NOT NULL, revision INTEGER NOT NULL,
 comparison_id TEXT REFERENCES comparison_sessions(comparison_id),
 status TEXT NOT NULL CHECK(status IN ('open','closed')),
 differences_json TEXT NOT NULL,
 resolution_method_id TEXT, resolution_revision INTEGER,
 triggered_by TEXT NOT NULL, triggered_at TEXT NOT NULL,
 closed_by TEXT, closed_at TEXT, resolution_note TEXT);
CREATE TABLE IF NOT EXISTS investigation_lots(
 investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 snapshot_json TEXT NOT NULL,
 PRIMARY KEY(investigation_id, lot_id));
CREATE TABLE IF NOT EXISTS governance_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,
 scope TEXT NOT NULL, ref_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _migrate_legacy_columns(db: sqlite3.Connection) -> None:
    """为 007 之前建库的文件补齐治理列。"""
    lot_columns = {row[1] for row in db.execute("PRAGMA table_info(chip_lots)")}
    for name, decl in (
        ("method_id", "ALTER TABLE chip_lots ADD COLUMN method_id TEXT"),
        ("revision", "ALTER TABLE chip_lots ADD COLUMN revision INTEGER"),
        ("suspended_at", "ALTER TABLE chip_lots ADD COLUMN suspended_at TEXT"),
        ("investigation_id", "ALTER TABLE chip_lots ADD COLUMN investigation_id TEXT"),
    ):
        if name not in lot_columns:
            db.execute(decl)
    measurement_columns = {row[1] for row in db.execute("PRAGMA table_info(measurements)")}
    for name, decl in (
        ("lab_id", "ALTER TABLE measurements ADD COLUMN lab_id TEXT"),
        ("calibration_id", "ALTER TABLE measurements ADD COLUMN calibration_id TEXT"),
        ("raw_json", "ALTER TABLE measurements ADD COLUMN raw_json TEXT"),
    ):
        if name not in measurement_columns:
            db.execute(decl)


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # HTTP 服务为 ThreadingHTTPServer：写路径由 BEGIN IMMEDIATE 串行化
    db = sqlite3.connect(path, check_same_thread=False, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    _migrate_legacy_columns(db)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
        (lot_id, event_type, actor, json.dumps(payload, sort_keys=True, ensure_ascii=False), utcnow()),
    )
