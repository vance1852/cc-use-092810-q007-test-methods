"""电芯组件批次、方法治理和检测记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS method_versions(
 method_id TEXT NOT NULL, version INTEGER NOT NULL,
 definition_json TEXT NOT NULL, content_sha256 TEXT NOT NULL,
 status TEXT NOT NULL, published_by TEXT NOT NULL, published_at TEXT NOT NULL,
 PRIMARY KEY(method_id,version), UNIQUE(content_sha256));
CREATE TABLE IF NOT EXISTS laboratories(
 lab_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS instruments(
 instrument_id TEXT PRIMARY KEY, lab_id TEXT NOT NULL REFERENCES laboratories(lab_id),
 model TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS calibrations(
 calibration_id TEXT PRIMARY KEY,
 instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
 calibration_batch TEXT NOT NULL, certificate_ref TEXT NOT NULL,
 calibrated_at TEXT NOT NULL, valid_until TEXT NOT NULL, recorded_by TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reference_comparisons(
 comparison_id TEXT PRIMARY KEY,
 lab_id TEXT NOT NULL REFERENCES laboratories(lab_id),
 instrument_id TEXT NOT NULL REFERENCES instruments(instrument_id),
 method_id TEXT NOT NULL, method_version INTEGER NOT NULL,
 reference_sample_id TEXT NOT NULL,
 expected_value REAL NOT NULL, observed_value REAL NOT NULL, tolerance REAL NOT NULL,
 passed INTEGER NOT NULL, compared_at TEXT NOT NULL,
 FOREIGN KEY(method_id,method_version) REFERENCES method_versions(method_id,version));
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 component_count INTEGER NOT NULL, lot_kind TEXT NOT NULL DEFAULT 'formal',
 status TEXT NOT NULL, previous_status TEXT, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 lab_id TEXT NOT NULL REFERENCES laboratories(lab_id),
 instrument TEXT NOT NULL REFERENCES instruments(instrument_id),
 method_id TEXT NOT NULL, method_version INTEGER NOT NULL,
 test_frequency_hz REAL NOT NULL,
 raw_value REAL NOT NULL, raw_unit TEXT NOT NULL,
 response REAL NOT NULL, noise REAL NOT NULL,
 excluded INTEGER NOT NULL DEFAULT 0, exclusion_reason TEXT,
 exclusion_method_id TEXT, exclusion_method_version INTEGER,
 operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id),
 FOREIGN KEY(method_id,method_version) REFERENCES method_versions(method_id,version));
CREATE TABLE IF NOT EXISTS analyses(
 analysis_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 method_id TEXT NOT NULL, method_version INTEGER NOT NULL,
 input_sha256 TEXT NOT NULL, result_json TEXT NOT NULL,
 created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(lot_id,method_id,method_version,input_sha256),
 FOREIGN KEY(method_id,method_version) REFERENCES method_versions(method_id,version));
CREATE TABLE IF NOT EXISTS reports(
 report_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 analysis_id TEXT NOT NULL REFERENCES analyses(analysis_id),
 method_id TEXT NOT NULL, method_version INTEGER NOT NULL,
 conclusion TEXT NOT NULL, snapshot_json TEXT NOT NULL,
 status TEXT NOT NULL, cause TEXT NOT NULL, supersedes_report_id TEXT,
 issued_by TEXT NOT NULL, issued_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_issued_report_per_lot
ON reports(lot_id) WHERE status='issued';
CREATE TABLE IF NOT EXISTS investigations(
 investigation_id TEXT PRIMARY KEY,
 method_id TEXT NOT NULL, method_version INTEGER NOT NULL,
 trigger_lot_id TEXT NOT NULL, detail_json TEXT NOT NULL,
 status TEXT NOT NULL, opened_by TEXT NOT NULL, opened_at TEXT NOT NULL,
 closed_by TEXT, closed_at TEXT, findings TEXT);
CREATE TABLE IF NOT EXISTS investigation_lots(
 investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
 lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 previous_status TEXT, suspended INTEGER NOT NULL,
 PRIMARY KEY(investigation_id,lot_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS governance_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT,
 entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    # HTTP 服务在处理线程中使用连接；SQLite 自身串行化写事务
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
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
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))


def governance_event(db: sqlite3.Connection, entity_type: str, entity_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute(
        "INSERT INTO governance_events(entity_type,entity_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?,?)",
        (entity_type, entity_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()),
    )
