"""SQLite storage for reports: one table, one shared connection, plain dicts in and out.

Everything else in the app talks in "row dicts" (contract section 5): one key per column,
booleans as bool, the JSON columns decoded, and hazards / people_at_risk / needs always
carrying every key. This module is the only place that knows how those map to SQLite.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import config
from .util import ms_to_iso, utc_now_iso

log = logging.getLogger(__name__)

# (column, SQL type) in table order. "id" is handled separately (INTEGER PRIMARY KEY AUTOINCREMENT).
COLUMNS: list[tuple[str, str]] = [
    ("created_at", "TEXT NOT NULL"),
    ("updated_at", "TEXT NOT NULL"),
    ("lat", "REAL"),
    ("lng", "REAL"),
    ("accuracy_m", "REAL"),
    ("location_source", "TEXT"),
    ("address_text", "TEXT"),
    ("location_hint", "TEXT"),
    ("ui_language", "TEXT NOT NULL DEFAULT 'en'"),
    ("input_type", "TEXT NOT NULL DEFAULT 'voice'"),
    ("language", "TEXT"),
    ("transcript_original", "TEXT"),
    ("transcript_english", "TEXT"),
    ("ai_summary", "TEXT"),
    ("confirmation_message", "TEXT"),
    ("water_depth_cm", "INTEGER"),
    ("location_type", "TEXT"),
    ("water_in_living_space", "INTEGER NOT NULL DEFAULT 0"),
    ("water_rising", "INTEGER NOT NULL DEFAULT 0"),
    ("hazards", "TEXT NOT NULL DEFAULT '{}'"),
    ("people_at_risk", "TEXT NOT NULL DEFAULT '{}'"),
    ("needs", "TEXT NOT NULL DEFAULT '{}'"),
    ("urgency_score", "INTEGER"),
    ("urgency_level", "TEXT"),
    ("urgency_reasons", "TEXT NOT NULL DEFAULT '[]'"),
    ("status", "TEXT NOT NULL DEFAULT 'new'"),
    ("audio_path", "TEXT"),
    ("audio_mime", "TEXT"),
    ("photo_path", "TEXT"),
    ("photo_mime", "TEXT"),
    ("ai_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("ai_engine", "TEXT"),
    ("ai_error", "TEXT"),
    ("ai_latency_ms", "INTEGER"),
    ("is_simulated", "INTEGER NOT NULL DEFAULT 0"),
]
COLUMN_NAMES = [name for name, _ in COLUMNS]
ALL_COLUMNS = ["id", *COLUMN_NAMES]

BOOL_COLUMNS = {"water_in_living_space", "water_rising", "is_simulated"}
INT_COLUMNS = {"water_depth_cm", "urgency_score", "ai_latency_ms"}
FLOAT_COLUMNS = {"lat", "lng", "accuracy_m"}

# The JSON dict columns and every key they must carry. Missing keys read back as False
# (count as None), so code downstream can write p["trapped"] without .get().
DICT_DEFAULTS: dict[str, dict[str, Any]] = {
    "hazards": {"electrical": False, "sewage": False, "gas": False, "structural": False},
    "people_at_risk": {
        "elderly": False,
        "children": False,
        "disabled": False,
        "medical": False,
        "trapped": False,
        "count": None,
    },
    "needs": {"evacuation": False, "pumping": False, "medical": False, "supplies": False},
}

# Defaults for a brand-new row; anything not listed here defaults to None.
SCALAR_DEFAULTS: dict[str, Any] = {
    "ui_language": "en",
    "input_type": "voice",
    "status": "new",
    "ai_status": "pending",
    "water_in_living_space": False,
    "water_rising": False,
    "is_simulated": False,
}

_lock = threading.RLock()  # sqlite3 objects are not safe to share across threads without one
_conn: sqlite3.Connection | None = None
_conn_path: Path | None = None
_last_write_ms = 0  # newest updated_at handed out, in epoch milliseconds (see _write_stamp)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _write_stamp() -> str:
    """updated_at for an update: milliseconds, and strictly later than any earlier update.

    Dashboards keep the newest copy of a report by comparing updated_at, and a live event and an
    HTTP response can arrive in either order. Second precision cannot order a "pending" write and
    the "AI failed" write that lands a few milliseconds later, so every write gets its own instant.
    Call with _lock held.
    """
    global _last_write_ms
    _last_write_ms = max(_now_ms(), _last_write_ms + 1)
    return ms_to_iso(_last_write_ms)


# ---------------------------------------------------------------- normalization


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decode_json(value: Any) -> Any:
    if isinstance(value, (bytes, str)):
        try:
            return json.loads(value) if value else None
        except ValueError:
            return None
    if hasattr(value, "model_dump"):  # a pydantic model (Hazards, PeopleAtRisk, ...)
        return value.model_dump()
    return value


def _complete_dict(name: str, value: Any) -> dict[str, Any]:
    """Merge whatever we got onto the full default dict so every key is present and typed."""
    decoded = _decode_json(value)
    out = dict(DICT_DEFAULTS[name])
    if isinstance(decoded, Mapping):
        for key in out:
            if key not in decoded:
                continue
            if key == "count":
                count = _as_int(decoded[key])
                out[key] = count if count is None or count >= 0 else None
            else:
                out[key] = _as_bool(decoded[key])
    return out


def _as_reasons(value: Any) -> list[str]:
    decoded = _decode_json(value)
    if isinstance(decoded, (list, tuple)):
        return [str(item) for item in decoded if item is not None and str(item).strip()]
    return []


def normalize_value(name: str, value: Any) -> Any:
    """One column's value in row-dict form (the form the rest of the app uses)."""
    if name in DICT_DEFAULTS:
        return _complete_dict(name, value)
    if name == "urgency_reasons":
        return _as_reasons(value)
    if name in BOOL_COLUMNS:
        return _as_bool(value)
    if name in INT_COLUMNS or name == "id":
        return _as_int(value)
    if name in FLOAT_COLUMNS:
        return _as_float(value)
    if value is None:
        return None
    return str(value)


def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """A complete row dict: every column present, defaults filled, types fixed. Unknown keys are dropped."""
    out: dict[str, Any] = {}
    for name in ALL_COLUMNS:
        value = row.get(name)
        if value is None and name in SCALAR_DEFAULTS:
            value = SCALAR_DEFAULTS[name]
        out[name] = normalize_value(name, value)
    return out


def _encode(name: str, value: Any) -> Any:
    """Row-dict value -> what SQLite stores."""
    value = normalize_value(name, value)
    if name in DICT_DEFAULTS or name == "urgency_reasons":
        return json.dumps(value, ensure_ascii=False)
    if name in BOOL_COLUMNS:
        return 1 if value else 0
    return value


def _from_sqlite(record: sqlite3.Row) -> dict[str, Any]:
    return normalize_row({key: record[key] for key in record.keys()})


# ---------------------------------------------------------------- connection


def _connection() -> sqlite3.Connection:
    """The shared connection, (re)opened lazily from config.DB_PATH. Call with _lock held."""
    global _conn, _conn_path
    path = Path(config.DB_PATH)
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:  # config.DB_PATH changed under us (tests do this): switch files
        _close_locked()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
    conn.execute("PRAGMA synchronous=NORMAL")
    _conn, _conn_path = conn, path
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    columns_sql = ",\n  ".join(f"{name} {sql_type}" for name, sql_type in COLUMNS)
    conn.execute(f"CREATE TABLE IF NOT EXISTS reports (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  {columns_sql}\n)")
    # A data dir from an older build may lack newer columns: add them rather than crash.
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(reports)")}
    for name, sql_type in COLUMNS:
        if name not in existing:
            plain_type = sql_type.replace("NOT NULL", "")  # ADD COLUMN NOT NULL needs a default; keep it loose
            conn.execute(f"ALTER TABLE reports ADD COLUMN {name} {plain_type}")
            log.warning("added missing column %s to reports", name)
    conn.commit()


def _close_locked() -> None:
    global _conn, _conn_path
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:
            log.exception("closing the database failed")
    _conn, _conn_path = None, None


# ---------------------------------------------------------------- public API


def init_db() -> None:
    """Create the data and upload directories and the reports table if missing."""
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    Path(config.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    with _lock:
        _connection()  # opening the connection creates the table


def close() -> None:
    """Close the connection; the next call reopens it from the current config.DB_PATH."""
    with _lock:
        _close_locked()


def insert_report(row: dict) -> dict:
    """Insert a row dict (no id needed; unknown keys ignored) and return the stored row dict."""
    values = normalize_row(row)
    now = utc_now_iso()
    values["created_at"] = values["created_at"] or now
    values["updated_at"] = values["updated_at"] or values["created_at"]
    names = COLUMN_NAMES  # never insert an explicit id: AUTOINCREMENT owns it
    placeholders = ", ".join("?" for _ in names)
    with _lock:
        conn = _connection()
        cursor = conn.execute(
            f"INSERT INTO reports ({', '.join(names)}) VALUES ({placeholders})",
            [_encode(name, values[name]) for name in names],
        )
        conn.commit()
        report_id = cursor.lastrowid
        record = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return _from_sqlite(record)


def update_report(report_id: int, fields: dict) -> dict | None:
    """Set the given columns (plus updated_at). Returns the updated row dict, or None if there is no such report."""
    changes = {name: _encode(name, value) for name, value in fields.items() if name in COLUMN_NAMES}
    changes.pop("created_at", None)
    with _lock:
        changes["updated_at"] = _write_stamp()
        assignments = ", ".join(f"{name} = ?" for name in changes)
        conn = _connection()
        cursor = conn.execute(f"UPDATE reports SET {assignments} WHERE id = ?", [*changes.values(), report_id])
        conn.commit()
        if cursor.rowcount == 0:
            return None
        record = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return _from_sqlite(record) if record else None


def get_report(report_id: int) -> dict | None:
    with _lock:
        record = _connection().execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return _from_sqlite(record) if record else None


def list_reports() -> list[dict]:
    """All reports, oldest first. The API sorts them into queue order (service.queue_order)."""
    with _lock:
        records = _connection().execute("SELECT * FROM reports ORDER BY id").fetchall()
    return [_from_sqlite(r) for r in records]


def count_reports() -> int:
    with _lock:
        (count,) = _connection().execute("SELECT COUNT(*) FROM reports").fetchone()
    return int(count)


def clear_reports() -> None:
    """Delete every report and restart ids at 1 (a fresh demo reads better as #1..#25)."""
    with _lock:
        conn = _connection()
        conn.execute("DELETE FROM reports")
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'reports'")
        except sqlite3.OperationalError:
            pass  # sqlite_sequence does not exist until the first AUTOINCREMENT insert
        conn.commit()
