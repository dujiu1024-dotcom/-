import pytest

from app.models import CommentRecord
from app.services.source_tracking import (
    configure_schedule, discover_candidates, get_checkpoint, list_candidates,
    list_schedules, list_source_runs, list_sources, merge_sources, review_candidate,
    save_checkpoint, update_source,
)
from app.storage.db import connect, upsert_comment


def _comment(comment_id: str, author: str, text: str):
    return CommentRecord(platform="xhs", comment_id=comment_id, author_key=author,
                         comment_text=text, content_id="post-1",
                         content_url="https://example.invalid/post/1?from=feed",
                         content_title="效率工具讨论", raw={"author_name": author})


def test_discovery_review_and_source_configuration(tmp_path, monkeypatch):
    from app.storage import db
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "sources.sqlite3")
    upsert_comment(_comment("c1", "creator-1", "有没有类似工具"))
    upsert_comment(_comment("c2", "creator-1", "求推荐效率软件"))
    assert discover_candidates()["scanned_groups"] == 1
    candidate = list_candidates("pending")[0]
    assert len(candidate["evidence"]) == 2
    reviewed = review_candidate(candidate["id"], "approve", "AI工具", 8, 360, 48)
    assert reviewed["status"] == "approved"
    sources = list_sources()
    assert len(sources) == 1
    assert sources[0]["profile_url"] is None
    assert sources[0]["group_name"] == "AI工具"
    assert sources[0]["priority"] == 8
    updated = update_source(sources[0]["id"], enabled=False, priority=9,
                            tracking_target="creator-1", tracking_verified=True)
    assert updated["enabled"] == 0
    assert updated["priority"] == 9
    assert updated["tracking_verified"] == 1
    assert len(list_schedules()) == 1


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
    assert client.get("/api/v1/sources/schedules").json()["count"] == 1


def test_checkpoint_roundtrip_and_idempotent_import_timestamps(tmp_path, monkeypatch):
    from app.storage import db
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "checkpoint.sqlite3")
    upsert_comment(_comment("c1", "creator-1", "第一版"))
    upsert_comment(_comment("c1", "creator-1", "更新版"))
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM content_records").fetchone()[0] == 1
        row = conn.execute("SELECT first_collected_at,last_collected_at FROM comments").fetchone()
        assert row["first_collected_at"]
        assert row["last_collected_at"]
    discover_candidates()
    source_id = review_candidate(list_candidates()[0]["id"], "approve")["source_id"]
    saved = save_checkpoint(source_id, {"next_page": "cursor-2"}, "post-2",
                            "2026-11-19T00:00:00+00:00", "2026-11-20T00:00:00+00:00")
    restored = get_checkpoint(source_id)
    assert saved["mode"] == "cursor"
    assert restored["cursor"] == {"next_page": "cursor-2"}
    assert restored["last_content_id"] == "post-2"


def test_schedule_configuration_and_scheduler_dispatch(tmp_path, monkeypatch):
    from app.storage import db
    from app.services import scheduler
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "scheduler.sqlite3")
    upsert_comment(_comment("c1", "creator-1", "求推荐"))
    discover_candidates()
    source_id = review_candidate(list_candidates()[0]["id"], "approve")["source_id"]
    update_source(source_id, tracking_target="creator-1", tracking_verified=True)
    configure_schedule(source_id, True)
    with connect() as conn:
        conn.execute("UPDATE source_schedules SET next_run_at='2000-01-01T00:00:00+00:00'")
    monkeypatch.setattr(scheduler, "list_jobs", lambda: [])
    monkeypatch.setattr(scheduler, "start_job", lambda payload: {
        "id": "job-123", "status": "running", "started_at": "2026-11-20T00:00:00+00:00"
    })
    result = scheduler.tick_schedules()
    assert len(result["started"]) == 1
    assert list_source_runs(source_id)[0]["job_id"] == "job-123"
