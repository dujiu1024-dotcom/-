import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from app.config import DATABASE_PATH
from app.models import CommentRecord


def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            platform TEXT NOT NULL,
            comment_id TEXT NOT NULL,
            content_id TEXT,
            content_url TEXT,
            content_title TEXT,
            comment_text TEXT NOT NULL,
            comment_created_at TEXT,
            content_created_at TEXT,
            parent_comment_id TEXT,
            author_key TEXT,
            like_count INTEGER,
            raw_json TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (platform, comment_id)
        )
    """)
    conn.commit()
    return conn


def upsert_comment(record: CommentRecord) -> bool:
    payload = record.model_dump()
    comment_dt = payload["comment_created_at"]
    content_dt = payload["content_created_at"]
    with connect() as conn:
        cur = conn.execute("""
            INSERT INTO comments (
                platform, comment_id, content_id, content_url, content_title,
                comment_text, comment_created_at, content_created_at,
                parent_comment_id, author_key, like_count, raw_json, imported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform, comment_id) DO UPDATE SET
                content_id=excluded.content_id,
                content_url=excluded.content_url,
                content_title=excluded.content_title,
                comment_text=excluded.comment_text,
                comment_created_at=COALESCE(excluded.comment_created_at, comments.comment_created_at),
                content_created_at=COALESCE(excluded.content_created_at, comments.content_created_at),
                parent_comment_id=excluded.parent_comment_id,
                author_key=excluded.author_key,
                like_count=excluded.like_count,
                raw_json=excluded.raw_json
        """, (
            record.platform, record.comment_id, record.content_id, record.content_url,
            record.content_title, record.comment_text,
            comment_dt.isoformat() if comment_dt else None,
            content_dt.isoformat() if content_dt else None,
            record.parent_comment_id, record.author_key, record.like_count,
            json.dumps(record.raw, ensure_ascii=False, default=str),
            datetime.now(timezone.utc).isoformat()
        ))
        return cur.rowcount > 0


def list_comments(since_hours: int | None = None, platform: str | None = None,
                  include_unknown_time: bool = False) -> list[dict]:
    clauses, params = [], []
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    # Filter in Python after parsing offsets; do not compare mixed timezone strings lexically.
    sql = "SELECT * FROM comments"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY imported_at DESC"
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    if since_hours is not None:
        cutoff_dt = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - since_hours * 3600, timezone.utc)
        filtered = []
        for row in rows:
            value = row["comment_created_at"]
            if not value:
                if include_unknown_time:
                    filtered.append(row)
                continue
            try:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    continue
                if dt.astimezone(timezone.utc) >= cutoff_dt:
                    filtered.append(row)
            except ValueError:
                if include_unknown_time:
                    filtered.append(row)
        rows = filtered
    return rows
