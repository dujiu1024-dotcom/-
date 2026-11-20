import pytest

from app.models import CommentRecord
from app.services.source_tracking import (
    discover_candidates, list_candidates, list_sources, merge_sources,
    review_candidate, update_source,
)
from app.storage.db import upsert_comment


def _comment(comment_id: str, author: str, text: str):
    return CommentRecord(platform="xhs", comment_id=comment_id, author_key=author,
                         comment_text=text, content_url="https://example.invalid/post/1",
                         raw={"author_name": author})


def test_discovery_review_and_source_configuration(tmp_path, monkeypatch):
    from app.storage import db
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "sources.sqlite3")
    upsert_comment(_comment("c1", "creator-1", "有没有类似工具"))
    upsert_comment(_comment("c2", "creator-1", "求推荐效率软件"))
    assert discover_candidates()["scanned_groups"] == 1
    candidates = list_candidates("pending")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert len(candidate["evidence"]) == 2
    reviewed = review_candidate(candidate["id"], "approve", "AI工具", 8, 360, 48)
    assert reviewed["status"] == "approved"
    sources = list_sources()
    assert len(sources) == 1
    assert sources[0]["group_name"] == "AI工具"
    assert sources[0]["priority"] == 8
    updated = update_source(sources[0]["id"], enabled=False, priority=9)
    assert updated["enabled"] == 0
    assert updated["priority"] == 9


def test_candidate_reject_and_merge(tmp_path, monkeypatch):
    from app.storage import db
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "merge.sqlite3")
    upsert_comment(_comment("c1", "creator-1", "内容一"))
    upsert_comment(_comment("c2", "creator-2", "内容二"))
    discover_candidates()
    candidates = list_candidates()
    a = review_candidate(candidates[0]["id"], "approve")["source_id"]
    b = review_candidate(candidates[1]["id"], "approve")["source_id"]
    merged = merge_sources([a, b], a)
    assert merged["target_id"] == a
    assert list_sources(enabled=False)[0]["id"] == b
    with pytest.raises(ValueError):
        review_candidate(candidates[0]["id"], "invalid")


def test_source_review_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.storage import db
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "api-sources.sqlite3")
    upsert_comment(_comment("c1", "creator-api", "求推荐"))
    client = TestClient(app)
    assert client.post("/api/v1/sources/discover").status_code == 200
    candidate = client.get("/api/v1/sources/candidates").json()["items"][0]
    response = client.post(f"/api/v1/sources/candidates/{candidate['id']}/review",
                           json={"action": "approve", "priority": 7})
    assert response.status_code == 200
    assert client.get("/api/v1/sources").json()["count"] == 1
