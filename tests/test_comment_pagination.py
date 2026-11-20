from datetime import UTC, datetime, timedelta

import pytest

from app.models import CommentRecord
from app.storage import db


@pytest.fixture
def comment_db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "comments.sqlite3")
    return db


def _insert(comment_db, platform, comment_id, created_at, imported_at):
    with comment_db.connect() as conn:
        conn.execute(
            """INSERT INTO comments (
                platform, comment_id, comment_text, comment_created_at,
                raw_json, imported_at
            ) VALUES (?, ?, ?, ?, '{}', ?)""",
            (platform, comment_id, comment_id, created_at, imported_at),
        )


def test_count_and_page_apply_identical_sql_filters(comment_db):
    now = datetime.now(UTC)
    recent = (now - timedelta(hours=2)).isoformat()
    old = (now - timedelta(hours=48)).isoformat()
    imported = now.isoformat()
    _insert(comment_db, "xhs", "recent-a", recent, imported)
    _insert(comment_db, "xhs", "recent-b", recent, imported)
    _insert(comment_db, "douyin", "recent-c", recent, imported)
    _insert(comment_db, "xhs", "old", old, imported)

    filters = db.CommentFilters(since_hours=24, platform="xhs")
    assert comment_db.count_comments(filters) == 2
    assert [row["comment_id"] for row in comment_db.page_comments(filters, limit=1)] == [
        "recent-a"
    ]


def test_unknown_time_is_excluded_unless_requested(comment_db):
    now = datetime.now(UTC).isoformat()
    _insert(comment_db, "xhs", "known", now, now)
    _insert(comment_db, "xhs", "missing", None, now)
    _insert(comment_db, "xhs", "invalid", "not-a-date", now)

    assert comment_db.count_comments({"since_hours": 24}) == 1
    assert comment_db.count_comments(
        {"since_hours": 24, "include_unknown_time": True}
    ) == 3


def test_page_order_is_stable_and_offset_is_applied_in_sql(comment_db):
    imported = datetime.now(UTC).isoformat()
    _insert(comment_db, "xhs", "b", None, imported)
    _insert(comment_db, "douyin", "a", None, imported)
    _insert(comment_db, "xhs", "a", None, imported)

    first = comment_db.page_comments(limit=2, offset=0)
    second = comment_db.page_comments(limit=2, offset=2)
    assert [(row["platform"], row["comment_id"]) for row in first] == [
        ("douyin", "a"),
        ("xhs", "a"),
    ]
    assert [(row["platform"], row["comment_id"]) for row in second] == [("xhs", "b")]


def test_iterator_streams_all_matching_rows_in_bounded_batches(comment_db):
    imported = datetime.now(UTC).isoformat()
    for index in range(7):
        _insert(comment_db, "xhs", str(index), None, imported)

    rows = list(comment_db.iter_comments(batch_size=2))
    assert len(rows) == 7
    assert len({row["comment_id"] for row in rows}) == 7


@pytest.mark.parametrize(("limit", "offset"), [(0, 0), (-1, 0), (1, -1)])
def test_page_rejects_invalid_pagination(comment_db, limit, offset):
    with pytest.raises(ValueError):
        comment_db.page_comments(limit=limit, offset=offset)


def test_iterator_rejects_invalid_batch_size(comment_db):
    with pytest.raises(ValueError):
        list(comment_db.iter_comments(batch_size=0))


def test_legacy_list_comments_keeps_existing_arguments(comment_db):
    now = datetime.now(UTC)
    recent = now.isoformat()
    old = (now - timedelta(days=2)).isoformat()
    _insert(comment_db, "xhs", "recent", recent, recent)
    _insert(comment_db, "douyin", "old", old, recent)

    rows = comment_db.list_comments(since_hours=24, platform="xhs")
    assert [row["comment_id"] for row in rows] == ["recent"]
