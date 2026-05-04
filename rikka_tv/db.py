from __future__ import annotations

import json
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

            CREATE TABLE IF NOT EXISTS play_resolution_cache (
              cache_key TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              video_id TEXT NOT NULL,
              poster TEXT,
              raw_poster TEXT,
              source_poster TEXT,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS detail_cache (
              source TEXT NOT NULL,
              video_id TEXT NOT NULL,
              payload TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (source, video_id)
            );

            CREATE TABLE IF NOT EXISTS search_cache (
              cache_key TEXT PRIMARY KEY,
              payload TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS source_resolution_cache (
              cache_key TEXT NOT NULL,
              source TEXT NOT NULL,
              status TEXT NOT NULL,
              updated_at INTEGER NOT NULL,
              PRIMARY KEY (cache_key, source)
            );

            CREATE TABLE IF NOT EXISTS recommend_cache (
              cache_key TEXT PRIMARY KEY,
              payload TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS visitor_stats (
              visitor_id TEXT PRIMARY KEY,
              first_seen_at INTEGER NOT NULL,
              last_seen_at INTEGER NOT NULL,
              first_seen_date TEXT NOT NULL,
              last_seen_date TEXT NOT NULL
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
            if test.get("playable_only"):
                continue
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


def record_visitor(visitor_id: str, today: str) -> dict[str, int]:
    visitor_id = str(visitor_id or "").strip()
    today = str(today or "").strip()
    if not visitor_id or not today:
        return get_visitor_stats(today)
    now = int(time.time())
    with connect() as db:
        db.execute(
            """
            INSERT INTO visitor_stats (visitor_id, first_seen_at, last_seen_at, first_seen_date, last_seen_date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(visitor_id) DO UPDATE SET
              last_seen_at = excluded.last_seen_at,
              last_seen_date = excluded.last_seen_date
            """,
            (visitor_id, now, now, today, today),
        )
        today_count = db.execute(
            "SELECT COUNT(*) FROM visitor_stats WHERE last_seen_date = ?",
            (today,),
        ).fetchone()[0]
        total_count = db.execute("SELECT COUNT(*) FROM visitor_stats").fetchone()[0]
    return {"today_users": int(today_count or 0), "total_users": int(total_count or 0)}


def get_visitor_stats(today: str) -> dict[str, int]:
    today = str(today or "").strip()
    with connect() as db:
        today_count = db.execute(
            "SELECT COUNT(*) FROM visitor_stats WHERE last_seen_date = ?",
            (today,),
        ).fetchone()[0] if today else 0
        total_count = db.execute("SELECT COUNT(*) FROM visitor_stats").fetchone()[0]
    return {"today_users": int(today_count or 0), "total_users": int(total_count or 0)}


def get_play_resolution_cache(cache_key: str, max_age_seconds: int) -> dict[str, Any] | None:
    if not cache_key or max_age_seconds <= 0:
        return None
    cutoff = int(time.time()) - int(max_age_seconds)
    with connect() as db:
        row = db.execute(
            """
            SELECT * FROM play_resolution_cache
            WHERE cache_key = ? AND updated_at >= ?
            """,
            (cache_key, cutoff),
        ).fetchone()
    return dict(row) if row else None


def save_play_resolution_cache(cache_key: str, item: dict[str, Any]) -> None:
    source = str(item.get("source") or "").strip()
    video_id = str(item.get("id") or "").strip()
    if not cache_key or not source or not video_id:
        return
    now = int(time.time())
    with connect() as db:
        db.execute(
            """
            INSERT INTO play_resolution_cache
              (cache_key, source, video_id, poster, raw_poster, source_poster, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              source = excluded.source,
              video_id = excluded.video_id,
              poster = excluded.poster,
              raw_poster = excluded.raw_poster,
              source_poster = excluded.source_poster,
              updated_at = excluded.updated_at
            """,
            (
                cache_key,
                source,
                video_id,
                str(item.get("poster") or ""),
                str(item.get("raw_poster") or ""),
                str(item.get("source_poster") or ""),
                now,
            ),
        )


def get_detail_cache(source: str, video_id: str, max_age_seconds: int) -> dict[str, Any] | None:
    if not source or not video_id or max_age_seconds <= 0:
        return None
    cutoff = int(time.time()) - int(max_age_seconds)
    with connect() as db:
        row = db.execute(
            """
            SELECT payload FROM detail_cache
            WHERE source = ? AND video_id = ? AND updated_at >= ?
            """,
            (source, video_id, cutoff),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(str(row["payload"] or "{}"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def save_detail_cache(source: str, video_id: str, payload: dict[str, Any]) -> None:
    if not source or not video_id or not payload:
        return
    now = int(time.time())
    with connect() as db:
        db.execute(
            """
            INSERT INTO detail_cache (source, video_id, payload, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source, video_id) DO UPDATE SET
              payload = excluded.payload,
              updated_at = excluded.updated_at
            """,
            (source, video_id, json.dumps(payload, ensure_ascii=False), now),
        )


def get_search_cache(cache_key: str, max_age_seconds: int) -> dict[str, Any] | None:
    if not cache_key or max_age_seconds <= 0:
        return None
    cutoff = int(time.time()) - int(max_age_seconds)
    with connect() as db:
        row = db.execute(
            """
            SELECT payload FROM search_cache
            WHERE cache_key = ? AND updated_at >= ?
            """,
            (cache_key, cutoff),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(str(row["payload"] or "{}"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def save_search_cache(cache_key: str, payload: dict[str, Any]) -> None:
    if not cache_key or not payload:
        return
    now = int(time.time())
    with connect() as db:
        db.execute(
            """
            INSERT INTO search_cache (cache_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              payload = excluded.payload,
              updated_at = excluded.updated_at
            """,
            (cache_key, json.dumps(payload, ensure_ascii=False), now),
        )


def get_source_resolution_cache(cache_key: str, max_age_seconds: int) -> dict[str, str]:
    if not cache_key or max_age_seconds <= 0:
        return {}
    cutoff = int(time.time()) - int(max_age_seconds)
    with connect() as db:
        rows = db.execute(
            """
            SELECT source, status FROM source_resolution_cache
            WHERE cache_key = ? AND updated_at >= ?
            """,
            (cache_key, cutoff),
        ).fetchall()
    return {
        str(row["source"]): str(row["status"] or "")
        for row in rows
        if row["source"]
    }


def save_source_resolution_cache(cache_key: str, source: str, status: str) -> None:
    if not cache_key or not source or not status:
        return
    now = int(time.time())
    with connect() as db:
        db.execute(
            """
            INSERT INTO source_resolution_cache (cache_key, source, status, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key, source) DO UPDATE SET
              status = excluded.status,
              updated_at = excluded.updated_at
            """,
            (cache_key, source, status, now),
        )


def delete_source_resolution_cache(cache_key: str, source: str) -> None:
    if not cache_key or not source:
        return
    with connect() as db:
        db.execute(
            "DELETE FROM source_resolution_cache WHERE cache_key = ? AND source = ?",
            (cache_key, source),
        )


def get_recommend_cache(cache_key: str, max_age_seconds: int) -> list[dict[str, Any]] | None:
    if not cache_key or max_age_seconds <= 0:
        return None
    cutoff = int(time.time()) - int(max_age_seconds)
    with connect() as db:
        row = db.execute(
            """
            SELECT payload FROM recommend_cache
            WHERE cache_key = ? AND updated_at >= ?
            """,
            (cache_key, cutoff),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(str(row["payload"] or "[]"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, list) else None


def save_recommend_cache(cache_key: str, items: list[dict[str, Any]]) -> None:
    if not cache_key:
        return
    now = int(time.time())
    with connect() as db:
        db.execute(
            """
            INSERT INTO recommend_cache (cache_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
              payload = excluded.payload,
              updated_at = excluded.updated_at
            """,
            (cache_key, json.dumps(items, ensure_ascii=False), now),
        )


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
    try:
        cap = float((load_config().get("speed_test") or {}).get("browser_speed_cap_kbps") or 12288)
    except Exception:
        cap = 12288
    cap = max(cap, 1024)
    speed_score = min(_capped_metric_speed(avg_speed_kbps) / cap * 100, 100)
    return round(max(0, min(100, speed_score * 0.6 + avg_score * 0.25 + success_rate * 15)), 1)


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
