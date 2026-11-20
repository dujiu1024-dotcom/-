from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.collectors.registry import list_platforms
from app.services.crawler_engine import SUPPORTED, list_jobs, start_job
from app.services.source_tracking import _schema
from app.storage.db import connect


logger = logging.getLogger(__name__)
_tick_lock = threading.Lock()
_LEASE_NAME = "source-scheduler-tick"
_DEFAULT_LEASE_TTL_SECONDS = 120


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _lease_ttl_seconds() -> int:
    try:
        configured = int(os.getenv("SCHEDULER_LEASE_TTL_SECONDS", str(_DEFAULT_LEASE_TTL_SECONDS)))
    except ValueError:
        configured = _DEFAULT_LEASE_TTL_SECONDS
    return max(30, min(configured, 900))


def _ensure_lease_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_leases (
            lease_name TEXT PRIMARY KEY,
            owner_token TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)


def _acquire_scheduler_lease(
    owner_token: str | None = None,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> str | None:
    """Atomically acquire the SQLite-backed lease shared by scheduler processes."""
    owner = owner_token or uuid.uuid4().hex
    current = now or _now()
    timestamp = _iso(current)
    expires = _iso(current + timedelta(seconds=max(30, ttl_seconds or _lease_ttl_seconds())))
    with connect() as conn:
        _ensure_lease_schema(conn)
        # BEGIN IMMEDIATE serializes competing writers before checking lease expiry.
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("""
            INSERT INTO scheduler_leases(lease_name,owner_token,expires_at,updated_at)
            VALUES(?,?,?,?)
            ON CONFLICT(lease_name) DO UPDATE SET
                owner_token=excluded.owner_token,
                expires_at=excluded.expires_at,
                updated_at=excluded.updated_at
            WHERE scheduler_leases.expires_at <= excluded.updated_at
        """, (_LEASE_NAME, owner, expires, timestamp))
        if cursor.rowcount != 1:
            return None
    return owner


def _release_scheduler_lease(owner_token: str) -> bool:
    """Release only the lease still owned by this tick; stale owners are harmless."""
    with connect() as conn:
        _ensure_lease_schema(conn)
        cursor = conn.execute(
            "DELETE FROM scheduler_leases WHERE lease_name=? AND owner_token=?",
            (_LEASE_NAME, owner_token),
        )
        return cursor.rowcount == 1


def _tick_schedules(limit: int = 1) -> dict[str, Any]:
    """Run due, explicitly verified creator schedules; never infer platform support."""
    now = _now()
    jobs = {job["id"]: job for job in list_jobs()}
    started: list[str] = []
    reconciled = 0
    with connect() as conn:
        _schema(conn)
        running = conn.execute("SELECT * FROM source_runs WHERE status='running'").fetchall()
        for run in running:
            job = jobs.get(run["job_id"])
            if job and job["status"] == "running":
                continue
            status = job["status"] if job else "interrupted"
            error = None if job and status == "succeeded" else (
                f"采集进程状态：{status}" if job else "服务重启或进程状态丢失，批次中断；可重新触发"
            )
            conn.execute("UPDATE source_runs SET status=?,finished_at=?,error=? WHERE id=?",
                         (status, _iso(now), error, run["id"]))
            if status == "succeeded":
                conn.execute("UPDATE tracked_sources SET last_success_at=?,updated_at=? WHERE id=?",
                             (_iso(now), _iso(now), run["source_id"]))
            source = conn.execute("SELECT interval_minutes FROM tracked_sources WHERE id=?",
                                  (run["source_id"],)).fetchone()
            delay = int(source["interval_minutes"]) if source else 1440
            if status != "succeeded":
                delay = min(delay, 15)
            conn.execute("UPDATE source_schedules SET next_run_at=?,last_run_at=?,updated_at=? WHERE source_id=?",
                         (_iso(now + timedelta(minutes=delay)), _iso(now), _iso(now), run["source_id"]))
            reconciled += 1

        if any(job.get("status") == "running" for job in jobs.values()):
            return {"started": started, "reconciled": reconciled, "deferred": "crawler_busy"}
        due = conn.execute("""
            SELECT s.*,sc.next_run_at FROM tracked_sources s
            JOIN source_schedules sc ON sc.source_id=s.id
            WHERE s.enabled=1 AND sc.enabled=1 AND s.tracking_verified=1
              AND trim(s.tracking_target)<>'' AND sc.next_run_at<=?
            ORDER BY s.priority DESC,sc.next_run_at ASC LIMIT ?
        """, (_iso(now), max(1, min(limit, 10)))).fetchall()
        if not due:
            return {"started": started, "reconciled": reconciled, "deferred": None}
        platform_caps = {item["code"]: item["capabilities"] for item in list_platforms()}
        for source in due:
            if source["tracking_scope"] != "creator" or source["platform"] not in SUPPORTED or "creator" not in platform_caps.get(source["platform"], []):
                reason = "平台未验证创作者追踪能力，自动调度已跳过"
                conn.execute("""INSERT INTO source_runs
                    (id,source_id,status,scheduled_at,finished_at,error)
                    VALUES(?,?, 'skipped',?,?,?)""",
                    (uuid.uuid4().hex[:16], source["id"], _iso(now), _iso(now), reason))
                conn.execute("UPDATE source_schedules SET next_run_at=?,last_run_at=?,updated_at=? WHERE source_id=?",
                             (_iso(now + timedelta(minutes=source["interval_minutes"])),
                              _iso(now), _iso(now), source["id"]))
                continue
            previous = source["last_success_at"]
            window_hours = int(source["window_hours"])
            if previous:
                try:
                    boundary = datetime.fromisoformat(previous).astimezone(UTC) - timedelta(hours=6)
                except ValueError:
                    boundary = now - timedelta(hours=window_hours)
                window_start = max(boundary, now - timedelta(hours=window_hours))
            else:
                window_start = now - timedelta(hours=window_hours)
            run_id = uuid.uuid4().hex[:16]
            try:
                job = start_job({
                    "platform": source["platform"], "mode": "creator",
                    "creator_ids": source["tracking_target"],
                    "max_posts": 20, "max_comments_per_post": 100,
                    "include_replies": True,
                })
            except (RuntimeError, ValueError) as exc:
                conn.execute("""INSERT INTO source_runs
                    (id,source_id,status,scheduled_at,finished_at,window_start,window_end,error)
                    VALUES(?,?, 'failed',?,?,?,?,?)""",
                    (run_id, source["id"], _iso(now), _iso(now), _iso(window_start), _iso(now), str(exc)[:1000]))
                conn.execute("UPDATE source_schedules SET next_run_at=?,last_run_at=?,updated_at=? WHERE source_id=?",
                             (_iso(now + timedelta(minutes=15)), _iso(now), _iso(now), source["id"]))
                continue
            conn.execute("""INSERT INTO source_runs
                (id,source_id,job_id,status,scheduled_at,started_at,window_start,window_end,error)
                VALUES(?,?,?,'running',?,?,?,?,?)""",
                (run_id, source["id"], job["id"], _iso(now), job["started_at"],
                 _iso(window_start), _iso(now),
                 "回扫边界已记录；当前 MediaCrawler 命令未提供可靠游标/时间过滤，需依靠幂等去重，可能重复扫描旧内容。"))
            conn.execute("UPDATE source_schedules SET next_run_at=?,last_run_at=?,updated_at=? WHERE source_id=?",
                         (_iso(now + timedelta(minutes=source["interval_minutes"])),
                          _iso(now), _iso(now), source["id"]))
            started.append(run_id)
            # Existing crawler engine is single-job; one start per scheduler tick.
            break
    return {"started": started, "reconciled": reconciled, "deferred": None}


def tick_schedules(limit: int = 1) -> dict[str, Any]:
    if not _tick_lock.acquire(blocking=False):
        return {"started": [], "reconciled": 0, "deferred": "scheduler_busy"}
    owner = _acquire_scheduler_lease()
    if owner is None:
        _tick_lock.release()
        return {"started": [], "reconciled": 0, "deferred": "scheduler_lease_held"}
    try:
        return _tick_schedules(limit)
    finally:
        try:
            _release_scheduler_lease(owner)
        finally:
            _tick_lock.release()


def schedule_loop(stop_event, interval_seconds: int = 30) -> None:
    while not stop_event.wait(interval_seconds):
        try:
            tick_schedules()
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            logger.warning("来源调度检查失败：%s", exc)
