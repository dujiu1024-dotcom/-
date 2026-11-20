from datetime import UTC

from app.collectors.mediacrawler_import import _parse_datetime, normalize_row


def test_normalize_common_comment_fields():
    record = normalize_row({
        "comment_id": "c-1",
        "content": "有没有批量处理 Excel 的工具？",
        "create_time": 1795161600,
        "aweme_id": "v-1",
        "user_id": "u-1",
    }, "douyin", 1)
    assert record is not None
    assert record.comment_id == "c-1"
    assert record.content_id == "v-1"
    assert record.comment_created_at is not None
    assert record.comment_created_at.tzinfo == UTC


def test_unknown_timezone_is_not_guessed():
    assert _parse_datetime("2026-11-20 10:00:00") is None


def test_empty_comment_is_skipped():
    assert normalize_row({"content": "  "}, "xhs", 1) is None
