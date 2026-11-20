import json
import sqlite3
from datetime import UTC, datetime

from app.config import DATABASE_PATH, DEFAULT_RECENT_HOURS
from app.models import CommentRecord


def connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
        CREATE TABLE IF NOT EXISTS collection_projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_sources (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES collection_projects(id) ON DELETE CASCADE,
            name TEXT NOT NULL, platform TEXT NOT NULL, mode TEXT NOT NULL, target TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_collection_sources_project ON collection_sources(project_id)")
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
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS keyword_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL DEFAULT 'default',
            keyword TEXT NOT NULL, normalized_keyword TEXT NOT NULL, category TEXT NOT NULL DEFAULT '场景表达',
            source_type TEXT NOT NULL, source_platform TEXT, source_comment_id TEXT, source_content_id TEXT,
            source_content_url TEXT, source_text TEXT NOT NULL, generation_reason TEXT NOT NULL,
            generation_method TEXT NOT NULL DEFAULT 'rule',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','merged','deferred','disabled','included')),
            merged_into_id INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(project_id, normalized_keyword), FOREIGN KEY(merged_into_id) REFERENCES keyword_candidates(id)
        );
        CREATE TABLE IF NOT EXISTS keyword_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('approve','reject','merge','defer','disable','include','reactivate')),
            reviewer TEXT NOT NULL DEFAULT 'local-user', note TEXT, target_candidate_id INTEGER,
            created_at TEXT NOT NULL, FOREIGN KEY(candidate_id) REFERENCES keyword_candidates(id),
            FOREIGN KEY(target_candidate_id) REFERENCES keyword_candidates(id)
        );
        CREATE TABLE IF NOT EXISTS keyword_verification_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL DEFAULT 'default',
            platform TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','succeeded','failed','cancelled')),
            requested_limit INTEGER NOT NULL DEFAULT 20, started_at TEXT, finished_at TEXT, error TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS keyword_verification_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0, valid_demand_count INTEGER NOT NULL DEFAULT 0,
            duplicate_rate REAL, irrelevant_rate REAL, failure_reason TEXT, evidence_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, UNIQUE(batch_id,candidate_id),
            FOREIGN KEY(batch_id) REFERENCES keyword_verification_batches(id),
            FOREIGN KEY(candidate_id) REFERENCES keyword_candidates(id)
        );
        CREATE TABLE IF NOT EXISTS keyword_quality_thresholds (
            project_id TEXT PRIMARY KEY, min_hits INTEGER NOT NULL DEFAULT 3,
            min_valid_demands INTEGER NOT NULL DEFAULT 1, max_duplicate_rate REAL NOT NULL DEFAULT 0.7,
            max_irrelevant_rate REAL NOT NULL DEFAULT 0.6, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_keyword_candidates_status ON keyword_candidates(status,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_keyword_candidates_project ON keyword_candidates(project_id,status);
        CREATE INDEX IF NOT EXISTS idx_keyword_verification_batch ON keyword_verification_results(batch_id);
    """)
\n    conn.execute("INSERT OR IGNORE INTO app_settings(key,value,updated_at) VALUES('recent_hours',?,?)",
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


def list_comments(since_hours: int | None = None, platform: str | None = None,
                  include_unknown_time: bool = False) -> list[dict]:
    clauses, params = [], []
    if platform:
        clauses.append("platform = ?")
        params.append(platform)
    sql = "SELECT * FROM comments" + (" WHERE " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY imported_at DESC"
    with connect() as conn:
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
    if since_hours is not None:
        cutoff_dt = datetime.fromtimestamp(datetime.now(UTC).timestamp() - since_hours * 3600, UTC)
        filtered = []
        for row in rows:
            value = row["comment_created_at"]
            if not value:
                if include_unknown_time:
                    filtered.append(row)
                continue
            try:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is not None and dt.astimezone(UTC) >= cutoff_dt:
                    filtered.append(row)
            except ValueError:
                if include_unknown_time:
                    filtered.append(row)
        rows = filtered
    return rows
