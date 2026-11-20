from datetime import UTC, datetime, timedelta

from app.storage import db


def _insert(conn, platform, comment_id, created_at, imported_at):
    conn.execute(
        """INSERT INTO comments (
            platform, comment_id, comment_text, comment_created_at,
            raw_json, imported_at
        ) VALUES (?, ?, ?, ?, '{}', ?)""",
        (platform, comment_id, comment_id, created_at, imported_at),
    )


def test_expression_index_is_used_for_platform_and_time_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "plan.sqlite3")
    now = datetime.now(UTC)
    with db.connect() as conn:
        for index in range(20):
            _insert(conn, "xhs", str(index), (now - timedelta(hours=index)).isoformat(), now.isoformat())

    plan = db.explain_comment_query_plan(db.CommentFilters(since_hours=24, platform="xhs"))
    assert any("idx_comments_platform_created_jd" in detail for detail in plan)


def test_imported_order_index_is_used_for_unfiltered_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "order-plan.sqlite3")
    db.connect().close()
    plan = db.explain_comment_query_plan()
    assert any("idx_comments_imported_order" in detail for detail in plan)


def test_iter_comments_applies_limit_and_offset_in_sql(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "batch.sqlite3")
    imported = datetime.now(UTC).isoformat()
    with db.connect() as conn:
        for index in range(8):
            _insert(conn, "xhs", str(index), None, imported)

    rows = list(db.iter_comments(batch_size=2, limit=3, offset=3))
    assert len(rows) == 3
    assert [row["comment_id"] for row in rows] == ["3", "4", "5"]
