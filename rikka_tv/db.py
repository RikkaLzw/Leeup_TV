from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

import sqlite3

from .config import database_path, load_config


DB_PATH = None


def init_db(config: dict[str, Any]) -> None:
    global DB_PATH
    DB_PATH = database_path(config)
    with connect() as db:
        db.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS source_metrics (
              source TEXT PRIMARY KEY,
              source_name TEXT NOT NULL,
              tests_total INTEGER DEFAULT 0,
              tests_ok INTEGER DEFAULT 0,
              avg_score REAL DEFAULT 0,
              avg_speed_kbps REAL DEFAULT 0,
              avg_latency_ms REAL DEFAULT 0,
              last_ok INTEGER DEFAULT 0,
              last_error TEXT,
              last_test_at INTEGER,
              updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    if DB_PATH is None:
        raise RuntimeError("database is not initialized")
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    finally:
        db.close()


def save_source_test_metrics(candidates: list[dict[str, Any]]) -> None:
    now = int(time.time())
    with connect() as db:
        for candidate in candidates:
            source = str(candidate.get("source") or "").strip()
            if not source:
                continue
            test = candidate.get("test") or {}
            ok = 1 if test.get("ok") else 0
            score = float(test.get("score") or 0)
            speed_kbps = _capped_metric_speed(float(test.get("speed_kbps") or 0)) if ok else 0
            latency_ms = float(test.get("latency_ms") or 0) if ok else 0
            source_name = candidate.get("source_name") or source
            last_error = "" if ok else str(test.get("error") or "probe_failed")

            row = db.execute("SELECT * FROM source_metrics WHERE source = ?", (source,)).fetchone()
            if row:
                tests_total = int(row["tests_total"] or 0) + 1
                previous_ok = int(row["tests_ok"] or 0)
                tests_ok = previous_ok + ok
                avg_score = _rolling_average(float(row["avg_score"] or 0), tests_total - 1, score)
                avg_speed = float(row["avg_speed_kbps"] or 0)
                avg_latency = float(row["avg_latency_ms"] or 0)
                if ok:
                    avg_speed = _rolling_average(avg_speed, previous_ok, speed_kbps)
                    avg_latency = _rolling_average(avg_latency, previous_ok, latency_ms)
                db.execute(
                    """
                    UPDATE source_metrics
                    SET source_name = ?, tests_total = ?, tests_ok = ?, avg_score = ?,
                        avg_speed_kbps = ?, avg_latency_ms = ?, last_ok = ?,
                        last_error = ?, last_test_at = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE source = ?
                    """,
                    (
                        source_name,
                        tests_total,
                        tests_ok,
                        avg_score,
                        avg_speed,
                        avg_latency,
                        ok,
                        last_error,
                        now,
                        source,
                    ),
                )
            else:
                db.execute(
                    """
                    INSERT INTO source_metrics
                      (source, source_name, tests_total, tests_ok, avg_score,
                       avg_speed_kbps, avg_latency_ms, last_ok, last_error, last_test_at)
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source,
                        source_name,
                        ok,
                        score,
                        speed_kbps,
                        latency_ms,
                        ok,
                        last_error,
                        now,
                    ),
                )


def get_source_metrics_map(source_keys: list[str] | None = None) -> dict[str, dict[str, Any]]:
    with connect() as db:
        if source_keys:
            keys = sorted({str(key) for key in source_keys if key})
            if not keys:
                return {}
            placeholders = ",".join("?" for _ in keys)
            rows = db.execute(f"SELECT * FROM source_metrics WHERE source IN ({placeholders})", keys).fetchall()
        else:
            rows = db.execute("SELECT * FROM source_metrics").fetchall()
    return {row["source"]: _row_to_source_metric(row) for row in rows}


def get_source_metrics(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM source_metrics ORDER BY avg_score DESC, avg_speed_kbps DESC, tests_ok DESC LIMIT ?",
            (int(limit or 50),),
        ).fetchall()
    return sorted((_row_to_source_metric(row) for row in rows), key=lambda item: item["source_score"], reverse=True)


def _row_to_source_metric(row: sqlite3.Row) -> dict[str, Any]:
    tests_total = int(row["tests_total"] or 0)
    tests_ok = int(row["tests_ok"] or 0)
    success_rate = tests_ok / tests_total if tests_total else 0
    avg_score = float(row["avg_score"] or 0)
    avg_speed_kbps = _capped_metric_speed(float(row["avg_speed_kbps"] or 0))
    return {
        "source": row["source"],
        "source_name": row["source_name"],
        "tests_total": tests_total,
        "tests_ok": tests_ok,
        "success_rate": round(success_rate, 3),
        "avg_score": round(avg_score, 1),
        "avg_speed_kbps": round(avg_speed_kbps, 1),
        "avg_latency_ms": round(float(row["avg_latency_ms"] or 0), 1),
        "last_ok": bool(row["last_ok"]),
        "last_error": row["last_error"] or "",
        "last_test_at": row["last_test_at"] or 0,
        "source_score": _source_score(avg_score, success_rate, avg_speed_kbps),
    }


def _source_score(avg_score: float, success_rate: float, avg_speed_kbps: float) -> float:
    speed_bonus = min(20, _capped_metric_speed(avg_speed_kbps) / 1024 * 2)
    return round(max(0, min(100, avg_score * 0.7 + success_rate * 10 + speed_bonus)), 1)


def _capped_metric_speed(speed_kbps: float) -> float:
    try:
        cap = float((load_config().get("speed_test") or {}).get("browser_speed_cap_kbps") or 12288)
    except Exception:
        cap = 12288
    cap = max(cap, 1024)
    return max(0, min(float(speed_kbps or 0), cap))


def _rolling_average(previous_average: float, previous_count: int, value: float) -> float:
    if previous_count <= 0:
        return value
    return ((previous_average * previous_count) + value) / (previous_count + 1)

