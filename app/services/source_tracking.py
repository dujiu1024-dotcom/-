from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.storage.db import connect


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _schema(conn) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS source_candidates (
        id TEXT PRIMARY KEY, platform TEXT NOT NULL, source_type TEXT NOT NULL,
        source_key TEXT NOT NULL, display_name TEXT, profile_url TEXT,
        content_count INTEGER NOT NULL DEFAULT 0, demand_count INTEGER NOT NULL DEFAULT 0,
        active_days INTEGER NOT NULL DEFAULT 0, relevance_score REAL NOT NULL DEFAULT 0,
        recommendation TEXT NOT NULL, evidence_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, UNIQUE(platform, source_type, source_key)
    );
    CREATE TABLE IF NOT EXISTS tracked_sources (
        id TEXT PRIMARY KEY, platform TEXT NOT NULL, source_type TEXT NOT NULL,
        source_key TEXT NOT NULL, display_name TEXT, profile_url TEXT,
        group_name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
        priority INTEGER NOT NULL DEFAULT 5, interval_minutes INTEGER NOT NULL DEFAULT 1440,
        window_hours INTEGER NOT NULL DEFAULT 72, tracking_scope TEXT NOT NULL DEFAULT 'creator',
        candidate_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        last_success_at TEXT, last_content_id TEXT, cursor_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(platform, source_type, source_key)
    );
    CREATE TABLE IF NOT EXISTS source_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT, candidate_id TEXT,
        action TEXT NOT NULL, actor TEXT NOT NULL DEFAULT 'local',
        detail_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
    );
    """)


def discover_candidates(limit: int = 500) -> dict[str, Any]:
    with connect() as conn:
        _schema(conn)
        rows = conn.execute("""
            SELECT c.platform, c.author_key, COUNT(*) AS content_count,
                   COUNT(DISTINCT substr(COALESCE(c.comment_created_at,''),1,10)) AS active_days,
                   MAX(c.comment_created_at) AS last_seen,
                   SUM(CASE WHEN a.category IS NOT NULL AND a.category NOT IN ('无效','其他','') THEN 1 ELSE 0 END) AS demand_count,
                   MAX(c.content_url) AS profile_url, MAX(c.content_title) AS sample_title
            FROM comments c LEFT JOIN demand_analyses a
              ON a.platform=c.platform AND a.comment_id=c.comment_id
            WHERE COALESCE(trim(c.author_key),'') <> ''
            GROUP BY c.platform, c.author_key
            ORDER BY content_count DESC LIMIT ?
        """, (max(1, min(limit, 5000)),)).fetchall()
        created = 0
        for row in rows:
            count = int(row["content_count"] or 0)
            demand_count = int(row["demand_count"] or 0)
            ratio = demand_count / count if count else 0.0
            active = int(row["active_days"] or 0)
            # Score is explainable: demand evidence 60%, volume 25%, recent activity 15%.
            volume_score = min(1.0, count / 20)
            activity_score = min(1.0, active / 7)
            score = round((ratio * .60 + volume_score * .25 + activity_score * .15) * 100, 2)
            source_key = str(row["author_key"]).strip()
            candidate_id = hashlib.sha256(f'{row["platform"]}:author:{source_key}'.encode()).hexdigest()[:24]
            reasons = [f"累计关联内容/评论 {count} 条", f"识别到有效需求证据 {demand_count} 条（{ratio:.0%}）",
                       f"覆盖活跃日期 {active} 天", f"最近出现时间 {row['last_seen'] or '未知'}"]
            evidence = conn.execute("""
                SELECT c.comment_id, c.content_id, c.content_url, c.content_title,
                       c.comment_text, c.comment_created_at
                FROM comments c WHERE c.platform=? AND c.author_key=?
                ORDER BY c.comment_created_at DESC LIMIT 10
            """, (row["platform"], source_key)).fetchall()
            cur = conn.execute("""
                INSERT INTO source_candidates
                (id,platform,source_type,source_key,display_name,profile_url,content_count,
                 demand_count,active_days,relevance_score,recommendation,evidence_json,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending',?,?)
                ON CONFLICT(platform,source_type,source_key) DO UPDATE SET
                  content_count=excluded.content_count,demand_count=excluded.demand_count,
                  active_days=excluded.active_days,relevance_score=excluded.relevance_score,
                  recommendation=excluded.recommendation,evidence_json=excluded.evidence_json,
                  updated_at=excluded.updated_at
                WHERE source_candidates.status='pending'
            """, (candidate_id, row["platform"], "author", source_key, source_key,
                  row["profile_url"], count, demand_count, active, score,
                  "；".join(reasons), json.dumps([dict(e) for e in evidence], ensure_ascii=False),
                  _now(), _now()))
            created += max(cur.rowcount, 0)
        return {"scanned_groups": len(rows), "candidates_updated": created}


def list_candidates(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    with connect() as conn:
        _schema(conn)
        sql = "SELECT * FROM source_candidates"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY relevance_score DESC, updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        result = []
        for row in conn.execute(sql, params).fetchall():
            item = dict(row)
            item["evidence"] = json.loads(item.pop("evidence_json"))
            result.append(item)
        return result


def list_sources(enabled: bool | None = None, limit: int = 500) -> list[dict[str, Any]]:
    with connect() as conn:
        _schema(conn)
        sql = "SELECT * FROM tracked_sources"
        params: list[Any] = []
        if enabled is not None:
            sql += " WHERE enabled=?"
            params.append(int(enabled))
        sql += " ORDER BY priority DESC, updated_at DESC LIMIT ?"
        params.append(max(1, min(limit, 2000)))
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def review_candidate(candidate_id: str, action: str, group_name: str = "",
                     priority: int = 5, interval_minutes: int = 1440,
                     window_hours: int = 72, tracking_scope: str = "creator") -> dict[str, Any]:
    if action not in {"approve", "reject", "defer"}:
        raise ValueError("action 仅支持 approve、reject、defer")
    if not 1 <= priority <= 10 or not 15 <= interval_minutes <= 10080 or not 1 <= window_hours <= 8760:
        raise ValueError("优先级、采集周期或时间窗口超出允许范围")
    if tracking_scope not in {"creator", "content"}:
        raise ValueError("tracking_scope 仅支持 creator、content")
    with connect() as conn:
        _schema(conn)
        candidate = conn.execute("SELECT * FROM source_candidates WHERE id=?", (candidate_id,)).fetchone()
        if not candidate:
            raise KeyError(candidate_id)
        status = {"approve": "approved", "reject": "rejected", "defer": "deferred"}[action]
        now = _now()
        source_id = None
        if action == "approve":
            source_id = hashlib.sha256(
                f'{candidate["platform"]}:{candidate["source_type"]}:{candidate["source_key"]}'.encode()
            ).hexdigest()[:24]
            conn.execute("""
                INSERT INTO tracked_sources
                (id,platform,source_type,source_key,display_name,profile_url,group_name,
                 priority,interval_minutes,window_hours,tracking_scope,candidate_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(platform,source_type,source_key) DO UPDATE SET
                  group_name=excluded.group_name,priority=excluded.priority,
                  interval_minutes=excluded.interval_minutes,window_hours=excluded.window_hours,
                  tracking_scope=excluded.tracking_scope,enabled=1,updated_at=excluded.updated_at
            """, (source_id, candidate["platform"], candidate["source_type"], candidate["source_key"],
                  candidate["display_name"], candidate["profile_url"], group_name, priority,
                  interval_minutes, window_hours, tracking_scope, candidate_id, now, now))
        conn.execute("UPDATE source_candidates SET status=?,updated_at=? WHERE id=?", (status, now, candidate_id))
        conn.execute("INSERT INTO source_audit(source_id,candidate_id,action,detail_json,created_at) VALUES(?,?,?,?,?)",
                     (source_id, candidate_id, action, json.dumps({"group_name": group_name,
                      "priority": priority, "interval_minutes": interval_minutes,
                      "window_hours": window_hours, "tracking_scope": tracking_scope}), now))
        return {"candidate_id": candidate_id, "status": status, "source_id": source_id}


def update_source(source_id: str, **changes: Any) -> dict[str, Any]:
    allowed = {"group_name", "enabled", "priority", "interval_minutes", "window_hours", "tracking_scope"}
    if not changes or set(changes) - allowed:
        raise ValueError("不支持的来源配置字段")
    if "priority" in changes and not 1 <= int(changes["priority"]) <= 10:
        raise ValueError("priority 必须在 1 到 10 之间")
    if "interval_minutes" in changes and not 15 <= int(changes["interval_minutes"]) <= 10080:
        raise ValueError("interval_minutes 必须在 15 到 10080 之间")
    if "window_hours" in changes and not 1 <= int(changes["window_hours"]) <= 8760:
        raise ValueError("window_hours 必须在 1 到 8760 之间")
    if "enabled" in changes:
        changes["enabled"] = int(bool(changes["enabled"]))
    if "tracking_scope" in changes and changes["tracking_scope"] not in {"creator", "content"}:
        raise ValueError("tracking_scope 仅支持 creator、content")
    with connect() as conn:
        _schema(conn)
        row = conn.execute("SELECT * FROM tracked_sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise KeyError(source_id)
        assignments = ", ".join(f"{key}=?" for key in changes)
        conn.execute(f"UPDATE tracked_sources SET {assignments},updated_at=? WHERE id=?",
                     [*changes.values(), _now(), source_id])
        conn.execute("INSERT INTO source_audit(source_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                     (source_id, "update", json.dumps(changes, ensure_ascii=False), _now()))
        return dict(conn.execute("SELECT * FROM tracked_sources WHERE id=?", (source_id,)).fetchone())


def merge_sources(source_ids: list[str], target_id: str) -> dict[str, Any]:
    unique_ids = list(dict.fromkeys(source_ids))
    if target_id not in unique_ids or len(unique_ids) < 2:
        raise ValueError("合并时必须包含目标来源及至少一个其他来源")
    with connect() as conn:
        _schema(conn)
        rows = conn.execute(f"SELECT id FROM tracked_sources WHERE id IN ({','.join('?' for _ in unique_ids)})",
                            unique_ids).fetchall()
        if len(rows) != len(unique_ids):
            raise KeyError("来源不存在")
        now = _now()
        for source_id in unique_ids:
            if source_id == target_id:
                continue
            conn.execute("UPDATE tracked_sources SET enabled=0,updated_at=? WHERE id=?", (now, source_id))
            conn.execute("INSERT INTO source_audit(source_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                         (source_id, "merged", json.dumps({"target_id": target_id}), now))
        return {"target_id": target_id, "merged_source_ids": [x for x in unique_ids if x != target_id]}
