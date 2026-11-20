from fastapi.testclient import TestClient

from app.main import app
from app.models import CommentRecord
from app.storage import db


def _comment(comment_id="c-1"):
    return CommentRecord(
        platform="xhs", comment_id=comment_id, content_id="post-1",
        content_url="https://example.test/post-1", content_title="测试内容",
        comment_text="希望增加导出功能", comment_created_at=None,
        content_created_at=None, parent_comment_id=None, author_key="u-1",
        like_count=1, raw={"id": comment_id},
    )


def test_feedback_api_append_only_history_and_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "feedback.sqlite3")
    db.upsert_comment(_comment())
    client = TestClient(app)
    first = client.post("/api/v1/feedback", json={
        "target_type": "comment", "target_id": "xhs::c-1",
        "label": "valid_demand", "reason": "明确表达功能诉求",
    })
    assert first.status_code == 201
    first_row = first.json()
    assert first_row["supersedes_feedback_id"] is None

    second = client.post("/api/v1/feedback", json={
        "target_type": "comment", "target_id": "xhs::c-1",
        "label": "category_correction", "corrected_category": "功能",
        "corrected_summary": "需要数据导出",
    })
    assert second.status_code == 201
    assert second.json()["supersedes_feedback_id"] == first_row["id"]

    history = client.get("/api/v1/feedback?target_type=comment&target_id=xhs%3A%3Ac-1")
    assert history.status_code == 200
    assert history.json()["total"] == 2
    assert [row["id"] for row in history.json()["items"]] == [
        second.json()["id"], first_row["id"]
    ]
    filtered = client.get("/api/v1/feedback?label=valid_demand")
    assert filtered.json()["total"] == 1


def test_feedback_api_validates_targets_and_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "feedback-validation.sqlite3")
    client = TestClient(app)
    missing = client.post("/api/v1/feedback", json={
        "target_type": "comment", "target_id": "xhs::missing", "label": "valid_demand"
    })
    assert missing.status_code == 404
    invalid = client.post("/api/v1/feedback", json={
        "target_type": "comment", "target_id": "xhs::missing", "label": "maybe"
    })
    assert invalid.status_code == 422
    assert client.get("/api/v1/feedback?limit=0").status_code == 422


def test_feedback_repository_preserves_original_comment(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "feedback-preserve.sqlite3")
    db.upsert_comment(_comment())
    before = db.page_comments(limit=10)[0]["comment_text"]
    db.create_feedback(
        target_type="comment", target_id="xhs::c-1", label="non_demand",
        reason="人工复核",
    )
    after = db.page_comments(limit=10)[0]["comment_text"]
    assert before == after == "希望增加导出功能"
