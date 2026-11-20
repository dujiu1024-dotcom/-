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
) -> Iterator[dict]:
    """Stream a bounded SQL result in fetchmany() batches."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1 when provided")
    where, params = _comment_where(filters)
    sql = (
        "SELECT * FROM comments" + where
        + " ORDER BY imported_at DESC, platform ASC, comment_id ASC"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
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
