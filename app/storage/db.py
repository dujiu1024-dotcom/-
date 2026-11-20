import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import DATABASE_PATH, DEFAULT_RECENT_HOURS
from app.models import CommentRecord


@dataclass(frozen=True)
class CommentFilters:
    """Normalized filters shared by comment COUNT, page and batch queries."""

    since_hours: int | None = None
    platform: str | None = None
    include_unknown_time: bool = False


def _normalize_comment_filters(
    filters: CommentFilters | Mapping[str, object] | None,
) -> CommentFilters:
    if filters is None:
        return CommentFilters()
    if isinstance(filters, CommentFilters):
        return filters
    if isinstance(filters, Mapping):
        return CommentFilters(
            since_hours=filters.get("since_hours"),  # type: ignore[arg-type]
            platform=filters.get("platform"),  # type: ignore[arg-type]
            include_unknown_time=bool(filters.get("include_unknown_time", False)),
        )
    raise TypeError("filters must be CommentFilters, a mapping, or None")


def _comment_where(
    filters: CommentFilters | Mapping[str, object] | None,
) -> tuple[str, list[object]]:
    normalized = _normalize_comment_filters(filters)
    clauses: list[str] = []
    params: list[object] = []

    if normalized.platform:
        clauses.append("platform = ?")
        params.append(normalized.platform)

    if normalized.since_hours is not None:
        if normalized.since_hours < 0:
            raise ValueError("since_hours must be non-negative")
        cutoff = datetime.now(UTC) - timedelta(hours=normalized.since_hours)
        time_clause = "julianday(comment_created_at) >= julianday(?)"
        params.append(cutoff.isoformat())
        if normalized.include_unknown_time:
            time_clause = (
                f"({time_clause} OR comment_created_at IS NULL "
                "OR trim(comment_created_at) = '' OR julianday(comment_created_at) IS NULL)"
            )
        clauses.append(time_clause)

    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            platform TEXT NOT NULL, comment_id TEXT NOT NULL, content_id TEXT,
            content_url TEXT, content_title TEXT, comment_text TEXT NOT NULL,
            comment_created_at TEXT, content_created_at TEXT, parent_comment_id TEXT,
            author_key TEXT, like_count INTEGER, raw_json TEXT NOT NULL, imported_at TEXT NOT NULL,
            PRIMARY KEY (platform, comment_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS demand_analyses (
            platform TEXT NOT NULL, comment_id TEXT NOT NULL, category TEXT NOT NULL,
            need_summary TEXT NOT NULL, intent TEXT NOT NULL, confidence REAL NOT NULL,
            semantic_text TEXT NOT NULL, cluster_id TEXT, strength_score REAL NOT NULL,
            analyzed_at TEXT NOT NULL, raw_analysis TEXT NOT NULL,
            PRIMARY KEY (platform, comment_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS demand_clusters (
            cluster_id TEXT PRIMARY KEY, label TEXT NOT NULL, category TEXT NOT NULL,
            member_count INTEGER NOT NULL, platform_count INTEGER NOT NULL,
            unique_authors INTEGER NOT NULL, strength_score REAL NOT NULL,
            first_seen TEXT, last_seen TEXT, updated_at TEXT NOT NULL, evidence_json TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analysis_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            finished_at TEXT, comments_seen INTEGER NOT NULL DEFAULT 0,
            demands_found INTEGER NOT NULL DEFAULT 0, clusters_found INTEGER NOT NULL DEFAULT 0,
            provider TEXT NOT NULL, status TEXT NOT NULL, error TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS analysis_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_run_id INTEGER NOT NULL,
            platform TEXT NOT NULL,
            comment_id TEXT NOT NULL,
            is_demand INTEGER NOT NULL CHECK(is_demand IN (0,1)),
            category TEXT NOT NULL,
            model_version TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            analyzed_at TEXT NOT NULL,
            UNIQUE(analysis_run_id, platform, comment_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_analysis_predictions_version_item
        ON analysis_predictions(model_version, prompt_version, platform, comment_id, analyzed_at DESC)
    """)
    analysis_run_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(analysis_runs)").fetchall()
    }
    if "model_version" not in analysis_run_columns:
        conn.execute("ALTER TABLE analysis_runs ADD COLUMN model_version TEXT")
    if "prompt_version" not in analysis_run_columns:
        conn.execute("ALTER TABLE analysis_runs ADD COLUMN prompt_version TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS demand_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_type TEXT NOT NULL CHECK(target_type IN ('comment','content','demand_cluster')),
            target_id TEXT NOT NULL,
            label TEXT NOT NULL CHECK(label IN (
                'valid_demand','non_demand','category_correction','merge','split'
            )),
            corrected_category TEXT,
            corrected_summary TEXT,
            reason TEXT,
            evidence_note TEXT,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            supersedes_feedback_id INTEGER REFERENCES demand_feedback(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_demand_feedback_target_created
        ON demand_feedback(target_type, target_id, created_at DESC, id DESC)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_task_runs (
            task_id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','blocked','interrupted')),
            retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
            result_count INTEGER NOT NULL DEFAULT 0 CHECK(result_count >= 0),
            error_code TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            duration_ms INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collection_task_runs_started
        ON collection_task_runs(started_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_collection_task_runs_platform_started
        ON collection_task_runs(platform, started_at DESC)
    """)

    # Expression indexes match julianday() predicates and preserve legacy timestamp compatibility.
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_comments_platform_created_jd
        ON comments(platform, julianday(comment_created_at))
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_comments_created_jd
        ON comments(julianday(comment_created_at))
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_comments_imported_order
        ON comments(imported_at DESC, platform ASC, comment_id ASC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_demand_analyses_cluster_analyzed
        ON demand_analyses(cluster_id, analyzed_at)
    """)
    conn.execute("INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES('recent_hours',?,?)",
                 (str(DEFAULT_RECENT_HOURS), datetime.now(UTC).isoformat()))
    conn.commit()
    return conn


def upsert_comment(record: CommentRecord) -> bool:
    payload = record.model_dump()
    comment_dt, content_dt = payload["comment_created_at"], payload["content_created_at"]
    with connect() as conn:
        cur = conn.execute("""
            INSERT INTO comments (
                platform, comment_id, content_id, content_url, content_title, comment_text,
                comment_created_at, content_created_at, parent_comment_id, author_key,
                like_count, raw_json, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, comment_id) DO UPDATE SET
                content_id=excluded.content_id, content_url=excluded.content_url,
                content_title=excluded.content_title, comment_text=excluded.comment_text,
                comment_created_at=COALESCE(excluded.comment_created_at, comments.comment_created_at),
                content_created_at=COALESCE(excluded.content_created_at, comments.content_created_at),
                parent_comment_id=excluded.parent_comment_id, author_key=excluded.author_key,
                like_count=excluded.like_count, raw_json=excluded.raw_json
        """, (
            record.platform, record.comment_id, record.content_id, record.content_url,
            record.content_title, record.comment_text,
            comment_dt.isoformat() if comment_dt else None,
            content_dt.isoformat() if content_dt else None,
            record.parent_comment_id, record.author_key, record.like_count,
            json.dumps(record.raw, ensure_ascii=False, default=str), datetime.now(UTC).isoformat()
        ))
        return cur.rowcount > 0


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute("""INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
            (key, value, datetime.now(UTC).isoformat()))


def get_recent_hours() -> int:
    try:
        return max(1, min(8760, int(get_setting("recent_hours", str(DEFAULT_RECENT_HOURS)))))
    except (TypeError, ValueError):
        return DEFAULT_RECENT_HOURS


def count_comments(
    filters: CommentFilters | Mapping[str, object] | None = None,
) -> int:
    """Count comments in SQL using the exact same predicates as page/iterator queries."""
    where, params = _comment_where(filters)
    with connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS total FROM comments{where}", params).fetchone()
        return int(row["total"])


def page_comments(
    filters: CommentFilters | Mapping[str, object] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Return one deterministically ordered page; filtering and pagination stay in SQLite."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    where, params = _comment_where(filters)
    sql = (
        "SELECT * FROM comments" + where
        + " ORDER BY imported_at DESC, platform ASC, comment_id ASC LIMIT ? OFFSET ?"
    )
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, [*params, limit, offset]).fetchall()]


def explain_comment_query_plan(
    filters: CommentFilters | Mapping[str, object] | None = None,
) -> list[str]:
    """Return SQLite's plan details for the production comment iteration query."""
    where, params = _comment_where(filters)
    sql = (
        "EXPLAIN QUERY PLAN SELECT * FROM comments" + where
        + " ORDER BY imported_at DESC, platform ASC, comment_id ASC"
    )
    with connect() as conn:
        return [row["detail"] for row in conn.execute(sql, params).fetchall()]


def iter_comments(
    filters: CommentFilters | Mapping[str, object] | None = None,
    batch_size: int = 250,
    limit: int | None = None,
    offset: int = 0,
) -> Iterator[dict]:
    """Stream a bounded SQL result in fetchmany() batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1 when provided")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    where, params = _comment_where(filters)
    sql = (
        "SELECT * FROM comments" + where
        + " ORDER BY imported_at DESC, platform ASC, comment_id ASC"
    )
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend((limit, offset))
    elif offset:
        sql += " LIMIT -1 OFFSET ?"
        params.append(offset)
    conn = connect()
    try:
        cursor = conn.execute(sql, params)
        while batch := cursor.fetchmany(batch_size):
            for row in batch:
                yield dict(row)
    finally:
        conn.close()


def list_comments(since_hours: int | None = None, platform: str | None = None,
                  include_unknown_time: bool = False) -> list[dict]:
    """Backward-compatible full-list wrapper; new callers should use page/iterator APIs."""
    filters = CommentFilters(
        since_hours=since_hours,
        platform=platform,
        include_unknown_time=include_unknown_time,
    )
    return list(iter_comments(filters, batch_size=500))


FEEDBACK_LABELS = {
    "valid_demand", "non_demand", "category_correction", "merge", "split"
}
FEEDBACK_TARGET_TYPES = {"comment", "content", "demand_cluster"}


def create_feedback(
    *,
    target_type: str,
    target_id: str,
    label: str,
    corrected_category: str | None = None,
    corrected_summary: str | None = None,
    reason: str | None = None,
    evidence_note: str | None = None,
    actor: str = "local_user",
) -> dict:
    """Append a human judgement; never mutates comments or AI analysis rows."""
    if target_type not in FEEDBACK_TARGET_TYPES:
        raise ValueError("unsupported target_type")
    if label not in FEEDBACK_LABELS:
        raise ValueError("unsupported feedback label")
    target_id = target_id.strip()
    actor = actor.strip()
    if not target_id:
        raise ValueError("target_id must not be empty")
    if not actor:
        raise ValueError("actor must not be empty")
    now = datetime.now(UTC).isoformat()
    with connect() as conn:
        previous = conn.execute(
            """SELECT id FROM demand_feedback
               WHERE target_type=? AND target_id=?
               ORDER BY created_at DESC, id DESC LIMIT 1""",
            (target_type, target_id),
        ).fetchone()
        cur = conn.execute(
            """INSERT INTO demand_feedback(
                target_type,target_id,label,corrected_category,corrected_summary,
                reason,evidence_note,actor,created_at,updated_at,supersedes_feedback_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (target_type,target_id,label,corrected_category,corrected_summary,
             reason,evidence_note,actor,now,now,previous["id"] if previous else None),
        )
        row = conn.execute(
            "SELECT * FROM demand_feedback WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def list_feedback(
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    label: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """List append-only feedback history with SQL filtering and pagination."""
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    clauses: list[str] = []
    params: list[object] = []
    if target_type:
        if target_type not in FEEDBACK_TARGET_TYPES:
            raise ValueError("unsupported target_type")
        clauses.append("target_type=?")
        params.append(target_type)
    if target_id:
        clauses.append("target_id=?")
        params.append(target_id)
    if label:
        if label not in FEEDBACK_LABELS:
            raise ValueError("unsupported feedback label")
        clauses.append("label=?")
        params.append(label)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS total FROM demand_feedback" + where, params
        ).fetchone()["total"]
        rows = conn.execute(
            "SELECT * FROM demand_feedback" + where
            + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return {"items": [dict(row) for row in rows], "total": int(total),
            "limit": limit, "offset": offset}


def start_collection_task(task_id: str, platform: str, mode: str, started_at: str) -> None:
    """Persist a collection attempt so quality metrics survive process restarts."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO collection_task_runs(
                task_id,platform,mode,status,retry_count,result_count,started_at
            ) VALUES(?,?,?,'running',0,0,?)
            ON CONFLICT(task_id) DO NOTHING""",
            (task_id, platform, mode, started_at),
        )


def finish_collection_task(
    task_id: str, *, status: str, result_count: int = 0,
    error_code: str | None = None, finished_at: str | None = None,
) -> None:
    if status not in {"succeeded", "failed", "blocked", "interrupted"}:
        raise ValueError("invalid collection task terminal status")
    if result_count < 0:
        raise ValueError("result_count must be non-negative")
    finished_at = finished_at or datetime.now(UTC).isoformat()
    with connect() as conn:
        conn.execute(
            """UPDATE collection_task_runs
               SET status=?, result_count=?, error_code=?, finished_at=?,
                   duration_ms=MAX(0, CAST(
                       (julianday(?) - julianday(started_at)) * 86400000 AS INTEGER
                   ))
               WHERE task_id=?""",
            (status, result_count, error_code, finished_at, finished_at, task_id),
        )


def list_collection_task_runs(since_hours: int = 168) -> list[dict]:
    if since_hours < 1 or since_hours > 8760:
        raise ValueError("since_hours must be between 1 and 8760")
    cutoff = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat()
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM collection_task_runs
               WHERE started_at >= ? ORDER BY started_at DESC, task_id ASC""",
            (cutoff,),
        ).fetchall()
    return [dict(row) for row in rows]
